"""
Bank Balance History
--------------------
Prior-day bank closing balances lookup + append log.

Custodians vary in what they send:
  - ICICI, Kotak, WS Bank Book — full statements (include opening balance)
  - HDFC, Axis — balance files (closing only; need prior-day closing to derive opening)

This module provides:
  - load(): reconstruct prior-day closing balances from raw bank files (tier 1)
            and a cumulative history JSON (tier 2 fallback).
  - append(): append today's reconciled per-pool closing balances to the
              cumulative history log.

The module is intentionally side-effect-free at import time and takes its
dependencies (file_manager, data_dir, logger) via injection so the caller
stays the owner of app-level singletons.
"""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from parsers.axis_bank import AxisBankParser
from parsers.hdfc_bank import HdfcBankParser
from parsers.icici_bank import ICICIBankParser
from parsers.kotak_bank import KotakBankParser
from parsers.ws_bank_book import WSBankBookParser

_logger = logging.getLogger(__name__)


def _date_variants(date_str: str) -> set:
    """Filename date variants for a given YYYY-MM-DD date.

    Mirrors app._date_variants — duplicated here deliberately so the module
    has no runtime dependency on app.py.
    """
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    d = dt.strftime('%d')
    m = dt.strftime('%m')
    y = dt.strftime('%Y')
    d_nz = str(int(d))
    m_nz = str(int(m))
    return {
        f"{d}{m}{y}",           # 18032026
        f"{y}{m}{d}",           # 20260318
        f"{d}_{m}_{y}",         # 18_03_2026
        f"{d}-{m}-{y}",         # 18-03-2026
        f"{d}{m}{y[2:]}",       # 180326
        f"{d_nz}_{m_nz}_{y}",   # 18_3_2026  (HDFC format)
        f"{d_nz}_{m}_{y}",      # 18_03_2026
    }


