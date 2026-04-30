"""
Recon exclusions — operator-managed list of bank accounts and WS pools
that should be skipped by the recon engines.

Use case: a custodian sometimes sends bank statements for parent /
aggregator accounts that aren't real reconciliation targets (e.g. Axis
sends 'GOLDMFOM_BankBalance_*.zip' for a master FoF account that
doesn't map to any pool). Without this list, those accounts surface
as 'unmapped' breaks every day.

Two parallel exclusion buckets:
  bank_excluded — keyed by (custodian, identifier). Identifier is what
                  the bank's filename / parser exposes (Axis: pool
                  prefix like GOLDMFOM; HDFC: zip prefix like GWPJ;
                  ICICI: 12-digit account number; Kotak: client_id).
  ws_excluded   — keyed by mapin. Pools that appear in the WS BankBook
                  but should be ignored by the WS-side bank recon.

Single config file: ``config/recon_exclusions.json``. Atomic writes
so a poll-during-save can't see a partial file.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

EXCLUSIONS_FILENAME = 'recon_exclusions.json'


def _bank_folders() -> tuple:
    """Custodians whose bank-statement folders we scan. Driven by
    config/sources.json (rows where ``is_bank=true``) so adding a 5th
    bank doesn't require a code edit. Falls back to the fixed
    GoldStandard 4-bank set when the registry can't load."""
    try:
        from core.bank_registry import bank_folder_names
        names = tuple(bank_folder_names())
        if names:
            return names
    except Exception as e:
        logger.warning(f'bank_registry unavailable, using default folders: {e}')
    return ('axis_bank', 'hdfc_bank', 'icici_bank', 'kotak_bank')


# ── Storage ─────────────────────────────────────────────────────────────── #

def _config_path() -> Path:
    """Resolve to config/recon_exclusions.json, honouring KEYSTONE_CONFIG_DIR."""
    cfg_dir = os.environ.get('KEYSTONE_CONFIG_DIR')
    if cfg_dir:
        return Path(cfg_dir) / EXCLUSIONS_FILENAME
    return Path(__file__).parent.parent / 'config' / EXCLUSIONS_FILENAME


def load_exclusions() -> Dict:
    """Read the exclusions file. Returns the canonical empty shape on
    missing/malformed file."""
    p = _config_path()
    if not p.exists():
        return {'bank_excluded': [], 'ws_excluded': []}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return {
            'bank_excluded': data.get('bank_excluded') or [],
            'ws_excluded':   data.get('ws_excluded')   or [],
        }
    except Exception as e:
        logger.warning(f'recon_exclusions read failed: {e}')
        return {'bank_excluded': [], 'ws_excluded': []}


def save_exclusions(payload: Dict) -> None:
    """Atomically write the exclusions file. ``payload`` should have
    keys 'bank_excluded' and 'ws_excluded' (lists of dicts)."""
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    out = {
        'bank_excluded': list(payload.get('bank_excluded') or []),
        'ws_excluded':   list(payload.get('ws_excluded')   or []),
    }
    fd, tmp = tempfile.mkstemp(prefix='.recon_exclusions.', suffix='.tmp',
                                dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, p)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


# ── Predicates (used by the recon engines) ─────────────────────────────── #

def _bank_set() -> Set[Tuple[str, str]]:
    """{(custodian_lower, identifier_upper)} for fast O(1) checks."""
    out: Set[Tuple[str, str]] = set()
    for r in load_exclusions().get('bank_excluded') or []:
        cust = (r.get('custodian') or '').strip().lower().replace('_bank', '')
        ident = (r.get('identifier') or '').strip().upper()
        if cust and ident:
            out.add((cust, ident))
    return out


def _ws_set() -> Set[str]:
    """{mapin_upper} for fast O(1) checks."""
    out: Set[str] = set()
    for r in load_exclusions().get('ws_excluded') or []:
        m = (r.get('mapin') or '').strip().upper()
        if m:
            out.add(m)
    return out


