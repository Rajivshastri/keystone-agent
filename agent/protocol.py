"""Agent ↔ control plane protocol — Python side.

Mirrors lib/protocol.ts in the control plane repo. If you change one side,
change the other. Keep field names in snake_case (wire format) and use
pydantic to validate on both send and receive.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

PROTOCOL_VERSION = "v1"
DEFAULT_POLL_INTERVAL_SECONDS = 60

# ── Pair / claim ─────────────────────────────────────────────────────── #

_CODE_RE = re.compile(r"^KW-[A-Z0-9]{4}-[A-Z0-9]{4}$")


class PairClaimRequest(BaseModel):
    code: str
    machine_fingerprint: str = Field(min_length=8, max_length=128)
    agent_name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=32)

    @field_validator("code")
    @classmethod
    def _validate_code(cls, v: str) -> str:
        if not _CODE_RE.match(v):
            raise ValueError("code must match KW-XXXX-XXXX")
        return v


class PairClaimResponse(BaseModel):
    status: Literal["claimed", "pending", "expired"]
    agent_id: str | None
    tenant_id: str | None
    token: str | None
    poll_interval_seconds: int = Field(gt=0)


# ── Poll ─────────────────────────────────────────────────────────────── #


class PollRequest(BaseModel):
    max_jobs: int = Field(default=8, ge=1, le=32)


JobType = Literal[
    # Reconciliation engines — produce RunPush results
    "holdings_recon",
    "bank_recon",
    "trade_recon",
    # Maintenance / utility jobs — also use RunPush (status only)
    "fetch_emails",
    "ws_download",
    "diagnostic_bundle",
    # Task-style jobs — produce TaskPush results
    "client_onboard",       # CML → registry → WS account creation → GST → welcome email
    "pool_create",          # Headless 3-step + broker invitations
    "welcome_email",        # Resend welcome email for existing client(s)
    "ws_upload",            # Upload a file via WS portal mapper (0096 / NSDL / GST / etc.)
    "fees_compute",         # Recompute fee earnings for a date range
    "fees_email",           # Email fee statement PDFs to entities
    "bod_run",              # Beginning-of-day pipeline (price upload + NAV + flags)
    "eod_run",              # End-of-day pipeline (file checks + recon summary email)
    # Phase 5 — agent config + read-only data round-trip
    "config_get",           # Read an agent-side JSON config file
    "config_set",           # Write an agent-side JSON config file
    "bank_history_query",   # Read bank_balance_history.json
    "benchmarks_query",     # Read config/benchmarks.json
]


class PollJob(BaseModel):
    id: str
    type: JobType
    payload: dict[str, Any]
    scheduled_at: str  # ISO 8601


class UpdateNotification(BaseModel):
    version: str
    download_url: str
    sha256: str = Field(min_length=64, max_length=64)
    required: bool = False


class FileRequestJob(BaseModel):
    id: str
    job_id: str | None
    run_id: str | None
    upload_url: str


class AgentCommand(BaseModel):
    """Reverse-channel command pushed from the control plane to the agent.

    Rides on the poll response alongside jobs and file_requests. The
    agent drains commands sequentially, executes each via
    agent.runner._execute_command, and ACKs the result to
    /api/v1/agent/commands/{id}/ack. See KEYSTONE_HANDOVER.md for the
    full design.

    ``kind`` is an open-ended string so new command types can be added
    without bumping the protocol. The agent's command dispatcher is the
    source of truth for what it recognises; unknown kinds ACK with an
    error so the control plane sees them in `failed` state.
    """

    id: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    expires_at: str  # ISO 8601


class PollResponse(BaseModel):
    jobs: list[PollJob]
    file_requests: list[FileRequestJob] = Field(default_factory=list)
    commands: list[AgentCommand] = Field(default_factory=list)
    update: UpdateNotification | None = None


# ── Runs (push) ──────────────────────────────────────────────────────── #

ReconType = Literal["holdings", "bank", "trade"]
RunStatus = Literal[
    "all_clear",
    "breaks_found",
    "explained",
    "pending_resolution",
    "failed",
]
LogLevel = Literal["debug", "info", "warning", "error"]


class LogLine(BaseModel):
    ts: str
    level: LogLevel
    msg: str = Field(max_length=4000)


class RunPush(BaseModel):
    job_id: str | None
    type: ReconType
    recon_date: str  # YYYY-MM-DD
    status: RunStatus
    counts: dict[str, int | str]
    attachments_meta: dict[str, Any] = Field(default_factory=dict)
    reminder_count: int = Field(default=0, ge=0)
    log_lines: list[LogLine] = Field(default_factory=list, max_length=1000)

    @field_validator("recon_date")
    @classmethod
    def _validate_date(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError("recon_date must be YYYY-MM-DD") from e
        return v


class RunPushResponse(BaseModel):
    ok: bool
    run_id: str | None
    duplicate: bool = False


# ── Tasks (push) ─────────────────────────────────────────────────────── #
#
# Tasks are jobs that aren't reconciliations — client onboarding, pool
# creation, WS uploads, welcome-email resends, etc. They reuse the
# poll → execute → push pipeline but produce a different result shape
# (no recon counts, no recon_date semantics).

TaskType = Literal[
    "client_onboard",
    "pool_create",
    "welcome_email",
    "ws_upload",
    "fees_compute",
    "fees_email",
    "bod_run",
    "eod_run",
    "config_get",
    "config_set",
    "bank_history_query",
    "benchmarks_query",
]  # type: ignore[assignment]
TaskStatus = Literal["ok", "partial", "failed"]


class TaskPush(BaseModel):
    """Result of a non-reconciliation task. Mirrors RunPush in spirit
    but carries a flexible result dict instead of recon counts."""
    job_id: str | None
    type: TaskType
    status: TaskStatus
    result: dict[str, Any] = Field(default_factory=dict)
    attachments_meta: dict[str, Any] = Field(default_factory=dict)
    log_lines: list[LogLine] = Field(default_factory=list, max_length=1000)


class TaskPushResponse(BaseModel):
    ok: bool
    task_id: str | None
    duplicate: bool = False


# ── Health ───────────────────────────────────────────────────────────── #


class HealthPing(BaseModel):
    version: str
    disk_free_mb: int | None = Field(ge=0, default=None)
    pending_jobs: int = Field(ge=0)
    metrics: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    ok: Literal[True]
    server_time: str
    poll_interval_seconds: int = Field(gt=0)
