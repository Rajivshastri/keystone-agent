"""
core/client_backfill.py — enrich the SQLite clients DB from
ClientDetail.xls (the WS Client Master export).

Pools were the previous backfill target (see core/pool_backfill.py); this
module is its sibling for clients. Source is the same daily-downloaded
WS master tree under data/{date}/masters/, just a different file.

Single source of truth: ClientDetail.xls (~34 columns, one row per client).
Match key: tax_pan == H1PANNO. The user's invariant — 'PAN is always
there' — means we don't need a fallback.

Output is a *draft* per client. The operator reviews each draft in the
Client Backfill Review screen and approves/edits/skips before we mutate
data/clients.db. Idempotent.

ClientDetail → SQLite mapping
=============================

  Direct
    BIRTHDATE        → birth_date
    MOBILE           → mobile
    EMAILID          → email
    H1PANNO          → tax_pan
    GROUPNAME        → group_name
    SCHEMENAME       → scheme_name
    INTERMEDIARYNAME → intermediary_user_name
    BRANCHNAME       → branch_name
    RELMGRNAME       → rm_user_name
    H1ADD1/2         → address1 / address2
    H1CITY           → city
    H1PIN            → pin_code
    H1STATE          → state
    H1STATUS         → client_status
    BANKCODE         → mandate_bank_name
    BANKACID         → mandate_bank_acno
    DPCLIENTID       → mandate_dp_clientid
    BILLGROUP        → bill_group
    ACCOUNTINGTXN    → accounting_txn
    STTTAKEN AS      → stt_taken_as
    TRXNTAKEN AS     → trxn_taken_as

  WS-only (new columns, see init_db migrations)
    CLIENTID         → ws_client_id
    CLIENTCODE       → ws_client_code
    GROUPID          → ws_group_id
    BROKERACID       → broker_acid
    ASSETS           → assets   (REAL)
    NETCAPITAL       → net_capital (REAL)

  Transformed
    CLIENTNAME       → first_name / middle_name / last_name OR
                        first_name only (when H1STATUS != 'Individual')
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FieldDelta:
    """One proposed change. The UI renders these as diff rows the
    operator can accept/reject individually."""
    field: str
    current: Any
    proposed: Any
    source: str = 'ClientDetail'


@dataclass
class ClientDraft:
    """Per-client enrichment proposal.

    ``mode`` is 'update' for clients that already exist in SQLite,
    'create' for ClientDetail rows we have no SQLite row for. The UI
    shows them in separate sections so the operator can opt-in to
    auto-creation explicitly.
    """
    tax_pan: str
    mode: str                                  # 'update' | 'create'
    matched_row: Optional[Dict] = None         # source ClientDetail row
    existing_id: Optional[int] = None          # SQLite row id if mode=='update'
    deltas: List[FieldDelta] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'tax_pan':     self.tax_pan,
            'mode':        self.mode,
            'existing_id': self.existing_id,
            'matched_row': self.matched_row,
            'deltas':      [{'field': d.field, 'current': d.current,
                              'proposed': d.proposed, 'source': d.source}
                             for d in self.deltas],
            'notes':       self.notes,
        }


# ── XLS reading (same shape as core/pool_backfill) ─────────────────────── #

def _read_xls_dicts(path: Path) -> List[Dict]:
    """Read sheet[0] of an .xls/.xlsx into [{column: value}] dicts."""
    if not path.exists():
        return []
    try:
        import xlrd
        wb = xlrd.open_workbook(str(path))
        sh = wb.sheet_by_index(0)
    except Exception as e:
        logger.warning(f'failed to read {path}: {e}')
        return []
    if sh.nrows < 2:
        return []
    headers = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
    out: List[Dict] = []
    for r in range(1, sh.nrows):
        row = {headers[c]: sh.cell_value(r, c) for c in range(sh.ncols)}
        out.append(row)
    return out


def _find_client_detail(masters_dir: Path) -> Optional[Path]:
    """Locate ClientDetail.xls (case-insensitive) under the masters dir."""
    if not masters_dir.exists():
        return None
    for name in ('ClientDetail.xls', 'clientdetail.xls'):
        cand = masters_dir / name
        if cand.exists():
            return cand
    for p in masters_dir.iterdir():
        if p.is_file() and p.name.lower() == 'clientdetail.xls':
            return p
    return None


def latest_client_detail(data_dir: Path) -> Optional[Path]:
    """Most recent date dir's ClientDetail.xls. None if nothing usable."""
    if not data_dir.exists():
        return None
    candidates = sorted(
        (p for p in data_dir.iterdir()
         if p.is_dir() and len(p.name) == 10 and p.name[4] == '-' and p.name[7] == '-'),
        reverse=True,
    )
    for d in candidates:
        cd = _find_client_detail(d / 'masters')
        if cd is not None:
            return cd
    return None


