"""
Fee calculator (daily-accrual)
==============================

Per-day fee accrual against WS Clientwise Daily AUM. Replaces the
previous Average-AUM-based flow whose period started from the first
capital inflow date — wrong for GSW's billing rule (we only bill from
the date AUM crosses the ₹50L threshold, recorded in the Client Bill
Group Master as the effective_date).

Math (all rates are per annum %):
  for each day in [eff_start, period_to]:
    fee_day = AUM_on_day × headline_pct / 365 / 100
  total_period_fee = sum(fee_day)

  eff_start = max(period_from, effective_date, first_aum_observation_date)

GSW share is always credited to a synthetic "GSW" earner — it has no
operator-set name. The other three (FM / distributor / residual) each
have a user_name on the fee_config; if blank, they roll up under
"(unassigned)" so the gap is visible rather than silently lost.

Edge cases:
  - Account with no effective_date in master → SKIP, surface as
    'no_effective_date' in unmatched (operator must set it on WS).
  - AUM data missing on a day, last_seen ≤ 2 business days back →
    carry forward last_aum.
  - AUM data missing for >2 consecutive business days →
    truncate fee accrual at last_seen, flag as 'stale_data'.

Business days are computed against config/calendar.json (NSE trading
calendar — handles weekends + holidays correctly).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Daily AUM file parsing ──────────────────────────────────────────────── #

@dataclass
class DailyAumRow:
    client_id:    str
    client_name:  str
    account_code: str
    value_date:   date    # parsed from 'DD/MM/YYYY' to a real date
    aum:          float


def parse_daily_aum_xls(path: Path) -> List[DailyAumRow]:
    """Parse the WS Clientwise Daily AUM XLS (Z0_Daily_AUM.xls or
    Daily_AUM_{from}_{to}.xls).

    Header row 1: CLIENTID, CLIENTNAME, ACCOUNTCODE, VALUEDATE, AUM,
                  RM NAME, ADVISOR NAME, BRANCH NAME, GROUP ID, GROUP NAME,
                  OWNER ID, OWNER NAME, WEALTH ADVISOR NAME, ARN NAME, SCHEME NAME
    Each row = one day's observation for one (CLIENTID, ACCOUNTCODE).
    Empty or unparseable rows are skipped silently.
    """
    import xlrd
    wb = xlrd.open_workbook(str(path))
    sh = wb.sheet_by_index(0)
    if sh.nrows < 2:
        return []
    header = [str(sh.cell_value(0, c)).strip().upper() for c in range(sh.ncols)]

    def col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            raise RuntimeError(f"Daily AUM XLS missing expected column: {name} "
                               f"(found: {header})")

    c_id, c_name, c_acct = col('CLIENTID'), col('CLIENTNAME'), col('ACCOUNTCODE')
    c_date, c_aum        = col('VALUEDATE'), col('AUM')

    rows: List[DailyAumRow] = []
    for r in range(1, sh.nrows):
        acct = str(sh.cell_value(r, c_acct) or '').strip()
        if not acct:
            continue
        try:
            aum = float(sh.cell_value(r, c_aum) or 0)
        except (TypeError, ValueError):
            aum = 0.0
        # CLIENTID often arrives as float (100002.0) — normalise.
        cid_raw = sh.cell_value(r, c_id)
        cid = (str(int(cid_raw)) if isinstance(cid_raw, (int, float))
               else str(cid_raw or '').strip())
        # VALUEDATE is dd/MM/yyyy in the WS export.
        date_raw = str(sh.cell_value(r, c_date) or '').strip()
        try:
            vdate = datetime.strptime(date_raw, '%d/%m/%Y').date()
        except (ValueError, AttributeError):
            continue   # skip rows with unparseable dates
        rows.append(DailyAumRow(
            client_id=cid,
            client_name=str(sh.cell_value(r, c_name) or '').strip(),
            account_code=acct,
            value_date=vdate,
            aum=aum,
        ))
    return rows


def find_daily_aum_file(data_root: Path,
                         from_str: str, to_str: str) -> Optional[Path]:
    """Locate Daily_AUM_{YYYYMMDD-from}_{YYYYMMDD-to}.xls under
    data/{to}/masters/. Returns None if not present (caller should
    download).
    """
    fd = datetime.strptime(from_str, '%Y-%m-%d')
    td = datetime.strptime(to_str,   '%Y-%m-%d')
    p = data_root / td.strftime('%Y-%m-%d') / 'masters' \
        / f"Daily_AUM_{fd:%Y%m%d}_{td:%Y%m%d}.xls"
    return p if p.exists() else None


# ── Fee config lookup (by account_code) ───────────────────────────────────── #

def normalize_fee_config(fc: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce a stored fee_config into the new ``breakup_periods``
    shape regardless of whether it was written under the legacy flat
    schema or the new period-aware schema.

    Legacy shape:
        {headline_fee_pct, share_gsw_pct, share_fm_pct, ...,
         fm_user_name, distributor_user_name, residual_user_name}

    New shape:
        {breakup_periods: [
            {effective_from, share_gsw_pct, share_fm_pct,
             share_distributor_pct, share_residual_pct,
             fm_user_name, distributor_user_name, residual_user_name},
            ...
        ]}

    Headline (FLATFEE) is sourced from the Client Bill Group Master
    rather than fee_config — operator no longer types it in. So
    normalization drops headline_fee_pct entirely.

    Legacy flat configs migrate to a single-entry breakup_periods list
    with ``effective_from = ''`` (sentinel resolved at compute time
    against the client's earliest CBG date)."""
    if not isinstance(fc, dict):
        return {'breakup_periods': []}
    bp = fc.get('breakup_periods')
    if isinstance(bp, list):
        # Already in new shape — keep entries that look valid.
        cleaned: List[Dict[str, Any]] = []
        for entry in bp:
            if isinstance(entry, dict):
                cleaned.append(entry)
        return {'breakup_periods': cleaned}
    # Legacy flat shape.
    has_legacy_share = any(
        k in fc for k in ('share_gsw_pct', 'share_fm_pct',
                          'share_distributor_pct', 'share_residual_pct',
                          'fm_user_name', 'distributor_user_name',
                          'residual_user_name')
    )
    if not has_legacy_share:
        return {'breakup_periods': []}
    return {'breakup_periods': [{
        'effective_from':       '',   # resolved at compute time
        'share_gsw_pct':         fc.get('share_gsw_pct'),
        'share_fm_pct':          fc.get('share_fm_pct'),
        'share_distributor_pct': fc.get('share_distributor_pct'),
        'share_residual_pct':    fc.get('share_residual_pct'),
        'fm_user_name':          fc.get('fm_user_name'),
        'distributor_user_name': fc.get('distributor_user_name'),
        'residual_user_name':    fc.get('residual_user_name'),
    }]}


