"""
Client Onboarding — CML Parser + SQLite Registry + WealthSpectrum API
=======================================================================
Responsibilities:
  1. Parse NSDL CML PDFs (HDFC / other custodians) and extract all
     available fields mapped to the WS ClientMaster API schema.
  2. Persist client records in a SQLite database (data/clients.db).
  3. Push new client records to WealthSpectrum via the REST API and
     update the DB with the returned refNumber.

WS API endpoints (base = http://app.thegoldstandard.in/app):
  GET  /api/create/token?panno=PAN&id=clientId   → Bearer token
  POST /api/clientMaster/create/account           → create (returns refNumber)
  GET  /api/clientMaster/authorize/account?refNumber=N → authorize
  GET  /api/clientMaster/delete/account?refNumber=N   → delete
"""

from __future__ import annotations

import json
import re
import sqlite3
import logging
import requests
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

WS_BASE_DEFAULT = "http://recon.thegoldstandard.in/app"

def _ws_base() -> str:
    """Return WS base URL from env var, then azure.json ws_base_url, then default."""
    import os
    env_base = os.environ.get('WS_BASE_URL', '').strip()
    if env_base:
        return env_base.rstrip('/')
    try:
        import json
        _cfg_dir = os.environ.get('KEYSTONE_CONFIG_DIR')
        if _cfg_dir:
            cfg_path = Path(_cfg_dir) / 'azure.json'
        else:
            cfg_path = Path(__file__).parent.parent / 'config' / 'azure.json'
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            base = cfg.get('ws_base_url', '').strip()
            if base:
                return base.rstrip('/')
    except Exception:
        pass
    return WS_BASE_DEFAULT

# ── DB path ──────────────────────────────────────────────────────────────── #

def _db_path() -> Path:
    # Reverted to the original module-relative path. Honouring
    # KEYSTONE_DATA_DIR here looked consistent with the rest of the
    # module, but production has always stored clients.db at
    # ${APP_DIR}/data/clients.db (wwwroot on Azure), separate from
    # the date-organised data at ${KEYSTONE_DATA_DIR}/data/{date}/.
    # Routing reads to the env-aware path on a deploy where the
    # env var pointed elsewhere caused the app to silently switch
    # to a fresh empty file while the real DB sat untouched.
    # Future test isolation should mock _db_path directly rather
    # than via env var.
    p = Path(__file__).parent.parent / 'data' / 'clients.db'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ── Schema ───────────────────────────────────────────────────────────────── #

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    -- DP / custody identifiers
    dp_id                  TEXT,
    dp_client_id           TEXT,
    -- WS tracking
    ws_ref_number          INTEGER,
    ws_account_code        TEXT,
    ws_authorized          INTEGER DEFAULT 0,
    ws_push_date           TEXT,
    ws_authorize_date      TEXT,
    -- Group
    group_short_name       TEXT,
    group_name             TEXT,
    group_contact_name     TEXT,
    -- First holder
    salutation             TEXT,
    first_name             TEXT,
    middle_name            TEXT,
    last_name              TEXT,
    short_name             TEXT,
    tax_pan                TEXT,
    birth_date             TEXT,
    mobile                 TEXT,
    phone_home             TEXT,
    email                  TEXT,
    father_husband         TEXT,
    gender                 TEXT,
    occupation             TEXT,
    client_status          TEXT,
    taxable                TEXT DEFAULT 'Y',
    -- Address
    address1               TEXT,
    address2               TEXT,
    city                   TEXT,
    state                  TEXT,
    pin_code               TEXT,
    country                TEXT DEFAULT 'India',
    nationality            TEXT DEFAULT 'Indian',
    -- KYC
    aadhar                 TEXT,
    ckyc                   TEXT,
    fatca                  TEXT DEFAULT 'N',
    ubo                    TEXT DEFAULT 'N',
    -- Bank mandate (first holder)
    mandate_bank_name      TEXT,
    mandate_bank_branch    TEXT,
    mandate_bank_actype    TEXT,
    mandate_bank_acno      TEXT,
    mandate_bank_micr      TEXT,
    mandate_bank_neft      TEXT,
    mandate_bank_rtgs      TEXT,
    -- DP/custody mandate (first holder)
    mandate_dep            TEXT DEFAULT 'NSDL',
    mandate_dpid           TEXT,
    mandate_dp_clientid    TEXT,
    -- Trading account
    trading_bank_code      TEXT,
    trading_bank_acno      TEXT,
    trading_dep            TEXT DEFAULT 'NSDL',
    trading_dpid           TEXT,
    trading_dp_clientid    TEXT,
    -- Second holder
    h2_name                TEXT,
    tax_h2_pan             TEXT,
    h2_relation            TEXT,
    h2_birth_date          TEXT,
    h2_gender              TEXT,
    h2_father_husband      TEXT,
    h2_mobile              TEXT,
    h2_email               TEXT,
    -- H2 bank mandate
    mandate_h2_bank_name   TEXT,
    mandate_h2_bank_branch TEXT,
    mandate_h2_bank_acno   TEXT,
    mandate_h2_bank_actype TEXT,
    mandate_h2_bank_micr   TEXT,
    mandate_h2_bank_neft   TEXT,
    mandate_h2_bank_rtgs   TEXT,
    mandate_h2_dep         TEXT,
    mandate_h2_dpid        TEXT,
    mandate_h2_dp_clientid TEXT,
    -- WS config (filled by user)
    user_name              TEXT,
    scheme_name            TEXT,
    pool_mapin             TEXT,
    rm_user_name           TEXT,
    advisor_user_name      TEXT,
    intermediary_user_name TEXT,
    firm_code              TEXT,
    branch_name            TEXT,
    inception_date         TEXT,
    maturity_date          TEXT,
    account_type           TEXT DEFAULT 'P',
    bill_group             TEXT,
    mapin                  TEXT,
    commit_amount          TEXT DEFAULT '5000000',
    account_operation_type TEXT DEFAULT 'D',
    accounting_txn         TEXT DEFAULT 'CS',
    stt_taken_as           TEXT DEFAULT 'E',
    mode_of_holding        TEXT DEFAULT 'S',
    auto_mail              TEXT DEFAULT 'Y',
    family_head            TEXT DEFAULT 'Y',
    share_report           TEXT DEFAULT 'P',
    -- Mailing address (if different)
    mail_address1          TEXT,
    mail_address2          TEXT,
    mail_address_city      TEXT,
    mail_address_state     TEXT,
    mail_address_pin_code  TEXT,
    -- Metadata
    cml_source_file        TEXT,
    created_at             TEXT DEFAULT (datetime('now')),
    updated_at             TEXT DEFAULT (datetime('now')),
    -- Maker-checker workflow
    pending_auth           INTEGER DEFAULT 0,
    creator_token          TEXT,
    auth_token             TEXT,
    pending_recipients     TEXT,
    submitted_at           TEXT,
    auth_notes             TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_dp
    ON clients(dp_id, dp_client_id)
    WHERE dp_id IS NOT NULL AND dp_client_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_clients_pan ON clients(tax_pan);
