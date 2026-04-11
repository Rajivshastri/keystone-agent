"""
Trade reconciliation workflow.

Extracted from app.py api_trade_recon_run() to keep the Flask route handler thin.
Contains WS OrderLog loading, security master loading (with Z8 cache), dealer/CN/
NSDL/exchange file loading, engine execution, and report file writing.
Returns a result dataclass; caller handles Flask state, email, and JSON response.
"""

import logging
import os
import re as _re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Z8 Security Master cache ──────────────────────────────────────────────── #
# Loading Z8_SecurityDetail.xlsx (7 MB, ~150 MB in memory) on every recon run
# OOM-kills the gunicorn worker on B1. Cache keyed by (path, mtime) so it is
# parsed once and reused until the file is replaced with a newer version.
_Z8_CACHE: dict = {}   # key: (str(path), mtime_float) → (sec_name_to_isin, isin_to_nse_ticker)


class TradeReconResult:
    """Result returned by run_trade_recon()."""
    __slots__ = ('summary', 'recon_path', 'xlsx_0096', 'ws_orders_count', 'cns_count')

    def __init__(self, summary, recon_path: str, xlsx_0096: str,
                 ws_orders_count: int, cns_count: int):
        self.summary          = summary
        self.recon_path       = recon_path
        self.xlsx_0096        = xlsx_0096
        self.ws_orders_count  = ws_orders_count
        self.cns_count        = cns_count


# ── Helpers (moved from app.py) ───────────────────────────────────────────── #