# ── Cell coercion ─────────────────────────────────────────────────────── #

def _str(v: Any) -> str:
    """Render a cell value as a stripped string. Floats with no decimal
    collapse to ints (CLIENTID 100002.0 → '100002')."""
    if v is None or v == '':
        return ''
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return str(v)
    return str(v).strip()


def _float(v: Any) -> Optional[float]:
    """Parse a numeric cell to float; return None if blank or unparseable."""
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _excel_date_to_dmy(v: Any) -> str:
    """Coerce a BIRTHDATE cell to dd/mm/yyyy. ClientDetail typically
    already serialises to a string like '04/01/1975'; numeric Excel
    serials (rare) get converted via the standard 1899-12-30 epoch."""
    if v is None or v == '':
        return ''
    if isinstance(v, str):
        return v.strip()
    try:
        from datetime import datetime, timedelta
        n = float(v)
        return (datetime(1899, 12, 30) + timedelta(days=n)).strftime('%d/%m/%Y')
    except Exception:
        return ''


# ── Name split (per the user's spec) ──────────────────────────────────── #

def split_name(full_name: str, status: str) -> Tuple[str, str, str]:
    """Split CLIENTNAME into (first, middle, last) per the operator's rules.

    - Non-Individual (Trust / HUF / Company / Partnership / LLP / etc.):
      keep the whole name in ``first``; middle and last stay blank.
      first_name doubles as 'entity name' on the registry display.
      Detection is belt-and-braces — the WS H1STATUS column is the
      authoritative signal, but if it's blank or mis-tagged (e.g. an
      LLP accidentally saved as 'Individual' in WS) we still recognise
      the entity by its name suffix (LLP / LTD / TRUST / HUF / ...).
    - Individual + 3 or more tokens: first / middle (joined with ' ')
      / last. The middle can be multi-token to handle 'Jane Mary Anne
      Smith' → first=Jane, middle='Mary Anne', last=Smith.
    - Individual + 2 tokens: first / '' / last.
    - Individual + 1 token: first / '' / ''.
    """
    name = (full_name or '').strip()
    if not name:
        return ('', '', '')

    status_says_individual = (status or '').strip().lower() == 'individual'
    name_is_entity = _name_looks_like_entity(name)
    is_individual = status_says_individual and not name_is_entity
    if not is_individual:
        # Non-individual — store the entire name in first_name; the
        # registry display prefers first+last and an empty last is fine.
        return (name, '', '')

    parts = name.split()
    if len(parts) == 1:
        return (parts[0], '', '')
    if len(parts) == 2:
        return (parts[0], '', parts[1])
    # 3+ tokens: first, middle (everything in between), last.
    return (parts[0], ' '.join(parts[1:-1]), parts[-1])