CREATE INDEX IF NOT EXISTS idx_clients_ws_ref ON clients(ws_ref_number);
"""


def init_db():
    """Create DB and tables if they don't exist. Also runs any pending migrations."""
    with sqlite3.connect(_db_path()) as conn:
        conn.executescript(SCHEMA)
        # ── Schema migrations (safe to run on every startup) ──
        # Adds columns introduced in session 7d that won't exist on DBs
        # created before that session. ALTER TABLE IF NOT EXISTS is not
        # supported in SQLite < 3.37 so we use try/except per column.
        _migrations = [
            "ALTER TABLE clients ADD COLUMN pending_auth       INTEGER DEFAULT 0",
            "ALTER TABLE clients ADD COLUMN creator_token      TEXT",
            "ALTER TABLE clients ADD COLUMN auth_token         TEXT",
            "ALTER TABLE clients ADD COLUMN pending_recipients TEXT",
            "ALTER TABLE clients ADD COLUMN submitted_at       TEXT",
            "ALTER TABLE clients ADD COLUMN auth_notes         TEXT",
            "ALTER TABLE clients ADD COLUMN user_name          TEXT",
            "ALTER TABLE clients ADD COLUMN arn                TEXT",
            "ALTER TABLE clients ADD COLUMN txn_fee_taken_as   TEXT",
            # Nominee fields (WS template cols 134-142, 151-152)
            "ALTER TABLE clients ADD COLUMN nominee_name       TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_relation   TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_address1   TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_address2   TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_city       TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_state      TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_pin_code   TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_phone      TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_fax        TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_dob        TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_pan        TEXT",
            # Nominee's guardian (for minor nominees) — WS cols 143-150
            "ALTER TABLE clients ADD COLUMN nom_guardian_name     TEXT",
            "ALTER TABLE clients ADD COLUMN nom_guardian_address1 TEXT",
            "ALTER TABLE clients ADD COLUMN nom_guardian_address2 TEXT",
            "ALTER TABLE clients ADD COLUMN nom_guardian_city     TEXT",
            "ALTER TABLE clients ADD COLUMN nom_guardian_state    TEXT",
            "ALTER TABLE clients ADD COLUMN nom_guardian_pin_code TEXT",
            "ALTER TABLE clients ADD COLUMN nom_guardian_phone    TEXT",
            "ALTER TABLE clients ADD COLUMN nom_guardian_fax      TEXT",
            # Nominee guardian KYC — WS cols 153-158
            "ALTER TABLE clients ADD COLUMN ng_dob              TEXT",
            "ALTER TABLE clients ADD COLUMN ng_pan              TEXT",
            "ALTER TABLE clients ADD COLUMN ng_aadhar           TEXT",
            "ALTER TABLE clients ADD COLUMN ng_ckyc             TEXT",
            "ALTER TABLE clients ADD COLUMN ng_fatca            TEXT",
            "ALTER TABLE clients ADD COLUMN ng_ubo              TEXT",
            # Additional pools — JSON array of {ws_account_code, scheme_name,
            # pool_mapin, ws_scheme_code} for clients joining more than one
            # pool in a single onboarding.
            "ALTER TABLE clients ADD COLUMN additional_pools    TEXT",
            # ClientDetail.xls backfill — WS exposes per-client AUM and a
            # few identifiers that didn't have a home in the registry.
            # Store them so the client list / future holdings recon can
            # look them up by tax_pan without round-tripping to WS.
            "ALTER TABLE clients ADD COLUMN ws_client_id        TEXT",
            "ALTER TABLE clients ADD COLUMN ws_client_code      TEXT",
            "ALTER TABLE clients ADD COLUMN ws_group_id         TEXT",
            "ALTER TABLE clients ADD COLUMN broker_acid         TEXT",
            "ALTER TABLE clients ADD COLUMN trxn_taken_as       TEXT",
            "ALTER TABLE clients ADD COLUMN assets              REAL",
            "ALTER TABLE clients ADD COLUMN net_capital         REAL",
            # Fee configuration — JSON {breakup_periods: [{effective_from,
            # share_gsw_pct, share_fm_pct, share_distributor_pct,
            # share_residual_pct, fm_user_name, distributor_user_name,
            # residual_user_name}, ...]}. Time-varying breakup; each
            # entry's effective_from anchors to a CBG-master FLATFEE
            # change-date. Headline rate is NOT stored here — sourced
            # from CBG master FLATFEE at compute time. One column per
            # pool: this is the primary pool; additional_pools entries
            # carry their own fee_config. Legacy flat-shape rows
            # ({headline_fee_pct, share_*_pct, ..._user_name} at the top
            # level) are auto-migrated to a single-period
            # breakup_periods entry at compute time via
            # core.fee_calculator.normalize_fee_config.
            "ALTER TABLE clients ADD COLUMN fee_config          TEXT",
            # Second holder address + KYC — operator-requested structure
            # mirroring the first holder's Address & KYC fields. Demat
            # for the second holder lives on the existing mandate_h2_*
            # columns (jointly held under the first holder's DP record).
            # Not all WS XLS columns map cleanly today — these may end
            # up Keystone-local until a future WS template upgrade.
            "ALTER TABLE clients ADD COLUMN h2_address1         TEXT",
            "ALTER TABLE clients ADD COLUMN h2_address2         TEXT",
            "ALTER TABLE clients ADD COLUMN h2_city             TEXT",
            "ALTER TABLE clients ADD COLUMN h2_state            TEXT",
            "ALTER TABLE clients ADD COLUMN h2_pin_code         TEXT",
            "ALTER TABLE clients ADD COLUMN h2_aadhar           TEXT",
            "ALTER TABLE clients ADD COLUMN h2_ckyc             TEXT",
            "ALTER TABLE clients ADD COLUMN h2_fatca            TEXT",
            "ALTER TABLE clients ADD COLUMN h2_ubo              TEXT",
            # Renamed nominee fields — replace nominee_phone (legacy
            # alias) with nominee_mobile to match the form label, plus
            # a dedicated nominee_email + nominee_country + a renamed
            # nominee_relationship (was nominee_relation). The parser
            # writes both old and new keys for backward compatibility,
            # so existing rows aren't disturbed.
            "ALTER TABLE clients ADD COLUMN nominee_mobile       TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_email        TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_country      TEXT",
            "ALTER TABLE clients ADD COLUMN nominee_relationship TEXT",
            # Group lookups by (PAN, ws_account_code) — one PAN can
            # have many accounts; this powers fast account-level
            # match in apply_drafts and the registry's PAN-grouped
            # display. Not UNIQUE because multiple legacy rows may
            # share a PAN with empty ws_account_code (CML uploads
            # before WS push).
            "CREATE INDEX IF NOT EXISTS idx_clients_pan_acct "
            "ON clients(tax_pan, ws_account_code)",
        ]
        for stmt in _migrations:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists — safe to ignore

        # ── One-shot data migrations, gated on PRAGMA user_version ──
        # v1: realign account_type / account_operation_type defaults
        # with the form. The original schema DEFAULTed these to 'S' /
        # 'A', but the form (and operator workflow) treats Pool /
        # Discretionary as the norm. Existing rows with the old
        # defaults were never operator-chosen — they came from
        # schema-level fall-through at INSERT — so flipping them to
        # 'P' / 'D' restores the intent. Operators who actively
        # picked 'S' / 'A' after this point are protected by the
        # user_version guard (the migration only runs once).
        ver = conn.execute('PRAGMA user_version').fetchone()[0]
        if ver < 1:
            conn.execute(
                "UPDATE clients SET account_type='P' WHERE account_type='S'"
            )
            conn.execute(
                "UPDATE clients SET account_operation_type='D' "
                "WHERE account_operation_type='A'"
            )
            conn.execute('PRAGMA user_version=1')

        # v2: NEUTRALISED. The original v2 housekeeping migration
        # purged orphan draft rows based only on (scheme_name,
        # ws_account_code) presence, ignoring all other operator
        # data on those rows. In practice many production rows had
        # populated names/address/KYC but no scheme yet, and were
        # incorrectly purged when a sibling row had a scheme.
        # Recovery for already-affected DBs lives in the
        # _clients_purged backup table that the original v2 wrote
        # before deletes. Run scripts/recover_v2_purged.py (or the
        # one-liner in the deploy notes) to restore.
        # The user_version bump stays so DBs already at 2 don't get
        # surprised by a re-run, and fresh DBs jump straight past
        # the dangerous block.
        if ver < 2:
            conn.execute('PRAGMA user_version=2')

        conn.commit()
    logger.info("clients.db initialised")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


# ── CML PDF Parser ───────────────────────────────────────────────────────── #

def _clean(s) -> str:
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def _find(text: str, pattern: str, group: int = 1, flags=re.IGNORECASE) -> str:
    m = re.search(pattern, text, flags)
    return _clean(m.group(group)) if m else ''