def breakup_on_day(breakup_periods: List[Dict[str, Any]],
                    day_iso: str) -> Optional[Dict[str, Any]]:
    """Pick the breakup entry effective on ``day_iso``.

    Same selection rule as core.client_billgroup_dates.rate_on_day:
    latest entry whose effective_from <= day_iso. None when the list
    is empty or every entry is in the future."""
    if not breakup_periods:
        return None
    chosen: Optional[Dict[str, Any]] = None
    for e in sorted(breakup_periods,
                    key=lambda x: (x.get('effective_from') or '')):
        ef = (e.get('effective_from') or '').strip()
        # Sentinel '' = effective from earliest covered day. Treated
        # as the open lower bound — covers any day until a later entry
        # supersedes it.
        if ef == '' or ef <= day_iso:
            chosen = e
        else:
            break
    return chosen


def load_fee_configs() -> Dict[str, Dict[str, Any]]:
    """Index every (ws_account_code → fee_config) across all clients.

    Picks up both:
      • clients.fee_config when the row's primary ws_account_code matches.
      • Each entry inside additional_pools[].fee_config keyed by the
        entry's ws_account_code.

    Plus carries scheme_name onto each entry so the line-item rendering
    can show the operator what they're looking at without an extra
    join. Configs are normalized to the period-aware shape on the way
    out — see :func:`normalize_fee_config`.
    """
    from .client_onboarding import _db_path, init_db
    init_db()
    out: Dict[str, Dict[str, Any]] = {}
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT id, tax_pan, first_name, middle_name, last_name, '
            'ws_account_code, scheme_name, fee_config, additional_pools FROM clients'
        ).fetchall()
    for r in rows:
        # Joined name from CML fields — used as client_name fallback when
        # the AUM file doesn't contain a row for this account (config_no_aum
        # branch in compute_fees). Without this, freshly-onboarded accounts
        # show up nameless in the per-account view.
        full = ' '.join(
            p for p in (r['first_name'], r['middle_name'], r['last_name']) if p
        ).strip()
        # Primary pool
        acct = (r['ws_account_code'] or '').strip()
        if acct and r['fee_config']:
            try:
                fc = json.loads(r['fee_config'])
                if isinstance(fc, dict):
                    out[acct] = {**normalize_fee_config(fc),
                                 '_scheme_name': r['scheme_name'] or '',
                                 '_client_name': full,
                                 '_client_row_id': r['id']}
            except Exception:
                pass
        # Additional pools
        try:
            extras = json.loads(r['additional_pools'] or '[]') or []
        except Exception:
            extras = []
        for p in extras:
            if not isinstance(p, dict):
                continue
            ea = (p.get('ws_account_code') or '').strip()
            ef = p.get('fee_config')
            if ea and isinstance(ef, dict):
                out[ea] = {**normalize_fee_config(ef),
                           '_scheme_name': p.get('scheme_name') or '',
                           '_client_name': full,
                           '_client_row_id': r['id']}
        # Clients with NO fee_config saved at all should still surface
        # in the Fee module so they can have their breakup entered.
        # Add an empty-breakup placeholder per pool so compute emits
        # them under no_breakup (amber).
        if acct and acct not in out:
            out[acct] = {'breakup_periods': [],
                         '_scheme_name': r['scheme_name'] or '',
                         '_client_name': full,
                         '_client_row_id': r['id']}
    return out


# ── Compute + aggregate ───────────────────────────────────────────────────── #

