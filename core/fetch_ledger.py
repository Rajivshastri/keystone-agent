"""
JIT Fetch Ledger
----------------
Tracks the last successful fetch timestamp per recon-type so the
Just-In-Time fetch on each recon run can compute a tight lookback
window (since = last_fetch_at - 30min) instead of pulling a fixed
72h or 7-day window every time.

File: data/last_fetch.json — keys are recon types ('bank' | 'holdings'
| 'trade'); values are ISO-8601 UTC timestamps.

  {
    "bank":     "2026-04-25T08:42:13Z",
    "holdings": "2026-04-25T08:30:01Z",
    "trade":    "2026-04-25T09:11:55Z"
  }

When a recon type is missing from the ledger (first run after deploy
or fresh install), the caller should fall back to a 7-day lookback
as a safety net. The admin 90-day override bypasses this entirely
and forces since = today - 90 days.

Slice 4 of the JIT-fetch redesign. See also: archive window alignment
(Slice 5), progress channel (Slice 6).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LEDGER_FILENAME = 'last_fetch.json'

# Overlap with the prior fetch window so we don't drop emails that
# arrive right at the seam between two runs.
DEFAULT_OVERLAP_MINUTES = 30

# First-run safety net when the ledger has no entry for this recon type.
# Covers a long weekend or short deploy gap; admin 90-day override
# exists for deeper rebuilds.
FIRST_RUN_LOOKBACK_DAYS = 7


def ledger_path(data_dir: Path | str) -> Path:
    """Resolve the ledger file path under the deploy's data dir."""
    return Path(data_dir) / 'data' / LEDGER_FILENAME


def load_ledger(data_dir: Path | str) -> dict:
    """Load the ledger. Empty dict if the file is missing or unreadable."""
    p = ledger_path(data_dir)
    if not p.exists():
        return {}
    try:
        with p.open(encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f'Fetch ledger unreadable ({p}): {e}')
        return {}


RECON_TYPES = ('bank', 'holdings', 'trade', 'bod')

# Side-list of email-fetch days that have succeeded but haven't yet been
# rolled up into the ledger timestamp. Lets a re-run after partial errors
# skip the days that already came down clean. Cleared on finalize.
FETCHED_DAYS_KEY = '_fetched_days_email'


def mark_day_fetched(data_dir: Path | str, day: str) -> None:
    """Record that the email pull for a given day succeeded. Shared across
    recon types because the email phase loads all configured sources."""
    p = ledger_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        ledger = load_ledger(data_dir)
        days = list(ledger.get(FETCHED_DAYS_KEY) or [])
        if day not in days:
            days.append(day)
        ledger[FETCHED_DAYS_KEY] = days
        with p.open('w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logger.warning(f'mark_day_fetched({day}) failed: {e}')


def clear_fetched_days(data_dir: Path | str) -> None:
    """Drop the side-list once a finalize call has rolled the timestamp
    ledger forward. Subsequent compute_since calls use the timestamp
    instead of the per-day list."""
    p = ledger_path(data_dir)
    try:
        ledger = load_ledger(data_dir)
        if FETCHED_DAYS_KEY in ledger:
            del ledger[FETCHED_DAYS_KEY]
            with p.open('w', encoding='utf-8') as f:
                json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logger.warning(f'clear_fetched_days failed: {e}')


def get_fetched_days(data_dir: Path | str) -> set:
    """Return the set of days whose email pull has already succeeded
    in the current (in-flight) window. Empty set when none / corrupt."""
    ledger = load_ledger(data_dir)
    days = ledger.get(FETCHED_DAYS_KEY) or []
    return set(days) if isinstance(days, list) else set()


def update_ledger(data_dir: Path | str, recon_type: str,
                  fetched_at: Optional[datetime] = None) -> None:
    """Stamp `now` (or the passed time) for a recon type. Best-effort —
    a write failure here doesn't fail the recon, just causes the next
    JIT fetch to use a slightly wider lookback.

    The email fetch loads ALL configured sources regardless of which
    recon triggered it (holdings / bank / trade share one inbox sweep),
    so a successful run for any type effectively brings every type up
    to date. We stamp all three keys at once — otherwise running
    holdings then bank back-to-back makes bank do a redundant 7-day
    first-run pull when its key is still empty.
    """
    if fetched_at is None:
        fetched_at = datetime.now(timezone.utc)
    p = ledger_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = fetched_at.strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        ledger = load_ledger(data_dir)
        for t in RECON_TYPES:
            ledger[t] = stamp
        with p.open('w', encoding='utf-8') as f:
            json.dump(ledger, f, indent=2, sort_keys=True)
    except Exception as e:
        logger.warning(f'Fetch ledger update failed for {recon_type}: {e}')


def compute_since(data_dir: Path | str, recon_type: str,
                  recon_date: str,
                  override_lookback_days: Optional[int] = None,
                  overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
                  first_run_lookback_days: int = FIRST_RUN_LOOKBACK_DAYS) -> str:
    """Return the ISO-8601 UTC timestamp the next fetch should start from.

    Logic:
      1. If override_lookback_days is set (admin 90-day pull), return
         today - that many days. Bypasses the ledger entirely.
      2. Else load the ledger entry for this recon_type. If present,
         return (entry - overlap_minutes).
      3. Else (first run for this recon type), return
         (today - first_run_lookback_days). Caller can tighten this for
         narrower workflows — e.g. BoD only needs 3 days to cover a
         long weekend, while recon defaults to 7.

    Returns the timestamp as 'YYYY-MM-DDTHH:MM:SSZ' suitable for the
    M365 Graph $filter clause used by EmailIngestor.fetch_for_range.
    """
    if override_lookback_days is not None and override_lookback_days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=int(override_lookback_days))
        return since.strftime('%Y-%m-%dT%H:%M:%SZ')

    ledger = load_ledger(data_dir)
    raw = ledger.get(recon_type, '')
    if raw:
        try:
            # Ledger writes 'Z'; parse strictly.
            ts = datetime.strptime(raw, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
            since = ts - timedelta(minutes=overlap_minutes)
            return since.strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            logger.warning(f'Ledger {recon_type} unparseable: {raw!r}; falling back to first-run window')

    # First run / unparseable: safety net anchored on TODAY (not the
    # recon date). Anchoring on recon_date inflates the window when the
    # operator runs an old back-date.
    # first_run_lookback_days = N means "today + (N-1) prior = N buckets",
    # so we subtract one less day than the parameter.
    since = datetime.now(timezone.utc) - timedelta(days=max(0, first_run_lookback_days - 1))
    return since.strftime('%Y-%m-%dT%H:%M:%SZ')
