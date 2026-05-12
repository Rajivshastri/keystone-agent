"""Per-day BOD upload log.

Tracks which Value Research NAV files have already been posted to WS so
that an afternoon re-run of the BOD pipeline only uploads files that
arrived after the morning run. Mirrors the per-day audit-trail style
the recon flows use, just narrower in scope.

File layout: ``data/{recon_date}/bod_log.json``::

    {
      "date":   "2026-04-26",
      "uploads": [
        {
          "filename":   "NAVUpdate-26042026-1.csv",
          "uploaded_at":"2026-04-26T03:11:42Z",
          "mapid":      46,
          "total":      11794,
          "processed":  11793,
          "errors":     1,
          "ok":         true
        },
        ...
      ],
      "asset_perf": {
        "ran_at":     "2026-04-26T03:13:05Z",
        "daily_from": "2026-04-25"
      },
      "bod_runs": [
        { "process": "Cash & Eq.",      "ran_at": "2026-04-26T03:14:11Z" },
        { "process": "Saleable holding","ran_at": "2026-04-26T03:14:50Z" }
      ]
    }

Best-effort writer — file-system errors log a warning and return; nothing
in the BOD pipeline should fail because the audit log couldn't be saved.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOG_FILENAME = "bod_log.json"


def _log_path(data_dir: Path | str, recon_date: str) -> Path:
    return Path(data_dir) / "data" / recon_date / LOG_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(data_dir: Path | str, recon_date: str) -> dict[str, Any]:
    """Read the log for a date, or return an empty skeleton."""
    p = _log_path(data_dir, recon_date)
    if not p.exists():
        return {
            "date": recon_date,
            "uploads": [],
            "asset_perf": None,
            "bod_runs": [],
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"BOD log unreadable ({p}): {e}")
        return {
            "date": recon_date,
            "uploads": [],
            "asset_perf": None,
            "bod_runs": [],
        }


def _save(data_dir: Path | str, recon_date: str, log: dict[str, Any]) -> None:
    p = _log_path(data_dir, recon_date)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(log, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as e:
        logger.warning(f"BOD log save failed ({p}): {e}")


def is_uploaded(data_dir: Path | str, recon_date: str, filename: str) -> bool:
    log = load(data_dir, recon_date)
    return any(
        u.get("filename") == filename and u.get("ok")
        for u in (log.get("uploads") or [])
    )


def list_uploaded(data_dir: Path | str, recon_date: str) -> list[str]:
    log = load(data_dir, recon_date)
    return [
        u.get("filename", "")
        for u in (log.get("uploads") or [])
        if u.get("ok")
    ]


def mark_uploaded(
    data_dir: Path | str,
    recon_date: str,
    filename: str,
    *,
    mapid: int = 46,
    total: int | None = None,
    processed: int | None = None,
    errors: int | None = None,
    ok: bool = True,
    error_message: str = "",
) -> None:
    """Record one upload result. Append-only — re-running with the same
    filename adds another row, so the audit trail keeps every attempt.
    """
    log = load(data_dir, recon_date)
    log.setdefault("uploads", []).append({
        "filename":     filename,
        "uploaded_at":  _utc_now_iso(),
        "mapid":        mapid,
        "total":        total,
        "processed":    processed,
        "errors":       errors,
        "ok":           ok,
        "error_message": error_message,
    })
    _save(data_dir, recon_date, log)


def mark_asset_perf(data_dir: Path | str, recon_date: str,
                    daily_from: str) -> None:
    log = load(data_dir, recon_date)
    log["asset_perf"] = {
        "ran_at":     _utc_now_iso(),
        "daily_from": daily_from,
    }
    _save(data_dir, recon_date, log)


def mark_bod_run(data_dir: Path | str, recon_date: str,
                 process: str) -> None:
    log = load(data_dir, recon_date)
    log.setdefault("bod_runs", []).append({
        "process": process,
        "ran_at":  _utc_now_iso(),
    })
    _save(data_dir, recon_date, log)