# Tokens that mark a holder name as a non-individual entity. Whole-word
# match after stripping punctuation. Mirrors the equivalent set in
# core/client_onboarding.py (kept private here to avoid a cross-module
# coupling — the two consumers should be free to evolve independently).
_ENTITY_TOKENS = frozenset({
    'LLP', 'LTD', 'LIMITED', 'PVT', 'PRIVATE', 'INC', 'CORP', 'CORPORATION',
    'COMPANY', 'CO', 'COMPANIES',
    'TRUST', 'FOUNDATION', 'SOCIETY', 'ASSOCIATION',
    'PARTNERS', 'PARTNERSHIP', 'ASSOCIATES',
    'HUF',
    'FUND', 'FUNDS', 'CAPITAL', 'INVESTMENTS', 'HOLDINGS',
    'BANK', 'INSURANCE',
    'AOP',
})


def _name_looks_like_entity(name: str) -> bool:
    """True if ``name`` carries a corporate / trust / partnership token
    (LLP, LTD, TRUST, HUF, PARTNERS, ...). Used as a safety net when
    H1STATUS is blank or wrong in WS — the name itself is enough to
    keep the splitter from carving 'Hxgon Partners LLP' into three
    pieces."""
    if not name:
        return False
    import re as _re
    tokens = _re.findall(r'[A-Za-z]+', name.upper())
    return any(t in _ENTITY_TOKENS for t in tokens)


# ── Delta building ────────────────────────────────────────────────────── #

# Direct ClientDetail-column → SQLite-column mappings. Keys are
# ClientDetail column names exactly as they appear in the xls header
# row; values are SQLite column names.
_DIRECT_MAP: Dict[str, str] = {
    'BIRTHDATE':        'birth_date',
    'MOBILE':           'mobile',
    'PHONE':            'phone_home',
    'EMAILID':          'email',
    'H1PANNO':          'tax_pan',
    'GROUPNAME':        'group_name',
    'SCHEMENAME':       'scheme_name',
    'INTERMEDIARYNAME': 'intermediary_user_name',
    'BRANCHNAME':       'branch_name',
    'RELMGRNAME':       'rm_user_name',
    'H1ADD1':           'address1',
    'H1ADD2':           'address2',
    'H1CITY':           'city',
    'H1PIN':            'pin_code',
    'H1STATE':          'state',
    'H1STATUS':         'client_status',
    'BANKCODE':         'mandate_bank_name',
    'BANKACID':         'mandate_bank_acno',
    'DPCLIENTID':       'mandate_dp_clientid',
    'BILLGROUP':        'bill_group',
    'ACCOUNTINGTXN':    'accounting_txn',
    'STTTAKEN AS':      'stt_taken_as',
    'TRXNTAKEN AS':     'trxn_taken_as',
    # WS-only columns (added in init_db migrations).
    'CLIENTID':         'ws_client_id',
    'CLIENTCODE':       'ws_client_code',
    'GROUPID':          'ws_group_id',
    'BROKERACID':       'broker_acid',
}


def _propose(deltas: List[FieldDelta], existing: Dict, sql_col: str,
             proposed: Any) -> None:
    """Append a delta only when the proposed value differs from the current.

    Backfill is allowed to overwrite stale values here (unlike the pool
    backfill which is blank-only) because ClientDetail is the single
    authoritative source on WS — if the master differs from SQLite,
    the master is right.
    """
    cur = '' if existing.get(sql_col) is None else str(existing.get(sql_col))
    new = '' if proposed is None else str(proposed)
    # Treat nothing-to-something as a fill, something-to-different as
    # an overwrite. Same → no delta.
    if cur.strip() == new.strip():
        return
    deltas.append(FieldDelta(field=sql_col, current=cur, proposed=new))


