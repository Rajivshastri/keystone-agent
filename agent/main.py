"""Foreground entry point — runs the local UI + poll loop under uvicorn.

This is what `keystone-agent` runs in dev. In production on Windows the
agent runs as a service (see agent/service.py) which in turn calls the
same start/stop primitives.
"""
from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

import uvicorn

from .local_ui import create_app, get_or_initialise_settings
from .poll import PollLoop

LOCAL_UI_HOST = "127.0.0.1"
LOCAL_UI_PORT = 5001


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> int:
    _configure_logging()
    logger = logging.getLogger("agent.main")
    settings = get_or_initialise_settings()
    logger.info(f"Keystone agent starting — fingerprint={settings.machine_fingerprint}")

    poll = PollLoop(settings)
    poll.start()

    def _shutdown(sig: int, _frame: FrameType | None) -> None:
        logger.info(f"Signal {sig} received — shutting down")
        poll.stop()
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except (AttributeError, ValueError):
        # Signals not supported on some Windows shells — service wrapper
        # handles shutdown via its own SCM callback.
        pass

    app = create_app()
    logger.info(f"Local UI listening on http://{LOCAL_UI_HOST}:{LOCAL_UI_PORT}")
    try:
        uvicorn.run(
            app,
            host=LOCAL_UI_HOST,
            port=LOCAL_UI_PORT,
            log_level="info",
            access_log=False,
        )
    finally:
        poll.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
