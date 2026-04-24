"""
File Manager
------------
Manages the folder structure under data/{YYYY-MM-DD}/:
  raw/{source_name}/   — extracted broker files
  masters/             — WS strategy master (and future master files)
  output/              — generated WS upload files

Also handles zip extraction with password support.
"""
import os
import io
import logging
import zipfile
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class FileManager:

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)

    # ------------------------------------------------------------------ #
    #  Folder helpers                                                       #
    # ------------------------------------------------------------------ #

    def date_dir(self, date_str: str) -> Path:
        return self.base_dir / 'data' / date_str

    def raw_dir(self, date_str: str, source_name: str = '') -> Path:
        p = self.date_dir(date_str) / 'raw'
        if source_name:
            p = p / source_name
        return p

    def masters_dir(self, date_str: str) -> Path:
        return self.date_dir(date_str) / 'masters'

    def shared_masters_dir(self) -> Path:
        """Shared masters folder — not date-specific. Files here are used as
        fallback for all dates. Ideal for rarely-changing files like Z8_SecurityDetail."""
        return self.base_dir / 'masters'

    def output_dir(self, date_str: str) -> Path:
        return self.date_dir(date_str) / 'output'

    def ensure_dirs(self, date_str: str, source_names: List[str]):
        """Create the full folder tree for a given date."""
        for d in [self.masters_dir(date_str), self.output_dir(date_str)]:
            d.mkdir(parents=True, exist_ok=True)
        for source in source_names:
            self.raw_dir(date_str, source).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    #  Zip extraction                                                       #
    # ------------------------------------------------------------------ #

    def extract_zip(self, zip_path: str, dest_dir: str,
                    password: str = '') -> Tuple[List[str], Optional[str]]:
        """
        Extract a zip file to dest_dir.
        Returns (list_of_extracted_paths, error_message_or_None).
        """
        extracted = []
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)

        try:
            pwd = password.encode('utf-8') if password else None
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for member in zf.infolist():
                    # Skip directories and macOS metadata
                    if member.filename.endswith('/') or '__MACOSX' in member.filename:
                        continue
                    # Extract to flat dest dir (no sub-folders from zip)
                    member.filename = os.path.basename(member.filename)
                    if not member.filename:
                        continue
                    out_path = dest / member.filename
                    try:
                        with zf.open(member, pwd=pwd) as src, \
                             open(out_path, 'wb') as dst:
                            dst.write(src.read())
                        extracted.append(str(out_path))
                    except RuntimeError as e:
                        # Wrong password or bad entry
                        return [], f"Zip extraction failed for '{member.filename}': {e}"

        except zipfile.BadZipFile as e:
            return [], f"Not a valid zip file: {e}"
        except Exception as e:
            return [], str(e)

        return extracted, None

    # ------------------------------------------------------------------ #
    #  File discovery                                                       #
    # ------------------------------------------------------------------ #

    def list_source_files(self, date_str: str, source_name: str) -> List[str]:
        """
        List all files in raw/{source_name}/ for the given date.
        For Kotak, this may include both xlsx and csv — caller filters.

        Deduplicates by logical stem: the email ingestor tags each
        attachment with a _YYYYMMDDTHHMMSS suffix based on the email's
        received time, so the same custodian report retransmitted twice
        lands as two files with different timestamps but the same base
        name. Left unchecked, the holdings/bank parsers read both and
        double-count every line. Dedup keeps only the newest per logical
        group (by the timestamp tag, falling back to file mtime).
        """
        import re as _re_ts
        d = self.raw_dir(date_str, source_name)
        if not d.exists():
            return []
        _ts_rx = _re_ts.compile(r'_(\d{8}T\d{6})$')
        grouped = {}
        for p in d.iterdir():
            if not p.is_file():
                continue
            m = _ts_rx.search(p.stem)
            if m:
                logical_stem = p.stem[:m.start()]
                sort_key = (1, m.group(1))
            else:
                logical_stem = p.stem
                try:
                    sort_key = (0, p.stat().st_mtime)
                except OSError:
                    sort_key = (0, 0)
            key = (logical_stem, p.suffix.lower())
            existing = grouped.get(key)
            if existing is None or sort_key > existing[0]:
                grouped[key] = (sort_key, p)
        return [str(entry[1]) for entry in grouped.values()]

    def list_all_source_files(self, date_str: str) -> dict:
        """Returns {source_name: [file_paths]} for all sources."""
        raw = self.raw_dir(date_str)
        if not raw.exists():
            return {}
        result = {}
        for item in raw.iterdir():
            if item.is_dir():
                files = [str(p) for p in item.iterdir() if p.is_file()]
                if files:
                    result[item.name] = files
        return result

    def get_strategy_master(self, date_str: str) -> Optional[str]:
        """Find the Pool Master (Z13_PoolMaster) in date-specific masters/ only.
        Mandatory daily upload — no shared/root fallback.
        xlsx/xls only — pool_master.py uses openpyxl which rejects CSV."""
        return self._find_master(date_str, keywords=['poolmaster', 'pool_master', 'z13_pool'],
                                 extensions=('.xlsx', '.xls'), shared_fallback=False)

    def get_security_master(self, date_str: str) -> Optional[str]:
        """Find the Security Detail master (Z13_SecurityDetail) in the masters/ folder.
        Accepts .csv (preferred) or .xlsx/.xls — uses _find_master default extensions."""
        return self._find_master(date_str, keywords=['securitydetail', 'security_detail', 'z13_security', 'z8_security', 'z8security'])

    def get_kotak_custody(self, date_str: str) -> Optional[str]:
        """Find the Kotak WM custody/transactions file in date-specific masters/ only.
        Mandatory daily upload — no shared/root fallback."""
        return self._find_master(date_str, keywords=['kotakwm', 'kotak_wm', 'transactionsshares',
                                                      'custodyinterface'],
                                 shared_fallback=False)

    def get_ws_holdings(self, date_str: str) -> Optional[str]:
        """Find the WS Holdings export (Z13_Holding) in the masters/ folder."""
        return self._find_master(date_str, keywords=['z13holding', 'z13_holding', 'schemeholding',
                                                      'holdings'])

    def get_ws_transactions(self, date_str: str) -> Optional[str]:
        """Find the WS Trade Transactions export (Z13_TradeTrans) in the masters/ folder."""
        return self._find_master(date_str, keywords=['tradetrans', 'trade_trans', 'z13trade', 'tradetrns'])

    def archive_base_dir(self) -> str:
        """Root directory for the email archive (sits alongside data/)."""
        d = self.base_dir / 'archive'
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def archive_summary(self) -> dict:
        """
        Scan the archive directory and return per-date flagged file counts.
        Returns {date_str: {source: {new, versioned, flagged}}}
        """
        import json as _json
        result = {}
        arch = self.base_dir / 'archive'
        if not arch.exists():
            return result
        for date_dir in sorted(arch.iterdir()):
            if not date_dir.is_dir():
                continue
            date_str = date_dir.name
            result[date_str] = {}
            for src_dir in sorted(date_dir.iterdir()):
                if not src_dir.is_dir():
                    continue
                index_path = src_dir / 'index.json'
                if not index_path.exists():
                    continue
                try:
                    index = _json.loads(index_path.read_text())
                    flagged = sum(1 for v in index.values() if v.get('flagged'))
                    total   = len(index)
                    result[date_str][src_dir.name] = {
                        'total': total, 'flagged': flagged
                    }
                except Exception:
                    pass
        return result

    def get_client_bank_details(self, date_str: str) -> Optional[str]:
        """Find Z13_ClientDPBankDetails in the masters/ folder."""
        return self._find_master(date_str, keywords=['clientdpbank', 'clientbank', 'dpbankdetail'])

    # ------------------------------------------------------------------ #
    #  Bank statement file discovery                                        #
    # ------------------------------------------------------------------ #

    def bank_files_present(self, date_str: str, bank_source: str) -> dict:
        """
        Lightweight presence check for bank files — NO extraction, NO subprocess.
        Used by /api/file-status to show whether files are available without
        triggering the heavy 7z extraction that get_bank_files(archive_fallback=True) does.

        Returns {'present': bool, 'filename': str, 'in_archive': bool}
        """
        # 1. Check raw folder
        raw_files = self.list_source_files(date_str, bank_source)
        if raw_files:
            return {'present': True, 'filename': Path(raw_files[-1]).name, 'in_archive': False}

        # 2. Kotak: check strategy subfolders for CSV
        if bank_source == 'kotak_bank':
            raw_dir = self.raw_dir(date_str)
            if raw_dir.exists():
                for item in raw_dir.iterdir():
                    if item.is_dir() and item.name.startswith('kotak'):
                        csvs = list(item.glob('*.csv'))
                        if csvs:
                            return {'present': True, 'filename': csvs[-1].name,
                                    'in_archive': False}
            # Also check raw/kotak/ directly
            kotak_raw2 = self.raw_dir(date_str, 'kotak')
            if kotak_raw2.exists():
                csvs = list(kotak_raw2.glob('*.csv'))
                if csvs:
                    return {'present': True, 'filename': csvs[-1].name,
                            'in_archive': False}
            # Check archive for zip files (don't extract — just confirm they exist)
            for arch_sub in ['kotak_bank', 'kotak']:
                arch_dir = self.base_dir / 'archive' / date_str / arch_sub / 'attachments'
                if arch_dir.exists():
                    zips = list(arch_dir.glob('*.zip'))
                    if zips:
                        return {'present': True, 'filename': zips[-1].name,
                                'in_archive': True}
            return {'present': False, 'filename': ''}

        # 3. Axis: check for zip files in raw or archive
        if bank_source == 'axis_bank':
            arch_dir = self.base_dir / 'archive' / date_str / 'axis_bank' / 'attachments'
            if arch_dir.exists():
                zips = list(arch_dir.glob('*.zip'))
                if zips:
                    return {'present': True, 'filename': zips[-1].name, 'in_archive': True}
            return {'present': False, 'filename': ''}

        # 4. ICICI bank / HDFC bank / HDFC bank balance: check archive folder
        arch_dir = self.base_dir / 'archive' / date_str / bank_source / 'attachments'
        if arch_dir.exists():
            if bank_source == 'icici_bank':
                files = list(arch_dir.glob('*.txt')) + list(arch_dir.glob('*.csv'))
            else:
                files = list(arch_dir.glob('*.zip'))
            if files:
                return {'present': True, 'filename': files[-1].name, 'in_archive': True}

        return {'present': False, 'filename': ''}

    def _kotak_csv_scan(self, date_str: str, window: int = 0) -> list:
        """Scan kotak_* and kotak_bank* folders for CSV bank statements.

        When *window* > 0, searches date ± window calendar days.
        Otherwise searches the exact date only.
        """
        from datetime import datetime as _dt, timedelta as _td
        _data_root = self.base_dir / 'data'
        if not _data_root.exists():
            return []
        try:
            _recon_dt = _dt.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return []

        _candidate_dirs = []
        for _ddir in sorted(_data_root.iterdir(), reverse=True):
            if not _ddir.is_dir():
                continue
            try:
                _d = _dt.strptime(_ddir.name, '%Y-%m-%d')
                if abs((_d - _recon_dt).days) <= window:
                    _candidate_dirs.append(_ddir)
            except ValueError:
                continue

        csv_files = []
        for _cdir in _candidate_dirs:
            _raw = _cdir / 'raw'
            if not _raw.exists():
                continue
            for item in _raw.iterdir():
                if item.is_dir() and item.name.lower().startswith('kotak'):
                    csv_files += [str(p) for p in item.iterdir()
                                  if p.suffix.lower() == '.csv']
        # Deduplicate
        seen: set = set()
        return [f for f in csv_files if not (f in seen or seen.add(f))]

    def get_bank_files(self, date_str: str, bank_source: str,
                       archive_fallback: bool = True) -> list:
        """
        Locate bank statement files for a given bank source name.

        Search order:
          1. raw/{bank_source}/           — placed here by fetch_for_date
          2. For Kotak: raw/kotak_MYSTIC_*/*.csv
          3. archive/{date}/{bank_source}/attachments/ — placed here by archive_for_date

        Archive folder mapping (from sources.json sender → last source_name wins):
          icici_bank  → archive/.../icici_bank/attachments/*.txt
          hdfc_bank   → archive/.../hdfc_bank/attachments/*.zip
          axis_bank   → archive/.../axis_bank/attachments/*.zip
          kotak_bank  → archive/.../kotak_bank/attachments/*.zip
                        (re-extracts CSV from zip automatically)

        Returns list of absolute file paths.
        """
        # ── 1. Kotak bank: scan strategy folders directly ──────────────
        # The email ingestor matches Kotak emails to the 'kotak' (holdings)
        # source before 'kotak_bank' because they share the same sender and
        # subject.  This means bank CSVs are extracted alongside holdings
        # XLSXes into kotak_MYSTIC_WEVA/ etc. — a kotak_bank/ folder is
        # never created.  So for Kotak bank we always scan kotak_* strategy
        # folders for CSVs instead of relying on a dedicated folder.
        if bank_source == 'kotak_bank':
            csv_files = self._kotak_csv_scan(date_str)
            if csv_files:
                return csv_files
            # No ±day fallback — files must be in the exact date folder.
            # If missing, the recon workflow will flag it.
        else:
            # ── 1b. Standard raw folder (non-Kotak) ──────────────────────
            raw_files = self.list_source_files(date_str, bank_source)
            if raw_files:
                return raw_files

        # No archive fallback for bank statements — bank files must be
        # fetched via email or uploaded manually. Missing files are flagged
        # by the recon workflow so the user can upload or proceed without.
        return []

    def _kotak_bank_from_archive(self, date_str: str) -> list:
        """
        Re-extract Kotak bank CSVs from archive zips using Python's zipfile.

        Kotak zips use standard ZIP encryption (not AES-256), so Python's zipfile
        handles them correctly on all platforms — no 7z required.

        Checks two archive locations:
          archive/{date}/kotak_bank/attachments/  — fetched as kotak_bank source
          archive/{date}/kotak/attachments/        — archived under holdings kotak source
        """
        import zipfile as _zipfile, re as _re

        PASSWORD = b'AALCG4181G'
        csv_paths = []

        arch_candidates = [
            self.base_dir / 'archive' / date_str / 'kotak_bank' / 'attachments',
            self.base_dir / 'archive' / date_str / 'kotak'      / 'attachments',
        ]

        for arch_dir in arch_candidates:
            if not arch_dir.exists():
                continue
            for zip_path in sorted(arch_dir.glob('*.zip')):
                zip_name = zip_path.stem.upper().replace(' ', '_')
                m = _re.search(r'(MYSTIC_\w+)', zip_name)
                strategy = m.group(1) if m else zip_path.stem.upper().replace(' ', '_')
                dest = self.raw_dir(date_str, f'kotak_{strategy}')
                dest.mkdir(parents=True, exist_ok=True)
                try:
                    with _zipfile.ZipFile(zip_path, 'r') as zf:
                        for member in zf.infolist():
                            if member.filename.lower().endswith('.csv'):
                                # Extract to flat dest (no sub-folders)
                                member.filename = Path(member.filename).name
                                zf.extract(member, path=dest, pwd=PASSWORD)
                    new_csvs = [str(p) for p in dest.glob('*.csv')]
                    csv_paths.extend(new_csvs)
                except Exception as e:
                    logger.warning(f'Kotak archive extraction failed for {zip_path.name}: {e}')

        return csv_paths


    def get_axis_bank_zip_pairs(self, date_str: str) -> list:
        """
        Return list of (balance_zip_path, txn_zip_path) tuples for Axis bank.
        Axis sends one BankBalance + one BankTransaction zip per strategy
        (e.g. GOLDETEPMS_BankBalance_*.zip + GOLDETEPMS_BankTransaction_*.zip,
              GOLDFOFPMS_BankBalance_*.zip + GOLDFOFPMS_BankTransaction_*.zip).

        Groups by strategy prefix and returns one pair per strategy.
        Looks in raw/axis_bank/ first, then archive fallback.
        Returns [] if nothing found.
        """
        import re

        def _pair_zips(zip_list):
            """Group a flat list of zip paths into (balance, txn) pairs by strategy prefix + date."""
            bal_zips = [z for z in zip_list if 'bankbalance' in Path(z).name.lower()]
            txn_zips = [z for z in zip_list if 'banktransaction' in Path(z).name.lower()]

            def _date_suffix(p):
                """Extract DDMMYYYY date string from filename, e.g. GOLDETEPMS_BankBalance_25032026."""
                m = re.search(r'(\d{8})(?:\.zip)?$', Path(p).stem.lower())
                return m.group(1) if m else ''

            pairs = []
            for bal in bal_zips:
                # Extract strategy prefix: GOLDETEPMS_BankBalance_*.zip → GOLDETEPMS
                m = re.match(r'^([A-Z0-9]+)_?BANKBALANCE', Path(bal).stem.upper())
                prefix = m.group(1).upper() if m else ''
                bal_date = _date_suffix(bal)
                # Find matching txn zip: same strategy prefix AND same date suffix
                # Fall back to prefix-only match if no date match exists
                txn = None
                if prefix:
                    # Priority 1: same prefix + same date
                    txn = next((t for t in txn_zips
                                if Path(t).name.upper().startswith(prefix)
                                and _date_suffix(t) == bal_date), None)
                    # Priority 2: same prefix, any date (picks first alphabetically)
                    if txn is None:
                        txn = next((t for t in sorted(txn_zips)
                                    if Path(t).name.upper().startswith(prefix)), None)
                else:
                    txn = next((t for t in txn_zips
                                if 'banktransaction' in Path(t).name.lower()), None)
                pairs.append((bal, txn))
            # Also catch any txn zips with no matching balance
            matched_txns = {t for _, t in pairs if t}
            for txn in txn_zips:
                if txn not in matched_txns:
                    pairs.append((None, txn))
            return pairs

        raw_files = self.list_source_files(date_str, 'axis_bank')
        if raw_files:
            pairs = _pair_zips(raw_files)
            if pairs:
                return pairs

        # Archive fallback
        arch_dir = self.base_dir / 'archive' / date_str / 'axis_bank' / 'attachments'
        if arch_dir.exists():
            zips = [str(p) for p in sorted(arch_dir.glob('*.zip'))]
            return _pair_zips(zips)

        return []

    def get_axis_bank_zip_pair(self, date_str: str) -> tuple:
        """
        Backwards-compatible wrapper — returns the FIRST (balance, txn) pair.
        Use get_axis_bank_zip_pairs() for all strategies.
        """
        pairs = self.get_axis_bank_zip_pairs(date_str)
        if pairs:
            return pairs[0]
        return None, None

    def get_ws_bank_book(self, date_str: str) -> Optional[str]:
        """Find the WS Bank Book CSV/XLSX export in masters/."""
        return self._find_master(date_str,
                                  keywords=['bankbook', 'bank_book', 'bankbookconsolid'],
                                  extensions=('.csv', '.xlsx', '.xls'))

    def _find_master(self, date_str: str, keywords: list,
                     extensions: tuple = ('.csv', '.xlsx', '.xls'),  # CSV preferred — avoids openpyxl
                     shared_fallback: bool = True) -> Optional[str]:
        """
        Find a master file by matching keywords in the filename (case-insensitive).

        Search order:
          1. data/{date}/masters/   — date-specific upload (always checked)
          2. masters/               — shared folder (only if shared_fallback=True)

        shared_fallback=False is used for date-specific files like Z13_OrderLog
        that must not be served from a previous date's shared masters/ copy.
        """
        def _search(d: Path) -> Optional[str]:
            if not d.exists():
                return None
            for p in sorted(d.iterdir()):
                if p.suffix.lower() not in extensions or not p.is_file():
                    continue
                name_norm = p.name.lower().replace(' ', '').replace('_', '').replace('-', '')
                if any(kw.replace('_', '').replace('-', '') in name_norm for kw in keywords):
                    return str(p)
            return None

        # 1. Date-specific masters first
        result = _search(self.masters_dir(date_str))
        if result:
            return result
        # 2. Shared masters fallback (only for genuinely date-agnostic files)
        if shared_fallback:
            return _search(self.shared_masters_dir())
        return None

    def save_uploaded_file(self, file_data: bytes, filename: str,
                           dest_dir: str) -> str:
        """Save an uploaded file to dest_dir and return its path."""
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / filename
        out_path.write_bytes(file_data)
        return str(out_path)

    def date_exists(self, date_str: str) -> bool:
        return self.date_dir(date_str).exists()

    # ── Trade Recon helpers ────────────────────────────────────────────────── #

    def get_ws_trade_trans(self, date_str: str) -> Optional[str]:
        """
        Find Z13_OrderLog for this specific date (trade recon only — NOT Z13_TradeTrans).

        IMPORTANT: Unlike the security master (Z8, genuinely date-agnostic),
        the OrderLog is date-specific — each day's orders differ.
        We search date-specific masters/ ONLY; shared masters/ is intentionally
        excluded to prevent a previous day's OrderLog from silently being used.
        """
        keywords = ['orderlog', 'z13order', 'z13_orderlog']  # OrderLog ONLY — never grab Z13_TradeTrans
        # Search date-specific masters/ only — do NOT fall back to shared masters/
        return self._find_master(date_str, keywords=keywords, shared_fallback=False)

    def _files_for_date(self, date_str: str, subfolder: str,
                         extensions=('.xlsx','.xls','.csv','.pdf')) -> List[str]:
        """
        Return all files in raw/{subfolder}/ whose name contains the YYYYMMDD
        date tag appended by the email ingestor (e.g. grid_20260323.xlsx).
        Falls back to ALL files in the folder if none are date-tagged —
        this handles manually uploaded files and legacy files without a tag.
        """
        folder = self.raw_dir(date_str, subfolder)
        if not folder.exists():
            return []
        date_tag = date_str.replace('-', '')  # '2026-03-23' → '20260323'
        all_files = [p for p in folder.iterdir()
                     if p.is_file() and p.suffix.lower() in extensions]
        tagged    = [p for p in all_files if date_tag in p.stem]
        return [str(p) for p in sorted(tagged or all_files, reverse=True)]

    def get_dealer_file(self, date_str: str) -> Optional[str]:
        """
        Find the dealer trade grid for date_str.
        Prefers files date-tagged by the ingestor (grid_20260323.xlsx).
        Falls back to any Excel/CSV in raw/dealer/ for manual uploads.
        """
        files = self._files_for_date(date_str, 'dealer',
                                      extensions=('.xlsx', '.xls', '.csv'))
        if not files:
            return None
        # Prefer grid_* files (canonical dealer grid name)
        for f in files:
            if Path(f).name.lower().startswith('grid'):
                return f
        # Skip Trade Allocation files (WS custodian notifications, not the dealer grid)
        allocation_prefixes = ('goldstandard_', 'z1_', 'z13_')
        for f in files:
            if not any(Path(f).name.lower().startswith(pfx) for pfx in allocation_prefixes):
                return f
        return files[0]

    def get_nsdl_file(self, date_str: str) -> Optional[str]:
        """Find the NSDL steady file.

        Search order:
          1. raw/nsdl/ — placed by email ingestor (SRK*.xls files)
          2. masters/  — uploaded manually or downloaded from WS
        Date-specific only — a stale file from a prior date must not
        silently contaminate today's C3 check.
        """
        # 1. Check raw/nsdl/ first (email-fetched files)
        raw_nsdl = self.raw_dir(date_str, 'nsdl')
        if raw_nsdl.exists():
            xls_files = sorted(
                [str(p) for p in raw_nsdl.iterdir()
                 if p.suffix.lower() in ('.xls', '.xlsx') and p.is_file()],
                reverse=True)
            if xls_files:
                return xls_files[0]
        # 2. Fallback to masters/
        return self._find_master(date_str,
                                  keywords=['nsdl', 'cnstat', 'contractnote', 'srk'],
                                  extensions=('.xls', '.xlsx'),
                                  shared_fallback=False)

    def list_broker_cn_files(self, date_str: str) -> List[str]:
        """List broker contract note PDF files only.
        Scans both raw/broker_cn/ and all raw/broker_cn_*/ subfolders
        (broker-specific folders created by the email ingestor).
        PDF-only: XLS block-deal files are no longer used for trade recon.
        """
        import logging as _log
        _logger = _log.getLogger(__name__)
        raw = self.raw_dir(date_str)
        if not raw.exists():
            return []
        files = []
        _scanned = []
        _skipped = []
        for folder in raw.iterdir():
            if not folder.is_dir():
                continue
            if folder.name == 'broker_cn' or folder.name.startswith('broker_cn_'):
                _scanned.append(folder.name)
                for p in folder.rglob('*'):
                    if p.is_file() and p.suffix.lower() == '.pdf':
                        files.append(str(p))
            else:
                _skipped.append(folder.name)
        _logger.info(f'CN scan: raw/{date_str} scanned={_scanned} skipped={_skipped} → {len(files)} PDF(s)')
        return sorted(files)

    def list_exchange_files(self, date_str: str) -> List[str]:
        """Return exchange files for this date (date-tagged by ingestor, or all if untagged)."""
        return self._files_for_date(date_str, 'exchange',
                                    extensions=('.xls', '.xlsx', '.pdf'))


    def trade_recon_files_status(self, date_str: str) -> dict:
        """Return presence dict for all trade recon input files."""
        ws_tt   = self.get_ws_trade_trans(date_str)
        dealer  = self.get_dealer_file(date_str)
        nsdl    = self.get_nsdl_file(date_str)
        sec_m   = self.get_security_master(date_str)
        cn_pdfs = self.list_broker_cn_files(date_str)
        exchange= []  # Exchange files (C5) removed from trade recon
        # Count all dealer files so the UI shows how many are in the folder
        _dd = self.raw_dir(date_str, 'dealer')
        _dealer_all = sorted([str(p) for p in _dd.iterdir()
                               if p.is_file() and p.suffix.lower() in ('.xlsx','.xls','.csv')]
                             ) if _dd.exists() else []
        return {
            'ws_trade_trans': {'present': bool(ws_tt),  'path': ws_tt  or '', 'label': 'Z13_OrderLog (WS Orders)'},
            'dealer_file':    {'present': bool(_dealer_all), 'path': dealer or '',
                               'count': len(_dealer_all), 'paths': _dealer_all},
            'nsdl_file':      {'present': bool(nsdl),  'path': nsdl  or ''},
            'security_master':{'present': bool(sec_m), 'path': sec_m or ''},
            'broker_cn_pdfs': {'present': bool(cn_pdfs), 'count': len(cn_pdfs),
                               'paths': cn_pdfs,
                               'filenames': [str(Path(p).name) for p in cn_pdfs]},
            'exchange_files': {'present': bool(exchange), 'count': len(exchange),
                               'paths': exchange},
            'ready':          bool(ws_tt and _dealer_all and sec_m and cn_pdfs),
        }

    def list_dates(self) -> List[str]:
        """List all dates that have data folders."""
        data_dir = self.base_dir / 'data'
        if not data_dir.exists():
            return []
        dates = []
        for p in sorted(data_dir.iterdir(), reverse=True):
            if p.is_dir() and len(p.name) == 10 and p.name.count('-') == 2:
                dates.append(p.name)
        return dates