def _parse_date_to_iso(d: str) -> str:
    """Convert DD-MM-YYYY or DD/MM/YYYY to YYYY-MM-DD."""
    d = d.strip()
    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(d, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return d


# Tokens (whole-word) in a holder name that mark it as a non-individual
# entity. Matched case-insensitively after stripping punctuation. Order
# in _classify_non_individual matters — LLP must beat the more generic
# 'PARTNERS' suffix so an LLP doesn't get tagged as 'Partnership firm'.
_NON_INDIVIDUAL_TOKENS = {
    'LLP', 'LTD', 'LIMITED', 'PVT', 'PRIVATE', 'INC', 'CORP', 'CORPORATION',
    'COMPANY', 'CO', 'COMPANIES',
    'TRUST', 'FOUNDATION', 'SOCIETY', 'ASSOCIATION',
    'PARTNERS', 'PARTNERSHIP', 'ASSOCIATES',
    'HUF',
    'FUND', 'FUNDS', 'CAPITAL', 'INVESTMENTS', 'HOLDINGS',
    'BANK', 'INSURANCE',
    'AOP',  # Association of Persons
}


def _is_non_individual_name(name: str) -> bool:
    """True if the holder name carries a corporate / trust / partnership
    suffix. Splits on whitespace, strips trailing punctuation, and looks
    for any token in ``_NON_INDIVIDUAL_TOKENS``. False for plain personal
    names with no entity marker."""
    if not name:
        return False
    tokens = re.findall(r'[A-Za-z]+', name.upper())
    return any(t in _NON_INDIVIDUAL_TOKENS for t in tokens)


def _classify_non_individual(name: str) -> str:
    """Map a non-individual entity name to its WS-canonical client_status
    value (the H1STATUS column in ClientDetail.xls). Order matters —
    LLP wins over the broader 'Partners'; HUF wins over 'Trust' which
    wins over the generic 'Company' / 'Body Corporate' fallback. The
    operator can always edit the field on the form."""
    up = ' ' + name.upper().replace('.', ' ').replace(',', ' ') + ' '
    if ' LLP ' in up:
        return 'LLP'
    if ' HUF ' in up:
        return 'HUF'
    if ' TRUST ' in up or ' FOUNDATION ' in up or ' SOCIETY ' in up:
        return 'Trust'
    if ' PARTNERS ' in up or ' PARTNERSHIP ' in up:
        return 'Partnership firm'
    if (' LTD ' in up or ' LIMITED ' in up or ' PVT ' in up or
            ' PRIVATE ' in up or ' INC ' in up or ' CORP ' in up or
            ' CORPORATION ' in up or ' COMPANY ' in up):
        return 'Company'
    return 'Body Corporate'


_BANK_NAME_HEADS = (
    'ICICI BANK', 'HDFC BANK', 'AXIS BANK', 'KOTAK MAHINDRA',
    'KOTAK BANK', 'STATE BANK', 'SBI', 'YES BANK', 'INDUSIND',
    'CITI', 'STANDARD CHARTERED', 'DEUTSCHE', 'HSBC',
    'BNP PARIBAS', 'BARCLAYS', 'IDBI', 'IDFC',
    'BANK OF INDIA', 'BANK OF BARODA', 'PUNJAB NATIONAL',
    'CANARA BANK', 'UNION BANK', 'BANDHAN BANK', 'RBL',
)


def _looks_like_bank_address(line: str) -> bool:
    """True if the candidate line looks like a bank's correspondence
    address (e.g. starts with an Indian bank name) rather than a
    client's residence. Used to detect ICICI-style CMLs where the
    bank's address sits in the LEFT 'Address' column and the
    client's actual home is in the RIGHT 'Other Address' column —
    we want to flip the columns for those CMLs so address1 / 2 /
    city / state / pin reflect the human's residence, not the
    bank's mailing room."""
    if not line:
        return False
    up = line.upper().lstrip()
    head = up[:80]
    if any(up.startswith(b) for b in _BANK_NAME_HEADS):
        return True
    if 'BANK LTD' in head or 'BANK LIMITED' in head:
        return True
    return False


def _extract_addresses_from_pdf(pdf_path: str) -> tuple:
    """Extract (address1, address2) from a CML page-1 using word coordinates.

    NSDL CML prints 'Address' (primary) and 'Other Address' side-by-side —
    the flat-text extraction collapses both columns onto the same line,
    which is why regex-based parsing fails. Word-level x/y coordinates
    let us isolate each column.

    Column-pick rule (added 2026-05-07):
      - Default to the LEFT 'Address' column (matches HDFC / Axis / Kotak
        CMLs where the holder's residence sits there).
      - If the LEFT column's first line looks like a bank correspondence
        address (starts with an Indian bank name — see
        ``_looks_like_bank_address``), swap to the RIGHT 'Other Address'
        column. ICICI's NSDL CML layout puts the bank's address in
        the LEFT column and the client's home in the RIGHT, which is
        the opposite of what every other custodian sends.

    Returns ('', '') on any error so callers can fall back.
    """
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            words = pdf.pages[0].extract_words(x_tolerance=2, y_tolerance=3)
    except Exception as e:
        logger.warning(f'CML word-level address extraction failed: {e}')
        return '', ''

    # Locate the 'Address' label on page 1
    addr_label = next(
        (w for w in words
         if w['text'].lower() == 'address' and w['top'] < 500),
        None,
    )
    if not addr_label:
        return '', ''
    addr_y = addr_label['top']

    # Column split — use 'Other Address' label's x as boundary, else x=400
    other_label = next(
        (w for w in words
         if w['text'].lower() == 'other'
         and abs(w['top'] - addr_y) < 3),
        None,
    )
    boundary = other_label['x0'] if other_label else 400.0

    # Bucket every word in the address band into LEFT or RIGHT column
    # rows so we can pick whichever column carries the human's address.
    left_rows:  dict = {}
    right_rows: dict = {}
    for w in words:
        if w['top'] < addr_y - 1 or w['top'] > addr_y + 35:
            continue
        if w['text'].lower() in ('address', 'other'):
            continue
        yb = round(w['top'])
        if w['x0'] < boundary:
            left_rows.setdefault(yb, []).append(w)
        else:
            right_rows.setdefault(yb, []).append(w)

    def _row_lines(rows: dict, max_lines: int = 2) -> list:
        out: list = []
        for yb in sorted(rows):
            row_words = sorted(rows[yb], key=lambda w: w['x0'])
            txt = ' '.join(w['text'] for w in row_words).strip()
            # Defensive trim — address lines often end with a stray
            # comma from the CML layout, and some WS picklists reject
            # it as malformed.
            txt = txt.rstrip(',').strip()
            if txt:
                out.append(txt)
            if len(out) == max_lines:
                break
        return out

    left  = _row_lines(left_rows)
    right = _row_lines(right_rows)

    # Content-based column flip — if the left column reads as a bank
    # mailing address, the holder's residence is in the right column.
    chosen = left
    flipped = False
    if left and _looks_like_bank_address(left[0]) and right:
        chosen = right
        flipped = True
        logger.info('CML address: left column looked like a bank '
                    'mailing address — using right (Other Address) column.')

    # Caller passes ``flipped`` back through the regex-on-full-text
    # path so the SECOND 'pin code XXXXXX' / 'State XXX' match in
    # document order is picked instead of the first (which on an
    # ICICI-flipped CML is the bank's, not the client's).
    return (
        chosen[0] if chosen else '',
        chosen[1] if len(chosen) > 1 else '',
        flipped,
    )


# Multi-word Indian states / UTs — used by both the holder address
# parser and the nominee address parser to avoid the 'Andhra Pradesh'
# truncation bug where a naive last-word split returns just 'Pradesh'.
_TWO_WORD_STATES = {
    'ANDHRA PRADESH':    'Andhra Pradesh',
    'ARUNACHAL PRADESH': 'Arunachal Pradesh',
    'HIMACHAL PRADESH':  'Himachal Pradesh',
    'MADHYA PRADESH':    'Madhya Pradesh',
    'UTTAR PRADESH':     'Uttar Pradesh',
    'TAMIL NADU':        'Tamil Nadu',
    'WEST BENGAL':       'West Bengal',
    'JAMMU AND KASHMIR': 'Jammu and Kashmir',
}


def _normalise_state(raw: str) -> str:
    """Title-case + multi-word-state-aware. ``raw`` may be a single
    token ('PRADESH') or two-token ('ANDHRA PRADESH'); when the
    suffix matches a known two-word state, return the canonical
    casing. Otherwise just .title() the input."""
    up = raw.upper().strip()
    for needle, canonical in _TWO_WORD_STATES.items():
        if needle in up:
            return canonical
    return raw.title()


def _split_nominee_address(raw: str) -> tuple:
    """Split ``LINE1, LINE2, ..., LINEN, CITY STATE`` into
    (address1, address2, city, state).

    Heuristics:
      - Comma chunks → address lines; the trailing chunk is ``CITY STATE``.
      - Multi-word state names ('Andhra Pradesh', 'Tamil Nadu', 'Uttar
        Pradesh', 'West Bengal', etc.) are detected against
        ``_TWO_WORD_STATES`` first, so 'CHITTOOR ANDHRA PRADESH'
        correctly splits into city='Chittoor' / state='Andhra Pradesh'
        rather than the previous naive last-word split which gave
        city='Chittoor Andhra' / state='Pradesh'.
      - Single-word state fallback when no multi-word match.

    Safe on single-line inputs (returns addr1 only). Blank input returns
    four empty strings.
    """
    if not raw:
        return '', '', '', ''
    parts = [p.strip() for p in raw.split(',') if p.strip()]
    if not parts:
        return '', '', '', ''
    tail = parts[-1].strip()
    tail_up = tail.upper()
    city, state = '', ''
    # Try multi-word state suffix first.
    matched = False
    for needle, canonical in _TWO_WORD_STATES.items():
        if tail_up.endswith(needle):
            state = canonical
            city  = tail[: -len(needle)].strip()
            matched = True
            break
    if not matched:
        # Single-word fallback.
        toks = tail.split()
        if len(toks) >= 2:
            city, state = ' '.join(toks[:-1]), toks[-1]
        elif toks:
            city, state = '', toks[0]
    body = parts[:-1] if (city or state) else parts
    addr1 = body[0] if body else ''
    addr2 = ', '.join(body[1:]) if len(body) > 1 else ''
    return addr1, addr2, city.title(), state.title() if not matched else state


def _extract_nominee_section(text: str) -> dict:
    """Extract nominee + guardian fields from the CML text blob.

    Looks for the 'First Nominee Details :' block (through end-of-report
    or next major section) and extracts:
      nominee: name, PAN (blank for minors), DOB, address, city/state/pin
      guardian: name, address, city/state/pin  (only if nominee is a minor
                or an explicit guardian block is present)

    Returns {} when no nominee details are found. WS's NGPAN/NGDOB/
    NGAADHAR/NGCKYC/NGFATCA/NGUBO fields are intentionally left unset —
    CMLs don't carry guardian KYC and the operator supplies them.
    """
    out: dict = {}

    nom_m = re.search(
        r'First\s+Nominee\s+Details\s*:?\s*(.+?)'
        r'(?:First\s+Nominee\s+Guardian\s+Details|POA/?DDPI|\*\*\*\s+End)',
        text, re.DOTALL | re.IGNORECASE,
    )
    if not nom_m:
        return out
    nom_text = nom_m.group(1)

    # Name + PAN — DOB is on the same logical line in NSDL CMLs but is
    # frequently EMPTY (the column header text "Minor Nominee DOB"
    # appears even when the nominee is an adult and no DOB is printed).
    # Split into two passes: name + PAN first (always present), then
    # DOB as a separate optional capture.
    is_minor = False
    nd = re.search(
        r'Nominee\s+Name\s+(.+?)\s+PAN\s+([A-Z]{5}\d{4}[A-Z]|Minor\s+Nominee)',
        nom_text, re.IGNORECASE,
    )
    if nd:
        nname, pan_or_minor = nd.groups()
        out['nominee_name'] = _clean(nname)
        pan_or_minor = _clean(pan_or_minor)
        if re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pan_or_minor):
            out['nominee_pan'] = pan_or_minor
        elif 'minor' in pan_or_minor.lower():
            is_minor = True
    # DOB is optional — present only for minor nominees on most CMLs.
    # Match either after the explicit "DOB" label, or as the standalone
    # date that sometimes trails the Name+PAN line.
    dob_m = re.search(
        r'(?:Minor\s+Nominee\s+)?DOB\s+(\d{2}-\d{2}-\d{4})',
        nom_text, re.IGNORECASE,
    )
    if dob_m:
        out['nominee_dob'] = _parse_date_to_iso(dob_m.group(1))

    addr = re.search(
        r'Nominee\s+Address\s+(.+?)\s+Pin\s+Code\s+(\d{6})',
        nom_text, re.DOTALL | re.IGNORECASE,
    )
    if addr:
        raw_addr = _clean(addr.group(1))
        a1, a2, city, state = _split_nominee_address(raw_addr)
        out['nominee_address1'] = a1
        out['nominee_address2'] = a2
        out['nominee_city']     = city
        out['nominee_state']    = state
        out['nominee_pin_code'] = addr.group(2)

    # Country + State are listed on a separate line under the address —
    # use them as the canonical source instead of inferring from the
    # address1/2 split (which collapsed CMLs into the wrong fields when
    # the city was buried inside the address text).
    cs = re.search(r'Country\s+([A-Za-z ]+?)\s+State\s+([A-Z][A-Z ]+?)(?:\s+Passport|\s+Driving|\n|$)',
                    nom_text, re.IGNORECASE)
    if cs:
        out['nominee_country'] = _clean(cs.group(1)).title()
        # State name often comes through truncated ("MAHARASHTR" sans the
        # trailing A from a column-overflow). Fix the common case.
        st = _clean(cs.group(2))
        if st.upper().startswith('MAHARASHTR'):
            st = 'Maharashtra'
        else:
            st = st.title()
        out['nominee_state'] = st

    # City extraction — when the dedicated City line is absent (NSDL
    # CMLs put City as the last token of Nominee Address before Pin
    # Code), pull the city from the trailing token of the address.
    if addr and not out.get('nominee_city'):
        # Address ends with ", <City>" or " <CITY>" — last comma-separated
        # token in the cleaned address is the city. Standardise to title
        # case + strip punctuation.
        addr_clean = re.sub(r'\s+', ' ', addr.group(1).strip())
        # Pick the LAST capitalised token-group that's followed by Pin Code.
        # Common Indian cities are short (1-2 words). Take everything after
        # the last comma as the city candidate.
        if ',' in addr_clean:
            tail = addr_clean.rsplit(',', 1)[1].strip()
            # Remove anything that's clearly part of an address (numbers,
            # building tags). City is plain alpha.
            if re.match(r'^[A-Z][A-Z ]+$', tail):
                out['nominee_city'] = tail.title()

    # Email — labelled "Email ID" in the nominee block.
    em_m = re.search(r'Email\s*ID\s+([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})',
                     nom_text, re.IGNORECASE)
    if em_m:
        out['nominee_email'] = em_m.group(1).lower()

    # Mobile — same shape as the holder Mobile/Phone label.
    phone_m = re.search(r'Mobile\s*/\s*Telephone\s+No\.?\s*(?:\+?91\s*)?(\d{10})',
                        nom_text, re.IGNORECASE)
    if phone_m:
        out['nominee_mobile'] = phone_m.group(1)
        # Keep the legacy alias too so any caller still using
        # nominee_phone keeps working.
        out['nominee_phone']  = phone_m.group(1)

    # Relationship with Applicant — free-text label until the next column.
    rel_m = re.search(
        r'Relationship\s+with\s+Applicant\s+([A-Za-z][A-Za-z ]{2,30}?)\s+'
        r'(?:Mobile|Email|\n|$)',
        nom_text, re.IGNORECASE,
    )
    if rel_m:
        rel = _clean(rel_m.group(1)).title()
        out['nominee_relationship'] = rel
        # Backward-compat alias: the legacy key was 'nominee_relation';
        # writing both keys means existing edit / save paths that still
        # reference the old name keep working through the migration.
        out['nominee_relation']     = rel

    # Guardian block (only present for minor nominees)
    gm = re.search(
        r'First\s+Nominee\s+Guardian\s+Details\s*:?\s*(.+?)'
        r'(?:POA/?DDPI|\*\*\*\s+End)',
        text, re.DOTALL | re.IGNORECASE,
    )
    if gm:
        g_text = gm.group(1)
        gn = re.search(
            r'Nominee\s+Guardian\s+Name\s+(.+?)\s+Pin\s+Code\s+(\d{6})',
            g_text, re.IGNORECASE,
        )
        if gn:
            out['nom_guardian_name']     = _clean(gn.group(1))
            out['nom_guardian_pin_code'] = gn.group(2)
        ga = re.search(
            r'Nominee\s+Guardian\s+Address\s+(.+?)\s+'
            r'(?:Mobile|Email|$)',
            g_text, re.DOTALL | re.IGNORECASE,
        )
        if ga:
            a1, a2, city, state = _split_nominee_address(_clean(ga.group(1)))
            out['nom_guardian_address1'] = a1
            out['nom_guardian_address2'] = a2
            if city:
                out['nom_guardian_city']  = city
            if state:
                out['nom_guardian_state'] = state
        gp = re.search(r'Mobile\s*/\s*Telephone\s+No\.?\s*(?:\+?91\s*)?(\d{10})',
                       g_text, re.IGNORECASE)
        if gp:
            out['nom_guardian_phone'] = gp.group(1)

    return out


