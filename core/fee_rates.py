"""
Fee rates — bulk upload + template
==================================

Lets the operator manage fee structures across many clients without
clicking through the per-account modal one at a time.

CSV columns (single sheet, no header magic — just plain CSV):

    tax_pan, ws_account_code, scheme_name,
    headline_fee_pct, share_gsw_pct, share_fm_pct,
    share_distributor_pct, share_residual_pct, fm_monthly_salary

Matching key is (tax_pan, ws_account_code). scheme_name is informational
only — it helps the operator see what they're editing in the spreadsheet
but isn't used for matching (account_code is unique per pool already).

A row updates either:
  • the matching client row's fee_config column (primary pool match), or
  • the matching entry inside that client row's additional_pools JSON
    (sub-pool match).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    'tax_pan',
    'ws_account_code',
    'scheme_name',
    'headline_fee_pct',
    'share_gsw_pct',
    'share_fm_pct',
    'share_distributor_pct',
    'share_residual_pct',
    'fm_user_name',
    'distributor_user_name',
    'residual_user_name',
]


def _fee_config_to_csv_fields(fc: Optional[Any]) -> Dict[str, str]:
    """Flatten a fee_config dict (or JSON string, or None) to the six
    CSV columns. Returns blanks when fc is empty/missing."""
    if fc is None or fc == '':
        return {k: '' for k in CSV_COLUMNS[3:]}
    if isinstance(fc, str):
        try:
            fc = json.loads(fc)
        except Exception:
            return {k: '' for k in CSV_COLUMNS[3:]}
    if not isinstance(fc, dict):
        return {k: '' for k in CSV_COLUMNS[3:]}
    def _v(k):
        v = fc.get(k)
        return '' if v is None else str(v)
    return {
        'headline_fee_pct':      _v('headline_fee_pct'),
        'share_gsw_pct':         _v('share_gsw_pct'),
        'share_fm_pct':          _v('share_fm_pct'),
        'share_distributor_pct': _v('share_distributor_pct'),
        'share_residual_pct':    _v('share_residual_pct'),
        'fm_user_name':          _v('fm_user_name'),
        'distributor_user_name': _v('distributor_user_name'),
        'residual_user_name':    _v('residual_user_name'),
    }


def _csv_fields_to_fee_config(row: Dict[str, str]) -> Optional[Dict[str, float]]:
    """Parse the six CSV fee fields back into a fee_config dict, or
    None when every field is blank (meaning "no fee config for this
    row"). Numeric parsing tolerates spaces and commas."""
    def _num(s: Optional[str]) -> Optional[float]:
        if s is None:
            return None
        s2 = str(s).strip().replace(',', '')
        if s2 == '':
            return None
        try:
            return float(s2)
        except ValueError:
            return None
    def _str(s: Optional[str]) -> Optional[str]:
        if s is None:
            return None
        s2 = str(s).strip()
        return s2 if s2 else None
    fc = {
        'headline_fee_pct':      _num(row.get('headline_fee_pct')),
        'share_gsw_pct':         _num(row.get('share_gsw_pct')),
        'share_fm_pct':          _num(row.get('share_fm_pct')),
        'share_distributor_pct': _num(row.get('share_distributor_pct')),
        'share_residual_pct':    _num(row.get('share_residual_pct')),
        'fm_user_name':          _str(row.get('fm_user_name')),
        'distributor_user_name': _str(row.get('distributor_user_name')),
        'residual_user_name':    _str(row.get('residual_user_name')),
    }
    if all(v is None for v in fc.values()):
        return None
    return fc


def list_roster_names() -> List[str]:
    """Roster of fee-earner names available in the role dropdowns.

    Source of truth is the entity-payments store (data/fee_entity_payments.json)
    — names added there appear in the FM / Distributor / Residual
    dropdowns on every fee modal and inline accordion. Existing names
    already used in any fee_config are also included so historical
    assignments aren't orphaned when this changeover lands; the operator
    sees them in the dropdown and can keep them or migrate to a tabled
    entity at their own pace.

    Sorted alphabetically (case-insensitive).
    """
    from .fee_entity_payments import load_payments
    names: set = set(load_payments().keys())
    # Union with names already referenced from any pool's fee_config so
    # we don't drop existing data when this rule starts being enforced.
    for row in list_pool_rows():
        fc = row.get('fee_config')
        if fc is None or fc == '':
            continue
        if isinstance(fc, str):
            try:
                fc = json.loads(fc)
            except Exception:
                continue
        if not isinstance(fc, dict):
            continue
        for key in ('fm_user_name', 'distributor_user_name', 'residual_user_name'):
            v = fc.get(key)
            if v and isinstance(v, str) and v.strip():
                names.add(v.strip())
    return sorted(names, key=lambda s: s.lower())


def list_investors_with_pools() -> List[Dict[str, Any]]:
    """Group every pool by PAN for the inline-edit accordion on the
    Fees screen. Each entry:
        {tax_pan, first_name, middle_name, last_name,
         pools: [{ws_account_code, scheme_name, pool_mapin,
                  fee_config, source, client_row_id,
                  client_id, cbg_timeline}, ...]}

    cbg_timeline is the list of (effective_from, flatfee_pct,
    billgroup, billgroup_description) entries from
    ClientBillgroupMaster keyed by (clients.tax_pan-derived CLIENTID,
    scheme_name). Empty when the master has no rows for the client/
    scheme — the UI surfaces those as "awaiting WS bill-group setup"
    rather than letting the operator enter a breakup against nothing.

    Pools array carries primary + additional in the same flat list,
    sorted by ws_account_code so the operator sees them in a stable
    order. PANs are sorted alphabetically.
    """
    from .client_onboarding import _db_path, init_db
    # CBG master timeline lookup. Loaded once per call — the file is
    # small and the lookup is per pool.
    try:
        from .client_billgroup_dates import (load as _cbg_load,
                                              _norm_id as _cbg_norm_id,
                                              _norm_scheme as _cbg_norm_scheme)
        cbg_timelines = _cbg_load()
    except Exception:
        cbg_timelines = {}
        _cbg_norm_id = lambda x: (str(x or '').strip().upper())
        _cbg_norm_scheme = lambda x: (str(x or '').strip().lower())

    def _timeline_for(client_id_raw: Any, scheme_name_raw: str) -> List[Dict[str, Any]]:
        cid = _cbg_norm_id(client_id_raw)
        sc = _cbg_norm_scheme(scheme_name_raw)
        return (cbg_timelines.get((cid, sc))
                or cbg_timelines.get((cid, '')) or [])

    init_db()
    by_pan: Dict[str, Dict[str, Any]] = {}
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT id, tax_pan, first_name, middle_name, last_name, '
            'ws_account_code, ws_client_id, scheme_name, pool_mapin, '
            'fee_config, additional_pools '
            'FROM clients ORDER BY tax_pan, ws_account_code'
        ).fetchall()
    for r in rows:
        pan = (r['tax_pan'] or '').strip().upper()
        if not pan:
            continue
        bucket = by_pan.setdefault(pan, {
            'tax_pan':    pan,
            'first_name': r['first_name']  or '',
            'middle_name':r['middle_name'] or '',
            'last_name':  r['last_name']   or '',
            'pools':      [],
        })
        # Newly-added investors often have one row with the names
        # populated and a sibling row (different account code, possibly
        # blank) without names. ORDER BY ws_account_code can land the
        # blank-acct row first, leaving the bucket's name fields empty.
        # Promote any non-empty name from a later row when the bucket's
        # current value is blank — never overwrite populated values.
        for fld in ('first_name', 'middle_name', 'last_name'):
            if not (bucket[fld] or '').strip() and (r[fld] or '').strip():
                bucket[fld] = r[fld]
        # Primary pool — only add when there's something identifiable.
        prim_acct   = (r['ws_account_code'] or '').strip()
        prim_scheme = (r['scheme_name']     or '').strip()
        ws_cid      = (r['ws_client_id']    or '').strip()
        if prim_acct or prim_scheme:
            try:
                fc = json.loads(r['fee_config']) if r['fee_config'] else None
                if not isinstance(fc, dict):
                    fc = None
            except Exception:
                fc = None
            bucket['pools'].append({
                'ws_account_code': prim_acct,
                'scheme_name':     prim_scheme,
                'pool_mapin':      r['pool_mapin'] or '',
                'fee_config':      fc,
                'source':          'primary',
                'client_row_id':   r['id'],
                'ws_client_id':    ws_cid,
                'cbg_timeline':    _timeline_for(ws_cid, prim_scheme),
            })
        # Additional pools.
        try:
            extras = json.loads(r['additional_pools'] or '[]') or []
        except Exception:
            extras = []
        for p in extras:
            if not isinstance(p, dict):
                continue
            ea = (p.get('ws_account_code') or '').strip()
            efc = p.get('fee_config') if isinstance(p.get('fee_config'), dict) else None
            if not (ea or p.get('scheme_name')):
                continue
            sub_scheme = (p.get('scheme_name') or '').strip()
            bucket['pools'].append({
                'ws_account_code': ea,
                'scheme_name':     sub_scheme,
                'pool_mapin':      (p.get('pool_mapin')  or '').strip(),
                'fee_config':      efc,
                'source':          'additional',
                'client_row_id':   r['id'],
                'ws_client_id':    ws_cid,
                'cbg_timeline':    _timeline_for(ws_cid, sub_scheme),
            })
    return sorted(by_pan.values(), key=lambda v: v['tax_pan'])