@dataclass
class FeeLineItem:
    account_code:    str
    client_name:     str
    scheme_name:     str
    period_days:     int
    period_from:     str   # ISO YYYY-MM-DD — start of THIS sub-segment
    period_to:       str   # ISO YYYY-MM-DD — end of THIS sub-segment
    avg_aum:         float
    sum_aum:         float   # = avg_aum × period_days; pre-computed for time-weighted rollups
    headline_pct:    float
    investor_fee:    float
    gsw_pct:         float
    gsw_inr:         float
    fm_pct:          float
    fm_inr:          float
    fm_name:         str
    distributor_pct: float
    distributor_inr: float
    distributor_name:str
    residual_pct:    float
    residual_inr:    float
    residual_name:   str
    billgroup_description: str = ''   # operator-readable label from CBG master


GSW_NAME = 'GoldStandard Wealth'
UNASSIGNED = '(unassigned)'


def _period_days(from_str: str, to_str: str) -> int:
    """Inclusive day count for an ISO YYYY-MM-DD period."""
    fd = datetime.strptime(from_str, '%Y-%m-%d').date()
    td = datetime.strptime(to_str,   '%Y-%m-%d').date()
    return (td - fd).days + 1


# ── Business-day calendar (NSE) ─────────────────────────────────────────── #
#
# config/calendar.json layout (typically empty unless operator has populated):
#   { "holidays": ["2026-01-26", ...],
#     "working_weekends": ["2026-03-15", ...],   # Sat/Sun that ARE trading days
#     "no_trade_days": [...] }                    # treated like holidays
# Used by compute_fees_daily to enforce the "stale data: > 2 business days
# missing" rule.

def _load_business_calendar() -> tuple:
    """Return (holidays, working_weekends) as sets of ISO date strings.
    Empty sets when config/calendar.json is missing or unreadable —
    business-day check then degenerates to Mon-Fri only."""
    cal_path = Path(__file__).parent.parent / 'config' / 'calendar.json'
    holidays: set = set()
    working_weekends: set = set()
    if not cal_path.exists():
        return holidays, working_weekends
    try:
        cal = json.loads(cal_path.read_text(encoding='utf-8'))
    except Exception as e:
        logger.warning(f'calendar.json unreadable, defaulting to Mon-Fri: {e}')
        return holidays, working_weekends
    holidays.update(cal.get('holidays')         or [])
    holidays.update(cal.get('no_trade_days')    or [])
    working_weekends.update(cal.get('working_weekends') or [])
    return holidays, working_weekends


def _is_business_day(d: date, holidays: set, working_weekends: set) -> bool:
    iso = d.isoformat()
    if iso in working_weekends:
        return True
    if iso in holidays:
        return False
    return d.weekday() < 5   # Mon=0 ... Fri=4


def _business_days_between(start: date, end: date,
                            holidays: set, working_weekends: set) -> int:
    """Count business days strictly AFTER ``start`` and through ``end``
    inclusive. Used to detect "more than 2 business days missing"
    truncation in compute_fees_daily."""
    from datetime import timedelta as _td
    if end <= start:
        return 0
    cnt = 0
    cur = start + _td(days=1)
    while cur <= end:
        if _is_business_day(cur, holidays, working_weekends):
            cnt += 1
        cur += _td(days=1)
    return cnt


def _zero_line_item(account_code: str, sample: 'DailyAumRow',
                     fc: Dict[str, Any], eff_start: 'date',
                     period_to_dt: 'date') -> 'FeeLineItem':
    """Zero-AUM placeholder for accounts whose effective_date is
    AFTER the operator's period end. Carries the LATEST breakup
    rates so the row's earner cells aren't blank."""
    bp = (fc.get('breakup_periods') or [])
    latest = bp[-1] if bp else {}
    return FeeLineItem(
        account_code     = account_code,
        client_name      = (sample.client_name or '').strip()
                            or (fc.get('_client_name') or '').strip(),
        scheme_name      = fc.get('_scheme_name') or '',
        period_days      = 0,
        period_from      = eff_start.isoformat(),
        period_to        = period_to_dt.isoformat(),
        avg_aum          = 0.0,
        sum_aum          = 0.0,
        headline_pct     = 0.0,
        investor_fee     = 0.0,
        gsw_pct          = float(latest.get('share_gsw_pct')         or 0),
        gsw_inr          = 0.0,
        fm_pct           = float(latest.get('share_fm_pct')          or 0),
        fm_inr           = 0.0,
        fm_name          = (latest.get('fm_user_name')               or '').strip(),
        distributor_pct  = float(latest.get('share_distributor_pct') or 0),
        distributor_inr  = 0.0,
        distributor_name = (latest.get('distributor_user_name')      or '').strip(),
        residual_pct     = float(latest.get('share_residual_pct')    or 0),
        residual_inr     = 0.0,
        residual_name    = (latest.get('residual_user_name')         or '').strip(),
    )