def parse_cml_pdf(pdf_path: str) -> dict:
    """
    Extract all available fields from an NSDL CML PDF.
    Handles pdfplumber layout quirks:
      - Mobile: "+91" on one line, 10-digit number on next
      - Email: domain split across lines ("@GMAI" + "L.COM")
    """
    import pdfplumber

    raw_pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text(x_tolerance=3, y_tolerance=3) or ''
            raw_pages.append(t)

    raw_text = '\n'.join(raw_pages)

    # Repair split lines where the PDF breaks mobile + email across lines.
    # Two patterns observed in NSDL HDFC CML:
    #
    # Pattern A (H1): "+91 Email Id PARTIAL@GMAI"  then  "9833130707 L.COM"
    #   → mobile is FIRST token of next line; email cont is SECOND token
    # Pattern B (H2): "Mobile No +91 Email Id FULL@GMAIL.COM"  then  "9820009944"
    #   → email is complete on current line; mobile is on next line alone
    lines = raw_text.splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Pattern A: partial email at end of line, next line = "MOBILE EMAIL_CONT".
        # The domain class includes '.' and '-' so the regex can ride over
        # split points like "...@GMAIL.CO" / "M" as well as the older
        # "...@GMAI" / "L.COM" pattern. End-anchored so we only fire on
        # genuinely truncated emails.
        partial = re.search(r'([A-Z0-9._%+\-]+@[A-Z0-9.\-]+)$', line, re.IGNORECASE)
        if partial and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            tokens = nxt.split()
            # Next line starts with 10-digit mobile, second token is email continuation
            if tokens and re.match(r'^\d{10}$', tokens[0]):
                mob10 = tokens[0]
                email_cont = tokens[1] if len(tokens) > 1 else ''
                rest = ' '.join(tokens[2:]) if len(tokens) > 2 else ''
                # Inject mobile into current line just before the partial email
                full_email = partial.group(1) + email_cont
                line = line[:partial.start()] + '+91' + mob10 + ' Email Id ' + full_email
                if rest:
                    line = line + ' ' + rest
                i += 2
                result.append(line)
                continue
            # Fallback: next line starts directly with email continuation
            if tokens and re.match(r'^[A-Z0-9.]+\.[A-Z]{2,}', tokens[0], re.IGNORECASE):
                cont = tokens[0]
                rest = ' '.join(tokens[1:])
                line = line[:partial.start()] + partial.group(1) + cont
                if rest:
                    line = line + ' ' + rest
                i += 2
                result.append(line)
                continue
            # Second fallback: next line starts with a single-letter
            # email continuation followed by junk. e.g. our test CML has
            # "...@GMAIL.CO" then "M" (single char + nothing else, OR
            # 'M' followed by "Family Flag..." prose). Treat any leading
            # A-Z token of length 1-3 as the email continuation.
            if (tokens and re.match(r'^[A-Z]{1,3}$', tokens[0], re.IGNORECASE)
                    and partial.group(1).count('.') >= 1):
                cont = tokens[0]
                rest = ' '.join(tokens[1:])
                line = line[:partial.start()] + partial.group(1) + cont
                if rest:
                    line = line + ' ' + rest
                i += 2
                result.append(line)
                continue

        # Pattern B: complete line, but mobile digits on next line alone
        if re.search(r'\+91\s*$', line) and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if re.match(r'^\d{10}', nxt):
                mob10 = re.match(r'^(\d{10})', nxt).group(1)
                line = line + mob10
                rest = nxt[10:].strip()
                lines[i + 1] = rest  # put remainder back for next iteration
                # don't skip i+1 — it may have more content
        result.append(line)
        i += 1
    text = '\n'.join(result)

    # Mop up any remaining split mobiles (safety net)
    text = re.sub(r'\+91\s*\n\s*(\d{10})', r'+91\1', text)

    r: dict = {}

    dp_m = re.search(r'\[([A-Z0-9]{8,12})\]', text)
    r['dp_id']        = dp_m.group(1) if dp_m else ''
    r['dp_client_id'] = _find(text, r'client\s+id\s+(\d{5,12})')
    r['short_name']   = _find(text, r'short\s+name\s+([A-Z][A-Z0-9 ]{1,30}?)(?:\s+a/c\s+category|\s+client\s+type|\n)')

    # Holder name can include hyphens, dots, ampersands and digits for
    # entity names like 'GOLDSTANDARD WEALTH PVT LTD - REVANTA' or
    # 'A&B HOLDINGS LLP'. The original [A-Z ] class rejected those.
    h1_full = _find(text, r'Sole/First\s+Holder\s+Name\s+([A-Z][A-Z0-9 .\-&/]{3,80}?)(?:\s+client\s+option|\n)')
    # Detect non-individual holders (LLP, Pvt Ltd, Trust, HUF, Partnership,
    # etc.) by name suffix. Splitting "HXGON PARTNERS LLP" into first /
    # middle / last is wrong — WS treats non-individuals as a single
    # entity name in first_name with middle/last blank. Auto-derived
    # gender + Mr/Mrs salutation also don't apply.
    if h1_full and _is_non_individual_name(h1_full):
        r['first_name']  = h1_full.strip()
        r['middle_name'] = ''
        r['last_name']   = ''
        r['_non_individual'] = True
    elif h1_full:
        parts = h1_full.split()
        r['first_name']  = parts[0] if parts else ''
        r['middle_name'] = parts[1] if len(parts) >= 3 else ''
        r['last_name']   = ' '.join(parts[2:]) if len(parts) >= 3 else (parts[1] if len(parts) == 2 else '')
    else:
        r['first_name'] = r['middle_name'] = r['last_name'] = ''

    r['father_husband'] = _find(text, r"First\s+Holder's\s+Father's/Spouse's\s+name\s+([A-Z][A-Z ]{3,60}?)(?:\s+Occupation|\n)")
    r['occupation']     = _find(text, r'Occupation\s+(Professional|Business|Service|Others|Retired|Student|Agriculturist|Housewife)')

    ctype = _find(text, r'client\s+type\s+(Resident|NRI|NRO|NRE|Foreign\s+National)')
    if r.get('_non_individual'):
        # Map common entity-name suffixes to the WS-canonical client_status
        # values (taken from H1STATUS in ClientDetail.xls). Default to
        # 'Body Corporate' so the registry still has something pickable.
        r['client_status'] = _classify_non_individual(r['first_name'])
        # Salutation default for non-individuals — form options include
        # 'M/s' which is the standard pick for company / firm / trust.
        r['salutation'] = 'M/s'
    else:
        r['client_status'] = {'Resident': 'Individual', 'NRI': 'NRI/OCB-NRE', 'NRO': 'NRI/OCB-NRO'}.get(ctype, ctype or 'Individual')

    r['tax_pan']   = _find(text, r'sole/first\s+holder\s+pan\s+([A-Z]{5}\d{4}[A-Z])')
    dob_raw        = _find(text, r'Sole/First\s+Holder\s+DOB\s+(\d{2}-\d{2}-\d{4})')
    r['birth_date'] = _parse_date_to_iso(dob_raw) if dob_raw else ''

    # Gender — present in the "other details" block of every NSDL CML
    # (line reads "Gender Female" / "Gender Male" etc.). Normalised to
    # the single-letter codes the form's <select> uses ('M' / 'F') —
    # the form options are 'M' = Male and 'F' = Female, so writing the
    # full word here would leave the dropdown blank. Skipped entirely
    # for non-individual holders — gender doesn't apply to entities.
    if r.get('_non_individual'):
        r['gender'] = ''
    else:
        g_raw = (_find(text, r'\bGender\s+(Male|Female|Others?)') or '').strip().lower()
        r['gender'] = ('M' if g_raw.startswith('m')
                        else 'F' if g_raw.startswith('f')
                        else '')

    # Mobile: after split repair, "+91" is immediately followed by 10 digits
    all_mobs       = re.findall(r'\+91\s*(\d{10})', text)
    r['mobile']    = all_mobs[0] if all_mobs else ''

    # Email: after split repair, full address should be on one line. The
    # nominee block ALSO has an "Email ID" line, so a blanket scan picks
    # up the nominee email as the second hit even on single-holder CMLs.
    # Restrict to the holder section by cutting the text at the
    # "First Nominee Details" sentinel before scanning.
    holder_text = re.split(r'First\s+Nominee\s+Details', text, maxsplit=1, flags=re.IGNORECASE)[0]
    emails       = re.findall(r'Email\s+Id\s+([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})',
                              holder_text, re.IGNORECASE)
    r['email']   = emails[0].lower() if emails else ''
    # h2 mobile + email only when a Second Holder block is actually
    # present. Without this check the nominee's contact details bleed
    # into the second-holder fields on single-holder CMLs.
    r['h2_email']  = emails[1].lower() if len(emails) > 1 else ''
    holder_mobs    = re.findall(r'\+91\s*(\d{10})', holder_text)
    r['h2_mobile'] = holder_mobs[1] if len(holder_mobs) > 1 else ''

    r['aadhar'] = _find(text, r'(?:Sole/First\s+Holder\s+Aadhaar|First\s+Holder\s+Aadhaar)\s+(X+\d{4})')
    # Second holder's masked Aadhaar — labelled "Second Holder Aadhaar"
    # in the other-details block when the CML has a second holder.
    r['h2_aadhar'] = _find(text, r'Second\s+Holder\s+Aadhaar\s+(X+\d{4})')

    # Address extraction via word coordinates — NSDL CML uses a two-column
    # layout ('Address' on the left, 'Other Address' on the right) and the
    # flat-text form collapses both columns into the same line. Word-level
    # x/y coordinates let us isolate just the left column.
    a1, a2, addr_flipped = _extract_addresses_from_pdf(pdf_path)
    if not a1:
        # Fallback for pre-NSDL formats where address starts with 'B <num>'
        addr_raw = _find(text, r'Address\s+(B\s+\d+.+?)(?:Other\s+Address|pin\s+code)',
                          1, re.IGNORECASE | re.DOTALL)
        if addr_raw:
            addr_lines = [l.strip() for l in addr_raw.splitlines() if l.strip()]
            a1 = addr_lines[0] if addr_lines else ''
            a2 = addr_lines[1] if len(addr_lines) > 1 else ''
    r['address1'] = a1
    r['address2'] = a2

    # When _extract_addresses_from_pdf reports a column flip (ICICI-style
    # CML where 'Address' carries the bank's mailing address and 'Other
    # Address' carries the client's residence), pick the SECOND match
    # in document order for pin / state — that's the right column. For
    # non-flipped CMLs use the FIRST match (legacy behaviour).
    pick = 1 if addr_flipped else 0

    pin_matches = re.findall(r'pin\s+code\s+(\d{6})', text, re.IGNORECASE)
    r['pin_code'] = (pin_matches[pick] if len(pin_matches) > pick
                      else (pin_matches[0] if pin_matches else ''))

    # NSDL CMLs print state names in a narrow column, so the longer
    # MAHARASHTRA wraps to 'MAHARASHTR\nA' — and that orphan 'A' gets
    # injected between two-word states on the next line ('ANDHRA\nA
    # PRADESH'). Normalise out the orphan so the state regex doesn't
    # capture just 'ANDHRA' / 'UTTAR' / 'TAMIL' / etc.
    text_for_state = re.sub(
        r'\b(ANDHRA|TAMIL|UTTAR|WEST|MADHYA|ARUNACHAL|HIMACHAL)\s+[A-Z]\s+(PRADESH|NADU|BENGAL)\b',
        r'\1 \2', text, flags=re.IGNORECASE,
    )
    state_pattern = r'\bState\s+(MAHARASHTR[A]?|GUJARAT|KARNATAKA|TAMIL\s+NADU|DELHI|RAJASTHAN|ANDHRA\s+PRADESH|UTTAR\s+PRADESH|MADHYA\s+PRADESH|ARUNACHAL\s+PRADESH|HIMACHAL\s+PRADESH|WEST\s+BENGAL|[A-Z]{4,20})\b'
    state_matches = re.findall(state_pattern, text_for_state)
    state_raw = (state_matches[pick] if len(state_matches) > pick
                  else (state_matches[0] if state_matches else ''))
    if 'MAHARASHTR' in state_raw.upper():
        r['state'] = 'Maharashtra'
    else:
        r['state'] = _normalise_state(state_raw)

    # City — when the address was flipped, the bank's hometown (Mumbai
    # etc.) shouldn't outrank the client's actual city. Scan the chosen
    # address text first; only fall back to the global hometown scan
    # when nothing from the chosen address matches.
    addr_text_up = ' '.join([(a1 or ''), (a2 or '')]).upper()
    city = ''
    for c in ['Tirupati', 'Chittoor', 'Mumbai', 'Delhi', 'Bangalore',
              'Bengaluru', 'Chennai', 'Pune', 'Ahmedabad', 'Hyderabad',
              'Kolkata', 'Noida', 'Gurugram', 'Gurgaon', 'Jaipur',
              'Lucknow', 'Coimbatore', 'Indore', 'Bhopal', 'Nagpur',
              'Vadodara', 'Surat', 'Kochi', 'Cochin', 'Thane']:
        if c.upper() in addr_text_up:
            city = c
            break
    if not city:
        for c in ['Mumbai', 'Delhi', 'Bangalore', 'Chennai', 'Pune',
                  'Ahmedabad', 'Hyderabad', 'Kolkata']:
            if c.upper() in text.upper():
                city = c
                break
    r['city'] = city

    r['mandate_bank_acno']   = _find(text, r'bank\s+account\s+number\s+(\d{8,18})')
    r['mandate_bank_actype'] = _find(text, r'bank\s+account\s+type\s+(Savings|Current|NRE|NRO)')
    r['mandate_bank_neft']   = _find(text, r'ifsc\s+code\s+([A-Z]{4}0[A-Z0-9]{6})')
    r['mandate_bank_micr']   = _find(text, r'micr\s+code\s+(\d{9})')

    bn = _find(text, r'bank\s+name\s+([A-Z][A-Z ]{2,20}?)(?:\s+POA|\s+bank\s+address|\n)').upper()
    r['mandate_bank_name'] = (
        'HDFC Bank' if 'HDFC' in bn else
        'ICICI Bank' if 'ICICI' in bn else
        'Kotak Mahindra Bank' if 'KOTAK' in bn else
        'Axis Bank' if 'AXIS' in bn else
        'State Bank of India' if 'SBI' in bn else bn.title()
    )

    r['trading_bank_code'] = r['mandate_bank_neft'][:4] if r['mandate_bank_neft'] else ''
    r['trading_bank_acno'] = r['mandate_bank_acno']

    for k in ('mandate_dpid', 'trading_dpid'):
        r[k] = r['dp_id']
    for k in ('mandate_dp_clientid', 'trading_dp_clientid'):
        r[k] = r['dp_client_id']
    r['mandate_dep'] = r['trading_dep'] = 'NSDL'

    act_raw = _find(text, r'a/c\s+activation\s+(\d{2}/\d{2}/\d{4})')
    r['inception_date'] = _parse_date_to_iso(act_raw) if act_raw else ''

    r['h2_name']      = _find(text, r'Second\s+Holder\s+Name\s+([A-Z][A-Z ]{3,60}?)(?:\s+Third\s+Holder|\n)')
    r['tax_h2_pan']   = _find(text, r'Second\s+Holder\s+[:\s]+PAN\s+([A-Z]{5}\d{4}[A-Z])')
    h2_dob_raw        = _find(text, r'Second\s+Holder\s+DOB:\s+(\d{2}-\d{2}-\d{4})')
    r['h2_birth_date'] = _parse_date_to_iso(h2_dob_raw) if h2_dob_raw else ''

    # Depository / DP ID / DP Client ID for the second holder — only
    # populate when there's actually a second holder. Demat accounts
    # are jointly held under a single DP record, so the demat values
    # are technically the same as the first holder's, BUT pre-filling
    # them on a SINGLE-holder CML gave the operator a false impression
    # there was a second holder to fill in. Gate strictly on h2_name.
    if r['h2_name']:
        r['mandate_h2_dep']         = 'NSDL'
        r['mandate_h2_dpid']        = r['dp_id']
        r['mandate_h2_dp_clientid'] = r['dp_client_id']
    else:
        r['mandate_h2_dep']         = ''
        r['mandate_h2_dpid']        = ''
        r['mandate_h2_dp_clientid'] = ''

    # Nominee + guardian (page 2 of the CML). Safe to call unconditionally;
    # returns {} if the CML has no nominee block.
    r.update(_extract_nominee_section(text))

    # NOTE: account_type and account_operation_type are deliberately NOT defaulted
    # here — they appear in the form's "Operator Input Required" red section so
    # the operator must consciously choose Separate/Pool and Discretionary/etc.
    r.update({
        'taxable': 'Y', 'auto_mail': 'Y',
        'family_head': 'Y', 'share_report': 'P', 'country': 'India',
        'nationality': 'Indian', 'fatca': 'N', 'ubo': 'N',
        'commit_amount': '5000000',
        # Standing WS defaults — set in one place so both the form UI and the
        # XLS writer see the same values. Overridable per-client in the form.
        # TXNFEETAKENAS (col 130) is intentionally NOT defaulted — WS rejects
        # the upload when a value is present for new clients.
        'accounting_txn':         'CS',
        'stt_taken_as':           'E',
        'mode_of_holding':        'a',
        # Hardcoded (no longer operator-input per firm policy)
        'advisor_user_name':      'Dummy',
        'rm_user_name':           'RM',
        'intermediary_user_name': 'DIRECT',
        'bill_group':             'No Charge - GSW',
        'arn':                    'ARN-0000',
        'cml_source_file': Path(pdf_path).name,
    })
    # Drop transient hint flags so they don't leak into the form / DB.
    r.pop('_non_individual', None)
    return r

