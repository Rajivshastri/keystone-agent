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
    """Return WS base URL from azure.json ws_base_url, or default."""
    try:
        import json
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
    scheme_name            TEXT,
    pool_mapin             TEXT,
    rm_user_name           TEXT,
    advisor_user_name      TEXT,
    intermediary_user_name TEXT,
    firm_code              TEXT,
    branch_name            TEXT,
    inception_date         TEXT,
    maturity_date          TEXT,
    account_type           TEXT DEFAULT 'S',
    bill_group             TEXT,
    mapin                  TEXT,
    commit_amount          TEXT DEFAULT '0',
    account_operation_type TEXT DEFAULT 'A',
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
        ]
        for stmt in _migrations:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists — safe to ignore
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

        # Pattern A: partial email at end of line, next line = "MOBILE EMAIL_CONT"
        partial = re.search(r'([A-Z0-9._%+\-]+@[A-Z0-9]+)$', line, re.IGNORECASE)
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

    h1_full = _find(text, r'Sole/First\s+Holder\s+Name\s+([A-Z][A-Z ]{3,60}?)(?:\s+client\s+option|\n)')
    if h1_full:
        parts = h1_full.split()
        r['first_name']  = parts[0] if parts else ''
        r['middle_name'] = parts[1] if len(parts) >= 3 else ''
        r['last_name']   = ' '.join(parts[2:]) if len(parts) >= 3 else (parts[1] if len(parts) == 2 else '')
    else:
        r['first_name'] = r['middle_name'] = r['last_name'] = ''

    r['father_husband'] = _find(text, r"First\s+Holder's\s+Father's/Spouse's\s+name\s+([A-Z][A-Z ]{3,60}?)(?:\s+Occupation|\n)")
    r['occupation']     = _find(text, r'Occupation\s+(Professional|Business|Service|Others|Retired|Student|Agriculturist|Housewife)')

    ctype = _find(text, r'client\s+type\s+(Resident|NRI|NRO|NRE|Foreign\s+National)')
    r['client_status'] = {'Resident': 'Individual', 'NRI': 'NRI/OCB-NRE', 'NRO': 'NRI/OCB-NRO'}.get(ctype, ctype or 'Individual')

    r['tax_pan']   = _find(text, r'sole/first\s+holder\s+pan\s+([A-Z]{5}\d{4}[A-Z])')
    dob_raw        = _find(text, r'Sole/First\s+Holder\s+DOB\s+(\d{2}-\d{2}-\d{4})')
    r['birth_date'] = _parse_date_to_iso(dob_raw) if dob_raw else ''

    # Mobile: after split repair, "+91" is immediately followed by 10 digits
    all_mobs       = re.findall(r'\+91\s*(\d{10})', text)
    r['mobile']    = all_mobs[0] if all_mobs else ''
    r['h2_mobile'] = all_mobs[1] if len(all_mobs) > 1 else ''

    # Email: after split repair, full address should be on one line
    emails       = re.findall(r'Email\s+Id\s+([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})', text, re.IGNORECASE)
    r['email']   = emails[0].lower() if emails else ''
    r['h2_email'] = emails[1].lower() if len(emails) > 1 else ''

    r['aadhar'] = _find(text, r'(?:Sole/First\s+Holder\s+Aadhaar|First\s+Holder\s+Aadhaar)\s+(X+\d{4})')

    addr_raw = _find(text, r'Address\s+(B\s+\d+.+?)(?:Other\s+Address|pin\s+code)', 1, re.IGNORECASE | re.DOTALL)
    if addr_raw:
        addr_lines = [l.strip() for l in addr_raw.splitlines() if l.strip()]
        r['address1'] = addr_lines[0] if addr_lines else ''
        r['address2'] = addr_lines[1] if len(addr_lines) > 1 else ''
    else:
        r['address1'] = r['address2'] = ''

    city = ''
    for c in ['Mumbai', 'Delhi', 'Bangalore', 'Chennai', 'Pune', 'Ahmedabad', 'Hyderabad', 'Kolkata']:
        if c.upper() in text.upper():
            city = c
            break
    r['city'] = city or ''

    state_raw  = _find(text, r'\bState\s+(MAHARASHTR[A]?|GUJARAT|KARNATAKA|TAMIL\s*NADU|DELHI|RAJASTHAN|[A-Z]{4,20})\b')
    r['state'] = 'Maharashtra' if 'MAHARASHTR' in state_raw.upper() else state_raw.title()

    pin_matches = re.findall(r'pin\s+code\s+(\d{6})', text, re.IGNORECASE)
    r['pin_code'] = pin_matches[0] if pin_matches else ''

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

    r['mandate_h2_dep']         = 'NSDL'
    r['mandate_h2_dpid']        = r['dp_id']
    r['mandate_h2_dp_clientid'] = r['dp_client_id']

    r.update({
        'account_type': 'S', 'taxable': 'Y', 'auto_mail': 'Y',
        'family_head': 'Y', 'share_report': 'P', 'country': 'India',
        'nationality': 'Indian', 'fatca': 'N', 'ubo': 'N',
        'commit_amount': '0', 'accounting_txn': 'CS', 'stt_taken_as': 'E',
        'mode_of_holding': 'J' if r.get('h2_name') else 'S',
        'account_operation_type': 'A',
        'cml_source_file': Path(pdf_path).name,
    })
    return r

# ── DB operations ────────────────────────────────────────────────────────── #

def upsert_client(data: dict) -> int:
    """Insert or update client record. Returns the row id."""
    init_db()
    cols = [c for c in data if data[c] is not None and data[c] != '']
    # Map dict keys to DB column names (snake_case already matches)
    with get_connection() as conn:
        existing = None
        if data.get('dp_id') and data.get('dp_client_id'):
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


def list_clients() -> list:
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            'SELECT * FROM clients ORDER BY created_at DESC'
        ).fetchall()
        return [dict(r) for r in rows]


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
        'userName':             client.get('ws_account_code') or '',
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
        'commitAmount':         client.get('commit_amount') or '0',
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
        "<span style='color:#fff;font-size:14px;margin-left:8px'>by GoldStandard</span>"
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
