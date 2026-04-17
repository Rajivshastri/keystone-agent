"""Keystone reconciliation agent — package entry point.

The agent is a Windows service that runs reconciliation jobs locally and
pushes only summary data up to the Keystone control plane. Raw customer
data never leaves the machine.

Modules:
  protocol  — pydantic models for the agent ↔ control plane HTTPS API
  config    — on-disk configuration (paths, URLs, feature flags)
  secrets   — DPAPI-backed secret store (M365, WS portal, custodian passwords)
  transport — HTTPS client for polling, pushing runs, sending heartbeats
  poll      — background loop that drains the job queue
  runner    — dispatches jobs to the core reconciliation engine
  local_ui  — FastAPI app on localhost:5001 for first-run wizard + admin
  service   — Windows service wrapper (pywin32)
  main      — foreground entry point (uvicorn + poll loop for dev)
"""

_BASE_VERSION = "0.1.0"

try:
    from ._build import GIT_SHA as _GIT_SHA, BUILT_AT as _BUILT_AT
except Exception:
    _GIT_SHA = "dev"
    _BUILT_AT = "dev"

__version__ = f"{_BASE_VERSION}+{_GIT_SHA[:7]}" if _GIT_SHA and _GIT_SHA != "dev" else f"{_BASE_VERSION}+dev"
__git_sha__ = _GIT_SHA
__built_at__ = _BUILT_AT