# ── DB operations ────────────────────────────────────────────────────────── #

def upsert_client(data: dict) -> int:
    """Insert or update client record. Returns the row id.

    Filters out keys that aren't columns in the clients table — the form
    may send transient values (e.g. ws_scheme_code captured from the scheme
    dropdown) that don't have their own DB column.

    Lookup priority:
      1. data['id'] — when the form is editing an existing row, the id
         is the most reliable signal. Catches rows that don't have a
         (dp_id, dp_client_id) pair (non-individual investors, backfilled
         rows from ClientDetail.xls without demat info).
      2. (dp_id, dp_client_id) — original CML-onboarded path.
      3. Otherwise INSERT a new row.

    The id column is never written explicitly: on INSERT we let SQLite
    auto-increment; on UPDATE we use the existing row's id from the
    lookup. Including id in INSERT would collide with the source row
    whenever the form is editing a row that lacks the (dp_id, dp_client_id)
    pair (the old behaviour: UNIQUE constraint failed: clients.id).
    """
    init_db()
    with get_connection() as conn:
        # Whitelist against actual schema, but never carry id through —
        # writes to the autoincrement primary key are handled below.
        valid_cols = {row[1] for row in conn.execute('PRAGMA table_info(clients)')}
        cols = [c for c in data
                if c in valid_cols and c != 'id'
                and data[c] is not None and data[c] != '']
        existing = None
        # Priority 1: explicit id from the edit form.
        raw_id = data.get('id')
        try:
            row_id_in = int(raw_id) if raw_id not in (None, '') else None
        except (TypeError, ValueError):
            row_id_in = None
        if row_id_in is not None:
            existing = conn.execute(
                'SELECT id FROM clients WHERE id=?', (row_id_in,)
            ).fetchone()
        # Priority 2: (dp_id, dp_client_id) lookup for fresh CML uploads.
        if not existing and data.get('dp_id') and data.get('dp_client_id'):
            existing = conn.execute(
                'SELECT id FROM clients WHERE dp_id=? AND dp_client_id=?',
                (data['dp_id'], data['dp_client_id'])
            ).fetchone()
        if existing:
            row_id = existing['id']
            sets = ', '.join(f"{c}=?" for c in cols)
            conn.execute(
                f"UPDATE clients SET {sets}, updated_at=datetime('now') WHERE id=?",
                [data[c] for c in cols] + [row_id]
            )
        else:
            col_str = ', '.join(cols)
            placeholders = ', '.join('?' * len(cols))
            cur = conn.execute(
                f"INSERT INTO clients ({col_str}) VALUES ({placeholders})",
                [data[c] for c in cols]
            )
            row_id = cur.lastrowid
        conn.commit()
    return row_id


def get_client(row_id: int) -> Optional[dict]:
    init_db()
    with get_connection() as conn:
        row = conn.execute('SELECT * FROM clients WHERE id=?', (row_id,)).fetchone()
        return dict(row) if row else None


def get_pan_accounts(tax_pan: str) -> list:
    """Return one entry per DB row sharing this PAN — used by the edit
    form to surface every investment a PAN has, even when they live on
    sibling rows under the (tax_pan, ws_account_code) compound key.

    Each entry expands the row's primary pool plus any additional_pools
    JSON so the form sees a flat list of {source_row_id, ws_account_code,
    scheme_name, pool_mapin, ws_scheme_code}. Entries are returned in
    row-id order so the edited row's pools sort before its siblings."""
    pan = (tax_pan or '').strip().upper()
    if not pan:
        return []
    init_db()
    out: list = []
    with get_connection() as conn:
        rows = conn.execute(
            'SELECT id, ws_account_code, scheme_name, pool_mapin, '
            'fee_config, additional_pools '
            'FROM clients WHERE UPPER(tax_pan)=? ORDER BY id',
            (pan,)
        ).fetchall()
    for r in rows:
        # Parse the primary pool's fee_config (TEXT/JSON column on the
        # clients row). Returned as a dict so the form can populate the
        # 💰 Fees modal directly without a second JSON.parse on the JS
        # side.
        try:
            primary_fee = json.loads(r['fee_config']) if r['fee_config'] else None
        except Exception:
            primary_fee = None
        # The clients table doesn't have a top-level ws_scheme_code
        # column — it lives only inside additional_pools JSON entries.
        # The form treats it as optional, so blank is fine for the
        # row's primary pool entry here.
        out.append({
            'source_row_id':   r['id'],
            'ws_account_code': r['ws_account_code'] or '',
            'scheme_name':     r['scheme_name']     or '',
            'pool_mapin':      r['pool_mapin']      or '',
            'ws_scheme_code':  '',
            'fee_config':      primary_fee,
        })
        try:
            extra = json.loads(r['additional_pools'] or '[]') or []
        except Exception:
            extra = []
        for p in extra:
            if not isinstance(p, dict):
                continue
            out.append({
                'source_row_id':   r['id'],
                'ws_account_code': p.get('ws_account_code', '') or '',
                'scheme_name':     p.get('scheme_name', '')     or '',
                'pool_mapin':      p.get('pool_mapin', '')      or '',
                'ws_scheme_code':  p.get('ws_scheme_code', '')  or '',
                'fee_config':      p.get('fee_config') or None,
            })
    return out


