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

__version__ = "0.1.0"