def build_draft(row: Dict, existing: Optional[Dict],
                pools_by_scheme: Optional[Dict[str, Dict]] = None) -> ClientDraft:
    """Build a backfill draft for one ClientDetail row.

    ``existing`` is the matching SQLite client (or None for create mode).
    ``pools_by_scheme`` maps lowercased scheme_name to the first matching
    pool dict from pools_hub. When supplied, build_draft proposes
    pool_mapin + ws_scheme_code for the matched scheme. Operator can
    correct via the multi-pool dropdown if the wrong pool was picked
    (multiple pools can share the same scheme name across custodians).
    """
    tax_pan = _str(row.get('H1PANNO')).upper()
    mode = 'update' if existing else 'create'
    draft = ClientDraft(
        tax_pan=tax_pan,
        mode=mode,
        matched_row={k: _str(v) for k, v in row.items()},
        existing_id=(existing.get('id') if existing else None),
    )
    if not tax_pan:
        draft.notes.append('skip — H1PANNO is blank')
        return draft

    cur = existing or {}

    # Direct mappings. BIRTHDATE gets the date coercion; everything
    # else is string-coerced.
    for src_col, sql_col in _DIRECT_MAP.items():
        v = row.get(src_col)
        if src_col == 'BIRTHDATE':
            new = _excel_date_to_dmy(v)
        else:
            new = _str(v)
        if new == '':
            continue
        _propose(draft.deltas, cur, sql_col, new)

    # AUM fields — REAL columns. Skip when the source cell is blank.
    for src_col, sql_col in (('ASSETS', 'assets'), ('NETCAPITAL', 'net_capital')):
        v = _float(row.get(src_col))
        if v is None:
            continue
        cur_v = cur.get(sql_col)
        if cur_v is not None:
            try:
                if abs(float(cur_v) - v) < 0.005:
                    continue   # same value (within rounding)
            except (TypeError, ValueError):
                pass
        draft.deltas.append(FieldDelta(field=sql_col, current=cur_v, proposed=v))

    # CLIENTNAME → first / middle / last per spec
    full_name = _str(row.get('CLIENTNAME'))
    status    = _str(row.get('H1STATUS'))
    first, middle, last = split_name(full_name, status)
    for sql_col, new in (
        ('first_name',  first),
        ('middle_name', middle),
        ('last_name',   last),
    ):
        # Only propose when we have a meaningful value; for non-
        # individuals middle/last are intentionally blank.
        if not new and not (cur.get(sql_col) or ''):
            continue
        _propose(draft.deltas, cur, sql_col, new)

    # Entity safety net — if the name signals a non-individual (LLP /
    # Trust / Pvt Ltd / HUF / ...) but the existing row has gender or
    # salutation populated (typically from a stale earlier save when
    # the splitter was less strict), propose clearing them. Gender
    # doesn't apply to entities and salutation should be 'M/s' rather
    # than 'Mr' / 'Mrs'.
    if _name_looks_like_entity(full_name):
        if (cur.get('gender') or '').strip():
            _propose(draft.deltas, cur, 'gender', '')
        cur_sal = (cur.get('salutation') or '').strip()
        if cur_sal and cur_sal != 'M/s':
            _propose(draft.deltas, cur, 'salutation', 'M/s')

    # ── Mirror mappings ──────────────────────────────────────────────
    # ClientDetail has ONE bank tuple (BANKCODE / BANKACID) and ONE
    # demat (DPCLIENTID); the WS Account Master form treats those as
    # both the 'first holder mandate' AND the 'operating / trading'
    # accounts in 99% of cases. Mirror them so trade recon and the
    # registry display both have what they need without the operator
    # typing the same values twice. Operator can split via Edit if a
    # particular client genuinely uses different operating accounts.
    bank_code   = _str(row.get('BANKCODE'))
    bank_acno   = _str(row.get('BANKACID'))
    dp_client   = _str(row.get('DPCLIENTID'))
    if bank_code: _propose(draft.deltas, cur, 'trading_bank_code',    bank_code)
    if bank_acno: _propose(draft.deltas, cur, 'trading_bank_acno',    bank_acno)
    if dp_client:
        _propose(draft.deltas, cur, 'trading_dp_clientid', dp_client)
        _propose(draft.deltas, cur, 'dp_client_id',        dp_client)

    # CLIENTCODE is the WS account code — store on ws_account_code so
    # the WS push/edit flows can reference it without re-fetching.
    client_code = _str(row.get('CLIENTCODE'))
    if client_code:
        _propose(draft.deltas, cur, 'ws_account_code', client_code)

    # ── Pool linkage ────────────────────────────────────────────────
    # ClientDetail tells us which scheme the client is on, but not
    # which pool (a scheme can have several pools across custodians).
    # Look up the scheme in pools_hub; if exactly one pool matches,
    # propose pool_mapin + ws_scheme_code. If multiple match we pick
    # the first; the operator can correct via the multi-pool dropdown
    # in the edit form.
    scheme_nm = _str(row.get('SCHEMENAME'))
    if scheme_nm and pools_by_scheme:
        pool = pools_by_scheme.get(scheme_nm.lower())
        if pool:
            if pool.get('mapin'):
                _propose(draft.deltas, cur, 'pool_mapin', pool['mapin'])
            ws_code = pool.get('ws_scheme_code') or pool.get('mapin')
            if ws_code:
                _propose(draft.deltas, cur, 'ws_scheme_code', ws_code)

    return draft