def list_clients() -> list:
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            'SELECT * FROM clients ORDER BY created_at DESC'
        ).fetchall()
        return [dict(r) for r in rows]


def list_user_names() -> list:
    """Return all non-blank user_name values currently in the registry.
    Used by the client form to suggest a unique user_name (first_name +
    numerical suffix) without exposing other PII."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_name FROM clients "
            "WHERE user_name IS NOT NULL AND TRIM(user_name) != ''"
        ).fetchall()
        return [r['user_name'] for r in rows]


def update_ws_result(row_id: int, ref_number: int, authorized: bool = False):
    init_db()
    with get_connection() as conn:
        conn.execute(
            '''UPDATE clients SET ws_ref_number=?, ws_push_date=datetime('now'),
               ws_authorized=?, updated_at=datetime('now') WHERE id=?''',
            (ref_number, 1 if authorized else 0, row_id)
        )
        conn.commit()


# ── WS API ───────────────────────────────────────────────────────────────── #

def _ws_get_token(api_key: str, pan: str, client_id: str) -> str:
    """Get a WS authentication token using the firm's API key."""
    url = f"{_ws_base()}/api/create/token"
    resp = requests.get(
        url,
        params={'panno': pan, 'id': client_id},
        headers={'Authorization': f'Bearer {api_key}'},
        timeout=30
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get('status'):
        raise RuntimeError(f"WS token error: {body.get('msg', 'unknown')}")
    return body['data']


def _build_ws_payload(client: dict) -> dict:
    """Map our DB/form fields to the WS API field names."""
    def _ymd(d):
        """Ensure date is YYYY-MM-DD."""
        if not d:
            return None
        try:
            return datetime.strptime(d, '%d-%m-%Y').strftime('%Y-%m-%d')
        except Exception:
            return d

    # Full name for groupName if not set
    full_name = ' '.join(filter(None, [
        client.get('first_name', ''),
        client.get('middle_name', ''),
        client.get('last_name', '')
    ]))

    return {
        'groupShortName':       client.get('group_short_name') or client.get('short_name') or full_name[:20],
        'groupName':            client.get('group_name') or full_name,
        'groupContactName':     client.get('group_contact_name') or full_name,
        'accountCode':          client.get('ws_account_code') or '',
        'salutation':           client.get('salutation') or 'Mr',
        'firstName':            client.get('first_name') or '',
        'middleName':           client.get('middle_name') or '',
        'lastName':             client.get('last_name') or '',
        'address1':             client.get('address1') or '',
        'address2':             client.get('address2') or '',
        'city':                 client.get('city') or '',
        'state':                client.get('state') or '',
        'pinCode':              client.get('pin_code') or '',
        'mobile':               client.get('mobile') or '',
        'phoneHome':            client.get('phone_home') or '',
        'email':                client.get('email') or '',
        'autoMail':             client.get('auto_mail') or 'Y',
        'occupation':           client.get('occupation') or '',
        'clientStatus':         client.get('client_status') or 'Individual',
        'taxPan':               client.get('tax_pan') or '',
        'taxable':              client.get('taxable') or 'Y',
        'inceptionDate':        _ymd(client.get('inception_date')),
        'maturityDate':         _ymd(client.get('maturity_date')) or None,
        'accountType':          client.get('account_type') or 'S',
        'userName':             client.get('user_name') or client.get('ws_account_code') or '',
        'tradingBankCode':      client.get('trading_bank_code') or '',
        'tradingBankAcno':      client.get('trading_bank_acno') or '',
        'tradingDep':           client.get('trading_dep') or 'NSDL',
        'tradingDpid':          client.get('trading_dpid') or '',
        'tradingDpClientid':    client.get('trading_dp_clientid') or '',
        'billGroup':            client.get('bill_group') or 'No Charge',
        'mapin':                client.get('mapin') or '',
        'schemeName':           client.get('scheme_name') or '',
        'intermediaryUserName': client.get('intermediary_user_name') or 'direct',
        'firmCode':             client.get('firm_code') or '',
        'advisorUserName':      client.get('advisor_user_name') or '',
        'rmUserName':           client.get('rm_user_name') or '',
        'branchName':           client.get('branch_name') or '',
        'country':              client.get('country') or 'India',
        'nationality':          client.get('nationality') or 'Indian',
        'commitAmount':         client.get('commit_amount') or '5000000',
        'perfReportingDate':    None,
        'mandateBankName':      client.get('mandate_bank_name') or '',
        'mandateBankBranch':    client.get('mandate_bank_branch') or '',
        'mandateBankActype':    client.get('mandate_bank_actype') or '',
        'mandateBankAcno':      client.get('mandate_bank_acno') or '',
        'mandateBankMicr':      client.get('mandate_bank_micr') or '',
        'mandateBankNeft':      client.get('mandate_bank_neft') or '',
        'mandateBankRtgs':      client.get('mandate_bank_rtgs') or client.get('mandate_bank_neft') or '',
        'mandateDep':           client.get('mandate_dep') or 'NSDL',
        'mandateDpid':          client.get('mandate_dpid') or '',
        'mandateDpClientid':    client.get('mandate_dp_clientid') or '',
        'birthDate':            _ymd(client.get('birth_date')),
        'gender':               client.get('gender') or '',
        'fatherHusband':        client.get('father_husband') or '',
        'ckyc':                 client.get('ckyc') or '',
        'aadhar':               client.get('aadhar') or '',
        'fatca':                client.get('fatca') or 'N',
        'ubo':                  client.get('ubo') or 'N',
        'shareReport':          client.get('share_report') or 'P',
        'h2Name':               client.get('h2_name') or '',
        'taxH2Pan':             client.get('tax_h2_pan') or '',
        'h2Relation':           client.get('h2_relation') or '',
        'h2BirthDate':          _ymd(client.get('h2_birth_date')),
        'h2Gender':             client.get('h2_gender') or '',
        'h2FatherHusband':      client.get('h2_father_husband') or '',
        'mandateH2BankName':    client.get('mandate_h2_bank_name') or '',
        'mandateH2BankBranch':  client.get('mandate_h2_bank_branch') or '',
        'mandateH2BankAcno':    client.get('mandate_h2_bank_acno') or '',
        'mandateH2BankActype':  client.get('mandate_h2_bank_actype') or '',
        'mandateH2BankMicr':    client.get('mandate_h2_bank_micr') or '',
        'mandateH2BankNeft':    client.get('mandate_h2_bank_neft') or '',
        'mandateH2BankRtgs':    client.get('mandate_h2_bank_rtgs') or '',
        'mandateH2Dep':         client.get('mandate_h2_dep') or 'NSDL',
        'mandateH2Dpid':        client.get('mandate_h2_dpid') or '',
        'mandateH2DpClientid':  client.get('mandate_h2_dp_clientid') or '',
        'poolMapin':            client.get('pool_mapin') or '',
        'accountOperationType': client.get('account_operation_type') or 'A',
        'mailAddress1':         client.get('mail_address1') or client.get('address1') or '',
        'mailAddress2':         client.get('mail_address2') or client.get('address2') or '',
        'mailAddressCity':      client.get('mail_address_city') or client.get('city') or '',
        'mailAddressState':     client.get('mail_address_state') or client.get('state') or '',
        'mailAddressPinCode':   client.get('mail_address_pin_code') or client.get('pin_code') or '',
        'mailAddressMobile':    client.get('mobile') or '',
        'familyHead':           client.get('family_head') or 'Y',
        'arn':                  client.get('arn') or '',
        'accountingTxn':        client.get('accounting_txn') or 'CS',
        'sttTakenAs':           client.get('stt_taken_as') or 'E',
        'modeOfHolding':        client.get('mode_of_holding') or 'S',
    }


# ── WS XLS upload (Account Creation_PMS template) ────────────────────────── #
#
# 223-column mapping for the WS "0001 - Account Creation_PMS.xls" template's
# Sample sheet. Each entry is the DB column name to pull, or None for fields
# we don't carry (third-holder, nominees, guardian, ref codes, fillers).
# The order MUST match the template's column ordering exactly.

WS_TEMPLATE_FIELDS = [
    'group_short_name',         # 0  GROUP CODE
    'group_name',               # 1  GROUP NAME
    'group_contact_name',       # 2  GROUP CONTACT
    'ws_account_code',          # 3  ACCOUNT CODE
    'salutation',               # 4  SALUTATION
    'first_name',               # 5  FIRST NAME
    'middle_name',              # 6  MIDDLE NAME
    'last_name',                # 7  LAST NAME
    'address1',                 # 8  ADDRESS 1
    'address2',                 # 9  ADDRESS 2
    'city',                     # 10 CITY
    'state',                    # 11 STATE
    'pin_code',                 # 12 PIN CODE
    'phone_home',               # 13 PHONE
    None,                       # 14 PHONE WORK
    'mobile',                   # 15 MOBILE
    None,                       # 16 FAX
    'email',                    # 17 EMAIL
    'auto_mail',                # 18 AUTO MAIL FLAG
    'occupation',               # 19 OCCUPATION
    'client_status',            # 20 CLIENT STATUS
    'tax_pan',                  # 21 PAN
    'taxable',                  # 22 TAXABLE
    'inception_date',           # 23 INCEPTION DATE
    'maturity_date',            # 24 MATURITY DATE
    'account_type',             # 25 ACCOUNT TYPE
    'user_name',                # 26 USER NAME
    'trading_bank_code',        # 27 BANK CODE             (operating account)
    'trading_bank_acno',        # 28 BANK ACCOUNT ID       (operating account)
    'trading_dep',              # 29 DEPOSITORY            (operating account)
    'trading_dpid',             # 30 DPID                  (operating account)
    'trading_dp_clientid',      # 31 DPCLIENTID            (operating account)
    'bill_group',               # 32 BILL GROUP
    'mapin',                    # 33 MAPIN
    None,                       # 34 REFERENCE CODE 1
    None,                       # 35 REFERENCE CODE 2
    None,                       # 36 REFERENCE CODE 3
    None,                       # 37 REFERENCE CODE 4
    'scheme_name',              # 38 SCHEME CODE
    'intermediary_user_name',   # 39 INTERMEDIARY
    'firm_code',                # 40 FIRM CODE
    'advisor_user_name',        # 41 ADVISOR CODE
    'rm_user_name',             # 42 RELATIONSHIP MGR
    'branch_name',              # 43 BRANCH CODE
    'country',                  # 44 COUNTRY
    'nationality',              # 45 NATIONALITY
    'commit_amount',            # 46 CAPITAL COMMETED
    None,                       # 47 PERFORMANCE REPORTING DATE
    'mandate_bank_name',        # 48 BANK NAME             (first holder bank mandate)
    'mandate_bank_branch',      # 49 BRANCH
    'mandate_bank_actype',      # 50 BANK ACCOUNT TYPE
    'mandate_bank_acno',        # 51 BANK ACCOUNT NUMBER
    'mandate_bank_micr',        # 52 MICR
    'mandate_bank_neft',        # 53 NEFT
    'mandate_bank_rtgs',        # 54 RTGS
    'mandate_dep',              # 55 DP                    (first holder custody mandate)
    'mandate_dpid',             # 56 DPID
    'mandate_dp_clientid',      # 57 DPCLIENTID
    'birth_date',               # 58 DATE OF BIRTH
    None,                       # 59 GUARDIAN
    'gender',                   # 60 GENDER
    'father_husband',           # 61 FATHER/HUSBAND
    None,                       # 62 WARD
    None,                       # 63 CIRCLE
    None,                       # 64 TAN
    'share_report',             # 65 SHARE REPORTS
    'h2_name',                  # 66 H2NAME
    'tax_h2_pan',               # 67 H2PAN
    'h2_relation',              # 68 H2 RELATION
    'h2_birth_date',            # 69 H2 BIRTH DATE
    'h2_gender',                # 70 H2 GENDER
    'h2_father_husband',        # 71 H2 FATHER-HUSBAND
    'mandate_h2_bank_name',     # 72 H2 BANK
    'mandate_h2_bank_branch',   # 73 H2 BRANCH
    'mandate_h2_bank_acno',     # 74 H2 BANK AC ID
    'mandate_h2_bank_actype',   # 75 H2 BANK AC TYPE
    'mandate_h2_bank_micr',     # 76 H2 MICR
    'mandate_h2_bank_neft',     # 77 H2 NEFT
    'mandate_h2_bank_rtgs',     # 78 H2 RTGS
    'mandate_h2_dep',           # 79 H2 DEPOSITORY
    'mandate_h2_dpid',          # 80 H2 DPID
    None,                       # 81 H2 DP NAME
    'mandate_h2_dp_clientid',   # 82 H2 DPCLIENTID
    None, None, None, None, None, None, None, None, None, None,
    None, None, None, None, None, None, None,                   # 83-99 H3 fields (not carried)
    'pool_mapin',               # 100 POOL MAPIN ID
    None,                       # 101 BANK NAME            (mystery duplicate; leave blank)
    'account_operation_type',   # 102 OPERATION TYPE
    'mail_address1',            # 103 MAILING ADDRESS 1    (fallback handled below)
    'mail_address2',            # 104 MAILING ADDRESS 2
    'mail_address_city',        # 105 MAILING CITY
    'mail_address_state',       # 106 MAILING STATE
    'mail_address_pin_code',    # 107 MAILING PIN CODE
    None,                       # 108 MAILING ADDRESS PHONE
    'mobile',                   # 109 MAILING ADDRESS - MOBILE  (mirror)
    None,                       # 110 MAILING ADDRESS - PHONE WORK
    None,                       # 111 MAILING ADDRESS - FAX
    'email',                    # 112 MAILING ADDRESS - EMAIL    (mirror)
    'family_head',              # 113 HOF
    None,                       # 114 CLIENT ID
    None,                       # 115 WM
    'arn',                      # 116 ARN NO
    'ckyc',                     # 117 H1CKYC
    'aadhar',                   # 118 H1AADHAR
    'fatca',                    # 119 H1FATCA
    'ubo',                      # 120 H1UBO
    None,                       # 121 H2CKYC
    None,                       # 122 H2AADHAR
    None,                       # 123 H2FATCA
    None,                       # 124 H2UBO
    None, None, None, None,     # 125-128 H3 KYC
    'accounting_txn',           # 129 ACCOUNTINGTXN
    None,                       # 130 TXNFEETAKENAS — WS rejects any value
                                 #     on new-client uploads; keep blank.
                                 #     DB column + form dropdown retained in
                                 #     case a future WS version allows it.
    'stt_taken_as',             # 131 STTTAKENAS
    None,                       # 132 FILLER
    'mode_of_holding',          # 133 MODE OF HOLDING
    'nominee_name',             # 134 NOMINEENAME
    'nominee_relation',         # 135 NOMINEERELATION
    'nominee_address1',         # 136 NOMINEEADD1
    'nominee_address2',         # 137 NOMINEEADD2
    'nominee_city',             # 138 NOMINEECITY
    'nominee_state',            # 139 NOMINEESTATE
    'nominee_pin_code',         # 140 NOMINEEPIN
    'nominee_phone',            # 141 NOMINEEPHONE
    'nominee_fax',              # 142 NOMINEEFAX
    'nom_guardian_name',        # 143 GUARDIANNAME
    'nom_guardian_address1',    # 144 GUARDIANADD1
    'nom_guardian_address2',    # 145 GUARDIANADD2
    'nom_guardian_city',        # 146 GUARDIANCITY
    'nom_guardian_state',       # 147 GUARDIANSTATE
    'nom_guardian_pin_code',    # 148 GUARDIANPIN
    'nom_guardian_phone',       # 149 GUARDIANPHONE
    'nom_guardian_fax',         # 150 GUARDIANFAX
    'nominee_dob',              # 151 NOMDOB
    'nominee_pan',              # 152 NOMPAN
    'ng_dob',                   # 153 NGDOB
    'ng_pan',                   # 154 NGPAN
    'ng_aadhar',                # 155 NGAADHAR
    'ng_ckyc',                  # 156 NGCKYC
    'ng_fatca',                 # 157 NGFATCA
    'ng_ubo',                   # 158 NGUBO
    None, None, None, None, None, None, None,                   # 159-165 BANKACTYPE2 etc
    None, None, None, None, None, None, None, None, None, None,
    None, None, None, None, None, None, None, None, None, None, # 166-185 FILLERs / MODELPORTFOLIO
    None, None,                 # 186 CLIENTCATEGORY, 187 ACCREDITEDINVESTOR
    None, None, None, None, None, None,                         # 188-193 REFCODE 5-10
    None, None, None, None, None, None, None,                   # 194-200 misc
    None, None,                 # 201 COUNTRY (dup), 202 MAILING COUNTRY
    None, None, None, None, None,                               # 203-207 INVESTOR TYPE etc
    None, None, None, None, None, None, None, None,
    None, None, None, None, None, None, None,                   # 208-222 REFCODE 11-25
]
# Sanity: WS_TEMPLATE_FIELDS must be exactly 223 entries
assert len(WS_TEMPLATE_FIELDS) == 223, \
    f"WS_TEMPLATE_FIELDS length mismatch: {len(WS_TEMPLATE_FIELDS)}"


def _ws_template_path() -> Path:
    """Return the path to the bundled 0001 - Account Creation_PMS template.
    The template lives in the operator's Downloads folder during dev; on
    Azure we drop it under data/templates/ so it's deployable."""
    import os
    candidates = [
        Path(os.environ.get('KEYSTONE_DATA_DIR', '')) /
            'templates' / '0001 - Account Creation_PMS.xls',
        Path(__file__).parent.parent / 'data' / 'templates' /
            '0001 - Account Creation_PMS.xls',
        Path(__file__).parent.parent / 'masters' /
            '0001 - Account Creation_PMS.xls',
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "0001 - Account Creation_PMS.xls template not found. "
        "Place it under data/templates/ or masters/."
    )


def _xls_value(client: dict, db_col: str):
    """Fetch a value from the client dict, applying mailing-address fallbacks
    so blank mailing fields fall back to the main address."""
    if db_col is None:
        return ''
    raw = client.get(db_col, '') or ''
    if db_col == 'mail_address1':       return raw or client.get('address1') or ''
    if db_col == 'mail_address2':       return raw or client.get('address2') or ''
    if db_col == 'mail_address_city':   return raw or client.get('city') or ''
    if db_col == 'mail_address_state':  return raw or client.get('state') or ''
    if db_col == 'mail_address_pin_code': return raw or client.get('pin_code') or ''
    return raw


# Column indices (0-based) with type-specific write rules. Mismatches here
# cause WS to silently reject the upload — the template's Sample row is the
# reference spec.
_WS_NUMBER_COLS = {12, 46, 107, 140, 148}  # PIN CODES, CAPITAL COMMETED,
                                           # NOMINEEPIN, GUARDIANPIN
_WS_DATE_COLS   = {23, 47}            # INCEPTION DATE, PERFORMANCE REPORTING DATE
_WS_DMY_COLS    = {58, 69, 151, 153}  # DATE OF BIRTH, H2 BIRTH DATE,
                                       # NOMDOB, NGDOB — all DD/MM/YYYY text
_WS_UPPER_COLS  = {27, 39}            # BANK CODE, INTERMEDIARY (user rule)
_WS_STRIP_ID_SUFFIX_COLS = {41, 42, 43}  # ADVISOR CODE, RELATIONSHIP MGR, BRANCH CODE
# WS enforces a 60-char limit on each address cell. Exceeding it silently
# rejects the row. Applies to main + mailing + nominee + guardian addresses.
_WS_ADDR_60_COLS = {8, 9, 103, 104, 136, 137, 144, 145}


def _clip60(s: str) -> str:
    """Truncate a string to 60 chars, preferring a word boundary."""
    if not s or len(s) <= 60:
        return s or ''
    cut = s[:60]
    sp = cut.rfind(' ')
    # Don't chop too aggressively — if the last space is before pos 40
    # the break happens mid-word (long unbroken token) and we fall back
    # to the hard 60-char cut.
    if sp >= 40:
        cut = cut[:sp]
    return cut.rstrip(' ,').strip()


def _strip_ws_id_suffix(s: str) -> str:
    """Strip the trailing ' - <id>' decoration that our form dropdowns use
    for display. WS's picklist resolver matches on the raw user_name /
    branch name, not the display string, so 'Internal - 5' must go as
    'Internal', 'Corporate - 10001' as 'Corporate', etc."""
    return re.sub(r'\s*-\s*\d+\s*$', '', s).strip()


def _coerce_to_int(raw):
    """Return int for numeric strings, else the original value."""
    if raw is None or raw == '':
        return ''
    try:
        return int(float(str(raw).replace(',', '').strip()))
    except (TypeError, ValueError):
        return raw


def _coerce_to_dmy(raw):
    """ISO YYYY-MM-DD → DD/MM/YYYY; leave other formats untouched."""
    if not raw:
        return ''
    try:
        return datetime.strptime(str(raw)[:10], '%Y-%m-%d').strftime('%d/%m/%Y')
    except ValueError:
        return raw


def _coerce_to_date(raw):
    """ISO YYYY-MM-DD → datetime (for xlwt date-typed cell). On parse
    failure returns the raw string so the cell still writes something
    rather than silently dropping."""
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], '%Y-%m-%d')
    except ValueError:
        return raw