def is_bank_account_excluded(custodian: str, identifier: str) -> bool:
    """True iff (custodian, identifier) is on the bank-side skip list.
    ``custodian`` is the short name ('axis' / 'hdfc' / 'icici' / 'kotak'),
    not the folder name. ``identifier`` is whatever the bank parser
    exposes — pool prefix, account number, etc."""
    if not custodian or not identifier:
        return False
    key = ((custodian or '').strip().lower().replace('_bank', ''),
           (identifier or '').strip().upper())
    return key in _bank_set()


def is_ws_pool_excluded(mapin: str) -> bool:
    """True iff this mapin should be skipped by the WS-side bank recon."""
    if not mapin:
        return False
    return (mapin or '').strip().upper() in _ws_set()


# ── Discovery (powers the admin UI) ────────────────────────────────────── #

def _latest_date_dirs(data_dir: Path, n: int = 5) -> List[Path]:
    if not data_dir.exists():
        return []
    return sorted(
        (p for p in data_dir.iterdir()
         if p.is_dir() and len(p.name) == 10 and p.name[4] == '-' and p.name[7] == '-'),
        reverse=True,
    )[:n]


def _extract_bank_identifier(custodian_folder: str, filename: str) -> Optional[str]:
    """Map a custodian + filename to the pool/account identifier the
    bank uses to scope this file. Per-custodian heuristics:

      axis_bank  : GOLDMFOM_BankBalance_29042026.zip → 'GOLDMFOM'
      hdfc_bank  : GWPJ.zip → 'GWPJ', GWPJ_fcat_*.xlsx → 'GWPJ'
      icici_bank : 000405165525-300426.txt → '000405165525'
      kotak_bank : (no per-file pool prefix today; left as None)
    """
    name = filename or ''
    if custodian_folder == 'axis_bank':
        m = re.match(r'^([A-Z][A-Z0-9]+)_(?:BankBalance|BankTransaction)_', name)
        if m:
            return m.group(1)
    elif custodian_folder == 'hdfc_bank':
        m = re.match(r'^([A-Z][A-Z0-9]+?)(?:_|\.)', name)
        if m:
            return m.group(1)
    elif custodian_folder == 'icici_bank':
        m = re.match(r'^(\d{12})\b', name)
        if m:
            return m.group(1)
    return None


def scan_bank_pools(data_dir: Path) -> List[Dict]:
    """Walk recent ``data/{date}/raw/{custodian_bank}/`` folders and
    return one entry per (custodian, identifier) pair seen. Each entry
    is enriched with a sample filename so the operator can verify what
    they're excluding before they tick the box.

    Caller layers the exclusion list on top to flag the ``excluded``
    boolean — kept separate so this scan stays read-only.
    """
    seen: Dict[Tuple[str, str], Dict] = {}
    for d in _latest_date_dirs(data_dir, n=5):
        raw = d / 'raw'
        if not raw.exists():
            continue
        for cust_folder in _bank_folders():
            folder = raw / cust_folder
            if not folder.exists():
                continue
            for fp in folder.iterdir():
                if not fp.is_file():
                    continue
                ident = _extract_bank_identifier(cust_folder, fp.name)
                if not ident:
                    continue
                cust = cust_folder.replace('_bank', '')
                key = (cust.lower(), ident.upper())
                if key in seen:
                    continue
                seen[key] = {
                    'custodian':   cust,
                    'identifier':  ident,
                    'sample_file': fp.name,
                    'sample_date': d.name,
                }
    # Sort by custodian, then identifier — predictable UI ordering.
    return sorted(seen.values(),
                   key=lambda r: (r['custodian'], r['identifier']))


def scan_ws_pools(data_dir: Path) -> List[Dict]:
    """Read the latest Z13_BankBook.* file and return one entry per
    distinct (mapin, scheme/pool name). Drives the WS-side exclusion
    list. Falls back to BankBook.csv / BankBook.xls naming variants."""
    candidates = ('Z13_BankBook.csv', 'Z13_BankBook.xlsx', 'Z13_BankBook.xls',
                   'BankBook.csv', 'BankBook.xlsx', 'BankBook.xls')
    for d in _latest_date_dirs(data_dir, n=10):
        masters = d / 'masters'
        if not masters.exists():
            continue
        for fn in candidates:
            fp = masters / fn
            if fp.exists():
                rows = _read_bankbook(fp)
                if rows:
                    return rows
    return []


