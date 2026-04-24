"""
Bank reconciliation workflow.

Extracted from app.py api_bank_recon() to keep the Flask route handler thin.
Contains all custodian file parsing (ICICI, HDFC, Axis, Kotak) and the
reconciliation engine orchestration. Returns results; caller handles Flask
state, email, and JSON response.
"""

import logging
import re as _re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class BankReconError(Exception):
    """Raised for fatal workflow errors (missing required files, no accounts found).
    Carries the parse_log so the caller can include it in the error response.
    """
    def __init__(self, message, parse_log=None):
        super().__init__(message)
        self.parse_log = parse_log or []


def _check_account_date(acct, bank_name: str, report_date_str: str, log_fn) -> bool:
    """Warn if the custodian account's as_on_date doesn't match the report date.
    Returns True if acceptable (matches or unknown), False if wrong.
    """
    if not acct.as_on_date:
        return True
    ao = acct.as_on_date.upper().strip()
    if ao == report_date_str:
        return True
    log_fn(f'WARNING: {bank_name} account {acct.account_no} as_on={ao!r} '
           f'but report date is {report_date_str} — balance may be for wrong date')
    return False


def _hdfc_as_on_from_zip(zip_path: str) -> str:
    """HDFC doesn't embed a date in the balance file.
    Infer as_on = zip's internal XLSX last-modified date minus 1 day.
    """
    try:
        import zipfile as _zf, datetime as _datetime
        with _zf.ZipFile(zip_path) as z:
            for info in z.infolist():
                if info.filename.endswith('.xlsx'):
                    dt = _datetime.datetime(*info.date_time)
                    ao = (dt.date() - timedelta(days=1)).strftime('%d-%b-%Y').upper()
                    return ao
    except Exception:
        pass
    return ''


