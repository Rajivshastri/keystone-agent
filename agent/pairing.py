"""Agent-side device-code pairing.

Generates a short device code, shows it to the operator via the local UI,
and polls the control plane until the operator has claimed it in their
browser. On success, stores the bearer token in the DPAPI secret store
and updates the persistent agent config so future starts are pre-paired.

The pairing state machine lives in memory (a module-level singleton)
while the wizard is running; it survives a local UI refresh but not an
agent restart — a restart means "start pairing from scratch" which is
the simplest and safest behaviour.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from . import __version__
from .config import AgentSettings, load_settings, save_settings
from .protocol import PairClaimResponse
from .secrets import KEY_AGENT_TOKEN, get_store
from .transport import ControlPlaneClient, TransportError

logger = logging.getLogger(__name__)

# Same unambiguous alphabet as the control plane — keep in sync.
_CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY23456789"

PairingState = Literal["idle", "showing_code", "claimed", "expired", "error"]


def generate_device_code() -> str:
    rng = random.SystemRandom()
    first = "".join(rng.choice(_CODE_ALPHABET) for _ in range(4))
    second = "".join(rng.choice(_CODE_ALPHABET) for _ in range(4))
    return f"KW-{first}-{second}"


@dataclass
class PairingSession:
    code: str
    state: PairingState = "showing_code"
    message: str = ""
    agent_id: str = ""
    tenant_id: str = ""
    started_at: float = field(default_factory=time.monotonic)
    last_poll_at: float = 0.0


class PairingCoordinator:
    """Module-level singleton that owns the current pairing attempt.

    Only one attempt at a time — starting a new pairing cancels whatever
    was in progress. The background thread polls the control plane every
    3 seconds for up to 10 minutes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._session: PairingSession | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._settings: AgentSettings | None = None

    # ---- lifecycle ---- #

    def start(self, *, agent_name: str) -> PairingSession:
        """Begin a new pairing attempt. Returns the session so the UI can
        show the device code immediately.
        """
        with self._lock:
            self._stop_thread_locked()
            settings = load_settings()
            # Persist the chosen agent name so we don't lose it if the
            # user refreshes the wizard. This is the only place the
            # agent_name is set.
            if agent_name.strip():
                settings.agent_name = agent_name.strip()
                save_settings(settings)
            self._settings = settings
            session = PairingSession(code=generate_device_code())
            self._session = session
            self._stop_event = threading.Event()
            self._thread = threading.Thread(
                target=self._run, name="keystone-pairing", daemon=True
            )
            self._thread.start()
            logger.info(f"Pairing started — code={session.code}")
            return session

    def cancel(self) -> None:
        with self._lock:
            self._stop_thread_locked()
            self._session = None

    def _stop_thread_locked(self) -> None:
        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=2.0)
            self._thread = None

    def status(self) -> PairingSession | None:
        with self._lock:
            return self._session

    # ---- poll thread ---- #

    def _run(self) -> None:
        assert self._settings is not None
        client = ControlPlaneClient(self._settings)
        deadline = time.monotonic() + 10 * 60  # 10 minutes
        poll_every = 3.0
        try:
            while not self._stop_event.is_set() and time.monotonic() < deadline:
                session = self.status()
                if session is None:
                    return
                session.last_poll_at = time.monotonic()
                try:
                    resp = client.pair_claim(session.code)
                except TransportError as e:
                    session.state = "error"
                    session.message = str(e)
                    logger.warning(f"Pairing poll transport error: {e}")
                    # Don't give up — network blip during install is common.
                    self._stop_event.wait(poll_every)
                    continue

                if resp.status == "claimed":
                    self._finalise(resp)
                    return
                if resp.status == "expired":
                    session.state = "expired"
                    session.message = (
                        "Code expired before claim. Generate a new one."
                    )
                    return
                # status == pending — keep polling
                self._stop_event.wait(poll_every)
            # Fell off the end without claim
            session = self.status()
            if session is not None and session.state == "showing_code":
                session.state = "expired"
                session.message = "Pairing timed out after 10 minutes."
        finally:
            client.close()

    def _finalise(self, resp: PairClaimResponse) -> None:
        session = self.status()
        if session is None:
            return
        if not resp.token or not resp.agent_id or not resp.tenant_id:
            session.state = "error"
            session.message = "Control plane returned no token"
            return
        # Persist: token into DPAPI, ids into agent-config.json
        store = get_store()
        store.set(KEY_AGENT_TOKEN, resp.token)
        settings = load_settings()
        settings.agent_id = resp.agent_id
        settings.tenant_id = resp.tenant_id
        settings.has_token = True
        settings.poll_interval_seconds = resp.poll_interval_seconds
        save_settings(settings)
        session.state = "claimed"
        session.message = "Paired successfully."
        session.agent_id = resp.agent_id
        session.tenant_id = resp.tenant_id
        logger.info(f"Pairing complete — agent_id={resp.agent_id}")


# Module-level singleton
_coordinator: PairingCoordinator | None = None


def get_coordinator() -> PairingCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = PairingCoordinator()
    return _coordinator


def unpair() -> None:
    """Forget the current pairing — useful for dev and for re-pair flows.

    Removes the token from DPAPI and clears agent_id/tenant_id in config.
    Does NOT call the control plane; that's a Phase 2 "unregister" feature.
    """
    store = get_store()
    store.delete(KEY_AGENT_TOKEN)
    settings = load_settings()
    settings.agent_id = ""
    settings.tenant_id = ""
    settings.has_token = False
    save_settings(settings)
    logger.info("Agent unpaired")