def _read_bankbook(fp: Path) -> List[Dict]:
    """Best-effort parse of WS BankBook into [{mapin, label}].

    Real BankBook CSVs/XLS files start with a 5-7 row preamble (firm
    name, address, 'BANK BOOK' title, date range) BEFORE the actual
    column header — first attempt was reading the firm-name row as
    headers and silently producing zero entries. We now scan for the
    real header row by looking for ``Code,Name,Bank Account``.

    Group by the ``Name`` column (scheme/pool name); look up the
    corresponding MAPINID via pools_hub when a match exists, else use
    the scheme name itself as the identifier (covers parent-aggregator
    rows that aren't in pools_hub).
    """
    try:
        if fp.suffix.lower() == '.csv':
            return _read_bankbook_csv(fp)
        if fp.suffix.lower() in ('.xls', '.xlsx'):
            return _read_bankbook_xls(fp)
    except Exception as e:
        logger.warning(f'BankBook read failed for {fp}: {e}')
    return []


def _read_bankbook_csv(fp: Path) -> List[Dict]:
    """CSV path: skip preamble, then DictReader from the real header row."""
    import csv
    from io import StringIO
    text = fp.read_text(encoding='utf-8', errors='replace')
    lines = text.splitlines()
    header_idx = _find_bankbook_header(lines)
    if header_idx is None:
        logger.warning(f'BankBook header not found in {fp}')
        return []
    body = '\n'.join(lines[header_idx:])
    reader = csv.DictReader(StringIO(body))
    return _bankbook_rows_to_pools(list(reader))


def _read_bankbook_xls(fp: Path) -> List[Dict]:
    """XLS path: scan for the header row, then read forward."""
    try:
        import xlrd
    except ImportError:
        return []
    wb = xlrd.open_workbook(str(fp))
    sh = wb.sheet_by_index(0)
    if sh.nrows < 2:
        return []
    # Find header row by content (same pattern as CSV).
    header_idx = None
    for r in range(min(sh.nrows, 20)):
        first_cell = str(sh.cell_value(r, 0)).strip().lower()
        second_cell = str(sh.cell_value(r, 1)).strip().lower() if sh.ncols >= 2 else ''
        if first_cell == 'code' and second_cell == 'name':
            header_idx = r
            break
    if header_idx is None:
        return []
    headers = [str(sh.cell_value(header_idx, c)).strip()
               for c in range(sh.ncols)]
    rows = []
    for r in range(header_idx + 1, sh.nrows):
        row = {headers[c]: sh.cell_value(r, c) for c in range(sh.ncols)}
        rows.append(row)
    return _bankbook_rows_to_pools(rows)


def _find_bankbook_header(lines: List[str]) -> Optional[int]:
    """Locate the BankBook column-header row inside CSV preamble. Looks
    for a line beginning with the canonical column names."""
    for i, line in enumerate(lines):
        s = line.strip().lower()
        # Tolerate quoting + minor variations.
        if s.startswith('code,name,bank account') \
           or s.startswith('"code","name","bank account"') \
           or (s.startswith('code,') and 'name' in s and 'bank' in s):
            return i
        if i > 25:
            break    # preamble shouldn't be that long
    return None


def _bankbook_rows_to_pools(rows: List[Dict]) -> List[Dict]:
    """Group BankBook rows by 'Name' (scheme / pool name), drop subtotal
    rows ('Bank Total'), and resolve each name to a MAPID via pools_hub
    when a match exists. Names with no pools_hub match become their own
    identifier (covers parent / aggregator pools the operator wants to
    exclude — that's the whole point of this scan).
    """
    if not rows:
        return []
    # The real BankBook export has Code / Name / Bank Account / etc.
    # If a different export shape ever turns up, keep the legacy
    # MAPINID-style fallback.
    sample = rows[0]
    if 'Name' in sample or 'NAME' in sample:
        name_col = 'Name' if 'Name' in sample else 'NAME'
        # Optional: account-code column for label context.
        seen: Dict[str, Dict] = {}
        # Build a name → mapin index from pools_hub once.
        name_to_mapin = _pools_hub_name_to_mapin()
        # BankBook re-prints the header row between strategy sections
        # (one Code,Name,Bank Account,... line per group). Filter
        # out those echoes by name and by the Code column when it
        # literally reads 'Code' instead of a number.
        code_col = 'Code' if 'Code' in sample else ('CODE' if 'CODE' in sample else None)
        for r in rows:
            nm = str(r.get(name_col) or '').strip()
            if not nm:
                continue
            low = nm.lower()
            if low == 'name' or low.startswith('bank total'):
                continue   # repeated header or subtotal
            if code_col and str(r.get(code_col) or '').strip().lower() == 'code':
                continue   # repeated header row
            mapin = name_to_mapin.get(low, nm)
            key = mapin.upper()
            if key not in seen:
                seen[key] = {'mapin': mapin, 'label': nm}
        return sorted(seen.values(), key=lambda r: r['mapin'].upper())
    # Legacy MAPINID-shape fallback (older exports).
    for cand in ('MAPINID', 'MAPIN', 'MAP IN ID', 'POOL_MAPIN'):
        if cand in sample:
            return _legacy_mapinid_rows(rows, cand)
    return []