def build_drafts(client_detail_rows: List[Dict],
                 existing_by_pan_acct: Dict[Tuple[str, str], Dict],
                 pools_by_scheme: Optional[Dict[str, Dict]] = None,
                 drafts_by_pan_scheme: Optional[Dict[Tuple[str, str], Dict]] = None,
                 drafts_any_by_pan: Optional[Dict[str, Dict]] = None,
                 ) -> List[ClientDraft]:
    """Build drafts for every ClientDetail row.

    ``existing_by_pan_acct`` is a {(tax_pan_upper, ws_account_code):
    sqlite_row_dict} index of rows that ALREADY have a populated
    ws_account_code — these are the WS-canonical rows, exact-matched
    by (PAN, account_code).

    ``drafts_by_pan_scheme`` and ``drafts_any_by_pan`` (optional)
    capture DB rows whose ws_account_code is BLANK — i.e. CML-stage
    drafts waiting to be promoted with an account code. When the
    strict (PAN, account_code) lookup misses, fall back to the
    matching draft so the proposal becomes an UPDATE on the draft
    rather than a CREATE that would duplicate the row.

    Lookup priority for each ClientDetail row:
      1. exact (PAN, CLIENTCODE) — already-populated row
      2. (PAN, SCHEMENAME) draft  — same scheme, awaiting account code
      3. any draft on the PAN     — fallback when scheme didn't match
      4. None                     — create a new row

    ``pools_by_scheme`` (optional) maps lower-cased scheme_name to a
    pools_hub pool dict; when supplied, build_draft proposes
    pool_mapin + ws_scheme_code per scheme match.
    """
    drafts_by_pan_scheme = drafts_by_pan_scheme or {}
    drafts_any_by_pan    = drafts_any_by_pan    or {}
    drafts: List[ClientDraft] = []
    for row in client_detail_rows:
        pan = _str(row.get('H1PANNO')).upper()
        if not pan:
            continue
        acct   = _str(row.get('CLIENTCODE'))
        scheme = _str(row.get('SCHEMENAME')).lower()
        existing = existing_by_pan_acct.get((pan, acct))
        if existing is None and scheme:
            existing = drafts_by_pan_scheme.get((pan, scheme))
        if existing is None:
            existing = drafts_any_by_pan.get(pan)
        drafts.append(build_draft(row, existing, pools_by_scheme=pools_by_scheme))
    return drafts


# ── Apply (commit deltas to clients.db) ───────────────────────────────── #