def upsert_pool_fee_config(tax_pan: str, ws_account_code: str,
                           fee_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Save a single pool's fee_config in place. Used by the inline-edit
    accordion's per-row Save button — finds the right row (primary
    column or additional_pools entry) and writes only that one.

    fee_config may be a dict, an empty dict, or None; an empty/None
    config clears the column / removes the entry's nested fee_config.

    Returns {ok, updated: 'primary'|'additional', client_row_id} on
    success or {ok: False, error} if no matching pool exists.
    """
    from .client_onboarding import _db_path, init_db
    pan  = (tax_pan or '').strip().upper()
    acct = (ws_account_code or '').strip()
    if not pan or not acct:
        return {'ok': False, 'error': 'tax_pan and ws_account_code required'}
    new_fc: Optional[Dict[str, Any]] = None
    if isinstance(fee_config, dict):
        if any(v is not None and v != '' for v in fee_config.values()):
            new_fc = fee_config
    init_db()
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        # Try primary-pool match first.
        row = conn.execute(
            'SELECT id FROM clients '
            'WHERE UPPER(tax_pan)=? AND ws_account_code=?',
            (pan, acct),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE clients SET fee_config=?, updated_at=datetime('now') "
                "WHERE id=?",
                (json.dumps(new_fc) if new_fc else None, row['id']),
            )
            conn.commit()
            return {'ok': True, 'updated': 'primary', 'client_row_id': row['id']}
        # Fall through to additional_pools — scan every PAN-mate row.
        candidates = conn.execute(
            'SELECT id, additional_pools FROM clients WHERE UPPER(tax_pan)=?',
            (pan,),
        ).fetchall()
        for c in candidates:
            try:
                extras = json.loads(c['additional_pools'] or '[]') or []
            except Exception:
                continue
            mutated = False
            for i, p in enumerate(extras):
                if not isinstance(p, dict):
                    continue
                if (p.get('ws_account_code') or '').strip() == acct:
                    if new_fc is None:
                        extras[i].pop('fee_config', None)
                    else:
                        extras[i]['fee_config'] = new_fc
                    mutated = True
                    break
            if mutated:
                conn.execute(
                    "UPDATE clients SET additional_pools=?, "
                    "updated_at=datetime('now') WHERE id=?",
                    (json.dumps(extras), c['id']),
                )
                conn.commit()
                return {'ok': True, 'updated': 'additional', 'client_row_id': c['id']}
    return {'ok': False, 'error': f'no pool found for {pan} / {acct}'}


def list_pool_rows() -> List[Dict[str, Any]]:
    """Return one entry per pool across all clients — primary pool +
    each additional_pools entry. Used to seed the CSV template so the
    operator doesn't have to type PANs / account codes."""
    from .client_onboarding import _db_path, init_db
    init_db()
    out: List[Dict[str, Any]] = []
    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT id, tax_pan, first_name, ws_account_code, '
            'scheme_name, pool_mapin, fee_config, additional_pools '
            'FROM clients ORDER BY tax_pan, ws_account_code'
        ).fetchall()
    for r in rows:
        pan = (r['tax_pan'] or '').strip().upper()
        if not pan:
            continue
        # Primary pool — only emit when there's something to identify
        # (account code or scheme). Skip purely empty rows.
        prim_acct   = (r['ws_account_code'] or '').strip()
        prim_scheme = (r['scheme_name']     or '').strip()
        if prim_acct or prim_scheme:
            out.append({
                'tax_pan':         pan,
                'ws_account_code': prim_acct,
                'scheme_name':     prim_scheme,
                'fee_config':      r['fee_config'],
                '_source':         'primary',
                '_client_row_id':  r['id'],
            })
        # Additional pools — each entry on the row gets its own line.
        try:
            extras = json.loads(r['additional_pools'] or '[]') or []
        except Exception:
            extras = []
        for i, p in enumerate(extras):
            if not isinstance(p, dict):
                continue
            out.append({
                'tax_pan':         pan,
                'ws_account_code': (p.get('ws_account_code') or '').strip(),
                'scheme_name':     (p.get('scheme_name')     or '').strip(),
                'fee_config':      p.get('fee_config'),
                '_source':         'additional',
                '_client_row_id':  r['id'],
                '_extra_idx':      i,
            })
    return out


