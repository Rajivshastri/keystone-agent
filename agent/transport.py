"""HTTPS transport layer between the agent and the control plane.

Every call is outbound. The agent never listens for the control plane;
the control plane never initiates a connection. All communication goes
through one of the four protocol endpoints defined in protocol.py.

Phase 0 implements the happy path. Phase 1 adds retry-with-backoff, the
offline outbox (queue sends when the control plane is unreachable), and
TLS certificate pinning.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import AgentSettings, load_settings
from .protocol import (
    HealthPing,
    HealthResponse,
    PairClaimRequest,
    PairClaimResponse,
    PollRequest,
    PollResponse,
    RunPush,
    RunPushResponse,
)
from .secrets import KEY_AGENT_TOKEN, get_store

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class TransportError(Exception):
    """Raised when the control plane is unreachable or returns non-2xx.

    The poll loop treats TransportError as a transient failure — it does
    not crash the agent, it just retries on the next tick. After enough
    failures in a row, the agent flips into offline mode and starts
    queueing outgoing messages in the local outbox (Phase 1).
    """


class ControlPlaneClient:
    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings
        self._client = httpx.Client(
            base_url=settings.control_plane_url,
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    # ---- auth helpers ---- #

    def _auth_headers(self) -> dict[str, str]:
        # Read settings fresh every time — the poll loop caches a
        # ControlPlaneClient for its lifetime, but agent_id is only
        # populated after pairing, which happens AFTER construction.
        # Without this reload, the first authenticated call after
        # pairing sends an empty X-Keystone-Agent-Id header.
        settings = load_settings()
        token = get_store().get(KEY_AGENT_TOKEN)
        if not token or not settings.agent_id:
            return {}
        return {
            "Authorization": f"Bearer {token}",
            "X-Keystone-Agent-Id": settings.agent_id,
        }

    # ---- endpoints ---- #

    def pair_claim(self, code: str) -> PairClaimResponse:
        req = PairClaimRequest(
            code=code,
            machine_fingerprint=self._settings.machine_fingerprint,
            agent_name=self._settings.agent_name,
            version=_version_string(),
        )
        resp = self._post_json("/api/v1/agent/pair/claim", req.model_dump())
        return PairClaimResponse.model_validate(resp)

    def poll(self, max_jobs: int = 8) -> PollResponse:
        req = PollRequest(max_jobs=max_jobs)
        resp = self._post_json(
            "/api/v1/agent/poll",
            req.model_dump(),
            authed=True,
        )
        return PollResponse.model_validate(resp)

    def push_run(self, run: RunPush) -> RunPushResponse:
        resp = self._post_json(
            "/api/v1/agent/runs",
            run.model_dump(),
            authed=True,
        )
        return RunPushResponse.model_validate(resp)

    def push_task(self, task: "TaskPush") -> "TaskPushResponse":
        """Push a non-recon task result to the control plane.

        Mirrors push_run but targets /api/v1/agent/tasks. Used by
        client_onboard, pool_create, welcome_email, ws_upload,
        fees_compute, fees_email, bod_run, eod_run.
        """
        from .protocol import TaskPush, TaskPushResponse  # local to avoid cycles
        resp = self._post_json(
            "/api/v1/agent/tasks",
            task.model_dump(),
            authed=True,
        )
        return TaskPushResponse.model_validate(resp)

    def upload_file_request(
        self, upload_path: str, filename: str, data: bytes
    ) -> dict[str, Any]:
        """PUT raw bytes to the control plane to fulfil a file_request.

        The upload_path is an agent-relative URL handed to us in the
        poll response, e.g. "/api/v1/agent/file-requests/{id}/upload".
        We send bytes as-is with an X-Keystone-Filename header so the
        control plane can record the original filename on the row.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/octet-stream",
            "X-Keystone-Filename": filename,
        }
        headers.update(self._auth_headers())
        try:
            resp = self._client.post(upload_path, content=data, headers=headers)
        except httpx.HTTPError as e:
            raise TransportError(f"{upload_path} network error: {e}") from e
        if resp.status_code >= 400:
            raise TransportError(
                f"{upload_path} {resp.status_code}: {resp.text[:300]}"
            )
        try:
            return resp.json()  # type: ignore[no-any-return]
        except ValueError as e:
            raise TransportError(f"{upload_path} bad JSON response") from e

    def health(self, ping: HealthPing) -> HealthResponse:
        resp = self._post_json(
            "/api/v1/agent/health",
            ping.model_dump(),
            authed=True,
        )
        return HealthResponse.model_validate(resp)

    def push_break_detail(
        self,
        run_id: str,
        breaks: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """POST per-break detail to the control plane's cache endpoint.

        Called from the push_break_detail command handler. If `error`
        is supplied, the server records the run as a fetch failure
        (so the dashboard can stop spinning). Otherwise `breaks` must
        be a list of engine-specific break rows the dashboard will
        render on the explain page.
        """
        body: dict[str, Any] = {}
        if error is not None:
            body["error"] = error
            body["breaks"] = []
        else:
            body["breaks"] = list(breaks or [])
        return self._post_json(
            f"/api/v1/agent/break-detail/{run_id}",
            body,
            authed=True,
        )

    def ack_command(
        self,
        command_id: str,
        ok: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """ACK a reverse-channel command back to the control plane.

        Flips the server-side command row to either `acked` (when ok=True)
        or `failed` (when ok=False, with an `error` string). Idempotent —
        a double-ACK returns 200 with already_acked=true. Raises
        TransportError on network or 5xx failure so the caller can
        decide whether to retry on the next poll tick.
        """
        body: dict[str, Any] = {"ok": ok}
        if ok and result is not None:
            body["result"] = result
        if not ok:
            body["error"] = error or "unknown error"
        return self._post_json(
            f"/api/v1/agent/commands/{command_id}/ack",
            body,
            authed=True,
        )

    # ---- low-level ---- #

    def _post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        authed: bool = False,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if authed:
            headers.update(self._auth_headers())
        try:
            resp = self._client.post(path, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise TransportError(f"{path} network error: {e}") from e
        if resp.status_code >= 500:
            raise TransportError(f"{path} {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise TransportError(f"{path} {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()  # type: ignore[no-any-return]
        except ValueError as e:
            raise TransportError(f"{path} bad JSON response") from e


def _version_string() -> str:
    from . import __version__

    return __version__