def apply_drafts(approvals: List[Dict], db_path: Optional[Path] = None) -> Dict:
    """Apply approved deltas to data/clients.db.

    Each approval entry:
      {
        tax_pan:     'AQUPM9906H',
        mode:        'update' | 'create',
        existing_id: 42 | null,
        accept:      ['mobile', 'email', 'first_name', ...],
        proposed:    {sql_col: value, ...},
        overrides:   {sql_col: value, ...},   # optional operator edits
      }

    Returns ``{ok, applied: [{tax_pan, mode, action, id, changed}]}``.
    """
    import sqlite3
    from . import client_onboarding as _co
    if db_path is None:
        db_path = _co._db_path()
    _co.init_db()

    applied: List[Dict] = []

    def _diagnose_dp_conflict(conn, pan: str, patch: Dict[str, Any],
                                self_id: Optional[int]) -> Optional[Dict[str, Any]]:
        """When the partial UNIQUE index on (dp_id, dp_client_id) fires,
        find the row already holding this pair so the operator can see
        which existing record blocks the sync. Returns a small dict with
        the colliding row's id / PAN / account code, or None when the
        diagnostic itself fails."""
        try:
            dp_id_v = patch.get('dp_id')
            dp_cl_v = patch.get('dp_client_id')
            if not (dp_id_v and dp_cl_v):
                return None
            sql = ("SELECT id, tax_pan, ws_account_code, scheme_name "
                   "FROM clients WHERE dp_id = ? AND dp_client_id = ?")
            params: List[Any] = [dp_id_v, dp_cl_v]
            if self_id is not None:
                sql += " AND id <> ?"
                params.append(self_id)
            row = conn.execute(sql, params).fetchone()
            if not row:
                return None
            return {'id':              row['id'],
                    'tax_pan':         row['tax_pan'],
                    'ws_account_code': row['ws_account_code'],
                    'scheme_name':     row['scheme_name']}
        except Exception:
            return None

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for ap in approvals or []:
            pan       = (ap.get('tax_pan') or '').strip().upper()
            mode      = (ap.get('mode') or '').strip()
            accept    = ap.get('accept') or []
            proposed  = ap.get('proposed') or {}
            overrides = ap.get('overrides') or {}
            if not pan or not accept:
                applied.append({'tax_pan': pan, 'mode': mode,
                                  'action': 'skipped',
                                  'reason': 'pan or accept empty'})
                continue

            # Resolve the values to write.
            patch: Dict[str, Any] = {}
            for col in accept:
                if col in overrides:
                    patch[col] = overrides[col]
                elif col in proposed:
                    patch[col] = proposed[col]
            if not patch:
                applied.append({'tax_pan': pan, 'mode': mode,
                                  'action': 'skipped',
                                  'reason': 'patch resolved empty'})
                continue
            patch.setdefault('tax_pan', pan)

            # Resolve the row to update. Priority:
            #   1. existing_id from the scan — most reliable. This
            #      handles the case where the scan matched a draft row
            #      by (PAN, scheme) fallback and the proposed account
            #      code differs from the draft's current (blank) one.
            #      Without this, the (PAN, new_acct) lookup would miss
            #      and apply would INSERT a duplicate.
            #   2. (PAN, account_code) — the exact-row match.
            #   3. (PAN, blank) — any draft on the PAN.
            existing_id = ap.get('existing_id')
            existing = None
            if existing_id is not None:
                try:
                    existing = conn.execute(
                        "SELECT * FROM clients WHERE id = ?",
                        (int(existing_id),),
                    ).fetchone()
                except (TypeError, ValueError):
                    existing = None
            if existing is None:
                acct = (patch.get('ws_account_code') or '').strip()
                if acct:
                    existing = conn.execute(
                        "SELECT * FROM clients WHERE tax_pan = ? AND ws_account_code = ?",
                        (pan, acct),
                    ).fetchone()
                else:
                    existing = conn.execute(
                        "SELECT * FROM clients WHERE tax_pan = ? "
                        "AND (ws_account_code IS NULL OR ws_account_code = '')",
                        (pan,),
                    ).fetchone()

            if existing:
                # UPDATE — restrict to columns that exist in the table.
                cols = {c[1] for c in conn.execute(
                    "PRAGMA table_info(clients)").fetchall()}
                set_cols = [c for c in patch.keys() if c in cols and c != 'tax_pan']
                if not set_cols:
                    applied.append({'tax_pan': pan, 'mode': mode,
                                      'action': 'skipped',
                                      'reason': 'no valid columns in patch',
                                      'id': existing['id']})
                    continue
                stmt = ("UPDATE clients SET "
                        + ', '.join(f'{c}=?' for c in set_cols)
                        + ", updated_at = datetime('now') "
                        + "WHERE id = ?")
                try:
                    conn.execute(stmt, [patch[c] for c in set_cols] + [existing['id']])
                    conn.commit()
                except sqlite3.IntegrityError as ie:
                    # Most common failure: the partial UNIQUE index on
                    # (dp_id, dp_client_id) fires because another row
                    # already holds the pair we're trying to write
                    # (typically a stale duplicate from an earlier flow,
                    # or two rows for the same client across schemes).
                    # Skip this approval and surface enough diagnostic
                    # for the operator to merge / clean up by hand.
                    conn.rollback()
                    conflict = _diagnose_dp_conflict(
                        conn, pan,
                        {**existing, **{c: patch[c] for c in set_cols}},
                        existing['id'])
                    logger.warning(
                        f"client_backfill UPDATE skipped (id={existing['id']}, "
                        f"pan={pan}): {ie} — conflict with {conflict}")
                    applied.append({'tax_pan': pan, 'mode': mode,
                                      'action': 'skipped',
                                      'reason': f'integrity: {ie}',
                                      'id': existing['id'],
                                      'conflict': conflict})
                    continue
                applied.append({'tax_pan': pan, 'mode': 'update',
                                  'action': 'updated',
                                  'id': existing['id'],
                                  'changed': set_cols})
            else:
                # CREATE — insert a new row. tax_pan is the only field
                # required; all others are nullable.
                cols = {c[1] for c in conn.execute(
                    "PRAGMA table_info(clients)").fetchall()}
                ins_cols = [c for c in patch.keys() if c in cols]
                stmt = ("INSERT INTO clients ("
                        + ', '.join(ins_cols)
                        + ") VALUES ("
                        + ', '.join('?' * len(ins_cols))
                        + ")")
                try:
                    cur = conn.execute(stmt, [patch[c] for c in ins_cols])
                    conn.commit()
                except sqlite3.IntegrityError as ie:
                    conn.rollback()
                    conflict = _diagnose_dp_conflict(conn, pan, patch, None)
                    logger.warning(
                        f"client_backfill INSERT skipped (pan={pan}): {ie} "
                        f"— conflict with {conflict}")
                    applied.append({'tax_pan': pan, 'mode': mode,
                                      'action': 'skipped',
                                      'reason': f'integrity: {ie}',
                                      'conflict': conflict})
                    continue
                applied.append({'tax_pan': pan, 'mode': 'create',
                                  'action': 'created',
                                  'id': cur.lastrowid,
                                  'changed': ins_cols})

    return {'ok': True, 'applied': applied}


