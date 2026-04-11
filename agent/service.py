"""Windows service wrapper (pywin32).

Installs/uninstalls the agent as a Windows service so it starts with the
machine. On non-Windows platforms, import will fail gracefully and the
module is a no-op — dev machines use agent/main.py directly.

Usage (elevated PowerShell, after PyInstaller packaging):

    KeystoneAgent.exe install           # register the service
    KeystoneAgent.exe start
    KeystoneAgent.exe stop
    KeystoneAgent.exe remove            # uninstall

Phase 0 scaffold. The actual servicing logic (start/stop of the local UI
and poll loop) is already in main.py; this module just wires it up to the
Service Control Manager.
"""
from __future__ import annotations

import logging
import platform
import sys
import threading
import time

logger = logging.getLogger(__name__)

_SERVICE_NAME = "KeystoneAgent"
_SERVICE_DISPLAY = "Keystone Reconciliation Agent"
_SERVICE_DESCRIPTION = (
    "Runs scheduled reconciliations locally and reports summaries to the "
    "Keystone control plane. Customer data never leaves this machine."
)


def _install_windows_service() -> int:
    """Import pywin32 lazily so non-Windows installs of this package don't
    blow up at import time.
    """
    if platform.system() != "Windows":
        print("service wrapper is Windows-only", file=sys.stderr)
        return 1

    # Imports are deferred so this module is safe to import on any platform.
    import servicemanager  # type: ignore[import-untyped]
    import win32event  # type: ignore[import-untyped]
    import win32service  # type: ignore[import-untyped]
    import win32serviceutil  # type: ignore[import-untyped]

    from .main import _configure_logging
    from .local_ui import create_app, get_or_initialise_settings
    from .poll import PollLoop

    class KeystoneService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
        _svc_name_ = _SERVICE_NAME
        _svc_display_name_ = _SERVICE_DISPLAY
        _svc_description_ = _SERVICE_DESCRIPTION

        def __init__(self, args: list[str]) -> None:
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._poll: PollLoop | None = None
            self._uvicorn_thread: threading.Thread | None = None

        def SvcStop(self) -> None:  # noqa: N802 — pywin32 contract
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self._stop_event)
            if self._poll is not None:
                self._poll.stop()

        def SvcDoRun(self) -> None:  # noqa: N802 — pywin32 contract
            _configure_logging()
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            settings = get_or_initialise_settings()
            self._poll = PollLoop(settings)
            self._poll.start()
            # Run uvicorn in a background thread so the main thread can
            # park on the stop event.
            app = create_app()
            import uvicorn

            def _serve() -> None:
                uvicorn.run(
                    app,
                    host="127.0.0.1",
                    port=5001,
                    log_level="info",
                    access_log=False,
                )

            self._uvicorn_thread = threading.Thread(
                target=_serve, name="keystone-uvicorn", daemon=True
            )
            self._uvicorn_thread.start()
            # Park until SCM asks us to stop
            while True:
                rc = win32event.WaitForSingleObject(self._stop_event, 5000)
                if rc == win32event.WAIT_OBJECT_0:
                    break
                time.sleep(0.1)

    # Forward argv — win32serviceutil expects to see its own args
    return win32serviceutil.HandleCommandLine(KeystoneService) or 0


def main() -> int:
    return _install_windows_service()


if __name__ == "__main__":
    raise SystemExit(main())