def compute_fees_daily(daily_rows: List[DailyAumRow],
                        fee_configs: Dict[str, Dict[str, Any]],
                        from_str: str, to_str: str,
                        entity_payments: Optional[Dict[str, Optional[float]]] = None
                        ) -> Dict[str, Any]:
    """Compute per-day-accrued fees for a period, supporting time-
    varying headline rates (sourced from Client Bill Group Master) and
    operator-entered breakup periods.

    Algorithm per (account_code):
      1. Resolve the rate timeline from CBG master keyed by
         (CLIENTID, scheme_name). No timeline → skip + surface as
         'no_effective_date'.
      2. eff_start = max(period_from, earliest_effective_date_in_timeline,
                          first_AUM_observation_for_this_account)
      3. Build segment boundaries: union of all rate-timeline
         effective_from dates AND the operator's breakup_periods
         effective_from dates that fall inside [eff_start, period_to].
      4. Walk each calendar day in [eff_start, period_to]:
         - Apply carry-forward / stale-data rules continuously across
           segments (the same 2-business-day cap as before).
         - Bucket the day's AUM into the segment that covers it.
      5. For each non-empty segment, look up:
         - headline = rate_on_day(timeline, segment_start).flatfee_pct
         - breakup  = breakup_on_day(breakup_periods, segment_start)
         No covering breakup → record under ``no_breakup`` and SKIP
         the segment (no fee accrued, but the gap is visible to the
         operator on the Fees screen for amber-flagging).
      6. Emit one FeeLineItem per non-empty segment. Sub-period fields
         (period_from / period_to / period_days) describe the
         sub-segment, not the full account window.

    Output shape matches the prior compute_fees so the existing UI
    rendering keeps working — multiple line items per account just
    surface as additional rows in by_account. ``unmatched`` gains
    'no_effective_date', 'stale_data', and 'no_breakup'.
    """
    from datetime import timedelta as _td

    days = _period_days(from_str, to_str)
    period_from_dt = datetime.strptime(from_str, '%Y-%m-%d').date()
    period_to_dt   = datetime.strptime(to_str,   '%Y-%m-%d').date()

    # Rate timelines keyed by (CLIENTID, scheme_name_lc).
    try:
        from core.client_billgroup_dates import (load as _load_eff_dates,
                                                  rate_on_day as _rate_on_day,
                                                  _norm_id as _norm_eff_id,
                                                  _norm_scheme as _norm_scheme)
        rate_timelines = _load_eff_dates()
    except Exception as _e:
        logger.debug(f'ClientBillgroup timeline load skipped: {_e}')
        rate_timelines = {}
        _rate_on_day = lambda tl, d: None
        _norm_eff_id = lambda x: (str(x or '').strip().upper())
        _norm_scheme = lambda x: (str(x or '').strip().lower())

    holidays, working_weekends = _load_business_calendar()

    # Build per-account day index from daily_rows.
    by_account: Dict[str, Dict[date, DailyAumRow]] = defaultdict(dict)
    for r in daily_rows:
        by_account[r.account_code][r.value_date] = r

    line_items: List[FeeLineItem] = []
    aum_keys: set = set()
    aum_no_config: List[Dict[str, Any]] = []
    no_effective_date: List[Dict[str, Any]] = []
    stale_data: List[Dict[str, Any]] = []
    no_breakup: List[Dict[str, Any]] = []

    _eff_loaded = sum(len(v) for v in rate_timelines.values())
    _eff_hit    = 0
    _eff_miss   = 0

    for account_code, day_index in by_account.items():
        aum_keys.add(account_code)
        fc = fee_configs.get(account_code)
        if not fc:
            sample = next(iter(day_index.values()))
            aum_no_config.append({'account_code': account_code,
                                  'client_name':  sample.client_name,
                                  'client_id':    sample.client_id})
            continue

        sample = next(iter(day_index.values()))
        cid_up    = _norm_eff_id(sample.client_id)
        scheme_lc = _norm_scheme(fc.get('_scheme_name') or '')
        rate_tl = (rate_timelines.get((cid_up, scheme_lc))
                    or rate_timelines.get((cid_up, '')) or [])
        if not rate_tl:
            _eff_miss += 1
            no_effective_date.append({
                'account_code': account_code,
                'client_id':    sample.client_id,
                'client_name':  sample.client_name,
                'scheme_name':  fc.get('_scheme_name') or '',
            })
            continue
        _eff_hit += 1

        try:
            earliest_eff = datetime.strptime(rate_tl[0]['effective_from'],
                                              '%Y-%m-%d').date()
        except (ValueError, KeyError, TypeError):
            no_effective_date.append({
                'account_code': account_code,
                'client_id':    sample.client_id,
                'client_name':  sample.client_name,
                'scheme_name':  fc.get('_scheme_name') or '',
                'note':         f'unparseable timeline entry: {rate_tl[0]!r}',
            })
            continue

        first_aum_date = min(day_index.keys())
        eff_start = max(period_from_dt, earliest_eff, first_aum_date)

        # Account becomes eligible AFTER the operator's period ends —
        # zero fee, but still surface so it shows up in the by_account
        # drill-down.
        if eff_start > period_to_dt:
            line_items.append(_zero_line_item(
                account_code, sample, fc, eff_start, period_to_dt))
            continue

        # Resolve breakup_periods (after migration / normalization at
        # load_fee_configs, this is always a list — possibly empty).
        breakup_periods = fc.get('breakup_periods') or []

        # Build segment boundaries: every rate / breakup change-date
        # that falls strictly inside (eff_start, period_to]. eff_start
        # is the first segment's start; period_to + 1 day is the
        # closing sentinel.
        boundaries: set = {eff_start, period_to_dt + _td(days=1)}
        for e in rate_tl:
            try:
                d = datetime.strptime(e['effective_from'], '%Y-%m-%d').date()
            except (ValueError, KeyError, TypeError):
                continue
            if eff_start < d <= period_to_dt:
                boundaries.add(d)
        for bp in breakup_periods:
            ef = (bp.get('effective_from') or '').strip()
            if not ef:
                continue
            try:
                d = datetime.strptime(ef, '%Y-%m-%d').date()
            except ValueError:
                continue
            if eff_start < d <= period_to_dt:
                boundaries.add(d)
        sorted_bounds = sorted(boundaries)

        # Pre-build segment skeletons (one entry per (start, end)).
        # Carry-forward state is global so we walk top-to-bottom and
        # bucket each day's AUM into the segment that covers it.
        segments: List[Dict[str, Any]] = []
        for i in range(len(sorted_bounds) - 1):
            s_start = sorted_bounds[i]
            s_end   = sorted_bounds[i + 1] - _td(days=1)
            if s_end < s_start:
                continue
            segments.append({
                'start':       s_start,
                'end':         s_end,
                'sum_aum':     0.0,
                'days_billed': 0,
                'last_seen':   None,
            })

        last_aum: Optional[float] = None
        last_seen: Optional[date] = None
        truncated = False
        for seg in segments:
            if truncated:
                break
            cur = seg['start']
            while cur <= seg['end']:
                if cur in day_index:
                    last_aum = day_index[cur].aum
                    last_seen = cur
                elif last_aum is not None and last_seen is not None:
                    gap = _business_days_between(last_seen, cur,
                                                  holidays, working_weekends)
                    if gap > 2:
                        truncated = True
                        break
                else:
                    cur += _td(days=1)
                    continue
                seg['sum_aum']     += last_aum
                seg['days_billed'] += 1
                seg['last_seen']    = last_seen
                cur += _td(days=1)

        if truncated:
            stale_data.append({
                'account_code':  account_code,
                'client_id':     sample.client_id,
                'client_name':   sample.client_name,
                'last_observed': last_seen.isoformat() if last_seen else None,
                'period_to':     period_to_dt.isoformat(),
            })

        # Emit one FeeLineItem per non-empty segment with a covering
        # breakup. Segments whose breakup is missing are recorded as
        # no_breakup gaps (amber on the UI) and not billed.
        client_display = (sample.client_name or '').strip() \
                         or (fc.get('_client_name') or '').strip()
        scheme_display = fc.get('_scheme_name') or ''
        for seg in segments:
            if seg['days_billed'] == 0:
                continue
            seg_start_iso = seg['start'].isoformat()
            rate_entry = _rate_on_day(rate_tl, seg_start_iso)
            if rate_entry is None:
                # Day before any rate timeline entry — defensive guard.
                continue
            headline = float(rate_entry.get('flatfee_pct') or 0)
            br = breakup_on_day(breakup_periods, seg_start_iso)
            seg_end_actual = (seg['last_seen']
                               if (truncated and seg['last_seen']
                                   and seg['last_seen'] <= seg['end'])
                               else seg['end'])
            if br is None:
                no_breakup.append({
                    'account_code':  account_code,
                    'client_id':     sample.client_id,
                    'client_name':   client_display,
                    'scheme_name':   scheme_display,
                    'seg_from':      seg_start_iso,
                    'seg_to':        seg_end_actual.isoformat(),
                    'headline_pct':  headline,
                    'billgroup_description': rate_entry.get(
                                                'billgroup_description') or '',
                })
                continue
            sum_aum = seg['sum_aum']
            days_billed = seg['days_billed']
            avg_aum = sum_aum / days_billed
            g  = float(br.get('share_gsw_pct')         or 0)
            f  = float(br.get('share_fm_pct')          or 0)
            d  = float(br.get('share_distributor_pct') or 0)
            rs = float(br.get('share_residual_pct')    or 0)
            accrual_factor = sum_aum / 365.0 / 100.0
            line_items.append(FeeLineItem(
                account_code     = account_code,
                client_name      = client_display,
                scheme_name      = scheme_display,
                period_days      = days_billed,
                period_from      = seg_start_iso,
                period_to        = seg_end_actual.isoformat(),
                avg_aum          = avg_aum,
                sum_aum          = sum_aum,
                headline_pct     = headline,
                investor_fee     = headline * accrual_factor,
                gsw_pct          = g,
                gsw_inr          = g * accrual_factor,
                fm_pct           = f,
                fm_inr           = f * accrual_factor,
                fm_name          = (br.get('fm_user_name')          or '').strip(),
                distributor_pct  = d,
                distributor_inr  = d * accrual_factor,
                distributor_name = (br.get('distributor_user_name') or '').strip(),
                residual_pct     = rs,
                residual_inr     = rs * accrual_factor,
                residual_name    = (br.get('residual_user_name')    or '').strip(),
                billgroup_description = rate_entry.get(
                                            'billgroup_description') or '',
            ))

    config_no_aum = [
        {'account_code': k, 'scheme_name': v.get('_scheme_name') or ''}
        for k, v in fee_configs.items() if k not in aum_keys
    ]

    # Surface config_no_aum as zero-AUM line items so the operator
    # sees them in the per-account breakdown — even with no AUM yet,
    # they need a breakup configured. Use the LATEST breakup entry
    # (or no shares at all if breakup_periods is empty) and zero
    # headline since no AUM means no rate context.
    for k, v in fee_configs.items():
        if k in aum_keys:
            continue
        bp = (v.get('breakup_periods') or [])
        latest = bp[-1] if bp else {}
        g  = float(latest.get('share_gsw_pct')         or 0)
        f  = float(latest.get('share_fm_pct')          or 0)
        d  = float(latest.get('share_distributor_pct') or 0)
        rs = float(latest.get('share_residual_pct')    or 0)
        line_items.append(FeeLineItem(
            account_code     = k,
            client_name      = (v.get('_client_name') or '').strip(),
            scheme_name      = v.get('_scheme_name') or '',
            period_days      = days,
            period_from      = from_str,
            period_to        = to_str,
            avg_aum          = 0.0,
            sum_aum          = 0.0,
            headline_pct     = 0.0,
            investor_fee     = 0.0,
            gsw_pct          = g,
            gsw_inr          = 0.0,
            fm_pct           = f,
            fm_inr           = 0.0,
            fm_name          = (latest.get('fm_user_name')          or '').strip(),
            distributor_pct  = d,
            distributor_inr  = 0.0,
            distributor_name = (latest.get('distributor_user_name') or '').strip(),
            residual_pct     = rs,
            residual_inr     = 0.0,
            residual_name    = (latest.get('residual_user_name')    or '').strip(),
        ))

    by_role = {
        'gsw':         sum(li.gsw_inr         for li in line_items),
        'fm':          sum(li.fm_inr          for li in line_items),
        'distributor': sum(li.distributor_inr for li in line_items),
        'residual':    sum(li.residual_inr    for li in line_items),
    }

    # earner[(name, role)] = [total_inr, account_count, total_aum, sum_aum_x_pct]
    #
    # The 4th slot — Σ(aum_i × pct_i) — drives the days-independent
    # weighted-rate calc. weighted_rate = Σ(aum × pct) / Σ(aum) gives
    # the AUM-weighted average annual rate without any reference to
    # the period length, which is correct when AUM rows have varying
    # spans (mid-period account opens / closes).
    #
    # Counting rule (named entities): the account counts toward an
    # entity whenever the entity is NAMED in that role on the pool,
    # regardless of the share rate or whether AUM exists for the
    # period. Rationale: an entity can be assigned to an account at
    # 0% (placeholder, mid-onboarding, paused) or have AUM yet to
    # arrive — operator still wants to see "Nirman is the FM on
    # 12 accounts" not "Nirman earns from 10 accounts".
    #
    # For GSW (no operator-set name): every line item counts since
    # the firm owns every account.
    #
    # For UNASSIGNED: only count when there's a non-zero share with
    # no name attached — a real accounting flag.
    #
    # Denominator rule for weighted_rate: only AUM with a non-zero
    # share for THIS entity contributes to the denominator. A pool
    # where the entity earns 0% must not dilute their headline rate
    # ("Nirman runs ₹50cr at 1% and ₹50cr at 0%, average rate 1%
    # not 0.5%"). The accounts count is unaffected — that still
    # tracks "is this entity named on this pool".
    # earner[(name, role)] = [total_inr, accounts_set, total_sum_aum, sum_aum_x_pct]
    #
    # Slot 1 is now a SET of distinct account_codes (was a counter)
    # since one account can produce multiple line items (one per
    # segment); without dedup the count would inflate. Final accounts
    # number is len(set).
    #
    # Slot 2 + 3 use sum_aum (= avg_aum × period_days) instead of
    # avg_aum so the weighted-rate calc is time-weighted: an account
    # at 0.5%× for 100 days averaged with the same account at 1.5%× for
    # 50 days correctly returns ~0.83% (= (100×0.5 + 50×1.5) / 150),
    # not 1.0% (the unweighted mean of 0.5 and 1.5).
    #
    # Counting rule (named entities): unchanged from prior version —
    # the account counts toward an entity whenever the entity is
    # NAMED in that role on ANY of its segments, regardless of share.
    earner: Dict[Tuple[str, str], List[Any]] = defaultdict(
        lambda: [0.0, set(), 0.0, 0.0])
    for li in line_items:
        ac = li.account_code
        # GSW — every account counts; aum-weighting only when GSW share > 0.
        k = (GSW_NAME, 'gsw')
        earner[k][0] += li.gsw_inr
        earner[k][1].add(ac)
        if li.gsw_pct > 0:
            earner[k][2] += li.sum_aum
            earner[k][3] += li.sum_aum * li.gsw_pct
        # FM
        fm_name = (li.fm_name or '').strip()
        if fm_name:
            k = (fm_name, 'fm')
            earner[k][0] += li.fm_inr
            earner[k][1].add(ac)
            if li.fm_pct > 0:
                earner[k][2] += li.sum_aum
                earner[k][3] += li.sum_aum * li.fm_pct
        elif li.fm_pct != 0 or li.fm_inr != 0:
            k = (UNASSIGNED, 'fm')
            earner[k][0] += li.fm_inr
            earner[k][1].add(ac)
            if li.fm_pct > 0:
                earner[k][2] += li.sum_aum
                earner[k][3] += li.sum_aum * li.fm_pct
        # Distributor
        dist_name = (li.distributor_name or '').strip()
        if dist_name:
            k = (dist_name, 'distributor')
            earner[k][0] += li.distributor_inr
            earner[k][1].add(ac)
            if li.distributor_pct > 0:
                earner[k][2] += li.sum_aum
                earner[k][3] += li.sum_aum * li.distributor_pct
        elif li.distributor_pct != 0 or li.distributor_inr != 0:
            k = (UNASSIGNED, 'distributor')
            earner[k][0] += li.distributor_inr
            earner[k][1].add(ac)
            if li.distributor_pct > 0:
                earner[k][2] += li.sum_aum
                earner[k][3] += li.sum_aum * li.distributor_pct
        # Residual
        res_name = (li.residual_name or '').strip()
        if res_name:
            k = (res_name, 'residual')
            earner[k][0] += li.residual_inr
            earner[k][1].add(ac)
            if li.residual_pct > 0:
                earner[k][2] += li.sum_aum
                earner[k][3] += li.sum_aum * li.residual_pct
        elif li.residual_pct != 0 or li.residual_inr != 0:
            k = (UNASSIGNED, 'residual')
            earner[k][0] += li.residual_inr
            earner[k][1].add(ac)
            if li.residual_pct > 0:
                earner[k][2] += li.sum_aum
                earner[k][3] += li.sum_aum * li.residual_pct

    def _wrate(aum_x_pct: float, total_aum: float) -> Optional[float]:
        # AUM-weighted average per-annum rate. Days-independent —
        # accounts in the AUM file may have varying spans (mid-period
        # opens/closes) and each contributes (aum × pct) to the
        # numerator, aum to the denominator. Result is in percent
        # already (no further conversion needed).
        if not total_aum:
            return None
        return aum_x_pct / total_aum

    # Fixed payments per entity. Convention: monthly_fixed × eff_days
    # / 30 (one month = 30 days). eff_days is the count of days from
    # max(period_from, entity.start_date) through period_to inclusive
    # — so an entity that joined mid-period only earns the prorated
    # portion. Negative eff_days (start_date is after period_to)
    # → 0 payment for the period.
    #
    # Accepts both legacy {name: float} and new
    # {name: {monthly_fixed, start_date}} shapes for graceful migration
    # via load_payments() but the in-memory map here is whatever the
    # caller passed. compute_fees's caller normalises to the new shape.
    payments = entity_payments or {}
    period_from_dt = datetime.strptime(from_str, '%Y-%m-%d').date()
    period_to_dt   = datetime.strptime(to_str,   '%Y-%m-%d').date()
    def _fixed_for_entity(name: str) -> Optional[float]:
        entry = payments.get(name)
        if entry is None:
            return None
        if isinstance(entry, dict):
            monthly = entry.get('monthly_fixed')
            sd_raw  = entry.get('start_date')
        else:
            monthly = entry
            sd_raw  = None
        if monthly in (None, ''):
            return None
        try:
            monthly_f = float(monthly)
        except (TypeError, ValueError):
            return None
        eff_from = period_from_dt
        if isinstance(sd_raw, str) and sd_raw:
            try:
                sd = datetime.strptime(sd_raw, '%Y-%m-%d').date()
                if sd > eff_from:
                    eff_from = sd
            except ValueError:
                pass
        if eff_from > period_to_dt:
            return 0.0
        eff_days = (period_to_dt - eff_from).days + 1
        return monthly_f * eff_days / 30.0

    # Build by_earner, attaching fixed/net at the entity level. For
    # entities with multiple roles, fixed_inr applies to the entity
    # overall, not per role — the role rows leave fixed/net null and
    # the parent row in the UI will compute them. For single-role
    # entities the role row carries the full fixed/net.
    rows: List[Dict[str, Any]] = []
    role_count_by_name: Dict[str, int] = defaultdict(int)
    for (name, _role), _data in earner.items():
        role_count_by_name[name] += 1
    for (name, role), data in earner.items():
        total_inr   = data[0]
        accounts_n  = len(data[1]) if isinstance(data[1], set) else int(data[1])
        total_aum_e = data[2]
        aum_x_pct   = data[3]
        wr = _wrate(aum_x_pct, total_aum_e)
        is_single = role_count_by_name[name] == 1
        # fixed_inr is on every row (so a multi-role parent can read it
        # from any child); net_inr only on single-role rows since for
        # multi-role the parent's net is sum_gross − fixed and the
        # child rows don't carry meaningful net values.
        # Net treats no-fixed (None) the same as zero-fixed, so an
        # entity without a configured fixed payment has Net = Gross
        # rather than '—'.
        fixed = _fixed_for_entity(name)
        net = (total_inr - (fixed or 0)) if is_single else None
        rows.append({
            'name': name, 'role': role,
            'accounts': accounts_n,
            'total_inr':         round(total_inr, 2),
            'total_aum':         round(total_aum_e, 2),
            'aum_x_pct':         round(aum_x_pct, 6),
            'weighted_rate_pct': round(wr, 4) if wr is not None else None,
            'fixed_inr':         round(fixed, 2) if fixed is not None else None,
            'net_inr':           round(net, 2)   if net   is not None else None,
        })
    by_earner = sorted(rows, key=lambda e: (-e['total_inr'], e['name'].lower()))

    # Totals — investor side (gross billed) + entity side (gross fees,
    # total fixed payments owed for the period, net = gross − fixed
    # which CAN go negative when fixed payments outweigh variable
    # earnings for the period).
    total_inv_fee = sum(li.investor_fee for li in line_items if li.avg_aum)
    # total_aum represents the average daily AUM under management
    # across all accounts. With one-line-per-segment, sum(avg_aum)
    # would over-count multi-segment accounts; instead, group by
    # account_code, compute each account's overall avg (sum_aum /
    # days_billed), then sum those.
    _per_account_aumdays: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0])
    for li in line_items:
        slot = _per_account_aumdays[li.account_code]
        slot[0] += li.sum_aum
        slot[1] += li.period_days
    total_aum = sum((s / d) for s, d in _per_account_aumdays.values() if d > 0)
    # Weighted-headline rate uses time-weighted aum (sum_aum) so a
    # rate change mid-period contributes correctly:
    # Σ(sum_aum × headline) / Σ(sum_aum) is the days-weighted average
    # rate across the whole book over the operator's window. Line
    # items with headline=0 are excluded from the denominator (zero-
    # earning AUM mustn't dilute the rate).
    total_aum_for_head = sum(li.sum_aum
                              for li in line_items if li.headline_pct > 0)
    total_aum_x_head  = sum(li.sum_aum * li.headline_pct
                              for li in line_items)
    # entity-level totals: sum gross fees across all earners (this is
    # the same as total_inv_fee since shares always sum to headline
    # by construction); sum fixed payments across every entity that
    # has any earnings (positive or negative).
    entity_gross  = sum(r['total_inr']  for r in rows)
    entity_fixed  = 0.0
    distinct_paid: set = set()
    for r in rows:
        if r['name'] in distinct_paid:
            continue
        f = _fixed_for_entity(r['name'])
        if f is not None:
            entity_fixed += f
            distinct_paid.add(r['name'])
    entity_net    = entity_gross - entity_fixed
    totals = {
        'total_fees':            round(total_inv_fee, 2),
        'total_aum':             round(total_aum, 2),
        'weighted_headline_pct': (round(_wrate(total_aum_x_head, total_aum_for_head), 4)
                                  if _wrate(total_aum_x_head, total_aum_for_head) is not None
                                  else None),
        # Total accounts = distinct account_codes across line items
        # (one account can produce multiple segments — counting the
        # underlying accounts, not the segments).
        'accounts':              len({li.account_code for li in line_items}),
        'accounts_with_aum':     len({li.account_code
                                       for li in line_items if li.avg_aum}),
        'segments':              len(line_items),
        'entity_gross':          round(entity_gross, 2),
        'entity_fixed':          round(entity_fixed, 2),
        'entity_net':            round(entity_net,   2),
    }

    # Surface effective-date stats. master_loaded=N, hits=M,
    # missed=K — when master_loaded>0 but hits=0 the (CLIENTID,
    # ACCOUNTCODE) keys aren't joining (silent-failure mode).
    # 'missed' = accounts in fee_configs WITH AUM data but no
    # effective_date (skipped from billing per operator policy).
    if _eff_loaded:
        logger.info(
            f'ClientBillgroup join: master={_eff_loaded} entries, '
            f'accounts_billed={_eff_hit}, accounts_skipped_no_eff_date={_eff_miss}, '
            f'accounts_with_stale_data={len(stale_data)}'
        )
    return {
        'period':     {'from': from_str, 'to': to_str, 'days': days},
        'by_account': [
            {'account_code': li.account_code, 'client_name': li.client_name,
             'scheme_name': li.scheme_name,
             'period_from': li.period_from,
             'period_to':   li.period_to,
             'period_days': li.period_days,
             'avg_aum': round(li.avg_aum, 2),
             'headline_pct':    li.headline_pct,
             'investor_fee': round(li.investor_fee, 2),
             'gsw_pct':         li.gsw_pct,
             'gsw_inr':         round(li.gsw_inr, 2),
             'fm_pct':          li.fm_pct,
             'fm_inr':          round(li.fm_inr, 2),
             'fm_name':         li.fm_name,
             'distributor_pct': li.distributor_pct,
             'distributor_inr': round(li.distributor_inr, 2),
             'distributor_name':li.distributor_name,
             'residual_pct':    li.residual_pct,
             'residual_inr':    round(li.residual_inr, 2),
             'residual_name':   li.residual_name,
             'billgroup_description': li.billgroup_description}
            for li in line_items
        ],
        'by_role':   {k: round(v, 2) for k, v in by_role.items()},
        'by_earner': by_earner,
        'totals':    totals,
        'unmatched': {'aum_no_config':     aum_no_config,
                      'config_no_aum':     config_no_aum,
                      'no_effective_date': no_effective_date,
                      'no_breakup':        no_breakup,
                      'stale_data':        stale_data},
        'billgroup_clip': {'master_loaded':            _eff_loaded,
                           'accounts_billed':          _eff_hit,
                           'accounts_skipped':         _eff_miss,
                           'accounts_with_stale_data': len(stale_data)},
    }
