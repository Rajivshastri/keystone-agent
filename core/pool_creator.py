"""
core/pool_creator.py — orchestrator for the Pool Creation workflow.

Parses a CML + Schedule-A PDF, suggests an IA Code from the portfolio
name, and orchestrates the 5-step WS portal flow:

  IA → Scheme → Benchmark → Bank Master → Pool

via ws_uploader bridge functions, persisting the result into
pools_hub.json. Every WS write goes through a fresh login (force_fresh
on _pc_open_session) — observed empirically that this WS install
gates POSTs to recently-authenticated sessions even when the cached
session passes the read-validity check.
"""
from __future__ import annotations
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import ws_uploader
from core.pools_hub import PoolsHub
from core.client_onboarding import parse_cml_pdf

logger = logging.getLogger(__name__)


# ── CML parsing ─────────────────────────────────────────────────────────── #

def parse_cml_for_pool(pdf_path: str) -> dict:
    """Extract pool-creation fields from an NSDL CML PDF.

    Re-uses core.client_onboarding.parse_cml_pdf for the shared fields
    (dp_id, dp_client_id), adds pool-specific extractions on top:
      - start_date: a/c activation date (dd/MM/yyyy)
      - custodian:  short label mapped from the DP header
                    ('DP:ICICI BANK LIMITED[IN301348]' → 'ICICI')
      - dp:         always 'NSDL' for NSDL CMLs (matches the DP picklist)
    """
    cml = parse_cml_pdf(pdf_path)

    import pdfplumber
    text = ''
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += (page.extract_text(x_tolerance=3, y_tolerance=3) or '') + '\n'

    # Dump the raw extracted text for diagnostics — lets us see exactly
    # what pdfplumber returns when a regex misses.
    try:
        import os as _os
        _data_dir = _os.environ.get("KEYSTONE_DATA_DIR")
        _dbg_dir = Path(_data_dir) / "data" / "ws_pool_creator_probe" \
                    if _data_dir else Path(pdf_path).parent
        _dbg_dir.mkdir(parents=True, exist_ok=True)
        (_dbg_dir / "cml_extracted_text.txt").write_text(text, encoding="utf-8")
    except Exception:
        pass

    # 1st attempt: proximity-based — take the first dd/MM/yyyy following
    # "a/c activation" (header spans two lines + real value on a later line).
    act = re.search(r'a/c\s+activation.{0,300}?(\d{2}/\d{2}/\d{4})',
                    text, re.IGNORECASE | re.DOTALL)
    start_date = act.group(1) if act else ''

    # 2nd attempt: first dd/MM/yyyy anywhere in the document. NSDL CMLs use
    # dd-MM-yyyy (dash) for DOBs and dd-MMM-yyyy for business/print dates —
    # the only *slashed* dd/MM/yyyy tends to be the a/c activation date.
    if not start_date:
        fallback = re.search(r'\b(\d{2}/\d{2}/\d{4})\b', text)
        start_date = fallback.group(1) if fallback else ''

    dp_header = re.search(r'DP\s*:\s*([A-Z][A-Z &.]+?)\s*\[', text)
    custodian_full = dp_header.group(1).strip() if dp_header else ''
    up = custodian_full.upper()
    if   'ICICI'  in up: custodian = 'ICICI'
    elif 'KOTAK'  in up: custodian = 'KOTAK'
    elif 'HDFC'   in up: custodian = 'HDFC'
    elif 'AXIS'   in up: custodian = 'AXIS'
    elif 'ORBIS'  in up: custodian = 'ORBIS'
    elif 'NUVAMA' in up: custodian = 'NUVAMA'
    else:                custodian = custodian_full.title()

    # Bank fields — pulled directly from the CML's "financial details" line
    # so the operator doesn't have to retype them. Branch address + IFSC +
    # MICR all live on a single (sometimes wrapped) line; the regexes
    # tolerate that with the IGNORECASE / DOTALL flags.
    bank_acno = ''
    bank_acno_m = re.search(r'bank\s+account\s+number\s+([A-Z0-9]+)', text, re.IGNORECASE)
    if bank_acno_m:
        bank_acno = bank_acno_m.group(1)
    bank_actype = ''
    bank_actype_m = re.search(r'bank\s+account\s+type\s+([A-Za-z]+)', text, re.IGNORECASE)
    if bank_actype_m:
        bank_actype = bank_actype_m.group(1)
    bank_ifsc = ''
    bank_ifsc_m = re.search(r'ifsc\s+code\s+([A-Z0-9]+)', text, re.IGNORECASE)
    if bank_ifsc_m:
        bank_ifsc = bank_ifsc_m.group(1).upper()
    bank_micr = ''
    bank_micr_m = re.search(r'micr\s+code\s+(\d+)', text, re.IGNORECASE)
    if bank_micr_m:
        bank_micr = bank_micr_m.group(1)
    bank_name = ''
    bank_name_m = re.search(r'\bbank\s+name\s+([^\n]+?)(?=\s{2,}|POA/DDPI|tax\s+ded|$)',
                            text, re.IGNORECASE)
    if bank_name_m:
        bank_name = bank_name_m.group(1).strip().rstrip('.').strip()
        # CMLs sometimes return the bank name as a single concatenated
        # token (e.g. "HDFCBANK", "ICICIBANK") because the PDF layout
        # collapses the space. Split common bank-prefix patterns so the
        # frontend's toTC renders them as "HDFC Bank" / "ICICI Bank"
        # rather than "Hdfcbank" / "Icicibank".
        _BANK_PREFIXES = ('HDFC', 'ICICI', 'AXIS', 'KOTAK', 'YES', 'IDFC',
                          'IDBI', 'INDUSIND', 'PNB', 'SBI', 'RBL', 'CITI')
        upper_name = bank_name.upper()
        for _pfx in _BANK_PREFIXES:
            if upper_name.startswith(_pfx + 'BANK') and ' ' not in bank_name:
                bank_name = f'{_pfx} BANK'
                break
    # Bank address: the CML wraps it across two lines; capture up to the
    # next double-space-separated label or pin code line.
    bank_branch = ''
    bank_branch_m = re.search(r'bank\s+address\s+([^\n]+?(?:\n[^\n]+?)?)\s*pin\s+code\s+(\d{6})',
                              text, re.IGNORECASE)
    if bank_branch_m:
        addr = re.sub(r'\s+', ' ', bank_branch_m.group(1)).strip()
        # CMLs often have stray ", ." or trailing punctuation from empty
        # placeholder fields — collapse any tail of just commas / dots / spaces.
        addr = re.sub(r'[,\.\s]+$', '', addr)
        pin  = bank_branch_m.group(2)
        bank_branch = f"{addr} - {pin}" if addr else f"PIN {pin}"
    pms_sebi = ''
    pms_sebi_m = re.search(r'PMS\s+SEBI\s+Registration\s+Number\s+(INP\d+)', text, re.IGNORECASE)
    if pms_sebi_m:
        pms_sebi = pms_sebi_m.group(1).upper()
    else:
        # Corporate / pool CMLs use a different label: "CC CM Id INP000009171"
        # — same value (the firm's PMS SEBI registration), different
        # formatting. Fall through to it when the explicit "PMS SEBI"
        # line isn't present.
        cc_cm_m = re.search(r'\bCC\s+CM\s+Id\s+(INP\d+)', text, re.IGNORECASE)
        if cc_cm_m:
            pms_sebi = cc_cm_m.group(1).upper()

    # Bank account holder name. CMLs label this differently between
    # individual and corporate/pool layouts:
    #   • individual: "Sole/First Holder Name INDRANI BHATTACHARYYA"
    #   • corporate: "Bank Account Holder GOLDSTANDARD WEALTH PVT LTD" or
    #                "Account Holder Name GOLDSTANDARD WEALTH PVT LTD"
    # Try the corporate label first (rarer but more specific) so a pool
    # CML doesn't accidentally pick up the holder's personal name.
    bank_acholder = ''
    for pat in (
        r'bank\s+account\s+holder\s+(?:name\s+)?([A-Z][A-Z &.\-\']{2,80}?)(?=\s{2,}|$|\n)',
        r'\baccount\s+holder\s+name\s+([A-Z][A-Z &.\-\']{2,80}?)(?=\s{2,}|$|\n)',
        r'sole/first\s+holder\s+name\s+([A-Z][A-Z &.\-\']{2,80}?)(?=\s+client\s+option|\s{2,}|$|\n)',
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            bank_acholder = m.group(1).strip().rstrip('.').strip()
            break

    # Exchange codes — pool CMLs carry a single CC Id and CM BP Id
    # (no NSE/BSE split — the values are exchange-agnostic). Both sit
    # on a one-liner like:
    #   "CC Id IN001150 CC CM Id INP000009171 CM BP Id IN594271"
    # The CC Id pattern uses a negative lookahead to avoid matching
    # the "CC CM Id" label that immediately follows.
    cc_id_m  = re.search(r'\bCC\s+Id\s+(?!CM\b)([A-Z0-9]+)', text, re.IGNORECASE)
    cm_bp_m  = re.search(r'\bCM\s+BP\s+Id\s+([A-Z0-9]+)',    text, re.IGNORECASE)
    cc_id    = cc_id_m.group(1).upper()  if cc_id_m  else ''
    cm_bp_id = cm_bp_m.group(1).upper()  if cm_bp_m  else ''

    return {
        'start_date':     start_date,
        'dp_client_id':   cml.get('dp_client_id', ''),
        'dp':             'NSDL',
        'dpid':           cml.get('dp_id', ''),
        'custodian':      custodian,
        'custodian_full': custodian_full,
        # New CML-extracted fields used by the strategy DOCX. Operator
        # can override on the form; missing ones surface as empty
        # placeholders the operator fills in by hand.
        'bank_account':           bank_acno,
        'bank_account_holder':    bank_acholder,
        'bank_account_type':      bank_actype,
        'bank_ifsc':              bank_ifsc,
        'bank_micr':              bank_micr,
        'bank_full_name':         bank_name,
        'bank_branch_address':    bank_branch,
        'pms_sebi_registration':  pms_sebi,
        # Exchange codes — single CC Id + CM BP Id per pool. Present on
        # corporate / pool CMLs, blank on individual CMLs.
        'cc_id':                  cc_id,
        'cm_bp_id':               cm_bp_id,
    }


# ── Schedule-A parsing ──────────────────────────────────────────────────── #

def parse_schedule_a(pdf_path: str) -> dict:
    """Extract IA Name + Strategy + Benchmark from a Schedule-A portfolio PDF.

    The first page has the portfolio name as a centered heading, then
    'Investment Objective:', 'Strategy:', etc.

    The benchmark sits in a later paragraph (sometimes second page) under
    the heading 'Appropriateness of the Benchmark:' which reads
    'The performance would be benchmarked against <NAME>.' so the
    extractor needs the full document text, not just the first 5000
    chars the IA-name lookup needs.
    """
    import pdfplumber
    text = ''
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text += (page.extract_text(x_tolerance=2, y_tolerance=3) or '') + '\n'

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # IA Name: first heading-like line after SCHEDULE A (stops at the
    # 'Investment Objective:' / 'Strategy:' section headers).
    ia_name = ''
    for i, ln in enumerate(lines):
        if re.match(r'^SCHEDULE\s+A', ln, re.IGNORECASE):
            for cand in lines[i + 1: i + 6]:
                if re.match(r'^(Investment\s+Objective|Strategy|Description|Allocation|Risks?)',
                             cand, re.IGNORECASE):
                    break
                if 8 <= len(cand) <= 100:
                    ia_name = cand
                    break
            if ia_name:
                break

    if not ia_name:
        # Fallback: pick the first line in top 30 that looks like a title
        for ln in lines[:30]:
            if ('Portfolio' in ln or 'Fund' in ln) and 8 < len(ln) < 100:
                ia_name = ln
                break

    # Schedule-A Strategy line looks like one of:
    #   "Strategy: Equity."             (period terminator — older docs)
    #   "Strategy: Multi Asset\n"       (no period — newer docs like
    #                                     Non-Discretionary Portfolio)
    # Accept either a period OR a newline as the terminator. Require
    # the colon so prose like "The strategy where…" isn't mis-matched.
    strat_m = re.search(r'Strategy\s*:\s*([A-Z][A-Za-z ]{2,30}?)\s*(?:\.|\n)', text)
    strategy = strat_m.group(1).strip() if strat_m else ''

    # Benchmark — sits in the "Appropriateness of the Benchmark" paragraph:
    #   "The performance would be benchmarked against <NAME>. The
    #    composition of the benchmark is such that..."
    # Match lazily until the first period; tolerate optional "the" before
    # the name and any whitespace (including line breaks) within it.
    benchmark = ''
    bm_m = re.search(
        r'benchmarked\s+against\s+(?:the\s+)?([A-Z][^.\n]+?)\s*\.\s*The\s+composition',
        text, re.IGNORECASE | re.DOTALL,
    )
    if bm_m:
        # Collapse internal whitespace (PDFs often wrap mid-name) and strip
        # any trailing punctuation that snuck in.
        benchmark = re.sub(r'\s+', ' ', bm_m.group(1)).strip().rstrip(',;:').strip()
    else:
        # Fallback: looser pattern when "The composition" sentinel is
        # absent or worded differently — first period after the keyword.
        bm_m2 = re.search(
            r'benchmarked\s+against\s+(?:the\s+)?([A-Z][^.\n]{2,80}?)\s*\.',
            text, re.IGNORECASE | re.DOTALL,
        )
        if bm_m2:
            benchmark = re.sub(r'\s+', ' ', bm_m2.group(1)).strip().rstrip(',;:').strip()

    return {
        'ia_name':   ia_name.strip(),
        'strategy':  strategy,
        'benchmark': benchmark,
    }


# ── IA Code heuristic ─────────────────────────────────────────────────── #

_IA_CODE_STOPWORDS = {
    'A', 'AN', 'THE', 'OF', 'AND', 'FOR', 'TO', 'IN', 'ON',
    'PORTFOLIO', 'PMS', 'STRATEGY', 'FUND', 'ADVISORY',
    'NON', 'DISCRETIONARY', 'WEALTH', 'CAPITAL', 'ASSET',
}


def suggest_ia_code(ia_name: str,
                    existing_codes: Optional[List[str]] = None) -> str:
    """Suggest an IA code matching the GOLD<xxx>PMS pattern.

    Heuristic:
      - tokenise name on non-letters, uppercase, drop stop-words.
      - >= 3 significant tokens  → first-letter of each of the first three.
      - else                     → first 3 letters of the first token.
    Returns 'GOLD<xxx>PMS'. If that's taken, appends digits.
    """
    tokens = [t for t in re.split(r'[^A-Za-z]+', (ia_name or '').upper()) if t]
    sig = [t for t in tokens if t not in _IA_CODE_STOPWORDS] or tokens[:1]
    if not sig:
        return ''

    if len(sig) >= 3:
        xxx = sig[0][:1] + sig[1][:1] + sig[2][:1]
    elif len(sig[0]) >= 3:
        xxx = sig[0][:3]
    else:
        xxx = (sig[0] + 'XXX')[:3]

    code = f'GOLD{xxx}PMS'
    used = {c.strip().upper() for c in (existing_codes or [])}
    if code not in used:
        return code
    for d in range(1, 10):
        cand = f'GOLD{xxx[:2]}{d}PMS' if len(xxx) >= 2 else f'GOLD{xxx}{d}PMS'
        if cand not in used:
            return cand
    return code


# ── Resolvers: free-text → WS-form values ─────────────────────────────── #

def resolve_fund_manager_id(name: str, fm_list: List[dict]) -> str:
    """Resolve a fund-manager name (free-text) to the numeric fundmgrid id.

    Entries in fm_list look like {'value': '7', 'label': 'Nirman Vithlani - 7'}.
    """
    target = (name or '').strip().lower()
    if not target:
        return ''
    for fm in fm_list:
        lab = (fm.get('label') or '').lower()
        base = lab.split(' - ')[0].strip()
        if base == target or lab == target:
            return fm['value']
    for fm in fm_list:
        if target in (fm.get('label') or '').lower():
            return fm['value']
    return ''


def resolve_strategy_code(strategy: str, strat_list: List[dict]) -> str:
    """Map 'Equity' → 'EQUITY'. Falls through to a keyword heuristic."""
    target = (strategy or '').strip().lower()
    if not target:
        return ''
    for s in strat_list:
        if s['value'].lower() == target or s['label'].lower() == target:
            return s['value']
    if 'equity' in target: return 'EQUITY'
    if 'debt'   in target: return 'DEBT'
    if 'hybrid' in target: return 'HYBRID'
    return 'OTHERS'


def resolve_dp_dpid(custodian: str, dp_list: List[dict]) -> Tuple[str, str]:
    """Map a custodian short-label ('ICICI') to (dp, dpid).

    Matches against the DP row's 'dp_name' column which is the custodian
    bank label ('ICICI BANK LTD', 'Kotak Mahindra Bank Limited', …).
    """
    target = (custodian or '').strip().lower()
    if not target:
        return ('', '')
    for d in dp_list:
        if target in (d.get('dp_name') or '').lower():
            return (d['dp'], d['dpid'])
    return ('', '')


# ── Orchestrator ──────────────────────────────────────────────────────── #

class PoolCreationResult:
    def __init__(self):
        self.ia: Dict = {}
        self.scheme: Dict = {}
        self.pool: Dict = {}
        self.bank_master: Dict = {}
        self.persisted: bool = False
        self.errors: List[str] = []

    def to_dict(self) -> dict:
        return {
            'ia':          self.ia,
            'scheme':      self.scheme,
            'pool':        self.pool,
            'bank_master': self.bank_master,
            'persisted':   self.persisted,
            'errors':      self.errors,
        }


# Custodian short-label → bank-name suffix the WS Bank Master form expects.
# Reads from config/sources.json's `is_bank=true` rows where each one
# can carry a ``bank_full_name`` field. Operators editing the source
# config thus pick up automatically. Falls back to the GoldStandard
# defaults so existing deployments without the new field still work.
def _custodian_to_bank_name() -> dict:
    """{CUSTODIAN_SHORT_UPPER: 'Bank Full Name'} sourced from
    config/sources.json via core.bank_registry. Falls back to the
    GoldStandard 4-bank defaults when the registry has nothing
    populated yet (e.g. fresh deployment with empty bank_full_name)."""
    out: dict = {
        'ICICI': 'ICICI Bank Ltd.',
        'AXIS':  'Axis Bank Ltd.',
        'HDFC':  'HDFC Bank Ltd.',
        'KOTAK': 'Kotak Mahindra Bank Ltd.',
    }
    try:
        from core.bank_registry import enabled_banks
        for b in enabled_banks():
            if b.get('bank_full_name'):
                out[b['short'].upper()] = b['bank_full_name']
    except Exception as e:
        logger.warning(f'bank_registry unavailable, using defaults: {e}')
    return out


# Back-compat alias for any caller that imports the constant directly.
# Resolved on each access so a sources.json edit takes effect without
# a restart.
class _CustodianBankNameProxy:
    def get(self, key, default=None): return _custodian_to_bank_name().get(key, default)
    def __getitem__(self, key):       return _custodian_to_bank_name()[key]
    def __contains__(self, key):      return key in _custodian_to_bank_name()
    def keys(self):                   return _custodian_to_bank_name().keys()
    def items(self):                  return _custodian_to_bank_name().items()

CUSTODIAN_TO_BANK_NAME = _CustodianBankNameProxy()


def _derive_bank_master_code(data: dict, pool_id: str, mapin: str) -> str:
    """Produce a Bank Master code (≤ 8 chars to fit the WS form). Order:
       caller override → MAPIN if short → first 8 chars of pool_id.
    """
    explicit = (data.get('bank_master_code') or '').strip()
    if explicit:
        return explicit[:8]
    m = (mapin or '').strip()
    if m and len(m) <= 8:
        return m
    pid = (pool_id or '').strip()
    return pid[:8] if pid else ''


def preview(data: dict, hub: Optional[PoolsHub] = None,
             auth_cache_path: str = '') -> dict:
    """Check what create_pool() would do without writing anything.

    Calls WS once to get the current IA list (to decide create/reuse for IA),
    then consults the local hub for scheme reuse. Pool is always 'create'.
    """
    if hub is None:
        hub = PoolsHub.load()

    listing = ws_uploader.ws_list_investment_approaches(auth_cache_path=auth_cache_path)
    ia_rows = listing['rows']
    ia_code = (data.get('ia_code') or '').strip().upper()

    ia_action = 'create'
    ia_match = None
    for r in ia_rows:
        if r['option_value'].strip().upper() == ia_code:
            ia_action = 'reuse'
            ia_match = r
            break

    scheme_name = (data.get('scheme_name') or data.get('ia_name') or '').strip()
    scheme_action = 'reuse' if hub.scheme_in_ia(ia_code, scheme_name) else 'create'

    return {
        'ia':     {'action': ia_action, 'ia_code': ia_code,
                   'existing': ia_match},
        'scheme': {'action': scheme_action, 'scheme_name': scheme_name,
                   'ia_code': ia_code},
        'pool':   {'action': 'create'},
    }


def create_pool(data: dict, hub: Optional[PoolsHub] = None,
                 auth_cache_path: str = '') -> PoolCreationResult:
    """End-to-end: create/reuse IA → Scheme → Pool on WS, persist to hub.

    `data` keys (all strings unless noted):
      IA:            ia_code, ia_name, ia_no (optional int)
      Scheme:        scheme_name (defaults to ia_name), start_date (dd/MM/yyyy),
                     fund_manager (name or id), strategy
      Pool:          dp_client_id, custody_scheme_code,
                     custodian (e.g. 'ICICI')  OR  dp + dpid directly,
                     mapin (Scheme MapinId for Pool Master form),
                     pool_id, display_name, active (bool),
                     ws_scheme_code, ws_scheme_names (list),
                     custodian_bank, custodian_code, pool_demat_account,
                     bank, bank_account, dealer_account,
                     kotak_client_id, ws_overrides (list)
                     (broker aliases live on broker_map.json, not on the pool)
    """
    if hub is None:
        hub = PoolsHub.load()
    result = PoolCreationResult()

    try:
        # force_fresh=True: cached sessions on this WS install pass the
        # read-validity check but bounce write POSTs to /auth.do with
        # a fresh JSESSIONID in the response (errorCSRF.jsp on Step B).
        # Pay the ~3s fresh-login cost so the IA/scheme/bank/pool/
        # benchmark POSTs all run on a write-eligible session.
        session, base = ws_uploader._pc_open_session(
            auth_cache_path, force_fresh=True)

        # ── Step 1: IA ─────────────────────────────────────────────────────
        ia_code = (data.get('ia_code') or '').strip()
        ia_name = (data.get('ia_name') or '').strip()
        if not ia_code or not ia_name:
            result.errors.append('ia_code and ia_name are required')
            return result

        ia_res = ws_uploader.ws_create_investment_approach(
            ia_code, ia_name,
            ia_no=data.get('ia_no'),
            session=session, base=base,
        )
        session = ia_res.get('session', session)
        if ia_res['status'] not in ('created', 'exists'):
            result.errors.append(f"IA create: unexpected status {ia_res}")
            return result

        row = ia_res['row']
        result.ia = {
            'action':  'reused' if ia_res['status'] == 'exists' else 'created',
            'ia_code': row['option_value'],
            'ia_name': row['option_desc'],
            'ia_no':   int(row.get('option_seq') or '0'),
        }
        hub.upsert_ia({
            'ia_code':      row['option_value'],
            'ia_name':      row['option_desc'],
            'ia_no':        int(row.get('option_seq') or '0'),
            'fund_manager': data.get('fund_manager', ''),
            'strategy':     data.get('strategy', ''),
        })

        # ── Step 2: Scheme ────────────────────────────────────────────────
        scheme_name = (data.get('scheme_name') or ia_name).strip()
        existing_scheme = hub.scheme_in_ia(ia_code, scheme_name)

        if existing_scheme:
            result.scheme = {
                'action':      'reused',
                'scheme_name': existing_scheme['scheme_name'],
                'ia_code':     existing_scheme['ia_code'],
            }
        else:
            fm_list    = ws_uploader.ws_list_fund_managers(session=session, base=base)
            strat_list = ws_uploader.ws_list_strategies(session=session, base=base)

            fm_raw = str(data.get('fund_manager') or '').strip()
            fund_mgr_id = fm_raw if fm_raw.isdigit() else resolve_fund_manager_id(fm_raw, fm_list)
            if not fund_mgr_id:
                avail = ', '.join(f.get('label', '') for f in fm_list)
                result.errors.append(f"Unknown fund manager {fm_raw!r}. "
                                     f"Available: {avail}")
                return result

            strat_code = resolve_strategy_code(data.get('strategy', ''), strat_list)
            if not strat_code:
                result.errors.append(f"Unknown strategy {data.get('strategy')!r}")
                return result

            sch_res = ws_uploader.ws_create_scheme(
                scheme_name=scheme_name,
                start_date=(data.get('start_date') or '').strip(),
                fund_manager_id=fund_mgr_id,
                ia_code=ia_code,
                strategy_code=strat_code,
                session=session, base=base,
            )
            session = sch_res.get('session', session)
            result.scheme = {
                'action':       'created',
                'scheme_name':  scheme_name,
                'ia_code':      ia_code,
                'start_date':   data.get('start_date', ''),
                'fund_manager': fm_raw,
                'strategy':     strat_code,
            }
            hub.upsert_scheme({
                'scheme_name':           scheme_name,
                'ia_code':               ia_code,
                'start_date':            data.get('start_date', ''),
                'fund_manager':          fm_raw,
                'strategy':              strat_code,
                'benchmark_index_code':  (data.get('benchmark_index_code') or '').strip(),
                'benchmark_description': (data.get('benchmark_description') or '').strip(),
            })

        # ── Step 2.5: Benchmark assignment ────────────────────────────────
        # When the operator supplies benchmark fields, attach the
        # benchmark to the scheme right after creating it. Skipped for
        # 'reused' schemes (the benchmark is already set from prior run)
        # and when no benchmark code is supplied. Failures here are
        # logged but don't block the pool — the operator can attach the
        # benchmark via the WS UI as a follow-up.
        bm_code = (data.get('benchmark_index_code') or '').strip()
        bm_name = (data.get('benchmark_description') or '').strip()
        if bm_code and result.scheme.get('action') == 'created':
            try:
                scheme_id = ws_uploader.ws_lookup_scheme_id(
                    scheme_name, session=session, base=base)
                if scheme_id:
                    bm_res = ws_uploader.ws_assign_benchmark(
                        scheme_id=scheme_id,
                        index_code=bm_code,
                        index_name=bm_name or bm_code,
                        weight_pct="100.0",
                        session=session, base=base,
                    )
                    session = bm_res.get('session', session)
                    result.scheme['benchmark_status'] = bm_res.get('status')
                    result.scheme['benchmark_index_code'] = bm_code
                    if bm_res.get('status') != 'assigned':
                        result.errors.append(
                            f"benchmark assign returned {bm_res.get('status')!r} "
                            f"(persisted_code={bm_res.get('persisted_code')!r}) — "
                            f"attach via WS UI as follow-up"
                        )
                else:
                    result.errors.append(
                        f"benchmark not assigned: could not look up scheme_id "
                        f"for {scheme_name!r}"
                    )
            except Exception as bm_e:
                logger.exception('Benchmark assignment failed (non-fatal)')
                result.errors.append(f'benchmark assign: {bm_e}')

        # ── Step 3: Bank Master ──────────────────────────────────────────
        # Bank now runs BEFORE Pool (was best-effort and ran after the pool
        # was already persisted). The bank record must exist on WS before
        # the pool can reference it operationally, and the new contract
        # makes Bank fields mandatory — operator validation gates this on
        # the frontend, but defensive checks live here too.
        mapin = (data.get('mapin') or '').strip()
        if not mapin:
            result.errors.append('mapin is required (Pool Master → Scheme MapinId)')
            return result

        bank_acct   = (data.get('bank_account') or '').strip()
        bank_full   = (data.get('bank_full_name') or '').strip()
        if not bank_full:
            cust_short = (data.get('custodian') or
                          data.get('bank') or '').strip().upper()
            bank_full = CUSTODIAN_TO_BANK_NAME.get(cust_short, '')
        bank_holder = (data.get('bank_account_holder') or '').strip()
        bank_actype = (data.get('bank_account_type') or 'Current').strip()
        # Pool ID is needed to derive the bank code (≤ 8 chars).
        pool_id_for_bank = (data.get('pool_id') or '').strip()
        bank_code   = _derive_bank_master_code(data, pool_id_for_bank, mapin)

        missing_bank = []
        if not bank_acct:   missing_bank.append('bank_account')
        if not bank_full:   missing_bank.append('bank_full_name')
        if not bank_holder: missing_bank.append('bank_account_holder')
        if not bank_code:   missing_bank.append('bank_master_code')
        if missing_bank:
            result.errors.append(
                'Bank Master fields missing: ' + ', '.join(missing_bank)
                + ' — every Bank Master field is mandatory now.'
            )
            return result

        try:
            bm = ws_uploader.ws_create_bank_master(
                bank_code=bank_code,
                bank_account_id=bank_acct,
                bank_name=bank_full,
                bank_account_name=bank_holder,
                account_type=bank_actype,
                mapin=mapin,
                session=session, base=base,
            )
        except Exception as bm_e:
            logger.exception('Bank Master step failed — pool will NOT be created')
            result.bank_master = {
                'action': 'error',
                'error':  f'{type(bm_e).__name__}: {bm_e}',
            }
            result.errors.append(f'bank-master create: {bm_e}')
            return result
        txt_low = (bm.get('response_text') or '').lower()
        ok      = 'successfully completed' in txt_low
        dup     = ('duplicate' in txt_low or 'already exists' in txt_low
                   or 'code already' in txt_low)
        action  = 'created' if ok else ('exists' if dup else 'unclear')
        result.bank_master = {
            'action':       action,
            'bank_code':    bank_code,
            'bank_name':    bank_full,
            'bank_account': bank_acct,
            'http_status':  bm.get('http_status'),
        }
        if action == 'unclear':
            result.errors.append(
                f'bank-master create: unclear response (HTTP '
                f'{bm.get("http_status")}, '
                f'{len(bm.get("response_text", ""))} bytes) — '
                f'pool not created. Inspect editBankMaster.do manually.'
            )
            return result

        # ── Step 4: Pool ──────────────────────────────────────────────────
        dp   = (data.get('dp')   or '').strip()
        dpid = (data.get('dpid') or '').strip()
        if not (dp and dpid):
            custodian = (data.get('custodian') or '').strip()
            dp_list = ws_uploader.ws_list_depositories(session=session, base=base)
            dp, dpid = resolve_dp_dpid(custodian, dp_list)
        if not (dp and dpid):
            result.errors.append('dp + dpid could not be resolved — '
                                 'provide them directly or give a valid custodian label')
            return result

        ws_uploader.ws_create_pool(
            dp=dp, dpid=dpid,
            dp_client_id=(data.get('dp_client_id') or '').strip(),
            scheme_mapin_id=mapin,
            description=ia_name,   # per spec: Pool Description = IA Name
            custody_scheme_code=(data.get('custody_scheme_code') or '').strip(),
            session=session, base=base,
        )

        pool_id = (data.get('pool_id') or '').strip()
        if not pool_id:
            result.errors.append('pool_id is required for local storage')
            return result

        pool_row = {
            'pool_id':              pool_id,
            'display_name':         (data.get('display_name') or ia_name).strip(),
            'active':               bool(data.get('active', True)),
            'mapin':                mapin,
            'ws_scheme_code':       (data.get('ws_scheme_code') or mapin).strip(),
            'ws_scheme_names':      data.get('ws_scheme_names') or [scheme_name.lower()],
            'ia_code':              ia_code,
            'scheme_name':          scheme_name,
            'custodian_bank':       (data.get('custodian_bank') or data.get('custodian') or '').strip(),
            'custodian_code':       (data.get('custody_scheme_code') or '').strip(),
            'pool_demat_account':   (data.get('pool_demat_account') or data.get('dp_client_id') or '').strip(),
            'bank':                 (data.get('bank') or data.get('custodian') or '').strip(),
            'bank_account':         (data.get('bank_account') or '').strip(),
            'dealer_account':       (data.get('dealer_account') or '').strip(),
            'kotak_client_id':      (data.get('kotak_client_id') or '').strip(),
            'ws_overrides':         data.get('ws_overrides', []),
            'dp':                   dp,
            'dp_id':                dpid,
            'dp_client_id':         (data.get('dp_client_id') or '').strip(),
            # Bank Master detail kept on the pool record so the Edit Pool
            # UI can show what was used at creation. Mirrors the fields
            # the Bank Master step writes to WS, plus the CML-extracted
            # branch / IFSC / MICR used by the strategy DOCX.
            'bank_master_code':     (data.get('bank_master_code') or '').strip(),
            'bank_full_name':       (data.get('bank_full_name') or '').strip(),
            'bank_account_holder':  (data.get('bank_account_holder') or '').strip(),
            'bank_account_type':    (data.get('bank_account_type') or 'Current').strip(),
            'bank_ifsc':            (data.get('bank_ifsc') or '').strip(),
            'bank_micr':            (data.get('bank_micr') or '').strip(),
            'bank_branch_address':  (data.get('bank_branch_address') or '').strip(),
            # Exchange codes — used by the broker invitation strategy DOCX.
            # cc_id + cm_bp_id auto-fill from the CML; ucc_code + cp_code are
            # operator-entered post-onboarding (not in the CML PDF). All
            # optional at create-time; can be filled in later via Edit Pool
            # and the DOCX regenerated.
            'ucc_code':             (data.get('ucc_code') or '').strip(),
            'cp_code':              (data.get('cp_code') or '').strip(),
            'cc_id':                (data.get('cc_id') or '').strip(),
            'cm_bp_id':             (data.get('cm_bp_id') or '').strip(),
            # Benchmark linkage — populated when the Benchmarks tab is wired
            # in Slice 3. Stored on the pool so Edit Pool can show/edit it.
            'benchmark_index_code':  (data.get('benchmark_index_code') or '').strip(),
            'benchmark_description': (data.get('benchmark_description') or '').strip(),
        }
        hub.upsert_pool(pool_row)
        hub.save()
        result.pool = {
            'action':              'created',
            'pool_id':             pool_id,
            'mapin':               mapin,
            'dp':                  dp,
            'dpid':                dpid,
            'dp_client_id':        pool_row['dp_client_id'],
            'custody_scheme_code': pool_row['custodian_code'],
        }
        result.persisted = True

    except Exception as e:
        logger.exception("Pool creation failed")
        result.errors.append(f"{type(e).__name__}: {e}")

    return result