def load(
    date_str: str,
    *,
    file_manager,
    prev_working_day,
    hdfc_zip_password: str,
    data_dir: Path,
    logger=None,
) -> dict:
    """Load prior working day's closing balances for banks that don't supply
    opening balance (Axis, HDFC).

    Two-tier lookup:
      1. PRIMARY — Re-parse prior working day's raw bank files + WS Bank Book.
         This is the most reliable source because it uses the actual files
         the custodians sent and matches what bank recon saw on that day.
      2. FALLBACK — Read ``data/bank_balance_history.json`` (cumulative log
         written by :func:`append`). Covers edge cases like the very first
         run or long holiday sequences where prior-day raw files may have
         been cleaned up.

    Args:
        date_str: Recon date (YYYY-MM-DD).
        file_manager: FileManager instance (provides get_bank_files, etc.).
        prev_working_day: Pre-computed previous working day (YYYY-MM-DD) or
            None if unavailable. When None, tier 1 is skipped.
        hdfc_zip_password: Password for HDFC/Axis encrypted ZIPs.
        data_dir: Project DATA_DIR (used to locate the fallback history file).
        logger: Optional logger override; defaults to module logger.

    Returns:
        ``{'cust': {account_no: cust_closing}, 'ws': {mapid: ws_closing_sum}}``
    """
    log = logger or _logger

    cust_result: dict = {}
    ws_result: dict = {}

    log.info(f'Opening balance lookup: recon={date_str}, prev_working_day={prev_working_day}')

    # ── Tier 1: Parse prior day's raw bank files + WS Bank Book ──
    if prev_working_day:
        prev_date = prev_working_day
        prev_variants = _date_variants(prev_date)

        def _file_date_ok(filepath):
            """True if filename contains a date variant for prev_date."""
            name = Path(filepath).name
            return any(v in name for v in prev_variants)

        # ICICI bank — ICICI filenames use account-date format (e.g. 000405165525-020426)
        try:
            for f in file_manager.get_bank_files(prev_date, 'icici_bank'):
                if not _file_date_ok(f):
                    log.debug(f'  ICICI skip (wrong date): {Path(f).name}')
                    continue
                r = ICICIBankParser().parse_file(f)
                if r.ok:
                    for a in r.accounts:
                        if a.account_no and a.closing_balance:
                            cust_result[a.account_no] = a.closing_balance
        except Exception as e:
            log.debug(f'Prior-day ICICI parse failed: {e}')

        # HDFC bank balance
        try:
            for f in file_manager.get_bank_files(prev_date, 'hdfc_bank_balance'):
                hp = HdfcBankParser()
                r = hp.parse_balance_zip(f, password=hdfc_zip_password) if f.endswith('.zip') \
                    else hp.parse_balance_file(f)
                if r.ok:
                    for a in r.accounts:
                        if a.account_no:
                            cust_result[a.account_no] = a.closing_balance
        except Exception as e:
            log.debug(f'Prior-day HDFC balance parse failed: {e}')

        # Axis bank — validate filename date, keep latest per strategy
        try:
            axis_best: dict = {}
            for bal_zip, _txn in file_manager.get_axis_bank_zip_pairs(prev_date):
                if not bal_zip:
                    continue
                if not _file_date_ok(bal_zip):
                    log.debug(f'  Axis skip (wrong date): {Path(bal_zip).name}')
                    continue
                m = re.match(r'^([A-Z0-9]+)', Path(bal_zip).name.upper())
                pfx = m.group(1) if m else Path(bal_zip).stem
                existing = axis_best.get(pfx)
                if existing is None or Path(bal_zip).name > Path(existing).name:
                    axis_best[pfx] = bal_zip
            if axis_best:
                log.info(f'Axis prior-day: {len(axis_best)} strategy(ies) with correct date for {prev_date}')
            axis_parser = AxisBankParser()
            for pfx, bal_zip in axis_best.items():
                r = axis_parser.parse_zip_pair(bal_zip, '', password=hdfc_zip_password)
                if r.ok:
                    for a in r.accounts:
                        if a.account_no:
                            cust_result[a.account_no] = a.closing_balance
                            log.info(f'  Axis [{pfx}] acct {a.account_no}: '
                                     f'closing={a.closing_balance} from {Path(bal_zip).name}')
                else:
                    log.warning(f'  Axis [{pfx}] parse failed: {r.error}')
        except Exception as e:
            log.debug(f'Prior-day Axis balance parse failed: {e}')

        # Kotak bank — Kotak filenames have no date, skip validation
        try:
            for f in file_manager.get_bank_files(prev_date, 'kotak_bank'):
                r = KotakBankParser().parse_file(f) if not f.endswith('.zip') \
                    else KotakBankParser().parse_zip(f, password=hdfc_zip_password)
                if r.ok:
                    for a in r.accounts:
                        if a.account_no:
                            cust_result[a.account_no] = a.closing_balance
        except Exception as e:
            log.debug(f'Prior-day Kotak parse failed: {e}')

        # WS Bank Book — use scheme-level Total row closing balance per MAPID
        try:
            bb_path = file_manager.get_ws_bank_book(prev_date)
            if bb_path:
                ws_book = WSBankBookParser().parse_file(bb_path)
                # Prefer scheme summaries (from Total rows)
                if ws_book.scheme_summaries:
                    # Resolve scheme_name → MAPID using pools_hub
                    name_to_mid: dict = {}
                    try:
                        from core.pools_hub import PoolsHub
                        for wsn, mid in PoolsHub.load().ws_scheme_names_index().items():
                            name_to_mid[wsn] = mid
                    except Exception:
                        pass
                    for ss in ws_book.scheme_summaries:
                        mid = name_to_mid.get(ss.scheme_name.lower().strip(), '')
                        if not mid:
                            mid = ss.scheme_code   # fallback to raw code
                        if mid:
                            ws_result[mid] = ss.closing_balance
                else:
                    # Fallback: sum individual accounts
                    ws_by_mapid: dict = {}
                    for a in ws_book.accounts:
                        mid = getattr(a, 'scheme_code', '') or ''
                        if mid:
                            ws_by_mapid[mid] = ws_by_mapid.get(mid, 0) + a.closing_balance
                    for mid, total in ws_by_mapid.items():
                        ws_result[mid] = round(total, 2)
        except Exception as e:
            log.debug(f'Prior-day WS Bank Book parse failed: {e}')

        if cust_result or ws_result:
            log.info(f'Bank opening balances: {len(cust_result)} custodian account(s), '
                     f'{len(ws_result)} WS pool(s) from prior-day files ({prev_date})')

    # ── Tier 2: Fallback to cumulative history ──
    # Excluded accounts (firm-wide custody accounts the operator marked as
    # 'never recon this') are pulled from PoolsHub and dropped before the
    # tier 2 lookup. This keeps ghost accounts (e.g. a pool's old bank
    # account number after it was migrated to a new one) from leaking
    # stale prior-day closings into today's recon. The set is keyed by
    # account number alone — bank-prefixing is unnecessary because account
    # numbers are unique within and across banks.
    excluded = _load_excluded_accounts()
    hist_path = str(data_dir / 'data' / 'bank_balance_history.json')
    if os.path.exists(hist_path):
        try:
            with open(hist_path) as f:
                hist = json.load(f)
            best: dict = {}   # account_no → (date_str, cust_closing)
            for entry in hist:
                acct = entry.get('cust_account', '')
                edate = entry.get('date', '')
                closing = entry.get('cust_closing', 0)
                if not acct or not edate or edate >= date_str:
                    continue
                if acct in excluded:
                    continue
                existing = best.get(acct)
                if existing is None or edate > existing[0]:
                    best[acct] = (edate, closing)
            fallback_count = 0
            for acct, (_, closing) in best.items():
                if acct not in cust_result:
                    cust_result[acct] = closing
                    fallback_count += 1
            if fallback_count:
                log.info(f'Bank opening balances: {fallback_count} account(s) '
                         f'from recon history (fallback)')
        except Exception as e:
            log.warning(f'Bank balance history load failed: {e}')

    # Excluded accounts must never carry a prior-day closing into recon,
    # even if today's parser produced one (e.g. the bank file still lists
    # a firm-wide custody account that the operator wants out of recon).
    for acct in list(cust_result.keys()):
        if acct in excluded:
            cust_result.pop(acct, None)

    if not cust_result:
        log.warning(f'Bank opening balances: no prior data found for {date_str} '
                    f'(prev working day: {prev_working_day or "unknown"})')

    return {'cust': cust_result, 'ws': ws_result}