def _write_client_row(sh_out, row_num: int, client: dict, date_style):
    """Write a single data row into the Sample sheet with per-column type rules."""
    for c, db_col in enumerate(WS_TEMPLATE_FIELDS):
        raw = _xls_value(client, db_col)

        # Strip ' - <id>' suffix from picklist values (advisor/RM/branch)
        if c in _WS_STRIP_ID_SUFFIX_COLS and isinstance(raw, str):
            raw = _strip_ws_id_suffix(raw)

        # Codes stay upper-case (toTC() on the client may have damaged them)
        if c in _WS_UPPER_COLS and isinstance(raw, str):
            raw = raw.upper()

        # Address-cell 60-char cap — WS silently rejects longer strings
        if c in _WS_ADDR_60_COLS and isinstance(raw, str):
            raw = _clip60(raw)

        # Numeric cells — WS rejects text for these
        if c in _WS_NUMBER_COLS:
            sh_out.write(row_num, c, _coerce_to_int(raw))
            continue

        # Date-typed cells — INCEPTION DATE etc. must be real Excel dates
        if c in _WS_DATE_COLS:
            val = _coerce_to_date(raw)
            if isinstance(val, datetime):
                sh_out.write(row_num, c, val, date_style)
            else:
                sh_out.write(row_num, c, val or '')
            continue

        # DD/MM/YYYY text (DOB format expected by WS)
        if c in _WS_DMY_COLS:
            sh_out.write(row_num, c, _coerce_to_dmy(raw))
            continue

        sh_out.write(row_num, c, raw)


