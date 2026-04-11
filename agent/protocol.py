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
    "holdings_recon",
    "bank_recon",
    "trade_recon",
    "fetch_emails",
    "ws_download",
    "diagnostic_bundle",
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


class PollResponse(BaseModel):
    jobs: list[PollJob]
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