def append(
    date_str: str,
    summary,
    *,
    data_dir: Path,
    logger=None,
) -> None:
    """Persist today's per-pool closing balances to
    ``data_dir/data/bank_balance_history.json``.

    Despite the name (kept for backwards compatibility), this is now a
    REPLACE-OR-INSERT per ``(date, cust_account, pool)`` rather than a
    blind append. Re-running bank recon during a day no longer stacks
    duplicate rows on top of each other; the latest run wins and the
    file size stays bounded. Excluded firm-wide accounts (managed via
    PoolsHub.excluded_bank_accounts) are skipped entirely so stale
    custody-aggregator balances can't poison the next day's recon.

    Args:
        date_str: Recon date (YYYY-MM-DD).
        summary: BankReconSummary-like object (must expose ``.to_dict()``
            returning ``{'pool_results': [{'cust_account', 'cust_closing',
            'ws_closing_sum', 'l1_variance', 'overall_status',
            'strategy_name', 'bank'}, ...]}``).
        data_dir: Project DATA_DIR.
        logger: Optional logger override; defaults to module logger.
    """
    log = logger or _logger
    hist_path = str(data_dir / 'data' / 'bank_balance_history.json')
    excluded = _load_excluded_accounts()
    try:
        hist = []
        if os.path.exists(hist_path):
            with open(hist_path) as f:
                hist = json.load(f)
        # Build today's new rows, skipping excluded accounts and the
        # synthetic NOT IN WS entries that have no real account number.
        s = summary.to_dict()
        new_rows: list = []
        for pool in s.get('pool_results', []):
            acct = pool.get('cust_account', '')
            if not acct:
                continue
            if acct in excluded:
                log.info(f'bank_balance_history: skipping excluded account {acct} '
                         f'({pool.get("bank","")} / {pool.get("strategy_name","")})')
                continue
            new_rows.append({
                'date':         date_str,
                'pool':         pool.get('strategy_name', ''),
                'bank':         pool.get('bank', ''),
                'cust_account': acct,
                'cust_closing': pool.get('cust_closing', 0),
                'ws_closing':   pool.get('ws_closing_sum', 0),
                'variance':     pool.get('l1_variance', 0),
                'status':       pool.get('overall_status', ''),
            })

        # Drop existing rows for the same (date, cust_account, pool) keys
        # — these are stale duplicates from earlier runs of today's recon
        # and would otherwise stack up with every re-run.
        replace_keys = {(r['date'], r['cust_account'], r['pool']) for r in new_rows}
        kept = [
            e for e in hist
            if (e.get('date'), e.get('cust_account'), e.get('pool')) not in replace_keys
        ]
        kept.extend(new_rows)

        # Rewrite atomically — write to a sibling tempfile then rename so a
        # crash mid-write can't corrupt the JSON file.
        tmp = hist_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(kept, f)
        os.replace(tmp, hist_path)
        log.debug(f'bank_balance_history: wrote {len(kept)} rows '
                  f'({len(new_rows)} from today, {len(kept)-len(new_rows)} prior).')
    except Exception as e:
        log.warning(f'Bank balance history save failed: {e}')


