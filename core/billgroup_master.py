"""
Bill Group Master
=================

Reads ``masters/BillgroupMaster.xls`` (downloaded from the WS portal via
``ws_downloader.download_billgroup_master``) and exposes the list of bill
group names for the client creation form's Bill Group dropdown.

The WS Bill Group Master XLS layout is operator-managed and the column
shape is not version-locked. We keep the parser defensive: skim every
column, find the one that looks most like a list of bill-group display
names (long-ish strings, mixed case, no obvious numerics), and return
those values deduped. Falls back to the hardcoded historical default
('No Charge - GSW') when the file is missing or unparseable so the form
never shows an empty dropdown.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


# Hardcoded fallback used to seed the dropdown when the WS master hasn't
# been downloaded yet. Matches the historical default in
# core/client_onboarding.py so existing flows keep working.
DEFAULT_BILL_GROUP = 'No Charge - GSW'


def _master_path() -> Path:
    """Resolve the BillgroupMaster XLS path. Two locations are valid:

      1. ``APP_DIR/masters/BillgroupMaster.xls`` — written by the
         dedicated /api/ws-download/billgroup-master refresh button
         (shared, not time-sliced).
      2. ``KEYSTONE_DATA_DIR/data/<latest-date>/masters/BillgroupMaster.xls``
         — written by the daily masters sweep (date-bucketed). When the
         shared copy is missing, fall back to the most recent date-bucket
         so the dropdown still populates without requiring an extra click.

    Returns the shared path if present, otherwise the most recent dated
    copy, otherwise the (non-existent) shared path so callers can call
    ``.exists()`` and surface 'no master yet'."""
    app_dir = Path(__file__).parent.parent
    shared = app_dir / 'masters' / 'BillgroupMaster.xls'
    if shared.exists():
        return shared
    # Fallback — scan the date-bucketed data tree for the newest copy.
    base = os.environ.get('KEYSTONE_DATA_DIR') or str(app_dir)
    data_root = Path(base) / 'data'
    if data_root.exists():
        candidates = sorted(
            (p for p in data_root.glob('*/masters/BillgroupMaster.xls')
             if p.is_file()),
            key=lambda p: p.parent.parent.name, reverse=True,
        )
        if candidates:
            return candidates[0]
    return shared


def list_bill_groups() -> List[str]:
    """Return the de-duplicated, sorted list of bill group names from
    the master XLS. Empty list when the master is missing or the parse
    finds no usable column — caller should fall back to a free-text
    input or to ``DEFAULT_BILL_GROUP`` so the form stays functional."""
    p = _master_path()
    if not p.exists():
        return []
    try:
        # WS exports are .xls (legacy CFBF) — pandas/openpyxl can't read
        # those. xlrd 1.x can. Try pandas first (it knows when to delegate
        # to xlrd); on failure fall through to xlrd directly so a missing
        # xlrd version doesn't kill the whole client form.
        names = _read_via_pandas(p)
        if not names:
            names = _read_via_xlrd(p)
        return names
    except Exception as e:
        logger.warning(f'BillgroupMaster parse failed ({p}): {e}')
        return []


def _read_via_pandas(p: Path) -> List[str]:
    try:
        import pandas as pd
    except ImportError:
        return []
    try:
        df = pd.read_excel(p, dtype=str)
    except Exception as e:
        logger.debug(f'pandas read_excel failed: {e}')
        return []
    return _pick_name_column(df.fillna('').astype(str).values.tolist(),
                              [str(c) for c in df.columns])


def _read_via_xlrd(p: Path) -> List[str]:
    try:
        import xlrd
    except ImportError:
        return []
    try:
        wb = xlrd.open_workbook(str(p))
        sh = wb.sheet_by_index(0)
        if sh.nrows == 0:
            return []
        header = [str(sh.cell_value(0, c) or '') for c in range(sh.ncols)]
        rows = [[str(sh.cell_value(r, c) or '') for c in range(sh.ncols)]
                for r in range(1, sh.nrows)]
        return _pick_name_column(rows, header)
    except Exception as e:
        logger.debug(f'xlrd read failed: {e}')
        return []


def _pick_name_column(rows: List[List[str]], headers: List[str]) -> List[str]:
    """Pick the column most likely to hold bill-group display names.

    Strategy:
      1. If any header contains 'bill group', 'group name', 'name' (case-
         insensitive) — use that column.
      2. Otherwise score every column by the share of non-empty cells
         that look like a name (3+ chars, contains a letter, isn't
         purely numeric / a code).
      3. Drop empties + duplicates, sort case-insensitively.
    """
    if not rows:
        return []
    ncols = max(len(r) for r in rows) if rows else 0
    if ncols == 0:
        return []

    # 1) Header match — preferred. WS exports two adjacent columns:
    # 'BILL GROUP' (numeric ID like 1.0, 2.0) and 'BILL GROUP NAME'
    # (the human-readable label like 'No Charge - GSW'). Prefer headers
    # that contain 'name' / 'description' so we don't accidentally pick
    # the ID column. Fall back to the broader 'bill group' / 'billgroup'
    # match only when no name-bearing header is found.
    target_idx = -1
    name_keys = ('bill group name', 'group name', 'description', 'name')
    for i, h in enumerate(headers):
        h_low = (h or '').strip().lower()
        if any(k in h_low for k in name_keys):
            target_idx = i
            break
    if target_idx < 0:
        for i, h in enumerate(headers):
            h_low = (h or '').strip().lower()
            if any(k in h_low for k in ('bill group', 'billgroup')):
                target_idx = i
                break

    # 2) Score columns when no header match.
    if target_idx < 0:
        best_score = -1.0
        for c in range(ncols):
            vals = [(r[c] if c < len(r) else '').strip() for r in rows]
            non_empty = [v for v in vals if v]
            if not non_empty:
                continue
            name_like = sum(1 for v in non_empty
                            if len(v) >= 3
                            and any(ch.isalpha() for ch in v)
                            and not v.replace('.', '').replace('-', '').isdigit())
            score = name_like / len(non_empty)
            if score > best_score:
                best_score = score
                target_idx = c

    if target_idx < 0:
        return []

    seen = set()
    out: List[str] = []
    for r in rows:
        if target_idx >= len(r):
            continue
        v = (r[target_idx] or '').strip()
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    out.sort(key=lambda s: s.lower())
    return out


def list_bill_groups_or_default() -> List[str]:
    """Convenience wrapper for the client form: returns the master list
    when populated, otherwise a one-element list with the hardcoded
    default so the dropdown is never empty."""
    names = list_bill_groups()
    if names:
        # Make sure the historical default is present even when the master
        # arrives without it — the operator can still pick it.
        if DEFAULT_BILL_GROUP not in names:
            names = [DEFAULT_BILL_GROUP] + names
        return names
    return [DEFAULT_BILL_GROUP]