def run_bank_recon(date_str: str, fm, sources: list, password: str,
                   bank_history: dict, log_fn, bank_dates: list = None,
                   bank_tolerance_rs: float = None):
    """
    Orchestrate custodian bank file parsing and reconciliation.

    Args:
        date_str:     Report date in YYYY-MM-DD format.
        fm:           FileManager instance.
        sources:      Parsed sources.json list.
        password:     Zip/file password for HDFC, Axis, Kotak.
        bank_history: Prior-day closing balances dict (from _load_bank_balance_history).
        log_fn:       Callable(msg) for logging (also receives to app log).
        bank_dates:   List of dates to load bank statements from (inclusive).
                      Covers non-working days between previous working day and
                      recon date. If None, defaults to [date_str].

    Returns:
        (summary, balance_summary, parse_log) tuple.

    Raises:
        BankReconError: Missing required files or no accounts found.
    """
    from parsers.icici_bank        import ICICIBankParser
    from parsers.hdfc_bank         import HdfcBankParser
    from parsers.axis_bank         import AxisBankParser
    from parsers.kotak_bank        import KotakBankParser
    from parsers.ws_bank_book      import WSBankBookParser
    from core.pool_master          import PoolMaster
    from core.client_bank_details  import ClientBankDetails
    from core.bank_vs_ws_recon     import BankVsWSReconEngine
    from core.bank_recon_engine    import ReconEngine as BankBalanceEngine
    from core.pools_hub            import PoolsHub

    _report_date     = datetime.strptime(date_str, '%Y-%m-%d').date()
    _report_date_str = _report_date.strftime('%d-%b-%Y').upper()

    # Date ranges:
    # _all_dates[0] = prev working day (for opening balances: HDFC/Axis/WS)
    # _all_dates[1:] = day after prev working day through recon day (for txns)
    # _all_dates[-1] = recon day (for closing balances)
    # For single-day recon: _all_dates = [recon_day], no prev working day data.
    _all_dates = bank_dates or [date_str]
    _prev_wd_date = _all_dates[0] if len(_all_dates) > 1 else None
    _txn_dates    = _all_dates[1:] if len(_all_dates) > 1 else _all_dates
    _recon_date   = _all_dates[-1]   # always the recon day
    if len(_all_dates) > 1:
        log_fn(f'Bank recon: prev working day={_prev_wd_date}, '
               f'txn dates={_txn_dates[0]}→{_txn_dates[-1]}, '
               f'recon date={_recon_date}')

    parse_log: list = []

    def plog(msg):
        log_fn(msg)
        parse_log.append(msg)

    custodian_accounts: list = []

    # ── Helper: dedup transactions across multi-day files ────────────────── #
    def _dedup_txns(all_txns, per_file_max):
        """Deduplicate transactions using ICICI-style max-count-per-file logic."""
        seen_count: dict = {}
        deduped = []
        for t in all_txns:
            key = (t.tran_date, round(t.debit, 2), round(t.credit, 2),
                   t.description[:30] if t.description else '')
            allowed = per_file_max.get(key, 1)
            if seen_count.get(key, 0) < allowed:
                seen_count[key] = seen_count.get(key, 0) + 1
                deduped.append(t)
        return deduped

    def _collect_file_max(transactions, existing_max: dict) -> dict:
        """Track max occurrence of each txn key within a single file."""
        file_cnt: dict = {}
        for t in transactions:
            _k = (t.tran_date, round(t.debit, 2), round(t.credit, 2),
                  t.description[:30] if t.description else '')
            file_cnt[_k] = file_cnt.get(_k, 0) + 1
        for _k, _v in file_cnt.items():
            existing_max[_k] = max(existing_max.get(_k, 0), _v)
        return existing_max

    # ── ICICI bank ───────────────────────────────────────────────────────── #
    # ICICI: daily files with B/F (opening), transactions (separate
    # debit/credit columns), and closing in footer text.
    #
    # Multi-day (and single-day) rule:
    #   Opening      = B/F from the EARLIEST file in _txn_dates.
    #   Transactions = pooled across ALL _txn_dates files, deduped.
    #   Closing      = opening + credits − debits (computed).
    #
    # Prev-WD files are not loaded — they sit outside the recon window.
    # Day-after-recon is included defensively: ICICI emails today's
    # statement around 2 AM the next morning, so it can land in the
    # day_after folder depending on routing.
    _icici_files_tagged = []  # (date_str, file_path)
    _icici_load_dates = list(_txn_dates)
    _day_after = (datetime.strptime(date_str, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')
    if _day_after not in _icici_load_dates:
        _icici_load_dates.append(_day_after)
    for _bd in _icici_load_dates:
        for _f in fm.get_bank_files(_bd, 'icici_bank'):
            _icici_files_tagged.append((_bd, _f))
    # Sort ascending so the earliest file's account object becomes the
    # base (and its B/F becomes opening).
    _icici_files_tagged.sort(key=lambda x: x[0])

    if _icici_files_tagged:
        parser = ICICIBankParser()
        _icici_closing:      dict = {}  # acct_no → (closing_balance, as_on_date, source_file)
        _icici_txns:         dict = {}  # acct_no → [BankTransaction, ...]
        _icici_fmax:         dict = {}  # acct_no → {txn_key → max_count}
        _icici_base:         dict = {}  # acct_no → BankAccount (from earliest file)

        for _bd, f in _icici_files_tagged:
            r = parser.parse_file(f)
            if not r.ok:
                plog(f'ICICI parse error {Path(f).name}: {r.error}')
                continue
            plog(f'ICICI: parsed {len(r.accounts)} account(s) from {_bd}')
            for acct in r.accounts:
                acct.source_file = f
                ano = acct.account_no
                # Earliest sighting = earliest _txn_dates file → B/F = opening.
                if ano not in _icici_base:
                    _icici_base[ano] = acct
                _icici_txns.setdefault(ano, []).extend(acct.transactions)
                _icici_fmax.setdefault(ano, {})
                _collect_file_max(acct.transactions, _icici_fmax[ano])
                if acct.closing_balance != 0.0:
                    _icici_closing[ano] = (acct.closing_balance, acct.as_on_date, f)

        merged_accounts = []
        for ano, acct in _icici_base.items():
            # Opening = acct.opening_balance (already set to the earliest
            # file's B/F by the parser + first-seen-wins logic above).
            acct.has_opening_balance = True
            acct.transactions = _dedup_txns(
                _icici_txns.get(ano, []), _icici_fmax.get(ano, {}))
            # Closing = opening + credits − debits (computed). ICICI's
            # file footer is stamped at email-sent time (~2 AM next day)
            # and can include next-day transactions that would be
            # double-counted if we trusted the footer.
            acct.closing_balance = acct.computed_closing
            acct.as_on_date      = date_str
            _cl = _icici_closing.get(ano)
            if _cl:
                acct.source_file = _cl[2]
            merged_accounts.append(acct)

        if merged_accounts:
            custodian_accounts.extend(merged_accounts)
            plog(f'ICICI bank: {len(merged_accounts)} account(s) '
                 f'(txns from {len(_icici_files_tagged)} file(s))')
        else:
            plog('ICICI bank: no usable accounts found')
    else:
        plog(f'WARNING: ICICI bank files not found. Fetch emails or upload statement.')

    # ── HDFC bank ────────────────────────────────────────────────────────── #
    # HDFC: separate balance file (opening/closing) and transaction file
    # (amounts in column C, C/D indicator in column D).
    # Multi-day: opening = prev working day's closing balance,
    # closing = recon day's closing balance.
    # Transactions = sum across all days with D→debit, C→credit, deduplicated.
    hdfc_txn_files = []
    hdfc_bal_files = []
    # Balance files: load from ALL dates (prev working day for opening + recon day for closing)
    for _bd in _all_dates:
        hdfc_bal_files.extend(fm.get_bank_files(_bd, 'hdfc_bank_balance'))
    # Transaction files: only from txn dates (day after prev working day onward)
    for _bd in _txn_dates:
        hdfc_txn_files.extend(fm.get_bank_files(_bd, 'hdfc_bank'))
    hdfc_parser = HdfcBankParser()

    # Parse ALL balance files chronologically — earliest = opening, latest = closing
    _hdfc_bal_by_acct: dict = {}  # acct_no → [(file, BankAccount), ...]
    for f in sorted(hdfc_bal_files):
        fname = Path(f).name
        if f.endswith('.zip'):
            r = hdfc_parser.parse_balance_zip(f, password=password)
        else:
            parts = fname.split('_', 1)
            bal_alias = parts[0] if len(parts) > 1 and parts[0].upper().startswith('GW') else ''
            r = hdfc_parser.parse_balance_file(f, zip_alias=bal_alias)
        if r.ok:
            for a in r.accounts:
                a.source_file = f
                _hdfc_bal_by_acct.setdefault(a.account_no, []).append((f, a))
            plog(f'HDFC balance: {len(r.accounts)} account(s) from {fname}')
        else:
            plog(f'HDFC balance parse error: {r.error}')

    # Parse ALL transaction files, collect per account
    _hdfc_txns:  dict = {}  # acct_no → [BankTransaction, ...]
    _hdfc_fmax:  dict = {}  # acct_no → {txn_key → max_count}
    _hdfc_base:  dict = {}  # acct_no → BankAccount (template from first txn file)
    for f in sorted(hdfc_txn_files):
        fname = Path(f).name
        if f.endswith('.zip'):
            r = hdfc_parser.parse_zip(f, password=password)
        else:
            parts = fname.split('_', 1)
            zip_alias = parts[0] if len(parts) > 1 and parts[0].upper().startswith('GW') else ''
            r = hdfc_parser.parse_file(f, zip_alias=zip_alias)
        if r.ok:
            for a in r.accounts:
                a.source_file = f
                if a.account_no not in _hdfc_base:
                    _hdfc_base[a.account_no] = a
                _hdfc_txns.setdefault(a.account_no, []).extend(a.transactions)
                _hdfc_fmax.setdefault(a.account_no, {})
                _collect_file_max(a.transactions, _hdfc_fmax[a.account_no])
            plog(f'HDFC bank txn: {len(r.accounts)} account(s) from {fname}')
        else:
            plog(f'HDFC bank parse error: {r.error}')

    # Merge: earliest balance closing = opening, latest balance closing = closing
    hdfc_merged: dict = {}
    for ano in set(list(_hdfc_base.keys()) + list(_hdfc_bal_by_acct.keys())):
        acct = _hdfc_base.get(ano)
        bal_entries = _hdfc_bal_by_acct.get(ano, [])
        if not acct and bal_entries:
            acct = bal_entries[-1][1]  # use balance-only account
        if not acct:
            continue
        if bal_entries:
            earliest_bal = bal_entries[0][1]   # first file = prev working day
            latest_bal   = bal_entries[-1][1]   # last file = recon day
            acct.opening_balance    = earliest_bal.closing_balance
            acct.closing_balance    = latest_bal.closing_balance
            acct.has_opening_balance = True
            acct.as_on_date         = latest_bal.as_on_date
            acct.source_file        = bal_entries[-1][0]
        acct.transactions = _dedup_txns(
            _hdfc_txns.get(ano, []), _hdfc_fmax.get(ano, {}))
        hdfc_merged[ano] = acct

    for a in hdfc_merged.values():
        _check_account_date(a, 'HDFC', _report_date_str, plog)
        custodian_accounts.append(a)

    if not hdfc_txn_files and not hdfc_bal_files:
        plog('HDFC bank: no files found in raw/hdfc_bank/ or raw/hdfc_bank_balance/')

    # ── Axis bank ────────────────────────────────────────────────────────── #
    # Axis: separate balance file (closing only) and transaction file
    # (amounts in column J, Credit/Debit in column G).
    # Multi-day: opening = prev working day's closing balance,
    # closing = recon day's closing balance.
    # Transactions = sum across all days with Credit→credit, Debit→debit, deduplicated.
    # Balance zips: from ALL dates (prev working day for opening)
    # Transaction zips: only from txn dates
    all_axis_bal_pairs = []
    all_axis_txn_pairs = []
    for _bd in _all_dates:
        all_axis_bal_pairs.extend(fm.get_axis_bank_zip_pairs(_bd))
    for _bd in _txn_dates:
        all_axis_txn_pairs.extend(fm.get_axis_bank_zip_pairs(_bd))

    # Group by strategy prefix: ALL balance zips (for opening/closing) + txn zips
    strategy_bals: dict = {}   # prefix → [bal_zip, ...] (chronological)
    strategy_txns: dict = {}   # prefix → [txn_zip, ...]
    for bal_zip, txn_zip in all_axis_bal_pairs:
        if not bal_zip:
            continue
        m = _re.match(r'^([A-Z0-9]+)', Path(bal_zip).name.upper())
        prefix = m.group(1) if m else Path(bal_zip).stem
        strategy_bals.setdefault(prefix, []).append(bal_zip)
    for bal_zip, txn_zip in all_axis_txn_pairs:
        if txn_zip:
            m = _re.match(r'^([A-Z0-9]+)', Path(txn_zip).name.upper())
            prefix = m.group(1) if m else Path(txn_zip).stem
            strategy_txns.setdefault(prefix, []).append(txn_zip)

    if strategy_bals:
        axis_parser = AxisBankParser()
        for prefix in sorted(strategy_bals):
            bal_zips = sorted(strategy_bals[prefix])   # chronological by filename
            txn_zips = sorted(strategy_txns.get(prefix, []))
            earliest_bal = bal_zips[0]    # prev working day = opening
            latest_bal   = bal_zips[-1]   # recon day = closing

            # Parse earliest balance for opening
            r_open = axis_parser.parse_zip_pair(earliest_bal, '', password=password)
            opening_by_acct: dict = {}
            if r_open.ok:
                for a in r_open.accounts:
                    opening_by_acct[a.account_no] = a.closing_balance

            # Parse latest balance + first txn for account structure
            first_txn = txn_zips[0] if txn_zips else ''
            r = axis_parser.parse_zip_pair(latest_bal, first_txn, password=password)
            if not r.ok:
                plog(f'Axis bank [{prefix}] parse error: {r.error}')
                continue

            # Collect transactions from ALL txn zips, with dedup
            _axis_txns: dict = {}   # acct_no → [BankTransaction, ...]
            _axis_fmax: dict = {}   # acct_no → {txn_key → max_count}
            for txn_zip in txn_zips:
                r2 = axis_parser.parse_zip_pair(latest_bal, txn_zip, password=password)
                if r2.ok:
                    for a2 in r2.accounts:
                        _axis_txns.setdefault(a2.account_no, []).extend(a2.transactions)
                        _axis_fmax.setdefault(a2.account_no, {})
                        _collect_file_max(a2.transactions, _axis_fmax[a2.account_no])

            for acct in r.accounts:
                acct.source_file = latest_bal
                # Set opening = earliest balance file's closing
                if acct.account_no in opening_by_acct:
                    acct.opening_balance    = opening_by_acct[acct.account_no]
                    acct.has_opening_balance = True
                # Dedup transactions across all days
                acct.transactions = _dedup_txns(
                    _axis_txns.get(acct.account_no, []),
                    _axis_fmax.get(acct.account_no, {}))

            custodian_accounts.extend(r.accounts)
            day_count = len(set(Path(t).parent.name for t in txn_zips)) if txn_zips else 0
            plog(f'Axis bank [{prefix}]: {len(r.accounts)} account(s)'
                 + (f' ({len(txn_zips)} txn file(s) across {day_count} day(s))' if txn_zips else ''))
            for w in (r.warnings or []):
                plog(f'  Axis warning: {w}')
    else:
        plog('Axis bank: no BankBalance/BankTransaction zips found')

    # ── Kotak bank ───────────────────────────────────────────────────────── #
    # Kotak: self-contained daily CSV with opening, transactions (signed:
    # negative=debit, positive=credit), and closing.
    # Multi-day: opening = earliest file's opening, closing = latest file's
    # closing, transactions = sum across all days, deduplicated.
    # Kotak: self-contained files with opening, transactions, and closing.
    # Each file covers prev business day through current day, so txns
    # overlap across consecutive files. No files for weekends/holidays.
    #
    # Opening balance = prev working day file's CLOSING (end-of-day balance).
    # Closing balance = recon day file's closing.
    # Transactions = recon day file, filtered to txn date range, deduped.
    #
    # Load prev WD file (for opening) + recon day file (for closing + txns).
    kotak_files_with_date = []  # (expected_date, file_path)
    if _prev_wd_date:
        for _kf in fm.get_bank_files(_prev_wd_date, 'kotak_bank'):
            kotak_files_with_date.append(('prev_wd', _kf))
    for _kf in fm.get_bank_files(_recon_date, 'kotak_bank'):
        kotak_files_with_date.append(('recon', _kf))
    kotak_files = [f for _, f in kotak_files_with_date]
    if kotak_files:
        parser = KotakBankParser()
        kotak_client_map: dict = {}

        seen_folders: set = set()
        for csv_path in kotak_files:
            seen_folders.add(str(Path(csv_path).parent))

        for folder_str in seen_folders:
            folder = Path(folder_str)
            xlsx_files = sorted(folder.glob('*.xlsx'))
            csv_files  = sorted(f for f in folder.glob('*.csv'))
            if not xlsx_files:
                continue
            for idx, xlsx_path in enumerate(xlsx_files):
                client_id = parser._extract_kotak_client_id(str(xlsx_path))
                if not client_id:
                    continue
                if idx < len(csv_files):
                    acct_hint = csv_files[idx].stem
                    kotak_client_map[acct_hint] = client_id
                    plog(f'Kotak: {xlsx_path.name} → {acct_hint} → client_id={client_id}')

        # Opening = prev WD file's closing. Closing = recon day file's closing.
        # Transactions from recon day file only, filtered to txn date range.
        _kotak_prev_closing: dict = {}  # acct_no → closing from prev WD
        kotak_merged:        dict = {}  # acct_no → BankAccount (recon day)
        _kotak_txns:         dict = {}
        _kotak_fmax:         dict = {}

        for tag, f in kotak_files_with_date:
            if f.endswith('.zip'):
                r = parser.parse_zip(f, password=password)
            else:
                r = parser.parse_file(f)
            if r.ok:
                for acct in r.accounts:
                    acct.source_file = f
                    if not acct.kotak_client_id and acct.account_no in kotak_client_map:
                        acct.kotak_client_id = kotak_client_map[acct.account_no]
                    ano = acct.account_no
                    if tag == 'prev_wd':
                        # Prev WD: capture closing as opening for recon
                        _kotak_prev_closing[ano] = acct.closing_balance
                    else:
                        # Recon day: use for closing + transactions
                        kotak_merged[ano] = acct
                        _kotak_txns.setdefault(ano, []).extend(acct.transactions)
                        _kotak_fmax.setdefault(ano, {})
                        _collect_file_max(acct.transactions, _kotak_fmax[ano])
                plog(f'Kotak bank ({tag}): {len(r.accounts)} account(s) from {Path(f).name}')
            else:
                plog(f'Kotak bank parse error: {r.error}')

        # Valid transaction dates: only the txn date range (day after prev WD
        # through recon day). The Kotak file covers prev business day through
        # current day, so earlier transactions must be excluded to match WS.
        _kotak_valid_txn_dates = {
            datetime.strptime(d, '%Y-%m-%d').strftime('%d-%b-%Y').upper()
            for d in _txn_dates
        }

        for ano, acct in kotak_merged.items():
            # Opening = prev WD file's closing (universal rule).
            # Kotak CSVs ship an "Opening Balance" row, but it's the start
            # of the file's multi-day window (typically prev WD), not
            # today's opening. So we can't treat it as sacrosanct the way
            # we do for ICICI — we still derive today's opening from the
            # prev-WD file's closing.
            if ano in _kotak_prev_closing:
                acct.opening_balance    = _kotak_prev_closing[ano]
                acct.has_opening_balance = True
            else:
                # No prev WD file — fall back to the file's own opening
                acct.has_opening_balance = True
            # Dedup then filter to valid txn dates
            _all_txns = _dedup_txns(
                _kotak_txns.get(ano, []), _kotak_fmax.get(ano, {}))
            acct.transactions = [t for t in _all_txns
                                 if t.tran_date.upper() in _kotak_valid_txn_dates]
            _check_account_date(acct, 'Kotak', _report_date_str, plog)
        custodian_accounts.extend(kotak_merged.values())
    else:
        plog('Kotak bank: no CSV found (checked raw/ and archive/)')

    if not custodian_accounts:
        raise BankReconError(
            'No bank statement files found for this date. '
            'Fetch emails first, then run bank recon.',
            parse_log,
        )

    plog(f'Total custodian accounts loaded: {len(custodian_accounts)}')

    # ── WS Bank Book ─────────────────────────────────────────────────────── #
    # Load WS Bank Book for all dates:
    # - Prev working day (_all_dates[0]): for opening balance (Total closing)
    # - Txn dates (_txn_dates): for category sums + recon-day closing
    ws_parser = WSBankBookParser()
    ws_books_opening = []   # prev working day only (for opening)
    ws_books_txn     = []   # txn dates (for categories + closing)
    if _prev_wd_date:
        _bb = fm.get_ws_bank_book(_prev_wd_date)
        if _bb:
            ws_books_opening.append((_prev_wd_date, ws_parser.parse_file(_bb)))
    for _bd in _txn_dates:
        _bb = fm.get_ws_bank_book(_bd)
        if _bb:
            _wb = ws_parser.parse_file(_bb)
            ws_books_txn.append((_bd, _wb))

    if not ws_books_txn:
        raise BankReconError(
            'WS Bank Book CSV not found in masters/ for any date. '
            'Upload the Z13_BankBook export.',
            parse_log,
        )

    # Use the LAST Bank Book (recon day) as the base — it has closing balances
    ws_book = ws_books_txn[-1][1]

    # Sum scheme-level categories across txn dates only (not prev working day)
    if len(ws_books_txn) > 1 and ws_book.scheme_summaries:
        from collections import defaultdict as _dd_ws
        _ws_cat_sums: dict = _dd_ws(lambda: {'buy_sell': 0.0, 'income': 0.0,
                                              'expenses': 0.0, 'dep_with': 0.0})
        for _bd, _wb in ws_books_txn:
            for ss in _wb.scheme_summaries:
                k = ss.scheme_name.lower().strip()
                _ws_cat_sums[k]['buy_sell'] += ss.buy_sell
                _ws_cat_sums[k]['income']   += ss.income
                _ws_cat_sums[k]['expenses'] += ss.expenses
                _ws_cat_sums[k]['dep_with'] += ss.dep_with

        for ss in ws_book.scheme_summaries:
            k = ss.scheme_name.lower().strip()
            if k in _ws_cat_sums:
                ss.buy_sell = round(_ws_cat_sums[k]['buy_sell'], 2)
                ss.income   = round(_ws_cat_sums[k]['income'], 2)
                ss.expenses = round(_ws_cat_sums[k]['expenses'], 2)
                ss.dep_with = round(_ws_cat_sums[k]['dep_with'], 2)

    # Opening = prev working day's Total closing (from opening Bank Book)
    _ws_open_matched = 0
    _ws_open_missed  = []
    if ws_books_opening and ws_book.scheme_summaries:
        _opening_wb = ws_books_opening[0][1]
        if _opening_wb.scheme_summaries:
            _open_by_name = {s.scheme_name.lower().strip(): s
                             for s in _opening_wb.scheme_summaries}
            plog(f'WS opening: {len(_open_by_name)} scheme(s) in prev WD Bank Book')
            for ss in ws_book.scheme_summaries:
                k = ss.scheme_name.lower().strip()
                if k in _open_by_name:
                    ss.opening_balance = _open_by_name[k].closing_balance
                    _ws_open_matched += 1
                else:
                    _ws_open_missed.append(ss.scheme_name)
                    plog(f'  WS opening MISS: "{ss.scheme_name}" not in prev WD '
                         f'(available: {list(_open_by_name.keys())[:5]}...)')
        else:
            plog('WS opening: prev WD Bank Book has no scheme summaries (no Total rows)')
    elif not ws_books_opening:
        plog('WS opening: no prev WD Bank Book found — using recon-day opening')

    _total_ws_days = len(ws_books_opening) + len(ws_books_txn)
    plog(f'WS Bank Book: {_total_ws_days} day(s) loaded '
         f'(opening from {ws_books_opening[0][0] if ws_books_opening else "N/A"}, '
         f'{_ws_open_matched} matched / {len(_ws_open_missed)} missed, '
         f'closing from {ws_books_txn[-1][0]}, '
         f'categories from {len(ws_books_txn)} txn day(s))')

    # ── Pool Master ──────────────────────────────────────────────────────── #
    pm_path = fm.get_strategy_master(date_str)
    if not pm_path:
        raise BankReconError('Z13_PoolMaster.xlsx not found in masters/.', parse_log)

    pm = PoolMaster().load(pm_path)

    # ── Client Bank Details (optional but strongly recommended) ──────────── #
    cbd_path = fm.get_client_bank_details(date_str)
    cbd = ClientBankDetails().load(cbd_path) if cbd_path else None
    if cbd:
        plog(f'Client bank details: {len(cbd.all_entries)} entries loaded')
    else:
        plog('Client bank details not found — using scheme-name fallback')

    # ── Pool maps ────────────────────────────────────────────────────────── #
    _hub           = PoolsHub.load()
    icici_pool_map = _hub.bank_pool_map('icici')
    kotak_pool_map = _hub.bank_pool_map('kotak')
    hdfc_pool_map  = _hub.bank_pool_map('hdfc')

    # Settlement timing adjustments (equity sells, MF orders) are not applied
    # automatically — the timing differences are handled via the manual break
    # explanation feature in the bank recon UI.
    _mf_orders_by_mapid: dict = {}

    # ── Layer 0: custodian internal balance check ─────────────────────────  #
    balance_engine  = BankBalanceEngine()
    balance_summary = balance_engine.reconcile(custodian_accounts, date_str)

    # ── Opening balance: universal rule ──────────────────────────────────── #
    # Opening = prev working day's closing balance for ALL banks + WS.
    # Already set during per-bank loading above (each bank loads prev working
    # day files and uses their closing as opening). bank_history is passed
    # as fallback only when prev-day files are missing.
    _cust_history = bank_history.get('cust', bank_history) if isinstance(bank_history, dict) else {}
    _ws_history   = bank_history.get('ws', {}) if isinstance(bank_history, dict) else {}

    # ── Layer 1-3: Bank vs WS ─────────────────────────────────────────────  #
    engine  = BankVsWSReconEngine()
    summary = engine.reconcile(
        custodian_accounts   = custodian_accounts,
        ws_book              = ws_book,
        pool_master          = pm,
        icici_pool_map       = icici_pool_map,
        kotak_pool_map       = kotak_pool_map,
        hdfc_pool_map        = hdfc_pool_map,
        client_bank_details  = cbd,
        bank_balance_history = _cust_history,
        ws_opening_history   = _ws_history,
        mf_orders_by_mapid   = _mf_orders_by_mapid,
        date                 = date_str,
        bank_tolerance_rs    = bank_tolerance_rs,
    )

    plog(f'Bank recon complete: {summary.total_pools} pools — '
         f'{summary.clean} Clean, {summary.breaks} Break')

    return summary, balance_summary, parse_log