# ── Helpers shared by load() / append() / cleanup ────────────────────────── #

def _load_excluded_accounts() -> set:
    """Return the set of bank-account numbers the operator has marked as
    'firm-wide / never recon'. Empty set when PoolsHub doesn't expose the
    accessor (older config / first run before the field is populated).
    Failures here are non-fatal — a bad config file shouldn't break the
    whole bank-recon flow."""
    try:
        from core.pools_hub import PoolsHub
        hub = PoolsHub.load()
        excl = getattr(hub, 'excluded_bank_accounts', None)
        if callable(excl):
            return {a.strip() for a in (excl() or []) if a and str(a).strip()}
    except Exception as e:
        _logger.debug(f'_load_excluded_accounts: {e}')
    return set()


def dedupe_history(
    *,
    data_dir: Path,
    drop_excluded: bool = True,
    logger=None,
) -> dict:
    """One-shot cleanup of ``bank_balance_history.json``.

    Two passes:
      1. Collapse duplicates — for each ``(date, cust_account, pool)``
         key, keep one row (the LAST occurrence in the file, which is
         what the historical append-only flow would have produced).
      2. (Optional) Drop rows for accounts in the excluded set so old
         entries for migrated / firm-wide accounts don't sit forever
         in the history.

    Returns ``{'before': N, 'after': M, 'dropped_excluded': K,
    'dropped_duplicates': D, 'excluded': [...]}`` so the caller can
    surface a summary on the UI.

    Safe to run multiple times — idempotent after the first pass.
    """
    log = logger or _logger
    hist_path = str(data_dir / 'data' / 'bank_balance_history.json')
    if not os.path.exists(hist_path):
        return {'before': 0, 'after': 0, 'dropped_excluded': 0,
                'dropped_duplicates': 0, 'excluded': []}
    with open(hist_path) as f:
        hist = json.load(f)
    before = len(hist)

    excluded = _load_excluded_accounts() if drop_excluded else set()

    # 1. Collapse duplicates — rebuild map keyed by (date, acct, pool).
    by_key: dict = {}
    for entry in hist:
        k = (entry.get('date', ''),
             entry.get('cust_account', ''),
             entry.get('pool', ''))
        # Keep the LAST occurrence (latest write wins). Earlier duplicates
        # for the same key are silently overwritten.
        by_key[k] = entry

    after_dedupe = list(by_key.values())
    duplicates_dropped = before - len(after_dedupe)

    # 2. Drop excluded accounts.
    if excluded:
        after_dedupe = [e for e in after_dedupe
                        if e.get('cust_account') not in excluded]
    excluded_dropped = (before - duplicates_dropped) - len(after_dedupe)

    # Re-sort chronologically so the file stays readable.
    after_dedupe.sort(key=lambda e: (e.get('date', ''),
                                      e.get('bank', ''),
                                      e.get('cust_account', '')))

    # Atomic rewrite.
    tmp = hist_path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(after_dedupe, f)
    os.replace(tmp, hist_path)

    log.info(f'bank_balance_history.dedupe_history: {before} → '
             f'{len(after_dedupe)} rows '
             f'(duplicates={duplicates_dropped}, excluded={excluded_dropped}).')

    return {
        'before':              before,
        'after':               len(after_dedupe),
        'dropped_duplicates':  duplicates_dropped,
        'dropped_excluded':    excluded_dropped,
        'excluded':            sorted(excluded),
    }
