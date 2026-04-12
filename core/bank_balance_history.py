"""Bank balance history — cumulative log of per-pool closing balances.

Mirrors the Flask app's `_append_bank_balance_history` and the Tier-2
fallback path of `_load_bank_balance_history` (`app.py:47-272`).

The bank reconciliation engine needs the previous working day's closing
balance as the opening balance for HDFC and Axis (the two banks that
don't supply opening balances in their statement files). The engine has
two ways to obtain that:

1. **Tier 1 (primary, implicit):** the engine reads the previous working
   day's raw bank statement files via the calendar-aware ``bank_dates``
   range. This works as long as the raw files are still on disk.

2. **Tier 2 (fallback, this module):** a cumulative JSON log written
   after every successful bank recon. Used when raw files have been
   cleaned up (long holiday windows, fresh installs, retention policies).

The history file lives at ``{workdir}/data/bank_balance_history.json``.
The format is a list of per-pool entries, append-only:

    [
      {
        "date":         "2026-04-13",
        "pool":         "Aristos HDFC",
        "bank":         "HDFC",
        "cust_account": "50100123456789",
        "cust_closing": 1234567.89,
        "ws_closing":   1234500.00,
        "variance":     67.89,
        "status":       "BREAK"
      },
      ...
    ]

Synthetic "NOT IN WS" pool rows (those without a ``cust_account``) are
intentionally excluded — they have no real account to track over time.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def history_path(workdir: str | Path) -> Path:
    """Resolve the canonical bank balance history file path under a workdir."""
    return Path(workdir) / "data" / "bank_balance_history.json"


def append_bank_balance_history(date_str: str, summary: Any,
                                workdir: str | Path) -> None:
    """Append today's per-pool closing balances to the cumulative history file.

    ``summary`` is a ``BankReconSummary`` (or anything with a ``to_dict()``
    method that yields a ``pool_results`` list). Skips pools with no
    custodian account (synthetic NOT-IN-WS entries). Failures are logged
    at WARNING but never raised — the history is best-effort, never
    blocks the run.

    Mirrors Flask ``app.py:47-72`` exactly.
    """
    path = history_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        hist: list[dict[str, Any]] = []
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    hist = json.load(f) or []
            except Exception:
                hist = []

        s = summary.to_dict() if hasattr(summary, "to_dict") else dict(summary)
        for pool in s.get("pool_results", []) or []:
            if not pool.get("cust_account"):
                continue  # skip NOT IN WS synthetic entries
            hist.append({
                "date":         date_str,
                "pool":         pool.get("strategy_name", ""),
                "bank":         pool.get("bank", ""),
                "cust_account": pool.get("cust_account", ""),
                "cust_closing": pool.get("cust_closing", 0),
                "ws_closing":   pool.get("ws_closing_sum", 0),
                "variance":     pool.get("l1_variance", 0),
                "status":       pool.get("overall_status", ""),
            })

        with path.open("w", encoding="utf-8") as f:
            json.dump(hist, f)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Bank balance history save failed: {e}")


def load_bank_balance_history(date_str: str,
                              workdir: str | Path) -> dict[str, dict[str, float]]:
    """Load the most recent per-account closing balance from the history file.

    Returns the same shape the bank workflow expects:

        {
          "cust": {account_no: cust_closing},
          "ws":   {mapid:      ws_closing_sum},
        }

    For each custodian account, picks the latest entry strictly before
    ``date_str``. Future-dated entries are ignored so a re-run on an
    earlier date doesn't pick up balances from a later run.

    Tier-1 (parsing prev-day raw files) is handled natively by the bank
    workflow via the calendar-aware ``bank_dates`` expansion. This loader
    is the Tier-2 fallback only — the part Flask called
    ``_load_bank_balance_history`` lines 241-266.

    Failures are logged and return empty dicts so the recon still runs.
    """
    cust: dict[str, float] = {}
    ws: dict[str, float] = {}
    path = history_path(workdir)
    if not path.exists():
        return {"cust": cust, "ws": ws}
    try:
        with path.open(encoding="utf-8") as f:
            hist = json.load(f) or []
        # account_no -> (latest_date, closing) so we keep the most recent
        # entry strictly before date_str.
        best: dict[str, tuple[str, float]] = {}
        for entry in hist:
            acct = entry.get("cust_account", "")
            edate = entry.get("date", "")
            closing = entry.get("cust_closing", 0)
            if not acct or not edate or edate >= date_str:
                continue
            existing = best.get(acct)
            if existing is None or edate > existing[0]:
                best[acct] = (edate, closing)
        for acct, (_, closing) in best.items():
            cust[acct] = closing
        if cust:
            logger.info(
                f"Bank balance history fallback: {len(cust)} account(s) "
                f"loaded from {path.name}"
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Bank balance history load failed: {e}")
    return {"cust": cust, "ws": ws}