def build_csv_template() -> str:
    """Render the current pool list as a CSV string ready for download.
    Existing fee_config values are pre-filled so the operator only has
    to fill the empty cells."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for row in list_pool_rows():
        fee_fields = _fee_config_to_csv_fields(row.get('fee_config'))
        writer.writerow({
            'tax_pan':         row['tax_pan'],
            'ws_account_code': row['ws_account_code'],
            'scheme_name':     row['scheme_name'],
            **fee_fields,
        })
    return buf.getvalue()


def apply_rate_updates(csv_text: str) -> Dict[str, Any]:
    """Apply rate updates from a CSV string. Returns:
        {ok, matched, updated, unmatched: [{tax_pan, ws_account_code}],
         skipped: [{row_idx, reason}]}
    Matching is case-insensitive on tax_pan and exact on ws_account_code.
    A row whose fee fields are all blank is treated as "clear the
    fee_config for this account" — useful for bulk reset.
    """
    from .client_onboarding import _db_path, init_db
    init_db()

    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return {'ok': False, 'error': 'empty CSV'}
    missing = [c for c in ('tax_pan', 'ws_account_code') if c not in reader.fieldnames]
    if missing:
        return {'ok': False,
                'error': f'CSV missing required columns: {", ".join(missing)}'}

    # Index DB pool rows by (PAN, account_code) for O(1) lookup.
    pool_rows = list_pool_rows()
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in pool_rows:
        if r['ws_account_code']:
            by_key[(r['tax_pan'], r['ws_account_code'])] = r

    matched: List[Tuple[str, str]] = []
    unmatched: List[Dict[str, str]] = []
    skipped:   List[Dict[str, Any]] = []
    updated_count = 0

    with sqlite3.connect(_db_path()) as conn:
        conn.row_factory = sqlite3.Row
        for idx, raw in enumerate(reader, start=2):  # start=2 (CSV row #s w/ header)
            pan  = (raw.get('tax_pan')         or '').strip().upper()
            acct = (raw.get('ws_account_code') or '').strip()
            if not pan or not acct:
                skipped.append({'row_idx': idx,
                                'reason': 'tax_pan or ws_account_code blank'})
                continue
            target = by_key.get((pan, acct))
            if not target:
                unmatched.append({'tax_pan': pan, 'ws_account_code': acct})
                continue
            new_fc = _csv_fields_to_fee_config(raw)
            row_id = target['_client_row_id']
            source = target['_source']
            if source == 'primary':
                conn.execute(
                    "UPDATE clients SET fee_config=?, updated_at=datetime('now') "
                    "WHERE id=?",
                    (json.dumps(new_fc) if new_fc else None, row_id),
                )
            else:
                # Read additional_pools, mutate the entry, write back.
                cur_row = conn.execute(
                    'SELECT additional_pools FROM clients WHERE id=?',
                    (row_id,)
                ).fetchone()
                try:
                    extras = json.loads(cur_row['additional_pools'] or '[]') or []
                except Exception:
                    extras = []
                ix = target.get('_extra_idx', -1)
                if 0 <= ix < len(extras) and isinstance(extras[ix], dict):
                    if new_fc is None:
                        extras[ix].pop('fee_config', None)
                    else:
                        extras[ix]['fee_config'] = new_fc
                    conn.execute(
                        "UPDATE clients SET additional_pools=?, "
                        "updated_at=datetime('now') WHERE id=?",
                        (json.dumps(extras), row_id),
                    )
                else:
                    skipped.append({'row_idx': idx,
                                    'reason': 'additional_pools index drifted'})
                    continue
            matched.append((pan, acct))
            updated_count += 1
        conn.commit()

    return {
        'ok': True,
        'matched':   len(matched),
        'updated':   updated_count,
        'unmatched': unmatched,
        'skipped':   skipped,
    }
