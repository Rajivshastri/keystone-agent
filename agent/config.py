r"""Agent configuration — persistent settings + runtime state.

The agent's settings live in `%ProgramData%\Keystone\agent-config.json`
(or an override path in dev). Settings are small, human-editable, and
never hold secrets — secrets go through the DPAPI-backed secret store.

For Phase 0 we keep everything in one JSON file; Phase 1 may promote this
to SQLite if the volume grows.
"""
from __future__ import annotations

import json
import os
import platform
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ── Data directory resolution ────────────────────────────────────────── #


def _default_data_dir() -> Path:
    """Return the agent's on-disk root.

    - Windows: %ProgramData%\\Keystone (falls back to %LOCALAPPDATA%)
    - Other:   ~/.keystone-agent  (for Mac/Linux dev boxes only)
    """
    if platform.system() == "Windows":
        base = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Keystone"
    return Path.home() / ".keystone-agent"


def data_dir() -> Path:
    """Resolve the agent data directory, honouring KEYSTONE_DATA_DIR if set.

    Phase 0 lets devs override the location so the agent can boot against
    a scratch directory without touching %ProgramData%.
    """
    override = os.environ.get("KEYSTONE_DATA_DIR")
    if override:
        return Path(override)
    return _default_data_dir()


# ── Settings model ───────────────────────────────────────────────────── #


@dataclass
class AgentSettings:
    """Persistent agent settings written to agent-config.json."""

    # Control plane base URL — filled during pairing, editable in the UI
    control_plane_url: str = "http://localhost:3000"

    # Assigned during successful pair/claim. Blank => unpaired.
    agent_id: str = ""
    tenant_id: str = ""

    # Machine fingerprint — stable across restarts, regenerated on install
    machine_fingerprint: str = ""

    # Polling cadence (seconds). Server can override via health response.
    # 3s default so interactive dashboard actions (master Load/Fetch,
    # break detail) feel responsive.
    poll_interval_seconds: int = 3

    # Friendly name shown in the control plane
    agent_name: str = ""

    # Where the agent writes run output, logs, outbox, etc.
    workdir: str = ""

    # Feature flags
    offline_mode: bool = False

    # The bearer token is NOT stored here — it goes through DPAPI.
    # But we remember whether a token exists so the UI can show paired state.
    has_token: bool = False

    extras: dict = field(default_factory=dict)


# ── Load / save ──────────────────────────────────────────────────────── #


def _config_path() -> Path:
    return data_dir() / "agent-config.json"


def load_settings() -> AgentSettings:
    """Load settings from disk, creating defaults if the file doesn't exist.

    Never raises on missing or corrupt config — the agent should still boot
    and present the first-run wizard. If the JSON is malformed we keep a
    backup and start fresh.
    """
    path = _config_path()
    if not path.exists():
        settings = AgentSettings()
        _ensure_defaults(settings)
        save_settings(settings)
        return settings
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        backup = path.with_suffix(".json.corrupt")
        try:
            path.rename(backup)
        except OSError:
            pass
        settings = AgentSettings()
        _ensure_defaults(settings)
        save_settings(settings)
        return settings
    settings = AgentSettings(**{k: raw.get(k) for k in AgentSettings.__dataclass_fields__ if k in raw})
    _ensure_defaults(settings)
    return settings


def save_settings(settings: AgentSettings) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    tmp.replace(path)


def _ensure_defaults(settings: AgentSettings) -> None:
    if not settings.workdir:
        settings.workdir = str(data_dir() / "workdir")
    # Deliberately do NOT auto-fill agent_name — the first-run wizard
    # requires the operator to pick one so the control plane shows a
    # human-friendly label. Until then the agent carries no public name.
    if not settings.machine_fingerprint:
        settings.machine_fingerprint = _generate_fingerprint()


def _generate_fingerprint() -> str:
    """Stable-ish fingerprint — node name + a random UUID saved once.

    Not cryptographic. Just a durable string the server can use to detect
    if the agent was reinstalled on the same machine (same node name) vs
    moved to a new one.
    """
    return f"{platform.node() or 'unknown'}-{uuid.uuid4().hex[:16]}"
