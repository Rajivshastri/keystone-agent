"""Per-day EoD pipeline audit log.

Mirrors ``core.bod_log`` but for the evening EoD process. Tracks which
Vidal files have been uploaded, which post-upload steps (apply corp
actions, batch corp action, price verification, recon query, etc.)
have run, and the resulting PDF parser output. The flask EoD orchestrator
writes to this file so re-runs can skip already-completed work and the
operator can audit what happened.

File layout: ``data/{eod_date}/eod_log.json``::

    {
      "date": "2026-04-24",
      "uploads": [
        {"filename": "XC240426.csv", "kind": "corp_action",
         "uploaded_at": "...", "ok": true, "error_message": ""},
        {"filename": "WY240426.csv", "kind": "price_bse", ...},
        {"filename": "WE240426.csv", "kind": "price_nse", ...},
      ],
      "batch_corp_action": {"ran_at": "...", "ok": true},
      "price_verification": {"ran_at": "...", "missing": N, "stale": N},
      "asset_perf":       {"ran_at": "...", "daily_from": "..."},
      "bod_runs": [
        {"process": "Order execution reconciliation", "ran_at": "..."},
        {"process": "Order execution reconciliation reversal", "ran_at": "..."},
        {"process": "Cash & Eq.", "ran_at": "..."},
        {"process": "Saleable holding", "ran_at": "..."},
      ],
      "recon_query": {"ran_at": "...", "pdf_path": "...", "summary": "..."},
      "email_sent":  {"ran_at": "...", "recipients": [...], "ok": true}
    }
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOG_FILENAME = "eod_log.json"


def _log_path(data_dir: Path | str, eod_date: str) -> Path:
    return Path(data_dir) / "data" / eod_date / LOG_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(data_dir: Path | str, eod_date: str) -> dict[str, Any]:
    p = _log_path(data_dir, eod_date)
    if not p.exists():
        return {"date": eod_date, "uploads": [], "bod_runs": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"EoD log unreadable ({p}): {e}")
        return {"date": eod_date, "uploads": [], "bod_runs": []}


def _save(data_dir: Path | str, eod_date: str, log: dict[str, Any]) -> None:
    p = _log_path(data_dir, eod_date)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(log, indent=2, sort_keys=True),
                     encoding="utf-8")
    except Exception as e:
        logger.warning(f"EoD log save failed ({p}): {e}")


def is_uploaded(data_dir: Path | str, eod_date: str, filename: str) -> bool:
    log = load(data_dir, eod_date)
    return any(
        u.get("filename") == filename and u.get("ok")
        for u in (log.get("uploads") or [])
    )


def mark_uploaded(data_dir: Path | str, eod_date: str, filename: str, *,
                  kind: str, mapid: int = 0,
                  ok: bool = True, error_message: str = "") -> None:
    log = load(data_dir, eod_date)
    log.setdefault("uploads", []).append({
        "filename":      filename,
        "kind":          kind,
        "mapid":         mapid,
        "uploaded_at":   _utc_now_iso(),
        "ok":            ok,
        "error_message": error_message,
    })
    _save(data_dir, eod_date, log)


def mark_step(data_dir: Path | str, eod_date: str, step: str, **fields) -> None:
    """Generic per-step marker — used for batch_corp_action,
    price_verification, asset_perf, recon_query, email_sent.
    """
    log = load(data_dir, eod_date)
    log[step] = {"ran_at": _utc_now_iso(), **fields}
    _save(data_dir, eod_date, log)


def mark_bod_run(data_dir: Path | str, eod_date: str, process: str) -> None:
    log = load(data_dir, eod_date)
    log.setdefault("bod_runs", []).append({
        "process": process,
        "ran_at":  _utc_now_iso(),
    })
    _save(data_dir, eod_date, log)
