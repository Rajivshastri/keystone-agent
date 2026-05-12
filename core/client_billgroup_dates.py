"""
core/client_billgroup_dates.py — read the per-client bill-group rate
timeline from the Client Bill Group Master XLS.

Each (CLIENTID, SCHEMENAME) can have multiple rows in the master, one
per change-of-bill-group event. Each row carries an EFFECTIVEDATE
(when that bill group came into force) and the FLATFEE that applies
from that date onward. Together those rows form a *rate timeline*:

    >>> load()[('100031', 'mystic wealth value portfolio')]
    [
      {'effective_from': '2025-12-04', 'flatfee_pct': 0.0,
       'billgroup_description': 'No Charge - GSW',     'billgroup': 4.0},
      {'effective_from': '2025-12-31', 'flatfee_pct': 1.6,
       'billgroup_description': 'Mystic Value 1-1.60 Monthly',
                                                       'billgroup': 8.0},
    ]

The compute path (fee_calculator.compute_fees_daily) walks per-day,
asking ``rate_on_day(timeline, day_iso)`` for the entry that applies
on each calendar day. The accrual then uses that day's FLATFEE as the
headline rate (rate change → fee accrues at the new rate from that
day onward).

FLATFEE units: percent per annum (e.g. 1.6 means 1.60% pa). Matches
the bill-group description ("Mystic Value 1-1.60 Monthly" carries
FLATFEE=1.6); used as-is for the headline_pct slot of the per-day
formula ``fee_day = aum × flatfee / 365 / 100``.

Public surface
==============

    load() -> dict[(client_id, scheme_name_lc), list[entry]]
        Empty dict when the file isn't present, no SCHEMENAME column,
        or no rows survive parsing. Caller treats missing keys as
        'no rate timeline → skip / surface as no_effective_date'.

    rate_on_day(timeline, day_iso) -> entry | None
        Pick the entry covering ``day_iso`` (latest entry whose
        effective_from <= day_iso). None when the timeline is empty
        or every entry is in the future.

    earliest_effective_for(client_id, scheme_name='') -> str
        Backward-compat helper for callers that just need 'when did
        this client first become billable'. Returns ISO date or ''.

    info() -> dict
        UI-facing status line: exists, size, modified, row_count
        (total rows kept across all timelines).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _master_path() -> Path:
    """Resolve ``masters/ClientBillgroupMaster.xls``. Same layout as
    BillgroupMaster — shared across the app, not date-bucketed
    (KEYSTONE_DATA_DIR is intentionally not consulted here)."""
    base = Path(__file__).parent.parent
    return base / 'masters' / 'ClientBillgroupMaster.xls'


def _parse_date(v: Any) -> str:
    """Coerce any date-shaped value to ISO ``YYYY-MM-DD``.

    Accepts:
      - ``datetime`` / ``date`` / pandas ``Timestamp`` (extracted directly).
      - Excel serial as int/float (1899-12-30 origin).
      - String forms: 'YYYY-MM-DD', 'DD/MM/YYYY', 'DD-MM-YYYY',
        'DD/MMM/YYYY', 'DD-MMM-YYYY', plus the same with a trailing
        ' HH:MM:SS' time component (which is what pandas renders
        Timestamp objects to via ``str()``).

    Returns '' on failure.
    """
    if v is None or v == '':
        return ''
    # Direct date-like objects (datetime, date, pandas.Timestamp).
    # Tested via attribute lookup so we don't need pandas imported.
    try:
        if hasattr(v, 'strftime') and not isinstance(v, str):
            return v.strftime('%Y-%m-%d')
    except Exception:
        pass
    if isinstance(v, (int, float)):
        try:
            from datetime import datetime as _dt, timedelta as _td
            n = float(v)
            return (_dt(1899, 12, 30) + _td(days=n)).strftime('%Y-%m-%d')
        except Exception:
            return ''
    s = str(v).strip()
    if not s:
        return ''
    # If a time component is appended (pandas Timestamp str repr is
    # 'YYYY-MM-DD HH:MM:SS'), strip it before format-matching so the
    # date-only formats below still hit.
    s_date_only = s.split(' ', 1)[0] if ' ' in s else s
    for candidate in (s, s_date_only):
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y',
                    '%d/%b/%Y', '%d-%b-%Y',
                    '%Y-%m-%d %H:%M:%S', '%d/%m/%Y %H:%M:%S'):
            try:
                return datetime.strptime(candidate, fmt).strftime('%Y-%m-%d')
            except ValueError:
                continue
    return ''


def _read_rows() -> Tuple[list, list]:
    """Return (headers, rows) from the master XLS. Tries pandas first
    (handles both legacy CFBF .xls via xlrd 1.x and newer xlsx via
    openpyxl); falls back to xlrd directly on import failure."""
    p = _master_path()
    if not p.exists():
        return [], []
    try:
        import pandas as pd
    except ImportError:
        pd = None
    if pd is not None:
        try:
            df = pd.read_excel(p, dtype=object)
            df = df.where(df.notna(), '')
            headers = [str(c) for c in df.columns]
            rows = [[v for v in row] for row in df.values.tolist()]
            return headers, rows
        except Exception as e:
            logger.debug(f'ClientBillgroupMaster: pandas read failed: {e}')
    try:
        import xlrd
        wb = xlrd.open_workbook(str(p))
        sh = wb.sheet_by_index(0)
        if sh.nrows == 0:
            return [], []
        headers = [str(sh.cell_value(0, c) or '') for c in range(sh.ncols)]
        rows = [[sh.cell_value(r, c) for c in range(sh.ncols)]
                for r in range(1, sh.nrows)]
        return headers, rows
    except Exception as e:
        logger.warning(f'ClientBillgroupMaster: read failed for {p}: {e}')
        return [], []


def _norm_id(v: Any) -> str:
    """Normalise an Excel cell value to an uppercase string suitable for
    key joins against the AUM CLIENTID column. Excel's .xls reader
    returns numeric-text columns as floats (CLIENTID 100002 →
    100002.0), so a naive ``str(v).strip()`` produces ``'100002.0'``
    which never joins against the AUM parser's ``str(int(cid))``
    output of ``'100002'``.

    Be careful: strip only the literal '.0' suffix, then fall back to
    ``int()`` for true floats. A naive ``rstrip('.0')`` would corrupt
    CLIENTIDs like ``1000200.0`` to ``'10002'`` (lost two zeros)."""
    if v is None:
        return ''
    if isinstance(v, float) and v.is_integer():
        return str(int(v)).upper()
    if isinstance(v, int):
        return str(v).upper()
    s = str(v).strip()
    if s.endswith('.0'):
        try:
            int(s[:-2])
            s = s[:-2]
        except ValueError:
            pass
    return s.upper()


def _norm_scheme(v: Any) -> str:
    """Lowercased + stripped scheme name suitable as a join key. Empty
    string when the cell is empty — caller's responsibility to decide
    whether to drop rows with no scheme."""
    if v is None:
        return ''
    return str(v).strip().lower()


def _find_col(headers: list, *needles: str) -> int:
    """Return the index of the first header whose lowercase form
    contains any of ``needles``. -1 when none match. Needles are
    matched against ``header.lower()`` with spaces and underscores
    stripped, so 'flat fee' matches FLATFEE, FLAT_FEE, etc."""
    for i, h in enumerate(headers):
        h_low = (h or '').strip().lower().replace(' ', '').replace('_', '')
        for n in needles:
            if n.replace(' ', '').replace('_', '') in h_low:
                return i
    return -1


def _to_float(v: Any) -> Optional[float]:
    """Numeric coercion with empty-string handling. None on failure
    so caller can distinguish 'blank' from '0.0' (FLATFEE = blank
    means 'no rate row' — current spec is the same as 0.0, but kept
    distinct in case the rule changes)."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def load() -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """Return the rate timeline for every (client_id, scheme_name).

    Key shape: ``(CLIENTID_upper, scheme_name_lower_stripped)``.

    Each value is a list of rate entries sorted by ``effective_from``
    (earliest first). Each entry:
      - ``effective_from``        — ISO YYYY-MM-DD when this rate began
      - ``flatfee_pct``           — annual headline %, e.g. 1.6 for 1.60%
      - ``billgroup``             — BILLGROUP id from CBG master
      - ``billgroup_description`` — operator-readable label
      - ``scheme_name``           — original-case scheme for display

    Rows missing CLIENTID or EFFECTIVEDATE are dropped silently.
    FLATFEE blank → flatfee_pct=0.0 (billable at zero, per operator
    spec). Multiple rows for the same (cid, scheme, date) keep the
    LAST occurrence — operator's most recent edit wins.
    """
    headers, rows = _read_rows()
    if not headers or not rows:
        return {}

    cid_idx    = _find_col(headers, 'clientid', 'clientcode')
    scheme_idx = _find_col(headers, 'schemename')
    eff_idx    = _find_col(headers, 'effective')
    flatfee_idx       = _find_col(headers, 'flatfee')
    billgroup_idx     = _find_col(headers, 'billgroup')
    billgroup_desc_idx = _find_col(headers, 'billgroupdescription')

    # FLATFEE matches FLATFEE_INCENTIVE too via substring; defensively
    # pick the column whose header is exactly 'flatfee' when both are
    # present (common case in the WS export — column 11 = FLATFEE,
    # column 12 = FLATFEE_INCENTIVE).
    for i, h in enumerate(headers):
        if (h or '').strip().lower() == 'flatfee':
            flatfee_idx = i
            break

    if cid_idx < 0 or eff_idx < 0:
        logger.warning(f'ClientBillgroupMaster: required columns missing — '
                       f'headers={headers!r}')
        return {}
    if scheme_idx < 0:
        # Legacy file without SCHEMENAME — bucket every row under the
        # empty-scheme key so older deploys don't break completely.
        # Compute path will fall back to (cid, '') lookup.
        logger.warning('ClientBillgroupMaster: no SCHEMENAME column — '
                       'all rows will land under (cid, "") key (legacy mode)')

    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        try:
            cid_raw    = r[cid_idx] if cid_idx < len(r) else ''
            scheme_raw = r[scheme_idx] if (scheme_idx >= 0 and scheme_idx < len(r)) else ''
            eff_raw    = r[eff_idx] if eff_idx < len(r) else ''
            flatfee_raw = (r[flatfee_idx]
                            if (flatfee_idx >= 0 and flatfee_idx < len(r))
                            else None)
            bg_raw      = (r[billgroup_idx]
                            if (billgroup_idx >= 0 and billgroup_idx < len(r))
                            else None)
            bg_desc_raw = (r[billgroup_desc_idx]
                            if (billgroup_desc_idx >= 0 and billgroup_desc_idx < len(r))
                            else '')
        except Exception:
            continue
        cid = _norm_id(cid_raw)
        scheme_lc = _norm_scheme(scheme_raw)
        scheme_orig = str(scheme_raw or '').strip()
        eff_iso = _parse_date(eff_raw)
        if not cid or not eff_iso:
            continue
        flatfee = _to_float(flatfee_raw)
        if flatfee is None:
            flatfee = 0.0
        key = (cid, scheme_lc)
        entry = {
            'effective_from':       eff_iso,
            'flatfee_pct':          flatfee,
            'billgroup':            _to_float(bg_raw),
            'billgroup_description': str(bg_desc_raw or '').strip(),
            'scheme_name':          scheme_orig,
        }
        out.setdefault(key, []).append(entry)

    # Sort each timeline by effective_from. Multiple entries for the
    # same date collapse to the last one parsed (operator's most
    # recent edit — XLS row order tends to be append-only).
    for key, entries in out.items():
        entries.sort(key=lambda e: e['effective_from'])
        deduped: List[Dict[str, Any]] = []
        for e in entries:
            if deduped and deduped[-1]['effective_from'] == e['effective_from']:
                deduped[-1] = e
            else:
                deduped.append(e)
        out[key] = deduped

    if not out:
        logger.info(f'ClientBillgroupMaster: parsed but produced no usable rows '
                    f'(headers={headers}, row_count={len(rows)})')
    else:
        total_entries = sum(len(v) for v in out.values())
        logger.info(f'ClientBillgroupMaster: loaded {total_entries} rate entries '
                    f'across {len(out)} (client, scheme) keys')
    return out


def rate_on_day(timeline: List[Dict[str, Any]], day_iso: str
                ) -> Optional[Dict[str, Any]]:
    """Pick the rate entry effective on ``day_iso`` (ISO YYYY-MM-DD).

    The covering entry is the one with the latest ``effective_from``
    that is <= ``day_iso``. Returns None when the timeline is empty
    or every entry is in the future relative to ``day_iso``."""
    if not timeline:
        return None
    chosen: Optional[Dict[str, Any]] = None
    for e in timeline:
        if e['effective_from'] <= day_iso:
            chosen = e
        else:
            break
    return chosen


def earliest_effective_for(client_id: str, scheme_name: str = '') -> str:
    """Earliest effective date across the timeline for a client. Used
    by callers that need 'first day this client/scheme was billable'
    (e.g. the breakup-period bootstrap that anchors the migrated
    fee_config's first period to the earliest CBG date).

    Lookup order:
      1. (client_id, scheme_name) — exact match.
      2. (client_id, '')          — legacy / no-scheme fallback.
      3. Any (client_id, *)       — earliest across all this client's
         schemes (best-effort when the caller doesn't know the scheme).
    """
    table = load()
    if not table:
        return ''
    cid = _norm_id(client_id)
    scheme_lc = _norm_scheme(scheme_name)
    direct = table.get((cid, scheme_lc)) or table.get((cid, ''))
    if direct:
        return direct[0]['effective_from']
    candidates = [v[0]['effective_from'] for k, v in table.items()
                  if k[0] == cid and v]
    if not candidates:
        return ''
    return min(candidates)


def effective_date_for(client_id: str, account_code: str = '') -> str:
    """Backward-compat shim. ``account_code`` is now ignored — the
    master keys by scheme, not account. Kept so legacy callers
    (welcome-email cc lookup etc.) don't break."""
    return earliest_effective_for(client_id, '')


def info() -> Dict[str, Any]:
    """Status line for the Settings UI — exists / size / modified /
    row_count. row_count is the total number of rate entries kept
    (across all (cid, scheme) timelines), so the operator sees the
    actual count of effective-date events parsed, not just unique
    clients. Never raises; returns ``exists: False`` on any read
    error so the form keeps rendering."""
    p = _master_path()
    if not p.exists():
        return {'exists': False}
    try:
        st = p.stat()
        timeline = load()
        total_entries = sum(len(v) for v in timeline.values())
        return {
            'exists':    True,
            'size':      st.st_size,
            'modified':  datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                            .strftime('%Y-%m-%dT%H:%M:%SZ'),
            'row_count': total_entries,
            'client_scheme_keys': len(timeline),
        }
    except Exception as e:
        logger.warning(f'ClientBillgroupMaster.info failed: {e}')
        return {'exists': False}