def auto_sync_from_xls(xls_path: Path,
                       db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Non-interactive sync: WealthSpectrum is the source of truth.

    Reads the supplied ClientDetail.xls, builds drafts against the
    current SQLite state, and applies every delta without operator
    review. The reasoning: per the WS-as-truth contract, every WS
    download is authoritative for the WS-tracked fields, so any drift
    between WS and the local DB is by definition stale local data.
    Keystone-only fields (fee_config, additional_pools' fee bits,
    drafts/notes) are NEVER touched here because build_draft only
    proposes deltas for the WS-mapped columns.

    Designed to be called both from the daily WS download workflow
    (auto-trigger) and from a manual "Sync from WS" button. Returns
    the same shape as apply_drafts so the UI can render a summary.
    """
    from . import client_onboarding as _co
    if db_path is None:
        db_path = _co._db_path()
    _co.init_db()

    xls_path = Path(xls_path)
    if not xls_path.exists():
        return {'ok': False, 'error': f'ClientDetail not found at {xls_path}'}

    rows = _read_xls_dicts(xls_path)
    if not rows:
        return {'ok': True, 'applied': [], 'meta': {
            'xls_path':   str(xls_path),
            'total_rows': 0,
            'note':       'ClientDetail is empty — nothing to sync.',
        }}

    # Mirror the indexing logic from /api/clients/backfill/scan so
    # CML-stage drafts get promoted rather than duplicated.
    existing_by_pan_acct: Dict[Tuple[str, str], Dict] = {}
    drafts_by_pan_scheme: Dict[Tuple[str, str], Dict] = {}
    drafts_any_by_pan:    Dict[str, Dict] = {}
    for c in _co.list_clients():
        pan = (c.get('tax_pan') or '').strip().upper()
        if not pan:
            continue
        acct = (c.get('ws_account_code') or '').strip()
        if acct:
            existing_by_pan_acct[(pan, acct)] = c
            continue
        scheme = (c.get('scheme_name') or '').strip().lower()
        if scheme:
            drafts_by_pan_scheme.setdefault((pan, scheme), c)
        drafts_any_by_pan.setdefault(pan, c)

    # Active-pools index keyed by lower-cased scheme name — drives the
    # pool_mapin / ws_scheme_code proposals on each draft.
    pools_by_scheme: Dict[str, Dict] = {}
    try:
        from .pools_hub import PoolsHub
        for p in PoolsHub.load().pools:
            if not p.get('active', True):
                continue
            sn = (p.get('scheme_name') or '').strip().lower()
            if sn and sn not in pools_by_scheme:
                pools_by_scheme[sn] = p
    except Exception as _e:
        logger.warning(f'pools_hub load for auto sync failed: {_e}')

    drafts = build_drafts(
        rows, existing_by_pan_acct,
        pools_by_scheme=pools_by_scheme,
        drafts_by_pan_scheme=drafts_by_pan_scheme,
        drafts_any_by_pan=drafts_any_by_pan,
    )

    # Auto-build approvals: accept every delta on every draft. apply_drafts
    # already short-circuits when accept is empty, so drafts that produced
    # no deltas are no-ops here.
    approvals: List[Dict[str, Any]] = []
    for d in drafts:
        if not d.deltas:
            continue
        proposed = {fd.field: fd.proposed for fd in d.deltas}
        approvals.append({
            'tax_pan':     d.tax_pan,
            'mode':        d.mode,
            'existing_id': d.existing_id,
            'accept':      list(proposed.keys()),
            'proposed':    proposed,
            'overrides':   {},
        })

    result = apply_drafts(approvals, db_path=db_path)
    applied_rows = result.get('applied', [])
    integrity_skips = [r for r in applied_rows
                        if r.get('action') == 'skipped'
                        and str(r.get('reason') or '').startswith('integrity')]
    result['meta'] = {
        'xls_path':       str(xls_path),
        'total_rows':     len(rows),
        'drafts_with_deltas': len(approvals),
        'updates':        sum(1 for r in applied_rows if r.get('action') == 'updated'),
        'creates':        sum(1 for r in applied_rows if r.get('action') == 'created'),
        'skipped':        sum(1 for r in applied_rows if r.get('action') == 'skipped'),
        'integrity_skipped': len(integrity_skips),
        'integrity_conflicts': [
            {'tax_pan': r.get('tax_pan'),
             'id':      r.get('id'),
             'reason':  r.get('reason'),
             'conflict': r.get('conflict')}
            for r in integrity_skips
        ],
    }
    return result
