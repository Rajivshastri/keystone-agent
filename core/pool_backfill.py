"""
core/pool_backfill.py — one-time enrichment of pools_hub.json from WS masters.

Pools created via the new Pool Creator workflow get Bank/IA/Scheme/Benchmark
detail persisted at creation time. Pools that pre-date that flow only have
a partial record (mapin, dealer_account, custodian fields) — this module
fills in the gaps by reading the auto-downloaded WS master files:

  - data/{date}/masters/SchemeMaster.xls       — scheme detail + fund manager
  - data/{date}/masters/BankMaster.xls         — bank master row per bank a/c id
  - data/{date}/masters/BenchmarkMapping.xls   — scheme → benchmark linkage

Output is a *draft* per pool. The operator reviews each draft in the
Backfill Review screen and approves/edits/skips before we mutate
pools_hub.json. Idempotent — re-running just produces fresh drafts.

Linkage rules:
  pool.scheme_name (case-insensitive)  → SchemeMaster.SCHEME_NAME → SCHEMEID
  SCHEMEID + SCOPE='Scheme'            → BenchmarkMapping → BENCHMARK_CODE/NAME
  pool.bank_account (exact match)      → BankMaster.BANK_A/C_ID → bank fields

Why .xls (not .xlsx): WS exports the masters as .xls. xlrd reads them.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Account-type code → human label (mirrors WS Bank Master select options).
BANK_AC_TYPE_LABEL = {
    'S': 'Savings',
    'C': 'Current',
    'COLLECT': 'Collection',
    'OPERATIV': 'Operative',
    'INVEST': 'Investment',
}


@dataclass
class FieldDelta:
    """One proposed change. Stored on the draft; the UI renders these as
    diff rows the operator can accept/reject individually."""
    field: str          # pool record field name
    current: Any        # what's in pools_hub.json today
    proposed: Any       # what we'd write
    source: str         # 'SchemeMaster' | 'BankMaster' | 'BenchmarkMapping'
    confidence: str = 'high'   # 'high' | 'medium' | 'low'


@dataclass
class PoolDraft:
    """Per-pool enrichment proposal.

    Three kinds of deltas live separately because they target different
    records in pools_hub.json:

    - ``pool_deltas``    — fields that live on the POOL record
                            (bank_master_*, ia_code linkage, scheme_name link)
    - ``scheme_deltas``  — fields that live on the SCHEME record, keyed by
                            (ia_code, scheme_name). One scheme can be shared
                            by many pools (e.g. mysticv_icici + mysticv_kotak
                            both ride 'Mystic Wealth Value Portfolio')
    - ``ia_deltas``      — fields on the INVESTMENT APPROACH record, keyed
                            by ia_code. The IA itself is created on WS via
                            pool_creator; backfill only fills local cache
                            entries when the IA already exists on WS

    ``needs_ia_heal`` flips when the pool already has ia_code set AND the
    backfill has matched_ia for it AND that IA is missing from
    investment_approaches[]. The frontend treats this as actionable so
    Apply isn't disabled — without it, "no proposed changes" hides the
    healing opportunity even though the data is right there to mirror.

    The Backfill Review UI renders all three and the operator can
    accept/reject each delta independently.
    """
    pool_id: str
    matched_scheme: Optional[Dict] = None       # row from SchemeMaster
    matched_bank: Optional[Dict] = None         # row from BankMaster
    matched_benchmark: Optional[Dict] = None    # row from BenchmarkMapping
    matched_ia: Optional[Dict] = None           # entry from WS IA list
    matched_pool_master: Optional[Dict] = None  # row from PoolMaster

    pool_deltas:   List[FieldDelta] = field(default_factory=list)
    scheme_deltas: List[FieldDelta] = field(default_factory=list)
    ia_deltas:     List[FieldDelta] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    needs_ia_heal: bool = False                  # see class docstring

    def all_deltas_for_ui(self) -> List[Dict]:
        """Flatten all deltas with a kind tag for UI rendering."""
        out = []
        for d in self.pool_deltas:
            out.append({'kind': 'pool', 'field': d.field, 'current': d.current,
                         'proposed': d.proposed, 'source': d.source,
                         'confidence': d.confidence})
        for d in self.scheme_deltas:
            out.append({'kind': 'scheme', 'field': d.field, 'current': d.current,
                         'proposed': d.proposed, 'source': d.source,
                         'confidence': d.confidence})
        for d in self.ia_deltas:
            out.append({'kind': 'ia', 'field': d.field, 'current': d.current,
                         'proposed': d.proposed, 'source': d.source,
                         'confidence': d.confidence})
        return out

    def to_dict(self) -> dict:
        return {
            'pool_id':              self.pool_id,
            'matched_scheme':       self.matched_scheme,
            'matched_bank':         self.matched_bank,
            'matched_benchmark':    self.matched_benchmark,
            'matched_ia':           self.matched_ia,
            'matched_pool_master':  self.matched_pool_master,
            # 'deltas' kept for UI/test back-compat — all delta kinds combined.
            'deltas':            self.all_deltas_for_ui(),
            'notes':             self.notes,
            'needs_ia_heal':     self.needs_ia_heal,
        }


# ── XLS reading ─────────────────────────────────────────────────────────── #

def _read_xls_dicts(path: Path) -> List[Dict]:
    """Read sheet[0] of an .xls/.xlsx file into a list of {column: value} dicts.
    Returns [] if the file is missing. Header is row 0; each subsequent row
    becomes a dict keyed by stripped header text."""
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
    out = []
    for r in range(1, sh.nrows):
        row = {headers[c]: sh.cell_value(r, c) for c in range(sh.ncols)}
        out.append(row)
    return out


def _norm(s: Any) -> str:
    return str(s or '').strip().lower()


# Tokens that appear in pool display_names OR WS scheme names but not in
# both — stripping them before key-comparison normalises away the
# difference. Examples observed in production:
#   pool: "GoldStandard AI Portfolio"
#   WS:   "GoldStandard Wealth Pvt Ltd AI Portfolio"
# After stripping {wealth, pvt, ltd, portfolio} from both → "goldstandardai".
#
# Two layers: a hardcoded universal set + an operator-extensible list
# at config/scheme_noise_tokens.json. The operator list is for tokens
# that are firm-specific (asset-manager prefix, family-name suffixes
# on NRO pools) — these vary per tenant and shouldn't live in code.

# Universal — applies to every Indian PMS deployment.
_BUILTIN_NOISE_TOKENS = frozenset({
    # Custodians (industry-wide)
    'kotak', 'icici', 'hdfc', 'axis', 'orbis', 'nuvama',
    # NRO / custody markers
    'nro', 'custody',
    # Corporate suffixes
    'wealth', 'pvt', 'ltd', 'private', 'limited',
    # Generic scheme-name words
    'pms', 'portfolio',
})


def _operator_noise_tokens() -> set:
    """Read tenant-specific noise tokens from config/scheme_noise_tokens.json.

    Falls back to the GoldStandard defaults so existing deployments
    keep their current matching behaviour without a config-add. Other
    tenants edit/replace the JSON to provide their own asset-manager
    prefixes and family-name suffixes.
    """
    import json
    cfg_dir = os.environ.get('KEYSTONE_CONFIG_DIR')
    if cfg_dir:
        p = Path(cfg_dir) / 'scheme_noise_tokens.json'
    else:
        p = Path(__file__).parent.parent / 'config' / 'scheme_noise_tokens.json'
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return {str(t).strip().lower() for t in data if t}
            if isinstance(data, dict) and isinstance(data.get('tokens'), list):
                return {str(t).strip().lower() for t in data['tokens'] if t}
        except Exception as e:
            logger.warning(f'scheme_noise_tokens read failed: {e}')
    # GoldStandard defaults — operator can override by writing the file.
    return {
        'goldstandard', 'gold', 'standard',
        'mustafa', 'anshuman', 'kataria',
    }


def _noise_tokens() -> set:
    """The full noise-token set used by _scheme_name_key + matchers.
    Re-reads on every call so an operator edit takes effect on the
    next backfill run without a restart."""
    return _BUILTIN_NOISE_TOKENS | _operator_noise_tokens()


# Back-compat alias — earlier code references _NOISE_TOKENS as a static
# frozenset. Keep as a property-style proxy so any inline access still
# works while the matchers themselves call _noise_tokens() each time.
class _NoiseTokensProxy:
    def __contains__(self, item): return item in _noise_tokens()
    def __iter__(self):           return iter(_noise_tokens())
    def __len__(self):            return len(_noise_tokens())

_NOISE_TOKENS = _NoiseTokensProxy()


def _scheme_name_key(s: Any) -> str:
    """Loose key for matching scheme names — drops every noise token
    (anywhere in the name, not just trailing) and collapses to alphanumeric."""
    import re
    base = _norm(s)
    # Replace non-alphanumerics with spaces so we can tokenize cleanly.
    base = re.sub(r'[^a-z0-9]+', ' ', base)
    tokens = [t for t in base.split() if t and t not in _NOISE_TOKENS]
    return ''.join(tokens)


def _excel_date_to_iso(val: Any) -> str:
    """Excel serial date → ISO yyyy-mm-dd. Best-effort; returns '' on failure."""
    try:
        from datetime import datetime, timedelta
        if not val or val == '':
            return ''
        if isinstance(val, str):
            return val.strip()
        # Excel epoch: 1899-12-30 (off-by-one from 1900-01-01)
        n = float(val)
        return (datetime(1899, 12, 30) + timedelta(days=n)).strftime('%Y-%m-%d')
    except Exception:
        return ''


# ── Master loaders ──────────────────────────────────────────────────────── #

def _find_master_file(masters_dir: Path, basenames: List[str]) -> Optional[Path]:
    """Find the first existing master file by basename (case-insensitive)."""
    if not masters_dir.exists():
        return None
    for fn in basenames:
        cand = masters_dir / fn
        if cand.exists():
            return cand
    # Fall back to case-insensitive scan
    targets = {b.lower() for b in basenames}
    for p in masters_dir.iterdir():
        if p.is_file() and p.name.lower() in targets:
            return p
    return None


def load_masters(masters_dir: Path) -> Dict[str, List[Dict]]:
    """Load Scheme/Bank/BenchmarkMapping/Pool rows from a per-date masters dir."""
    return {
        'schemes': _read_xls_dicts(_find_master_file(masters_dir, [
            'SchemeMaster.xls', 'schememaster.xls',
        ]) or Path('/nonexistent')),
        'banks':   _read_xls_dicts(_find_master_file(masters_dir, [
            'BankMaster.xls', 'bankmaster.xls',
        ]) or Path('/nonexistent')),
        'bench_mappings': _read_xls_dicts(_find_master_file(masters_dir, [
            'BenchmarkMapping.xls', 'benchmarkmapping.xls',
        ]) or Path('/nonexistent')),
        # PoolMaster carries the demat tuple (DP/DPID/DPCLIENTID) and the
        # Kotak client account at REFCODE6 — fields that the SchemeMaster
        # export omits but trade recon and bank recon both rely on.
        'pools':   _read_xls_dicts(_find_master_file(masters_dir, [
            'PoolMaster.xls', 'poolmaster.xls',
        ]) or Path('/nonexistent')),
    }


REQUIRED_MASTER_FILES = ('SchemeMaster.xls', 'BankMaster.xls',
                         'BenchmarkMapping.xls', 'PoolMaster.xls')


def scan_date_dirs(data_dir: Path, limit: int = 10) -> List[Dict]:
    """Per-date diagnostic: which required masters are present in each
    recent data/{YYYY-MM-DD}/masters/ folder.

    Walks dates DESC and returns at most ``limit`` entries. Used for both
    picking the latest usable dir and reporting the gap when none qualify.
    """
    if not data_dir.exists():
        return []
    candidates = sorted(
        (p for p in data_dir.iterdir()
         if p.is_dir() and len(p.name) == 10 and p.name[4] == '-' and p.name[7] == '-'),
        reverse=True,
    )[:limit]
    out = []
    for d in candidates:
        masters = d / 'masters'
        present = {fn: bool(_find_master_file(masters, [fn]))
                   for fn in REQUIRED_MASTER_FILES}
        out.append({
            'date':         d.name,
            'masters_dir':  str(masters),
            'exists':       masters.exists(),
            'present':      present,
            'all_present':  all(present.values()),
            'missing':      [fn for fn, ok in present.items() if not ok],
        })
    return out


def latest_masters_dir(data_dir: Path) -> Optional[Path]:
    """Most recent date dir whose masters folder has all three required
    files. None if no dir qualifies."""
    for entry in scan_date_dirs(data_dir):
        if entry['all_present']:
            return Path(entry['masters_dir'])
    return None


# ── Matching ─────────────────────────────────────────────────────────────── #

def _scheme_tokens_set(s: Any) -> set:
    """Return the distinctive tokens of a scheme name (noise stripped,
    min length 3 so 'ai'/'er'/'tc' don't dominate)."""
    import re
    if not s:
        return set()
    base = re.sub(r'[^a-z0-9]+', ' ', _norm(s))
    return {t for t in base.split()
            if t and t not in _NOISE_TOKENS and len(t) >= 3}


def _match_scheme(pool: Dict, schemes: List[Dict]) -> Optional[Dict]:
    """Match a pool to a scheme row.

    Tries strategies in order of strictness:
      1. Exact normalized key match (scheme_name OR display_name).
      2. Substring match — one full key contained in the other (e.g.
         pool 'GoldStandard 57Forward Diversified Momentum Portfolio'
         normalises to 'goldstandard57forwarddiversifiedmomentum'
         which contains the WS key 'goldstandard57forward').
      3. Distinctive-token match — if the pool and one (and only one)
         WS scheme share a token that appears nowhere else in the
         WS list, treat that as a match. This bridges abbreviation
         mismatches like 'Cautilya Time Cycles Quant' (pool) vs
         'Cautilya TC' (WS): 'cautilya' is a unique token, present
         in only one WS scheme.
    """
    candidates = [pool.get('scheme_name'), pool.get('display_name')]
    keys = {_scheme_name_key(c) for c in candidates if c}
    keys.discard('')
    if not keys:
        return None
    # Strategy 1 — exact normalized key match.
    for s in schemes:
        if _scheme_name_key(s.get('SCHEME NAME')) in keys:
            return s
    # Strategy 2 — substring match (min 3 chars to avoid false positives
    # on 1-2 char tokens like 'ai'). Either direction: a longer pool key
    # may fully contain a shorter WS canonical, or vice versa.
    for s in schemes:
        sk = _scheme_name_key(s.get('SCHEME NAME'))
        if len(sk) < 3:
            continue
        for pk in keys:
            if len(pk) < 3:
                continue
            if sk in pk or pk in sk:
                return s
    # Strategy 3 — distinctive-token match. Build a token-frequency
    # index over WS schemes; any token that appears in exactly one
    # scheme is "distinctive". If the pool's token set contains a
    # distinctive token, match it to the unique scheme that owns it.
    # If multiple distinctive tokens point to different schemes the
    # pool is ambiguous and we fall through to no-match.
    pool_tokens = set()
    for c in candidates:
        if c:
            pool_tokens |= _scheme_tokens_set(c)
    if not pool_tokens:
        return None
    token_to_idx: Dict[str, List[int]] = {}
    for i, s in enumerate(schemes):
        for t in _scheme_tokens_set(s.get('SCHEME NAME')):
            token_to_idx.setdefault(t, []).append(i)
    matched_indices = set()
    for t in pool_tokens:
        idxs = token_to_idx.get(t, [])
        if len(idxs) == 1:
            matched_indices.add(idxs[0])
    if len(matched_indices) == 1:
        return schemes[matched_indices.pop()]
    return None


def _bank_acct_key(s: Any) -> str:
    """Strip non-digits and leading zeros so '0050200115218016' and
    '50200115218016' compare equal."""
    import re
    digits = re.sub(r'[^0-9]', '', str(s or ''))
    return digits.lstrip('0') or digits   # all-zeros stays '0'


def _match_bank(pool: Dict, banks: List[Dict]) -> Optional[Dict]:
    """Match a pool to a bank master row by BANK A/C ID == pool.bank_account.

    Two-pass: exact string match first, then digit-only normalized match
    (handles leading-zero / formatting differences between WS and the
    operator's pool record)."""
    bank_acct = (pool.get('bank_account') or '').strip()
    if not bank_acct:
        return None
    for b in banks:
        bid = str(b.get('BANK A/C ID') or '').strip()
        if bid and bid == bank_acct:
            return b
    target = _bank_acct_key(bank_acct)
    if target:
        for b in banks:
            if _bank_acct_key(b.get('BANK A/C ID')) == target:
                return b
    return None


def _closest_scheme_candidates(pool: Dict, schemes: List[Dict],
                                  k: int = 3) -> List[str]:
    """Return up to k SchemeMaster names with the highest token overlap
    against the pool's scheme_name / display_name. Used for diagnostic
    notes when no match was found, so the operator can see what was
    closest and edit the pool's scheme_name accordingly."""
    candidates = [pool.get('scheme_name'), pool.get('display_name')]
    pool_tokens = set()
    for c in candidates:
        if not c:
            continue
        import re as _re
        toks = _re.sub(r'[^a-z0-9]+', ' ', _norm(c)).split()
        pool_tokens.update(t for t in toks if t and t not in _NOISE_TOKENS)
    if not pool_tokens:
        return []
    scored = []
    import re as _re
    for s in schemes:
        sn = (s.get('SCHEME NAME') or '').strip()
        if not sn:
            continue
        s_toks = set(_re.sub(r'[^a-z0-9]+', ' ', _norm(sn)).split())
        s_toks = {t for t in s_toks if t and t not in _NOISE_TOKENS}
        if not s_toks:
            continue
        overlap = len(pool_tokens & s_toks)
        if overlap == 0:
            continue
        # Score by Jaccard so longer scheme names don't dominate.
        score = overlap / max(len(pool_tokens | s_toks), 1)
        scored.append((score, sn))
    scored.sort(reverse=True)
    return [name for _, name in scored[:k]]


def _closest_bank_candidates(pool: Dict, banks: List[Dict],
                                k: int = 3) -> List[str]:
    """Return up to k BANK A/C IDs whose digit suffix overlaps the
    pool's bank_account most. Helps spot leading-zero / typo issues."""
    target = _bank_acct_key(pool.get('bank_account'))
    if not target or len(target) < 4:
        return []
    scored = []
    for b in banks:
        bid_raw = str(b.get('BANK A/C ID') or '').strip()
        if not bid_raw:
            continue
        bid = _bank_acct_key(bid_raw)
        if not bid:
            continue
        # Score by length of common prefix or suffix — typos at one end
        # are common (extra leading 0, extra trailing digit).
        n = min(len(bid), len(target))
        prefix = sum(1 for i in range(n) if bid[i] == target[i])
        suffix = sum(1 for i in range(1, n + 1) if bid[-i] == target[-i])
        score = max(prefix, suffix)
        if score < 4:
            continue
        scored.append((score, bid_raw))
    scored.sort(reverse=True)
    return [bid for _, bid in scored[:k]]


def _match_pool_master(pool: Dict, pool_master_rows: List[Dict]) -> Optional[Dict]:
    """Match a pool to a PoolMaster row by MAPINID == pool.mapin.

    PoolMaster carries the demat tuple (DP/DPID/DPCLIENTID), the Kotak
    client account at REFCODE6, and the WS pool id — none of which are
    in SchemeMaster. Without this, pools created on WS directly come
    back to Keystone with dp_client_id blank.
    """
    pool_mapin = (pool.get('mapin') or '').strip()
    if not pool_mapin:
        return None
    target = pool_mapin.upper()
    for r in pool_master_rows:
        mapid = _coerce_str(r.get('MAPINID')).upper()
        if mapid and mapid == target:
            return r
    return None


def _match_benchmark(scheme_row: Optional[Dict],
                     mappings: List[Dict]) -> Optional[Dict]:
    """Match a scheme to its benchmark row in BenchmarkMapping (SCOPE='Scheme')."""
    if not scheme_row:
        return None
    sid = scheme_row.get('SCHEMEID')
    try:
        target_id = float(sid)
    except (TypeError, ValueError):
        return None
    for m in mappings:
        if _norm(m.get('SCOPE')) != 'scheme':
            continue
        try:
            if float(m.get('SCOPEID') or 0) == target_id:
                return m
        except (TypeError, ValueError):
            continue
    return None


def _match_ia(scheme_row: Optional[Dict],
              ia_list: List[Dict]) -> Optional[Dict]:
    """Match a scheme to its Investment Approach by NAME equality.

    User invariant: 'Investment Approach Name = Scheme Name'. We match
    case- and punctuation-insensitively (same key as scheme matching).
    The first IA whose ``ia_name`` equals the scheme name wins; if no
    match, returns None and the operator gets a note.

    ``ia_list`` is the WS-side list (rows from generalOption.do, with
    keys ``ia_code``, ``ia_name``, ``ia_no``).
    """
    if not scheme_row or not ia_list:
        return None
    target = _scheme_name_key(scheme_row.get('SCHEME NAME'))
    if not target:
        return None
    for ia in ia_list:
        if _scheme_name_key(ia.get('ia_name')) == target:
            return ia
    return None


def _ia_cache_path() -> Path:
    """Local cache for the WS Investment Approach list. Lives next to
    pools_hub.json so KEYSTONE_CONFIG_DIR survives across restarts."""
    cfg_dir = os.environ.get('KEYSTONE_CONFIG_DIR')
    if cfg_dir:
        return Path(cfg_dir) / 'ia_list_cache.json'
    return Path(__file__).parent.parent / 'config' / 'ia_list_cache.json'


def _load_cached_ia_list() -> List[Dict]:
    """Read the most recent cached IA list, or [] if no cache exists."""
    p = _ia_cache_path()
    if not p.exists():
        return []
    try:
        import json
        return json.loads(p.read_text(encoding='utf-8')).get('rows', []) or []
    except Exception as e:
        logger.warning(f'IA cache read failed: {e}')
        return []


def _save_cached_ia_list(rows: List[Dict]) -> None:
    """Persist the IA list so backfill stays useful on Azure even when
    a live WS fetch fails (auth cookie expired, FINCRM creds missing)."""
    if not rows:
        return
    p = _ia_cache_path()
    try:
        import json
        from datetime import datetime, timezone
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            'rows':       rows,
            '_synced_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }, indent=2), encoding='utf-8')
    except Exception as e:
        logger.warning(f'IA cache write failed: {e}')


def fetch_ws_ia_list_status() -> Dict:
    """Pull the live Investment Approach list from WS (generalOption.do).

    Returns ``{rows, source, error}``:
      rows   — list of {ia_code, ia_name, ia_no}; [] only if both live
               and cache fail
      source — 'live' | 'cache' | 'none'
      error  — non-empty string when the live fetch failed (still set
               even when ``source == 'cache'`` so the UI can warn)

    Cache fallback: if live WS fails, returns the most recent cached
    list so backfill still produces IA proposals during transient WS
    outages or stale cookie windows. Successful live fetches refresh
    the cache.
    """
    error = ''
    try:
        import ws_uploader
        listing = ws_uploader.ws_list_investment_approaches()
        rows = []
        for r in listing.get('rows', []) or []:
            rows.append({
                'ia_code': (r.get('option_value') or '').strip(),
                'ia_name': (r.get('option_desc') or '').strip(),
                'ia_no':   r.get('option_seq') or '',
            })
        if rows:
            _save_cached_ia_list(rows)
            return {'rows': rows, 'source': 'live', 'error': ''}
        error = 'WS returned 0 IAs (login may have failed silently)'
    except Exception as e:
        error = f'{type(e).__name__}: {e}'
        logger.warning(f'fetch_ws_ia_list failed: {error}')

    cached = _load_cached_ia_list()
    if cached:
        return {'rows': cached, 'source': 'cache', 'error': error}
    return {'rows': [], 'source': 'none', 'error': error}


def fetch_ws_ia_list() -> List[Dict]:
    """Back-compat wrapper. Returns just the rows; callers that need the
    source/error indicator should use ``fetch_ws_ia_list_status()``."""
    return fetch_ws_ia_list_status()['rows']


# ── Delta builder ────────────────────────────────────────────────────────── #

def _coerce_str(v: Any) -> str:
    """Render a cell value as a string for diffing — collapse 0.0 → '0',
    floats with no decimal → int-string, blank → ''."""
    if v is None or v == '':
        return ''
    if isinstance(v, float):
        if v.is_integer():
            return str(int(v))
        return str(v)
    return str(v).strip()


def _propose_blank_only(deltas: List[FieldDelta], existing: Dict, key: str,
                          proposed: Any, source: str,
                          confidence: str = 'high') -> None:
    """Append a delta only when the existing field is blank.

    Operator-tuned non-blank values are sacred — backfill never offers
    to overwrite them. Use the Edit Pool form for explicit overrides.
    """
    cur_s = _coerce_str(existing.get(key) or '')
    new_s = _coerce_str(proposed)
    if not new_s or cur_s:
        return
    deltas.append(FieldDelta(field=key, current='', proposed=new_s,
                              source=source, confidence=confidence))


def _propose_blank_or_stale_numeric(deltas: List[FieldDelta], existing: Dict,
                                       key: str, proposed: Any, source: str,
                                       confidence: str = 'high') -> None:
    """Like _propose_blank_only but also re-proposes when the existing
    value is *purely numeric* and the proposed value is a name.

    This handles fund_manager records left over from before the
    'save name not WS id' fix (commit ddca514): values like '62' that
    don't render in the Edit Pool dropdown's name-prefix matcher.
    Only triggers on numeric→name; numeric→numeric cases (or when the
    operator typed a real name) stay sacred.
    """
    cur_s = _coerce_str(existing.get(key) or '')
    new_s = _coerce_str(proposed)
    if not new_s:
        return
    is_stale_id = cur_s.isdigit() and not new_s.isdigit()
    if cur_s and not is_stale_id:
        return
    deltas.append(FieldDelta(field=key, current=cur_s, proposed=new_s,
                              source=source, confidence=confidence))


def build_draft(pool: Dict, masters: Dict[str, List[Dict]],
                hub_schemes: Optional[List[Dict]] = None,
                ws_ia_list: Optional[List[Dict]] = None,
                hub_ias: Optional[List[Dict]] = None) -> PoolDraft:
    """Build a per-pool enrichment draft.

    ``masters`` carries Scheme/Bank/BenchmarkMapping rows from XLS.
    ``hub_schemes`` is the local schemes[] table — used to compute current
    values for scheme-level fields so we don't re-propose operator-set values.
    ``ws_ia_list`` is the live IA list from WS (generalOption.do) — empty
    list disables IA matching gracefully.
    ``hub_ias`` is the local investment_approaches[] table — used to flag
    pools whose IA cache record is missing so Apply can heal it.
    """
    draft = PoolDraft(pool_id=(pool.get('pool_id') or '').strip())
    hub_schemes = hub_schemes or []
    hub_ias = hub_ias or []

    scheme    = _match_scheme(pool, masters['schemes'])
    bank      = _match_bank(pool, masters['banks'])
    benchmark = _match_benchmark(scheme, masters['bench_mappings'])
    ia        = _match_ia(scheme, ws_ia_list or [])
    pool_mst  = _match_pool_master(pool, masters.get('pools', []))

    draft.matched_scheme    = scheme
    draft.matched_bank      = bank
    draft.matched_benchmark = benchmark
    draft.matched_ia        = ia
    draft.matched_pool_master = pool_mst

    # IA cache-heal flag: pool has ia_code set AND backfill matched the
    # same IA on WS AND investment_approaches[] is missing that ia_code.
    # This is the case where a pool was created (or partially backfilled
    # earlier) with ia_code populated, but the local IA cache never got
    # filled — Edit Pool then shows IA Name / IA No as blank.
    pool_ia_code = (pool.get('ia_code') or '').strip().upper()
    if ia and pool_ia_code:
        ia_code_match = (ia.get('ia_code') or '').strip().upper()
        if pool_ia_code == ia_code_match:
            existing = next(
                (x for x in hub_ias
                 if (x.get('ia_code') or '').strip().upper() == pool_ia_code),
                None,
            )
            # Heal needed when the IA record is missing OR has blank
            # ia_name / ia_no. Operator-tuned values stay sacred — the
            # apply_drafts mirror only fills blanks, never overwrites.
            if not existing or not (existing.get('ia_name') or '').strip() \
                            or not str(existing.get('ia_no') or '').strip():
                draft.needs_ia_heal = True

    if not scheme:
        cands = _closest_scheme_candidates(pool, masters['schemes'])
        if cands:
            draft.notes.append('no scheme match — closest candidates: '
                                + ' / '.join(repr(c) for c in cands)
                                + '. Set scheme_name on the pool to one '
                                'of these (or fix the WS scheme name).')
        else:
            draft.notes.append('no scheme match — no SchemeMaster row had '
                                'any token overlap. Verify the pool is for '
                                'a scheme that exists on WS.')
    if not bank:
        cands = _closest_bank_candidates(pool, masters['banks'])
        if cands:
            draft.notes.append('no bank match — closest BANK A/C IDs: '
                                + ' / '.join(cands)
                                + '. Check pool.bank_account for typos.')
        else:
            draft.notes.append('no bank match — bank_account not in '
                                'BankMaster (account may not be on WS yet).')
    if not benchmark and scheme:
        draft.notes.append('scheme matched but no benchmark mapping — may '
                            'inherit benchmark from a Client-level scope')
    if not ia and scheme:
        draft.notes.append('scheme matched but no IA matched — local IA list '
                            'may need a refresh, or IA Name differs from Scheme Name')
    if not pool_mst and pool.get('mapin'):
        draft.notes.append('no PoolMaster match — mapin not found in '
                            'PoolMaster.MAPINID (download masters and re-scan)')

    # ── Pool-level deltas ────────────────────────────────────────────── #
    # Linkage: ia_code + scheme_name on the pool unlocks the GET endpoint's
    # scheme/IA resolution and persists Edit Pool reads.
    if ia:
        _propose_blank_only(draft.pool_deltas, pool, 'ia_code',
                              ia.get('ia_code'), 'WS IA list')
    if scheme:
        _propose_blank_only(draft.pool_deltas, pool, 'scheme_name',
                              scheme.get('SCHEME NAME'), 'SchemeMaster')

    # Bank Master detail lives on the pool (one bank per pool).
    if bank:
        _propose_blank_only(draft.pool_deltas, pool, 'bank_master_code',
                              bank.get('BANK CODE'), 'BankMaster')
        _propose_blank_only(draft.pool_deltas, pool, 'bank_full_name',
                              bank.get('BANK NAME'), 'BankMaster')
        _propose_blank_only(draft.pool_deltas, pool, 'bank_account_holder',
                              bank.get('BANK A/C NAME'), 'BankMaster')
        ac_type = (bank.get('A/C TYPE') or '').strip()
        if ac_type:
            label = BANK_AC_TYPE_LABEL.get(ac_type.upper(), ac_type)
            _propose_blank_only(draft.pool_deltas, pool, 'bank_account_type',
                                  label, 'BankMaster')

    # PoolMaster detail — demat tuple + Kotak client. These come back blank
    # for pools created on WS directly (not via Keystone's pool creator)
    # because SchemeMaster doesn't carry them.
    if pool_mst:
        _propose_blank_only(draft.pool_deltas, pool, 'dp',
                              pool_mst.get('DP'), 'PoolMaster')
        _propose_blank_only(draft.pool_deltas, pool, 'dp_id',
                              pool_mst.get('DPID'), 'PoolMaster')
        _propose_blank_only(draft.pool_deltas, pool, 'dp_client_id',
                              pool_mst.get('DPCLIENTID'), 'PoolMaster')
        # REFCODE6 = Kotak client account when this is a Kotak-side pool.
        # Empty for non-Kotak pools, which is fine — _propose_blank_only
        # skips when proposed is blank.
        _propose_blank_only(draft.pool_deltas, pool, 'kotak_client_id',
                              pool_mst.get('REFCODE6'), 'PoolMaster')

    # ── Scheme-level deltas ──────────────────────────────────────────── #
    # Stored in pools_hub.schemes[] keyed by (ia_code, scheme_name).
    # Resolve the existing scheme record so we don't clobber operator edits.
    if scheme and ia:
        ia_code = (ia.get('ia_code') or '').strip().upper()
        sch_name = (scheme.get('SCHEME NAME') or '').strip()
        existing_scheme = next(
            (s for s in hub_schemes
             if (s.get('ia_code') or '').strip().upper() == ia_code
             and (s.get('scheme_name') or '').strip().lower() == sch_name.lower()),
            {},
        )
        # Fund manager + start date go on the scheme record.
        # Use the stale-numeric variant so left-over WS ids (e.g. '62')
        # from before commit ddca514 get replaced by the human name.
        _propose_blank_or_stale_numeric(draft.scheme_deltas, existing_scheme,
                                          'fund_manager',
                                          scheme.get('FUND MANAGER'),
                                          'SchemeMaster')
        sd = _excel_date_to_iso(scheme.get('STARTDATE'))
        if sd:
            _propose_blank_only(draft.scheme_deltas, existing_scheme,
                                  'start_date', sd, 'SchemeMaster')
        if benchmark:
            _propose_blank_only(draft.scheme_deltas, existing_scheme,
                                  'benchmark_index_code',
                                  benchmark.get('BENCHMARK CODE'),
                                  'BenchmarkMapping')
            _propose_blank_only(draft.scheme_deltas, existing_scheme,
                                  'benchmark_description',
                                  benchmark.get('BENCHMARK NAME'),
                                  'BenchmarkMapping')

    return draft


def build_drafts(pools: List[Dict], data_dir: Path,
                  hub_schemes: Optional[List[Dict]] = None,
                  ws_ia_list: Optional[List[Dict]] = None,
                  hub_ias: Optional[List[Dict]] = None) -> Tuple[List[PoolDraft], Dict]:
    """Produce drafts for every pool. Returns (drafts, meta).

    ``meta`` always includes ``date_scan`` — a per-date breakdown of which
    masters are present — so the UI can show the operator exactly what's
    missing and where, instead of a vague 'no masters' error.

    ``ws_ia_list`` should be the live WS IA list (rows with ia_code,
    ia_name, ia_no). If omitted/empty, IA proposals are skipped and
    each pool gets a 'no IA matched' note. Pass an empty list to
    explicitly disable IA matching (test friendliness).

    ``hub_ias`` is the local investment_approaches[] table — used to
    decide which pools need an IA cache heal (pool.ia_code set, IA on
    WS, but missing locally). Without this, "no proposed changes"
    silently hides healing opportunities.
    """
    diag = scan_date_dirs(data_dir)
    masters_dir = latest_masters_dir(data_dir)
    if masters_dir is None:
        return [], {
            'masters_dir':    '',
            'schemes':        0,
            'banks':          0,
            'bench_mappings': 0,
            'ias':            len(ws_ia_list or []),
            'date_scan':      diag,
            'data_dir':       str(data_dir),
            'note':           'No date folder under data/ contains all three '
                              'required masters. Inspect ``date_scan`` for the '
                              'missing files per date, then click '
                              '"Download masters now" or run the daily WS '
                              'download.',
        }
    masters = load_masters(masters_dir)
    # Only enrich active pools. Inactive pools are already excluded
    # from recon engines (they filter on active=True). The is_advisory
    # field used to live here too but was retired — recon-level
    # exclusions are now operator-managed in Settings → Recon
    # Configuration.
    pools_in_scope = [p for p in pools if p.get('active', True)]
    drafts = [build_draft(p, masters, hub_schemes=hub_schemes,
                            ws_ia_list=ws_ia_list, hub_ias=hub_ias)
              for p in pools_in_scope]
    meta = {
        'masters_dir':    str(masters_dir),
        'schemes':        len(masters['schemes']),
        'banks':          len(masters['banks']),
        'bench_mappings': len(masters['bench_mappings']),
        'pool_master':    len(masters.get('pools', [])),
        'ias':            len(ws_ia_list or []),
        'date_scan':      diag,
        'data_dir':       str(data_dir),
        # Count of pools that just need their IA cache filled — surfaces
        # in the UI even when no field-level deltas exist.
        'needs_ia_heal':  sum(1 for d in drafts if d.needs_ia_heal),
    }
    return drafts, meta


# ── Commit ───────────────────────────────────────────────────────────────── #

def apply_drafts(hub_path: Path, approvals: List[Dict]) -> Dict:
    """Apply approved deltas to pools_hub.json.

    Each approval entry:
      {
        pool_id: 'aristos_hdfc',
        accept_pool:    ['ia_code', 'scheme_name', 'bank_master_code', ...],
        accept_scheme:  ['fund_manager', 'start_date', 'benchmark_index_code', ...],
        proposed_pool:    {field: value, ...},
        proposed_scheme:  {field: value, ...},
        overrides_pool:   {field: value, ...},   # operator-edited at apply time
        overrides_scheme: {field: value, ...},
        scheme_key: {ia_code, scheme_name},      # which schemes[] row to upsert
        ia_record:  {ia_code, ia_name, ia_no},   # WS-side IA tuple — we mirror
                                                  # this into investment_approaches[]
                                                  # whenever ia_code is being
                                                  # written to the pool, so the
                                                  # Edit Pool form can resolve
                                                  # IA Name / IA No on reload
      }

    Pool-level fields write to pools[i].
    Scheme-level fields upsert into schemes[] keyed by (ia_code, scheme_name).
    IA records mirror into investment_approaches[] keyed by ia_code.
    Atomic file write.
    """
    import json
    import tempfile
    if not hub_path.exists():
        return {'ok': False, 'error': f'hub not found: {hub_path}'}

    hub = json.loads(hub_path.read_text(encoding='utf-8'))
    pools = hub.get('pools', []) or []
    # Bind to the same list reference inside hub so appends persist on save.
    # `setdefault(...) or []` returns a fresh list when the stored list is
    # empty (because `[]` is falsy), which silently drops appends.
    schemes = hub.setdefault('schemes', [])
    if not isinstance(schemes, list):
        schemes = []
        hub['schemes'] = schemes
    ias = hub.setdefault('investment_approaches', [])
    if not isinstance(ias, list):
        ias = []
        hub['investment_approaches'] = ias
    pool_by_id = {(p.get('pool_id') or '').strip(): p for p in pools}

    def _ia_index(ia_code: str) -> Optional[int]:
        target = (ia_code or '').strip().upper()
        if not target:
            return None
        for i, row in enumerate(ias):
            if (row.get('ia_code') or '').strip().upper() == target:
                return i
        return None

    def _scheme_index(ia_code: str, scheme_name: str) -> Optional[int]:
        ia_target = (ia_code or '').strip().upper()
        nm_target = (scheme_name or '').strip().lower()
        for i, s in enumerate(schemes):
            if ((s.get('ia_code') or '').strip().upper() == ia_target
                    and (s.get('scheme_name') or '').strip().lower() == nm_target):
                return i
        return None

    applied: List[Dict] = []
    for ap in approvals or []:
        pid = (ap.get('pool_id') or '').strip()
        # New typed fields with back-compat fallback to the old flat 'accept'.
        accept_pool   = ap.get('accept_pool')   or []
        accept_scheme = ap.get('accept_scheme') or []
        if not accept_pool and not accept_scheme:
            # Legacy callers that send 'accept'/'proposed'/'overrides' without
            # the kind separator — treat all as pool-level.
            accept_pool      = ap.get('accept') or []
        proposed_pool    = ap.get('proposed_pool')    or ap.get('proposed')   or {}
        proposed_scheme  = ap.get('proposed_scheme')  or {}
        overrides_pool   = ap.get('overrides_pool')   or ap.get('overrides')  or {}
        overrides_scheme = ap.get('overrides_scheme') or {}

        pool = pool_by_id.get(pid)
        if not pool:
            applied.append({'pool_id': pid, 'status': 'not_found'})
            continue
        changed_pool = []
        changed_scheme = []
        changed_ia = []

        for f in accept_pool:
            if f in overrides_pool:
                pool[f] = overrides_pool[f]
                changed_pool.append(f)
            elif f in proposed_pool:
                pool[f] = proposed_pool[f]
                changed_pool.append(f)

        # Mirror the IA tuple into investment_approaches[] so the Edit Pool
        # form can resolve IA Name / IA No from pool.ia_code. The data comes
        # straight from WS (no operator input needed) — we always write it
        # when ``ia_record`` is supplied and the pool is now linked to that
        # ia_code.
        ia_record = ap.get('ia_record') or {}
        ia_code_for_record = (ia_record.get('ia_code') or '').strip().upper()
        # Only mirror if the pool's ia_code matches the record's ia_code
        # (defends against stale frontend state writing a wrong IA tuple).
        pool_ia_code = (pool.get('ia_code') or '').strip().upper()
        if ia_code_for_record and ia_code_for_record == pool_ia_code:
            row = {
                'ia_code': ia_code_for_record,
                'ia_name': (ia_record.get('ia_name') or '').strip(),
                'ia_no':   ia_record.get('ia_no') or '',
            }
            idx = _ia_index(ia_code_for_record)
            if idx is None:
                ias.append(row)
                changed_ia = ['ia_code', 'ia_name', 'ia_no']
            else:
                existing = ias[idx]
                # Only fill blanks — don't overwrite operator-tuned values.
                merged_changes = []
                for k in ('ia_name', 'ia_no'):
                    if not (existing.get(k) or '') and row.get(k):
                        existing[k] = row[k]
                        merged_changes.append(k)
                if merged_changes:
                    changed_ia = merged_changes

        # Scheme-level patch — upsert into schemes[] using the linkage that
        # was just (or already) written to the pool.
        if accept_scheme:
            sk = ap.get('scheme_key') or {}
            ia_code     = (sk.get('ia_code')     or pool.get('ia_code')     or '').strip()
            scheme_name = (sk.get('scheme_name') or pool.get('scheme_name') or '').strip()
            if ia_code and scheme_name:
                idx = _scheme_index(ia_code, scheme_name)
                if idx is None:
                    new_row = {'ia_code': ia_code.upper(), 'scheme_name': scheme_name}
                    schemes.append(new_row)
                    idx = len(schemes) - 1
                row = schemes[idx]
                for f in accept_scheme:
                    if f in overrides_scheme:
                        row[f] = overrides_scheme[f]
                        changed_scheme.append(f)
                    elif f in proposed_scheme:
                        row[f] = proposed_scheme[f]
                        changed_scheme.append(f)
            else:
                # No linkage available → caller forgot to also accept ia_code +
                # scheme_name on the pool side. Surface that as a status.
                applied.append({'pool_id': pid, 'status': 'scheme_unlinked',
                                'reason': 'accept ia_code + scheme_name on the '
                                          'pool side (or pass scheme_key) before '
                                          'scheme-level fields can be written'})
                continue
        applied.append({'pool_id': pid,
                         'changed_pool':   changed_pool,
                         'changed_scheme': changed_scheme,
                         'changed_ia':     changed_ia,
                         # Legacy field, kept so older clients still see something.
                         'changed':        changed_pool + changed_scheme + changed_ia})

    fd, tmp = tempfile.mkstemp(prefix='.pools_hub.', suffix='.json.tmp',
                                dir=str(hub_path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(hub, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, hub_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return {'ok': True, 'applied': applied}
