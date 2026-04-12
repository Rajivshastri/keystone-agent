"""Calendar helpers — holiday/working-weekend awareness.

Mirrors the Flask app's calendar logic exactly. The reconciliation engines
need to know which dates count as working days so that bank balance
opening = previous working day's closing, and so the date-range expansion
for multi-day bank statements is correct.

The calendar lives at ``config/calendar.json`` (or wherever
``agent.paths.config_dir()`` resolves to) with this shape::

    {
      "holidays":         ["2026-04-14", ...],
      "working_weekends": ["2026-04-12", ...],
      "no_trade_days":    ["2026-04-15", ...]
    }

Day-type semantics (matches Flask ``_day_type`` in ``app.py:3855``):

- ``holiday``         — marked as holiday, no recons fire
- ``working_weekend`` — weekend marked as working, all recons fire
- ``no_trade``        — marked as no-trade, holdings + bank only (no trade recon)
- ``weekend``         — Saturday/Sunday, not overridden, no recons fire
- ``working``         — normal Mon–Fri, all recons fire

Anything in ``("working", "working_weekend", "no_trade")`` counts as a
"bank business day" for the purposes of opening-balance carry-over.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def load_calendar(config_dir: Path | None = None) -> dict[str, list[str]]:
    """Load calendar.json into the {holidays, working_weekends, no_trade_days} shape.

    If ``config_dir`` is not supplied we resolve it via
    ``agent.paths.config_dir()``. Missing files / unreadable JSON degrade to
    an empty calendar so callers never crash on a fresh install.
    """
    if config_dir is None:
        from agent.paths import config_dir as _resolve_config_dir
        config_dir = _resolve_config_dir()

    cal_path = Path(config_dir) / "calendar.json"
    raw: dict[str, Any] = {}
    if cal_path.exists():
        try:
            raw = json.loads(cal_path.read_text(encoding="utf-8")) or {}
        except Exception:
            raw = {}
    return {
        "holidays":         list(raw.get("holidays", []) or []),
        "working_weekends": list(raw.get("working_weekends", []) or []),
        "no_trade_days":    list(raw.get("no_trade_days", []) or []),
    }


def day_type(date_str: str, cal: dict[str, list[str]]) -> str:
    """Return the effective day classification for ``date_str``.

    Identical to Flask's ``_day_type``. Returns one of:
    ``'holiday'``, ``'working_weekend'``, ``'no_trade'``, ``'weekend'``,
    ``'working'``. Falls back to ``'working'`` for unparseable dates so
    callers don't crash on bad input.
    """
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "working"
    if date_str in cal.get("holidays", []):
        return "holiday"
    if date_str in cal.get("working_weekends", []):
        return "working_weekend"
    if date_str in cal.get("no_trade_days", []):
        return "no_trade"
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return "weekend"
    return "working"


# Day types that count as "the books are open" for bank recon.
# Mirrors Flask's check at app.py:2273.
_BANK_BUSINESS_TYPES = ("working", "working_weekend", "no_trade")


def prev_working_day(date_str: str, cal: dict[str, list[str]] | None = None,
                     max_lookback: int = 10) -> str | None:
    """Walk backwards from ``date_str - 1`` until we find a working day.

    Returns the YYYY-MM-DD string of the previous working day, or ``None``
    if nothing in the lookback window qualifies (10 days by default — long
    enough to skip any realistic stretch of weekends + holidays).
    """
    if cal is None:
        cal = load_calendar()
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
    except ValueError:
        return None
    for _ in range(max_lookback):
        ds = d.strftime("%Y-%m-%d")
        if day_type(ds, cal) in _BANK_BUSINESS_TYPES:
            return ds
        d -= timedelta(days=1)
    return None


def bank_dates_range(date_str: str,
                     cal: dict[str, list[str]] | None = None) -> list[str]:
    """Build the calendar-aware list of dates for a bank recon run.

    Mirrors Flask ``app.py:2265-2289`` exactly. The bank reconciliation
    needs statements from the previous working day (for opening balances)
    through the recon day (for closing balances), with every day in
    between (for transactions). Bank statements arrive daily including
    weekends/holidays even though recon only runs on working days.

    The list always starts with the previous working day and always ends
    with ``date_str``. If no previous working day can be found within the
    lookback window, falls back to ``[date_str]`` so single-day bank
    recon still runs (matching Flask's fallback at app.py:2288-2289).
    """
    if cal is None:
        cal = load_calendar()
    prev_wd = prev_working_day(date_str, cal)
    if not prev_wd:
        return [date_str]
    out: list[str] = []
    cursor = datetime.strptime(prev_wd, "%Y-%m-%d")
    end = datetime.strptime(date_str, "%Y-%m-%d")
    while cursor <= end:
        out.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return out or [date_str]


def bank_dates_for_range(date_from: str, date_to: str,
                         cal: dict[str, list[str]] | None = None) -> list[str]:
    """Build the bank_dates list when the operator supplies an explicit range.

    Used by the agent's job dispatcher when the job payload has both
    ``date_from`` and ``date_to`` set. We expand the range one step
    earlier than ``date_from`` (to the previous working day before
    ``date_from``) so opening balances are correct, then enumerate
    every calendar day through ``date_to`` inclusive — same shape as
    ``bank_dates_range`` but anchored on a user-supplied window.
    """
    if cal is None:
        cal = load_calendar()
    prev_wd = prev_working_day(date_from, cal)
    start_str = prev_wd or date_from
    out: list[str] = []
    try:
        cursor = datetime.strptime(start_str, "%Y-%m-%d")
        end = datetime.strptime(date_to, "%Y-%m-%d")
    except ValueError:
        return [date_to]
    if end < cursor:
        return [date_to]
    while cursor <= end:
        out.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return out or [date_to]