def build_client_xls(row_id: int, output_dir: Path = None) -> Path:
    """Build a WS-format Account Creation XLS for a single client.

    Writes one Sample row per pool the client is joining — primary pool
    first (from the top-level columns) followed by any entries in the
    additional_pools JSON column. All non-pool fields repeat across rows.
    """
    import xlwt, json as _json
    init_db()
    client = get_client(row_id)
    if not client:
        raise ValueError(f"Client {row_id} not found")

    # Build the list of pools this client joins. Primary always present.
    pools = [{
        'ws_account_code': client.get('ws_account_code') or '',
        'scheme_name':     client.get('scheme_name') or '',
        'pool_mapin':      client.get('pool_mapin') or '',
    }]
    try:
        extras = _json.loads(client.get('additional_pools') or '[]') or []
    except (ValueError, TypeError):
        extras = []
    for p in extras:
        if not isinstance(p, dict):
            continue
        pools.append({
            'ws_account_code': p.get('ws_account_code') or '',
            'scheme_name':     p.get('scheme_name') or '',
            'pool_mapin':      p.get('pool_mapin') or '',
        })

    template_path = _ws_template_path()
    src = xlrd.open_workbook(str(template_path), formatting_info=False)
    sample = src.sheet_by_name('Sample')

    wb_out = xlwt.Workbook(encoding='utf-8')
    sh_out = wb_out.add_sheet('Sample', cell_overwrite_ok=True)

    # Row 0 — copy headers verbatim
    for c in range(sample.ncols):
        sh_out.write(0, c, sample.cell_value(0, c))

    date_style = xlwt.easyxf(num_format_str='DD/MM/YYYY')

    # One data row per pool; all common fields repeat.
    for idx, pool in enumerate(pools):
        row_client = dict(client)
        row_client['ws_account_code'] = pool['ws_account_code']
        row_client['scheme_name']     = pool['scheme_name']
        row_client['pool_mapin']      = pool['pool_mapin']
        _write_client_row(sh_out, idx + 1, row_client, date_style)

    if output_dir is None:
        import os
        base = os.environ.get('KEYSTONE_DATA_DIR') or str(Path(__file__).parent.parent)
        output_dir = Path(base) / 'data' / 'client_uploads'
    output_dir.mkdir(parents=True, exist_ok=True)

    pan = (client.get('tax_pan') or 'NOPAN').upper()
    out_path = output_dir / f"ClientUpload_{pan}_{row_id}.xls"
    wb_out.save(str(out_path))
    return out_path


# Module-import dependency (xlrd is already imported indirectly by build_client_xls)
import xlrd  # noqa: E402


def send_client_pending_email(client: dict, recipients: list, ingestor) -> dict:
    """Send pending-authorization notification email via Microsoft Graph API."""
    name = ' '.join(filter(None, [
        client.get('first_name',''), client.get('middle_name',''), client.get('last_name','')
    ]))
    pan    = client.get('tax_pan', '—')
    scheme = client.get('scheme_name', '—')
    rows = ''.join(
        f"<tr><td style='padding:4px 12px;color:#888;font-size:12px'>{k}</td>"
        f"<td style='padding:4px 12px;font-size:12px'>{v}</td></tr>"
        for k, v in [
            ('Name', name), ('PAN', pan),
            ('DP ID', f"{client.get('dp_id','')}/{client.get('dp_client_id','')}"),
            ('Scheme', scheme), ('Inception', client.get('inception_date','—')),
            ('Bank', f"{client.get('mandate_bank_name','')} {client.get('mandate_bank_acno','')}"),
            ('RM', client.get('rm_user_name','—')),
        ] if v and v != '—'
    )
    html = (
        "<div style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto'>"
        "<div style='background:#0b1929;padding:20px;border-bottom:3px solid #c9a84c'>"
        "<span style='color:#c9a84c;font-size:18px;font-weight:700'>Keystone</span>"
        "</div>"
        "<div style='padding:24px;background:#f9f9f9'>"
        "<h2 style='color:#0b1929;margin:0 0 4px'>New Client Pending Authorization</h2>"
        "<p style='color:#666;font-size:13px;margin:0 0 20px'>"
        "A new client record has been submitted and requires authorization before it can be "
        "pushed to WealthSpectrum.</p>"
        f"<table style='width:100%;border-collapse:collapse;background:#fff;"
        f"border:1px solid #e0e0e0;border-radius:4px'>{rows}</table>"
        "<p style='margin:20px 0 0;font-size:12px;color:#888'>"
        "Log in to Keystone → Clients → Pending Authorization to review and approve."
        "</p></div></div>"
    )
    payload = {
        'message': {
            'subject': f'Client Pending Auth — {name} ({pan})',
            'body': {'contentType': 'HTML', 'content': html},
            'toRecipients': [
                {'emailAddress': {'address': r.strip()}}
                for r in recipients if r.strip()
            ],
        },
        'saveToSentItems': True,
    }
    try:
        import requests as _req
        resp = _req.post(
            f'https://graph.microsoft.com/v1.0/users/{ingestor.mailbox}/sendMail',
            headers=ingestor._headers(), json=payload, timeout=30
        )
        resp.raise_for_status()
        return {'ok': True}
    except Exception as e:
        logger.warning(f"Client pending email failed: {e}")
        return {'ok': False, 'message': str(e)}


def submit_for_authorization(row_id: int, creator_token: str, recipients: list) -> dict:
    """Mark client as pending authorization (maker-checker: submit step)."""
    import json as _json
    init_db()
    client = get_client(row_id)
    if not client:
        return {'ok': False, 'message': f'Client {row_id} not found'}
    if client.get('pending_auth'):
        return {'ok': False, 'message': 'Already submitted for authorization'}
    with get_connection() as conn:
        conn.execute(
            "UPDATE clients SET pending_auth=1, creator_token=?, pending_recipients=?,"
            " submitted_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
            (creator_token, _json.dumps(recipients), row_id)
        )
        conn.commit()
    return {'ok': True}


def get_pending_clients() -> list:
    """Return all clients with pending_auth=1."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM clients WHERE pending_auth=1 ORDER BY submitted_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def authorize_pending(row_id: int, authorizer_token: str, api_key: str) -> dict:
    """Authorize a pending client: enforce maker-checker, push to WS, authorize in WS."""
    init_db()
    client = get_client(row_id)
    if not client:
        return {'ok': False, 'message': f'Client {row_id} not found'}
    if not client.get('pending_auth'):
        return {'ok': False, 'message': 'Client is not in pending state'}
    if client.get('creator_token') and client['creator_token'] == authorizer_token:
        return {
            'ok': False,
            'message': 'Maker-checker: you cannot authorize a record you submitted'
        }
    if not api_key:
        return {'ok': False, 'message': 'WS API key not configured'}

    push_result = push_to_ws(row_id, api_key)
    if not push_result.get('ok'):
        return push_result

    auth_result = authorize_in_ws(row_id, api_key)

    with get_connection() as conn:
        conn.execute(
            "UPDATE clients SET pending_auth=0, auth_token=?, ws_authorized=1,"
            " ws_authorize_date=datetime('now'), updated_at=datetime('now') WHERE id=?",
            (authorizer_token, row_id)
        )
        conn.commit()

    return {
        'ok': True,
        'ref_number': push_result.get('ref_number'),
        'ws_authorized': auth_result.get('ok', False),
        'message': 'Authorized and pushed to WealthSpectrum'
    }



def push_to_ws(row_id: int, api_key: str) -> dict:
    """
    Push client to WealthSpectrum.
    1. Fetches token using client PAN
    2. POSTs to clientMaster/create/account
    3. Stores refNumber in DB
    Returns {'ok': bool, 'ref_number': int, 'message': str}
    """
    client = get_client(row_id)
    if not client:
        return {'ok': False, 'message': f'Client id {row_id} not found in DB'}

    try:
        token = _ws_get_token(api_key, client['tax_pan'], client['dp_client_id'] or '100001')
    except Exception as e:
        return {'ok': False, 'message': f'Token fetch failed: {e}'}

    payload = _build_ws_payload(client)

    try:
        resp = requests.post(
            f"{_ws_base()}/api/clientMaster/create/account",
            json=payload,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            },
            timeout=60
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        return {'ok': False, 'message': f'WS API call failed: {e}'}

    if not body.get('status'):
        return {'ok': False, 'message': body.get('msg', 'Unknown WS error'), 'raw': body}

    ref_number = body.get('data', {}).get('refNumber')
    update_ws_result(row_id, ref_number, authorized=False)
    logger.info(f"Client {row_id} pushed to WS → refNumber={ref_number}")
    return {'ok': True, 'ref_number': ref_number, 'message': 'Created successfully'}


def authorize_in_ws(row_id: int, api_key: str) -> dict:
    """Authorize a previously created WS client record."""
    client = get_client(row_id)
    if not client or not client.get('ws_ref_number'):
        return {'ok': False, 'message': 'No WS ref number — push first'}

    try:
        token = _ws_get_token(api_key, client['tax_pan'], client['dp_client_id'] or '100001')
        resp = requests.get(
            f"{_ws_base()}/api/clientMaster/authorize/account",
            params={'refNumber': client['ws_ref_number']},
            headers={'Authorization': f'Bearer {token}'},
            timeout=30
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        return {'ok': False, 'message': str(e)}

    if not body.get('status'):
        return {'ok': False, 'message': body.get('msg', 'Authorize failed')}

    with get_connection() as conn:
        conn.execute(
            "UPDATE clients SET ws_authorized=1, ws_authorize_date=datetime('now') WHERE id=?",
            (row_id,)
        )
        conn.commit()
    return {'ok': True, 'message': 'Authorized successfully'}
