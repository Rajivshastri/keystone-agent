"""
Activity / audit log
====================

A single, app-wide record of who did what, when. One JSONL file per
month at ``data/audit_log/<YYYY-MM>.jsonl``. Append-only — entries
are never edited, only inserted. Read paths skim the most recent file
first, falling back to older months when the operator filters by date.

Why JSONL (one row per line) instead of one big JSON array:

  * Append is a single fsync — no read-then-rewrite race when two
    requests log at the same moment.
  * Files stay readable with `tail -f` / `head` for ad-hoc forensics.
  * Reading is a one-pass scan; we never need the whole month in
    memory at once.

Each entry shape:

    {
      "ts":      "2026-05-06T15:42:31Z",   # ISO 8601 UTC
      "user":    "rajiv@thegoldstandard.in",
      "role":    "admin" | "operator" | "guest",
      "action":  "client.update",          # dotted namespace
      "target":  "client:42 (Mukti Vasani)",
      "details": {arbitrary key-value},
      "ip":      "10.0.0.5"
    }

Public API:

    log(action, *, target='', details=None,
        user='', role='', ip='', when=None)
        — write one entry. ``user`` / ``role`` / ``ip`` default to
          empty when called outside a Flask request; the convenience
          wrapper :func:`audit` (in app.py) fills them from
          ``_easy_auth_user`` and ``request.remote_addr``.

    read(*, limit=200, since='', until='', user='', action_prefix='',
         target_substr='')
        — return entries in newest-first order, filtered by the
          supplied criteria. Walks at most three months back to
          avoid pathological scans on a long-running deploy.

    available_months()
        — list YYYY-MM files present, newest first. Drives the UI
          date picker and limits the scan window for ``read``.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _root() -> Path:
    """``data/audit_log/`` under the deploy's data tree (KEYSTONE_DATA_DIR
    aware). The directory is created on first write."""
    base = os.environ.get('KEYSTONE_DATA_DIR') or str(Path(__file__).parent.parent)
    p = Path(base) / 'data' / 'audit_log'
    p.mkdir(parents=True, exist_ok=True)
    return p


def _file_for(when: datetime) -> Path:
    """Per-month JSONL file. ``when`` is interpreted as UTC."""
    return _root() / f'{when.strftime("%Y-%m")}.jsonl'


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    """ISO 8601 UTC with 'Z' suffix — matches what the rest of the app
    writes (recon_log, eod_log, etc.)."""
    return when.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def log(action: str,
        *,
        target: str = '',
        details: Optional[Dict[str, Any]] = None,
        user: str = '',
        role: str = '',
        ip: str = '',
        when: Optional[datetime] = None) -> None:
    """Append one audit entry to this month's JSONL file.

    All fields are optional except ``action``. Non-fatal — any failure
    here is logged at WARNING level and swallowed; we never want a
    broken audit log to break the operator's workflow."""
    if not action:
        return
    try:
        ts = when or _now_utc()
        rec = {
            'ts':      _iso(ts),
            'user':    (user or '').strip().lower(),
            'role':    (role or '').strip().lower(),
            'action':  action.strip(),
            'target':  (target or '').strip(),
            'details': details or {},
            'ip':      (ip or '').strip(),
        }
        path = _file_for(ts)
        # Append-and-flush. open(..., 'a') is atomic for a single
        # write() on Linux/Windows when the line is small enough to fit
        # in PIPE_BUF (4 KB on Linux, ~512 B on Windows). Audit lines
        # are well under either limit, so concurrent writes don't tear.
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as e:
        logger.warning(f'audit_log.log failed: {e}')


def available_months() -> List[str]:
    """Months that have an audit file, newest first. Each entry is
    ``YYYY-MM``."""
    root = _root()
    if not root.exists():
        return []
    months: List[str] = []
    for p in root.glob('*.jsonl'):
        stem = p.stem
        if len(stem) == 7 and stem[4] == '-':
            months.append(stem)
    months.sort(reverse=True)
    return months


def read(*,
         limit: int = 200,
         since: str = '',
         until: str = '',
         user: str = '',
         action_prefix: str = '',
         target_substr: str = '',
         max_months_back: int = 6) -> List[Dict[str, Any]]:
    """Return audit entries in newest-first order.

    Filters (all optional, ANDed):
      * ``since`` / ``until``  — ISO date or datetime; inclusive bounds.
      * ``user``               — exact match (case-insensitive).
      * ``action_prefix``      — startswith match on action.
      * ``target_substr``      — substring match on target (case-insensitive).
      * ``limit``              — cap (default 200).

    Walks at most ``max_months_back`` files even if ``limit`` isn't
    reached — bounds the scan on a long-running deploy."""
    user_l         = (user or '').strip().lower()
    action_pfx     = (action_prefix or '').strip()
    target_sub_l   = (target_substr or '').strip().lower()
    out: List[Dict[str, Any]] = []
    months = available_months()
    if max_months_back > 0:
        months = months[:max_months_back]

    def _matches(rec: Dict[str, Any]) -> bool:
        if user_l and rec.get('user', '').lower() != user_l:
            return False
        if action_pfx and not rec.get('action', '').startswith(action_pfx):
            return False
        if target_sub_l and target_sub_l not in (rec.get('target', '') or '').lower():
            return False
        ts = rec.get('ts', '')
        if since and ts < since:
            return False
        if until and ts > until:
            return False
        return True

    for ym in months:
        path = _root() / f'{ym}.jsonl'
        if not path.exists():
            continue
        # Read the whole month, filter, then prepend (newest first
        # within the month, then older months on the tail). Reading
        # in reverse line order is more code than is worth the
        # microseconds saved at this scale.
        try:
            with open(path, 'r', encoding='utf-8') as f:
                month_entries: List[Dict[str, Any]] = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if _matches(rec):
                        month_entries.append(rec)
            month_entries.sort(key=lambda r: r.get('ts', ''), reverse=True)
            out.extend(month_entries)
            if len(out) >= limit:
                break
        except Exception as e:
            logger.warning(f'audit_log.read: skipping unreadable {path.name}: {e}')
            continue

    return out[:limit]