def _pools_hub_name_to_mapin() -> Dict[str, str]:
    """{lower-cased scheme/display name → MAPIN} from pools_hub.json.
    Best-effort — failures silently return {}; the BankBook scan then
    falls back to using the scheme name as the identifier."""
    try:
        from core.pools_hub import PoolsHub
        out: Dict[str, str] = {}
        for p in PoolsHub.load().pools:
            mapin = (p.get('mapin') or '').strip()
            if not mapin:
                continue
            for nm in (p.get('display_name'), p.get('scheme_name')):
                if nm:
                    out.setdefault(nm.strip().lower(), mapin)
        return out
    except Exception as e:
        logger.warning(f'pools_hub name index unavailable: {e}')
        return {}


def _legacy_mapinid_rows(rows: List[Dict], mapin_col: str) -> List[Dict]:
    """Pre-2026 BankBook exports had a flat MAPINID column; preserved
    for any legacy file that still surfaces."""
    seen: Dict[str, Dict] = {}
    for r in rows:
        mapin = str(r.get(mapin_col) or '').strip()
        if isinstance(r.get(mapin_col), float) and float(r[mapin_col]).is_integer():
            mapin = str(int(r[mapin_col]))
        if not mapin:
            continue
        key = mapin.upper()
        if key not in seen:
            seen[key] = {'mapin': mapin, 'label': ''}
    return sorted(seen.values(), key=lambda r: r['mapin'].upper())


def build_view(data_dir: Path) -> Dict:
    """Compose the full payload the admin UI needs: scanned pools (both
    sides) annotated with current exclusion state. Reads exclusions
    once so the bank/ws scans share a snapshot."""
    excl = load_exclusions()
    bank_keys = {((r.get('custodian') or '').strip().lower(),
                   (r.get('identifier') or '').strip().upper())
                 for r in excl.get('bank_excluded') or []}
    ws_keys   = {(r.get('mapin') or '').strip().upper()
                 for r in excl.get('ws_excluded') or []}

    bank_seen = scan_bank_pools(data_dir)
    for r in bank_seen:
        r['excluded'] = ((r['custodian'].lower(), r['identifier'].upper())
                         in bank_keys)

    ws_seen = scan_ws_pools(data_dir)
    for r in ws_seen:
        r['excluded'] = (r['mapin'].upper() in ws_keys)

    # Surface any orphan exclusions (left over after a custodian renamed
    # an account) so the operator can prune them.
    bank_seen_keys = {(r['custodian'].lower(), r['identifier'].upper())
                      for r in bank_seen}
    bank_orphans = [r for r in (excl.get('bank_excluded') or [])
                    if ((r.get('custodian') or '').lower(),
                         (r.get('identifier') or '').upper()) not in bank_seen_keys]
    ws_seen_keys = {r['mapin'].upper() for r in ws_seen}
    ws_orphans = [r for r in (excl.get('ws_excluded') or [])
                  if (r.get('mapin') or '').upper() not in ws_seen_keys]

    return {
        'bank_pools':   bank_seen,
        'ws_pools':     ws_seen,
        'bank_orphans': bank_orphans,
        'ws_orphans':   ws_orphans,
    }