def _load_ws_trade_trans(path: str):
    """
    Load WS orders for trade reconciliation.

    Accepts both formats:
      - Z13_OrderLog.xlsx  (new, preferred) — detected by ORDERID + BROKERACID columns
      - Z13_TradeTrans.xlsx (legacy)         — detected by absence of ORDERID column

    Returns (orders_list, isin_to_instr_dict).
    """
    import openpyxl as _openpyxl
    from parsers.order_log import OrderLogParser, detect_order_log_format

    _ext = Path(path).suffix.lower()
    _peek_row = None
    try:
        if _ext == '.xls':
            import xlrd as _xlrd
            _xwb = _xlrd.open_workbook(path)
            _xws = _xwb.sheet_by_index(0)
            _peek_row = tuple(_xws.cell_value(0, c) for c in range(_xws.ncols)) if _xws.nrows > 0 else None
        else:
            _wb = _openpyxl.load_workbook(path, read_only=True, data_only=True)
            _ws = _wb.active
            _peek_row = next(_ws.iter_rows(max_row=1, values_only=True), None)
            _wb.close()
    except Exception as _e:
        logger.error(f"_load_ws_trade_trans peek error: {_e}", exc_info=True)
        return [], {}

    if _peek_row and detect_order_log_format(list(_peek_row)):
        result = OrderLogParser().parse_file(path)
        if not result.ok:
            logger.error(f"OrderLog parse error: {result.error}")
            return [], {}
        for w in result.warnings:
            logger.warning(f"OrderLog warning: {w}")
        return result.orders, result.isin_to_instr

    # Legacy format: Z13_TradeTrans
    orders = []
    isin_to_instr = {}
    try:
        _ext2 = str(path).lower().rsplit(".", 1)[-1]
        if _ext2 == "xls":
            import xlrd as _xlrd2
            _wb2 = _xlrd2.open_workbook(path)
            _ws2 = _wb2.sheet_by_index(0)
            rows = [tuple(_ws2.cell_value(rx, cx) for cx in range(_ws2.ncols)) for rx in range(_ws2.nrows)]
        else:
            import openpyxl as _openpyxl2
            wb = _openpyxl2.load_workbook(path, read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            rows = list(ws.iter_rows(values_only=True))
            wb.close()
        if not rows:
            return orders, isin_to_instr
        headers = [str(h or '').strip().upper() for h in rows[0]]
        for row in rows[1:]:
            if not row or row[0] is None:
                continue
            order = {headers[i]: row[i] for i in range(min(len(headers), len(row)))}
            orders.append(order)
            isin       = str(order.get('ISINCODE', '') or '').strip()
            instr_code = str(order.get('INSTRUMENT_CODE', '') or '').strip()
            if isin and instr_code and isin not in isin_to_instr:
                isin_to_instr[isin] = instr_code
    except Exception as e:
        logger.error(f"_load_ws_trade_trans (legacy) error: {e}", exc_info=True)
    return orders, isin_to_instr


def _find_broker_for_pdf(filename: str, broker_lookup: dict) -> Optional[dict]:
    """Attempt to identify broker from PDF filename."""
    fn_up = filename.upper()
    for code, broker in broker_lookup.items():
        if code in fn_up:
            return broker
        sebi = broker.get('sebi_code', '').upper()
        if sebi and sebi in fn_up:
            return broker
        name_words = broker.get('name', '').upper().split()
        if name_words and name_words[0] in fn_up:
            return broker
    return None


def _build_cbd_client_map(cbd_path: Optional[str], ws_orders: list,
                          pool_map_raw: dict) -> dict:
    """
    Build client_code → mapin mapping.
    Primary: CBD file (CLIENTCODE → MAPINID).
    Fallback: pattern-match from pool_map_raw.

    pool_map_raw is the raw pool_map.json dict (not the processed pool_map_dict).
    """
    import openpyxl as _openpyxl3
    client_map = {}

    if cbd_path:
        try:
            _ext3 = str(cbd_path).lower().rsplit(".", 1)[-1]
            if _ext3 == "xls":
                import xlrd as _xlrd3
                _wb3 = _xlrd3.open_workbook(cbd_path)
                _ws3 = _wb3.sheet_by_index(0)
                rows = [tuple(_ws3.cell_value(rx, cx) for cx in range(_ws3.ncols)) for rx in range(_ws3.nrows)]
            else:
                wb = _openpyxl3.load_workbook(cbd_path, read_only=True, data_only=True)
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                wb.close()
            if rows:
                headers = [str(h or '').strip().upper() for h in rows[0]]
                col = {h: i for i, h in enumerate(headers)}
                for row in rows[1:]:
                    if not row:
                        continue
                    def s(c, _row=row, _col=col):
                        i = _col.get(c)
                        return str(_row[i] or '').strip() if i is not None and i < len(_row) else ''
                    client_code = s('CLIENTCODE') or s('CLIENT_CODE')
                    mapid       = s('MAPINID') or s('MAPIN_ID') or s('MAPID')
                    if client_code and mapid:
                        client_map[client_code.upper()] = mapid
        except Exception as e:
            logger.warning(f"CBD load error for trade recon: {e}")

    # Fallback: prefix matching from pool_map_raw
    for order in ws_orders:
        cc = str(order.get('CLIENT_CODE', '') or '').strip().upper()
        if not cc or cc in client_map:
            continue
        for scheme_code in [p['mapin'] for p in pool_map_raw.get('pools', []) if p.get('dealer_account')]:
            sc_up = scheme_code.upper().replace('MYSTICV', 'MYSV').replace('MYSTICM', 'MYSM')
            if cc.startswith(sc_up[:4]):
                client_map[cc] = scheme_code
                break
        if cc not in client_map:
            client_map[cc] = cc

    return client_map


def _norm_name(s: str) -> str:
    """Normalize company name: remove punctuation, standardize Ltd/Limited."""
    n = str(s).upper().strip()
    n = _re.sub(r"'", '', n)
    n = _re.sub(r'&', 'AND', n)
    n = _re.sub(r'\bLIMITED\b', 'LTD', n)
    n = _re.sub(r'\bLTD\.?\b', 'LTD', n)
    n = _re.sub(r'\bPRIVATE\b', 'PVT', n)
    n = _re.sub(r'\bPVT\.?\b', 'PVT', n)
    n = _re.sub(r'\bCOMPANY\b', 'CO', n)
    n = _re.sub(r'\bCO\.?\b', 'CO', n)
    n = _re.sub(r'[,\.\-]', ' ', n)
    n = _re.sub(r'\s+', ' ', n).strip()
    return n


# ── Main workflow ─────────────────────────────────────────────────────────── #

def run_trade_recon(date_str: str, fm, broker_map: dict, pool_map_dict: dict,
                    pool_map_raw: dict, out_dir: str, log_fn) -> TradeReconResult:
    """
    Orchestrate trade reconciliation: load all files, run engine, write reports.

    Args:
        date_str:      Report date in YYYY-MM-DD format.
        fm:            FileManager instance.
        broker_map:    Parsed broker_map.json dict.
        pool_map_dict: Processed pool map dict (from PoolsHub.pool_map_dict()).
        pool_map_raw:  Raw pool_map.json dict (for CBD prefix matching).
        out_dir:       Output directory path string.
        log_fn:        Callable(msg, level='info') for logging.

    Returns:
        TradeReconResult with summary, file paths, and counts.

    Raises:
        ValueError: If required WS OrderLog file is not found.
        Exception:  Propagated from engine for unexpected failures.
    """
    from parsers.dealer        import DealerParser
    from parsers.broker_pdf    import BrokerPDFParser, ContractNote as _CN
    from parsers.nsdl_steady   import NSDLParser
    from parsers.exchange_file import ExchangeFileParser
    from core.trade_recon_engine import TradeReconEngine, write_trade_recon_report, write_0096_excel
    from core.client_bank_details import ClientBankDetails

    engine = TradeReconEngine(broker_map, pool_map_dict)

    # ── Load WS TradeTrans / OrderLog ─────────────────────────────────────── #
    ws_tt_path = fm.get_ws_trade_trans(date_str)
    if not ws_tt_path:
        raise ValueError(
            'Z13_OrderLog not found for this date. '
            'Upload Z13_OrderLog.xlsx via the file panel first.'
        )

    ws_orders, isin_to_instr = _load_ws_trade_trans(ws_tt_path)

    # Filter out INF-prefixed ISINs (mutual fund units, not equity trades)
    _before = len(ws_orders)
    ws_orders = [o for o in ws_orders
                 if not (
                     str(o.get('ISINCODE', '') or '').upper().startswith('INF')
                     and 'ETF' not in str(o.get('INSTRUMENT_NAME', '') or '').upper()
                 )]
    if len(ws_orders) < _before:
        log_fn(f"WS OrderLog: filtered {_before - len(ws_orders)} non-ETF INF ISIN order(s) "
               f"(mutual funds); {len(ws_orders)} order(s) remain")
    log_fn(f"WS OrderLog: {len(ws_orders)} authorized orders, {len(isin_to_instr)} ISIN mappings")

    # Warn if the OrderLog contains orders from a different date
    if ws_orders:
        _recon_dd_mm = datetime.strptime(date_str, '%Y-%m-%d').strftime('%d/%m/%Y')
        order_dates = set(str(o.get('ORDER_DATE', '') or '').strip() for o in ws_orders
                          if o.get('ORDER_DATE'))
        wrong_date = [d for d in order_dates if d and d != _recon_dd_mm]
        if wrong_date:
            log_fn(
                f"⚠ OrderLog date mismatch: orders are dated {wrong_date} but recon is "
                f"for {_recon_dd_mm}. Upload the correct Z13_OrderLog for today via the "
                f"file panel.", 'warning'
            )

    # ── Load Security Master (Z8_SecurityDetail) ──────────────────────────── #
    sec_master_path = fm.get_security_master(date_str)
    sec_name_to_isin   = {}
    isin_to_nse_ticker = {}

    if sec_master_path:
        try:
            import csv as _csv
            _cache_key = (str(sec_master_path), os.path.getmtime(sec_master_path))
            if _cache_key in _Z8_CACHE:
                sec_name_to_isin, isin_to_nse_ticker = _Z8_CACHE[_cache_key]
                log_fn(f"Security master: loaded from cache "
                       f"({len(sec_name_to_isin)} name/ticker mappings, "
                       f"{len(isin_to_nse_ticker)} NSE tickers)")
            else:
                _added = 0
                _skipped_inactive = 0
                _equity_keys: set = set()
                _sni: dict = {}
                _itn: dict = {}

                def _put(key, isin, is_eq):
                    if key in _equity_keys:
                        return
                    _sni[key] = isin
                    if is_eq:
                        _equity_keys.add(key)

                def _process_row(row_dict):
                    nonlocal _added, _skipped_inactive
                    _isin     = str(row_dict.get('ISINCODE')    or '').strip()
                    _name     = str(row_dict.get('SYMBOLNAME')  or '').strip()
                    _nse_tick = str(row_dict.get('NSEMAPPING')  or '').strip()
                    _bse_tick = str(row_dict.get('BSEMAPPING')  or '').strip()
                    _active   = str(row_dict.get('ACTIVEIND')   or 'Y').strip()
                    _assetcls = str(row_dict.get('ASTCLSNAME')  or '').strip()
                    _is_eq    = (_assetcls == 'Equity')
                    if not _isin:
                        return
                    if _active != 'Y':
                        _skipped_inactive += 1
                        return
                    if _name:
                        _put(_norm_name(_name), _isin, _is_eq)
                    if _nse_tick:
                        _put(_nse_tick.upper(), _isin, _is_eq)
                        if _is_eq:
                            isin_to_instr.setdefault(_isin, _nse_tick)
                            if _isin not in _itn:
                                _itn[_isin] = _nse_tick
                    if _bse_tick:
                        _put(_bse_tick.upper(), _isin, _is_eq)
                    _added += 1

                if str(sec_master_path).lower().endswith('.csv'):
                    with open(sec_master_path, newline='', encoding='utf-8-sig') as _f:
                        for _row in _csv.DictReader(_f):
                            _process_row({k.upper().strip(): v for k, v in _row.items()})
                else:
                    import openpyxl as _openpyxl_z8
                    _wb_z8 = _openpyxl_z8.load_workbook(sec_master_path, read_only=True, data_only=True)
                    try:
                        _ws_z8 = _wb_z8[_wb_z8.sheetnames[0]]
                        _headers_z8 = None
                        for _row_z8 in _ws_z8.iter_rows(values_only=True):
                            if _headers_z8 is None:
                                _headers_z8 = [str(c or '').upper().strip() for c in _row_z8]
                                continue
                            _process_row({_headers_z8[i]: str(v or '') for i, v in enumerate(_row_z8) if i < len(_headers_z8)})
                    finally:
                        _wb_z8.close()

                sec_name_to_isin   = _sni
                isin_to_nse_ticker = _itn
                _Z8_CACHE[_cache_key] = (sec_name_to_isin, isin_to_nse_ticker)
                log_fn(f"Security master: {_added} active securities loaded, "
                       f"{_skipped_inactive} inactive skipped, "
                       f"{len(sec_name_to_isin)} total name/ticker mappings, "
                       f"{len(isin_to_nse_ticker)} NSE tickers")
        except Exception as _e:
            log_fn(f"Security master load warning: {_e}", 'warning')
    else:
        log_fn("Security master (Z8_SecurityDetail) not found — upload it to improve Exchange matching", 'warning')

    # ── CBD client→Mapin mapping ──────────────────────────────────────────── #
    cbd_path = fm.get_client_bank_details(date_str)
    cbd_client_map = _build_cbd_client_map(cbd_path, ws_orders, pool_map_raw)
    log_fn(f"CBD mapping: {len(cbd_client_map)} client codes mapped")

    # ── Load dealer file ──────────────────────────────────────────────────── #
    dealer_path   = fm.get_dealer_file(date_str)
    dealer_trades = []
    if dealer_path:
        dp_result = DealerParser().parse_file(dealer_path)
        if dp_result.ok:
            try:
                _recon_fmt = datetime.strptime(date_str, '%Y-%m-%d').strftime('%d/%m/%Y')
            except ValueError:
                _recon_fmt = date_str
            today = [t for t in dp_result.trades if not t.trade_date or t.trade_date == _recon_fmt]
            other = [t for t in dp_result.trades if t.trade_date and t.trade_date != _recon_fmt]
            dealer_trades = today if today else dp_result.trades
            log_fn(f"Dealer: {len(dealer_trades)} trade(s) from {Path(dealer_path).name}")
            if other:
                log_fn(f"Dealer: excluded {len(other)} trade(s) from other dates "
                       f"{sorted(set(t.trade_date for t in other))}", 'warning')
        else:
            log_fn(f"Dealer parse error: {dp_result.error}", 'warning')
    else:
        if len(ws_orders) == 0:
            log_fn('No dealer file and no WS equity orders — confirmed no trades today')
        else:
            log_fn(f'No dealer file but WS has {len(ws_orders)} equity order(s) — '
                   f'dealer file may be missing. C1 will show DEALER_MISSING.', 'warning')

    # ── Load broker contract note PDFs ────────────────────────────────────── #
    cn_files = fm.list_broker_cn_files(date_str)
    log_fn(f"CN files in broker_cn: {[os.path.basename(f) for f in cn_files]}")

    NON_CN_KEYWORDS = [
        'nomination', 'kyc', 'account opening', 'onboarding',
        'circular', 'advisory', 'newsletter', 'mandate', 'agreement',
        'demat', 'form_', '_form', 'account statement', 'bank statement',
        'holding report', 'settlement report', 'annexure_report',
    ]

    def _is_likely_cn(path: str) -> bool:
        fname = os.path.basename(path).lower()
        return not any(kw in fname for kw in NON_CN_KEYWORDS)

    cn_files_filtered = [f for f in cn_files if _is_likely_cn(f)]
    if len(cn_files_filtered) < len(cn_files):
        skipped = [os.path.basename(f) for f in cn_files if not _is_likely_cn(f)]
        log_fn(f'Skipping {len(skipped)} non-CN file(s): {", ".join(skipped)}', 'warning')
    cn_files = cn_files_filtered

    contract_notes = []
    pdf_parser = BrokerPDFParser()
    broker_lookup = {b['dealer_code'].upper(): b for b in broker_map.get('brokers', [])}
    _ticker_to_isin = {v.upper(): k for k, v in isin_to_nse_ticker.items() if v}

    for pdf_path in cn_files:
        fname = os.path.basename(pdf_path)
        broker_info = _find_broker_for_pdf(fname, broker_lookup)
        sebi   = broker_info.get('sebi_code', '') if broker_info else ''
        b_name = broker_info.get('name', '') if broker_info else ''
        result = pdf_parser.parse_file(pdf_path, sebi, b_name,
                                       ticker_to_isin=_ticker_to_isin)
        if result.ok and result.contract_notes:
            contract_notes.extend(result.contract_notes)
            log_fn(f"CN parsed: {fname} → {len(result.contract_notes)} CN(s)")
            for _cn in result.contract_notes:
                log_fn(f"  cn_no={_cn.cn_no!r} ucc={_cn.ucc!r} trades={[(t.isin,t.qty,t.side,t.stt_total) for t in _cn.trades]}")
        else:
            for w in result.warnings:
                log_fn(f"CN warning ({fname}): {w}", 'warning')
            if result.error:
                log_fn(f"CN error ({fname}): {result.error}", 'error')

    # ── CN Deduplication ──────────────────────────────────────────────────── #
    _broker_norm: dict = {}
    for _b in broker_map.get('brokers', []):
        _dc = (_b.get('dealer_code') or '').upper()
        if not _dc:
            continue
        for _id in [_b.get('dealer_code', ''), _b.get('sebi_code', ''),
                    _b.get('sebi_reg_no', ''), _b.get('name', '')]:
            if _id and str(_id).strip():
                _broker_norm[str(_id).strip().upper()] = _dc

    # Cross-CN dedup removed — PDF-only workflow means each CN is a distinct
    # trade. The ECN dedup in the recon engine (_dedup_ecn_duplicates) handles
    # the only remaining scenario: same CN number from multiple file sources.
    log_fn(f"Broker CNs: {len(contract_notes)} contract notes from {len(cn_files)} PDFs")

    # ── Load NSDL ─────────────────────────────────────────────────────────── #
    nsdl_path    = fm.get_nsdl_file(date_str)
    nsdl_records = []
    if nsdl_path:
        nr = NSDLParser().parse_file(nsdl_path)
        if nr.ok:
            nsdl_records = nr.records
            log_fn(f"NSDL: {len(nsdl_records)} records")
        else:
            log_fn(f"NSDL parse error: {nr.error}", 'warning')
    else:
        log_fn('NSDL file not uploaded — Check 3 skipped', 'warning')

    # Exchange files (C5) removed — NSDL matching (C3) is sufficient for
    # trade confirmation. Exchange files have thousands of individual fills
    # that cause timeouts without adding value beyond what C3 provides.
    exchange_trades = []

    # ── Build isin_to_name (for C3 NSDL name matching) ───────────────────── #
    isin_to_name: dict = {}
    for _o in ws_orders:
        _isin = str(_o.get('ISINCODE', '') or '').strip()
        _name = str(_o.get('SYMBOL_NAME', '') or _o.get('INSTRUMENT_NAME', '') or '').strip()
        if _isin and _name and _isin not in isin_to_name:
            isin_to_name[_isin] = _name
    for _name_key, _isin in sec_name_to_isin.items():
        if _isin and _isin not in isin_to_name and len(_name_key) > 6:
            isin_to_name[_isin] = _name_key
    log_fn(f"ISIN→name map: {len(isin_to_name)} entries (for C3 NSDL name matching)")

    # ── Run engine ────────────────────────────────────────────────────────── #
    log_fn('Running trade reconciliation engine…')
    summary = engine.run(
        date_str          = date_str,
        ws_orders         = ws_orders,
        dealer_trades     = dealer_trades,
        contract_notes    = contract_notes,
        nsdl_records      = nsdl_records,
        exchange_trades   = exchange_trades,
        cbd_client_map    = cbd_client_map,
        isin_to_instr     = isin_to_instr,
        sec_name_to_isin  = sec_name_to_isin,
        isin_to_name      = isin_to_name,
        isin_to_nse_ticker = isin_to_nse_ticker,
    )

    # ── Write output files ────────────────────────────────────────────────── #
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    ts       = datetime.now().strftime('%H%M%S')
    date_fmt = date_str.replace('-', '')

    recon_path = os.path.join(out_dir, f'TradeRecon_{date_fmt}_{ts}.xlsx')
    xlsx_0096  = os.path.join(out_dir, f'0096_{date_fmt}_{ts}.xls')

    write_trade_recon_report(summary, recon_path)
    write_0096_excel(summary.output_0096, xlsx_0096, date_str)

    log_fn(f"Trade recon complete — C1:{summary.c1_breaks} C2:{summary.c2_breaks} "
           f"C3:{summary.c3_breaks} breaks | 0096: {len(summary.output_0096)} rows")

    return TradeReconResult(
        summary         = summary,
        recon_path      = recon_path,
        xlsx_0096       = xlsx_0096,
        ws_orders_count = len(ws_orders),
        cns_count       = len(contract_notes),
    )
