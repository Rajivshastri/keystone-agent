"""Recon break-detail sidecar — on-disk JSON held next to the Excel
report so the reverse-channel push_break_detail handler can serve
break-level rows to the control plane's explain page without
re-running the engine.

One sidecar per run, named ``breaks_{job_id}.json``, co-located with
the final Keystone_* Excel file. Format:

    {
      "type":       "bank" | "holdings" | "trade",
      "recon_date": "YYYY-MM-DD",
      "breaks":     [ ... engine-specific rows ... ]
    }

Readers are expected to be tolerant of engine-specific schema
differences — the control plane's explain page has per-type
renderers that know how to interpret each shape.

Writers should NEVER raise on failure. A missing sidecar is handled
by the push_break_detail command handler as a recoverable error
(control plane shows "break detail not available" and the run can
still be explained via the dashboard's free-text fallback form).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def sidecar_path(output_path: str | Path, job_id: str) -> Path:
    """Canonical path for the sidecar next to an Excel report."""
    return Path(output_path).parent / f"breaks_{job_id}.json"


def write_bank_sidecar(
    output_path: str | Path,
    job_id: str,
    recon_date: str,
    summary_dict: dict[str, Any],
) -> None:
    """Write the sidecar for a bank recon run.

    Extracts pool break rows from the engine summary. A row is
    considered a break (and therefore explained-eligible) when its
    ``overall_status`` is anything other than ``CLEAN``. Pools without
    a ``cust_account`` (the synthetic NOT-IN-WS aggregates) are
    included because they can still surface as breaks on the
    dashboard and operators may want to explain them.

    Schema of each row, as the control plane explain page expects:
        {
          break_id:        stable unique id (cust_account for real
                           pools, pool_id for synthetic)
          pool:            strategy_name
          bank:            HDFC / AXIS / ICICI / KOTAK
          cust_account:    account number or empty
          cust_closing:    closing balance from the bank file
          ws_closing:      closing balance from the WS Bank Book
          variance:        l1_variance (cust - ws)
          status:          overall_status (MATCH / BREAK / TXN ONLY / etc)
          explained_items: any pre-existing explained_items from the
                           engine (reused by the ₹100 rule)
        }
    """
    try:
        pool_results = summary_dict.get("pool_results", []) or []
        rows = []
        for pool in pool_results:
            status = str(pool.get("overall_status") or "").upper()
            if status in ("", "MATCH", "CLEAN"):
                continue
            cust_acct = (pool.get("cust_account") or "").strip()
            # Stable id: prefer cust_account (unique), fall back to
            # strategy_name for synthetic pools.
            break_id = cust_acct or str(pool.get("strategy_name") or "")
            if not break_id:
                continue
            rows.append({
                "break_id":       break_id,
                "pool":           pool.get("strategy_name", ""),
                "bank":           pool.get("bank", ""),
                "cust_account":   cust_acct,
                "cust_closing":   pool.get("cust_closing", 0),
                "ws_closing":     pool.get("ws_closing_sum", 0),
                "variance":       pool.get("l1_variance", 0),
                "status":         status,
                "explained_items": pool.get("explained_items", []) or [],
            })
        _write(output_path, job_id, "bank", recon_date, rows)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Bank break sidecar write failed: {e}")


def write_holdings_sidecar(
    output_path: str | Path,
    job_id: str,
    recon_date: str,
    results: dict[str, list],
) -> None:
    """Write the sidecar for a holdings recon run.

    Extracts rows from the three "break" categories (unexplained,
    custody_only, ws_only) and flattens them into a single list with
    a ``category`` field so the dashboard can group by category.

    Each break row is the engine's native dict shape plus two keys:
        break_id: ``{category}:{client_id}:{isin}`` stable id
        category: which bucket the row came from
    """
    try:
        rows = []
        for category in ("unexplained", "custody_only", "ws_only"):
            for r in results.get(category, []) or []:
                client_id = str(r.get("client_id") or "")
                isin = str(r.get("isin") or "")
                if not client_id or not isin:
                    continue
                rows.append({
                    "break_id": f"{category}:{client_id}:{isin}",
                    "category": category,
                    **{k: v for k, v in r.items() if not k.startswith("_")},
                })
        _write(output_path, job_id, "holdings", recon_date, rows)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Holdings break sidecar write failed: {e}")


def write_trade_sidecar(
    output_path: str | Path,
    job_id: str,
    recon_date: str,
    summary_dict: dict[str, Any],
) -> None:
    """Write the sidecar for a trade recon run.

    Flattens the check results from check1/check2/check3/check5 into
    a single list of break rows. Each row carries a ``check`` field
    so the dashboard can group by check.

    Trade recon does NOT currently use the explain workflow (breaks
    must be reconciled, not explained) but we still write the sidecar
    so the ``push_break_detail`` command has something to return for
    audit/investigation purposes.
    """
    try:
        rows = []
        for check_key, check_label in (
            ("check1_results", "C1"),
            ("check2_results", "C2"),
            ("check3_results", "C3"),
            ("check5_results", "C5"),
        ):
            for idx, r in enumerate(summary_dict.get(check_key, []) or []):
                if not isinstance(r, dict):
                    continue
                status = str(r.get("status") or "").upper()
                if status in ("", "MATCH"):
                    continue
                rows.append({
                    "break_id": f"{check_label}:{idx}",
                    "check":    check_label,
                    **{k: v for k, v in r.items() if not k.startswith("_")},
                })
        _write(output_path, job_id, "trade", recon_date, rows)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Trade break sidecar write failed: {e}")


def read_sidecar(output_path: str | Path, job_id: str) -> dict[str, Any] | None:
    """Load a sidecar next to an Excel report, or None if not present.

    Returns the raw parsed JSON dict (``{type, recon_date, breaks}``)
    or None if the file is missing or unreadable. Callers handle
    absence gracefully — it's an expected state for pre-sidecar runs
    or runs where the Excel was cleaned up without the sidecar.
    """
    path = sidecar_path(output_path, job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Break sidecar read failed for {path}: {e}")
        return None


def _write(
    output_path: str | Path,
    job_id: str,
    recon_type: str,
    recon_date: str,
    breaks: list[dict[str, Any]],
) -> None:
    path = sidecar_path(output_path, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "type":       recon_type,
        "recon_date": recon_date,
        "breaks":     breaks,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    logger.info(f"Break sidecar written: {path.name} ({len(breaks)} row(s))")
