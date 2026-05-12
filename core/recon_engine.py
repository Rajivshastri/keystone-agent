"""
Recon Engine
------------
Reconciles custodian holdings against WS system holdings,
accounting for pending transactions.

Inputs:
  - Parsed custodian HoldingRecord list (logical + saleable per client per ISIN)
  - Z13_Holding export from WS (WS system quantity per client per ISIN)
  - Z13_TradeTrans export from WS (pending buys/sells not yet settled)
  - Z13_PoolMaster (strategy names for display)

Output:
  - Excel recon report with 6 sheets:
      Summary         — counts and totals by strategy
      Clean           — perfect matches
      Pending Explained — logical ≠ WS qty but transactions explain it
      Unexplained     — real breaks requiring investigation
      Custody Only    — in custodian but not WS
      WS Only         — in WS but not custodian
"""
import logging
import os
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from collections import defaultdict

import openpyxl
from openpyxl.styles import (Font, PatternFill, Alignment,
                              Border, Side, GradientFill)
from openpyxl.utils import get_column_letter

from parsers.base import HoldingRecord

logger = logging.getLogger(__name__)


def _open_wb(path: str):
    """
    Open an Excel file with the correct engine.
    .xls  → xlrd (openpyxl rejects the old binary format)
    .xlsx → openpyxl (xlrd rejects xlsx since xlrd 2.0)
    Returns a lightweight wrapper with .sheetnames and iter_rows support.
    """
    ext = str(path).lower().rsplit('.', 1)[-1]
    if ext == 'xls':
        import xlrd as _xlrd
        import datetime as _dt

        class _XlrdSheet:
            def __init__(self, sheet, datemode):
                self._s = sheet
                self._dm = datemode
            @property
            def sheetnames(self):
                return [self._s.name]
            def iter_rows(self, min_row=1, max_row=None, values_only=True):
                nrows = self._s.nrows
                end = nrows if max_row is None else min(max_row, nrows)
                for rx in range(min_row - 1, end):
                    row = []
                    for cx in range(self._s.ncols):
                        cell = self._s.cell(rx, cx)
                        if cell.ctype == _xlrd.XL_CELL_DATE:
                            try:
                                tpl = _xlrd.xldate_as_tuple(cell.value, self._dm)
                                val = _dt.datetime(*tpl) if tpl[3:] != (0,0,0) else _dt.datetime(*tpl[:3])
                            except Exception:
                                val = cell.value
                        elif cell.ctype == _xlrd.XL_CELL_EMPTY:
                            val = None
                        else:
                            val = cell.value
                        row.append(val)
                    yield tuple(row)
            def close(self):
                pass

        class _XlrdWb:
            def __init__(self, wb):
                self._wb = wb
                self._sheet = wb.sheet_by_index(0)
            @property
            def sheetnames(self):
                return [s.name for s in [self._wb.sheet_by_index(i) for i in range(self._wb.nsheets)]]
            def __getitem__(self, name):
                if isinstance(name, int):
                    s = self._wb.sheet_by_index(name)
                else:
                    s = self._wb.sheet_by_name(name)
                return _XlrdSheet(s, self._wb.datemode)
            def close(self):
                pass

        wb = _xlrd.open_workbook(path)
        return _XlrdWb(wb)
    else:
        import openpyxl as _openpyxl
        return _openpyxl.load_workbook(path, read_only=True, data_only=True)

# ── Colour palette ────────────────────────────────────────────────────────── #
C_NAVY      = '1B2A4A'
C_GOLD      = 'C9A84C'
C_WHITE     = 'FFFFFF'
C_GREEN_BG  = 'EAF7F0'
C_GREEN_FG  = '1A7A4A'
C_AMBER_BG  = 'FDF5EB'
C_AMBER_FG  = 'B06820'
C_RED_BG    = 'FDF1F1'
C_RED_FG    = 'B03030'
C_BLUE_BG   = 'EEF4FF'
C_BLUE_FG   = '2A4FA8'
C_ORANGE_BG = 'FFF3E0'
C_ORANGE_FG = 'E65100'
C_ALT       = 'F5F7FA'
C_BORDER    = 'D0D5DE'


# ── Master file loaders ───────────────────────────────────────────────────── #

def load_ws_holdings(path: str) -> Dict[Tuple[str, str], float]:
    """
    Load Z13_Holding export.
    Returns {(client_code, isin): quantity}
    Cols: A=CLIENT_CODE, B=INSTRUMENT_CODE, C=ISINCODE, F=QUANTITY
    """
    lookup = {}
    try:
        wb = _open_wb(path)
        ws = wb[wb.sheetnames[0]]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                continue
            client = str(row[0] or '').strip()
            isin   = str(row[2] or '').strip()
            qty    = float(row[5] or 0)
            if client and isin:
                lookup[(client, isin)] = qty
        wb.close()
        logger.info(f"WS Holdings loaded: {len(lookup):,} positions")
    except Exception as e:
        logger.error(f"Failed to load WS holdings: {e}", exc_info=True)
    return lookup


def load_ws_isin_names(path: str) -> Dict[str, str]:
    """
    Build {isin: instrument_code} from Z13_Holding as a fallback name source.
    Used for WS-only positions where no custodian name is available.
    Cols: B=INSTRUMENT_CODE, C=ISINCODE
    """
    names: Dict[str, str] = {}
    try:
        wb = _open_wb(path)
        ws = wb[wb.sheetnames[0]]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                continue
            isin      = str(row[2] or '').strip()
            instr_code = str(row[1] or '').strip()
            if isin and isin not in names and instr_code:
                names[isin] = instr_code
        wb.close()
    except Exception as e:
        logger.warning(f"Could not load WS instrument codes: {e}")
    return names


def load_ws_transactions(path: str, holding_date: str) -> Dict[Tuple[str, str], dict]:
    """
    Load Z13_TradeTrans export.

    Returns {(client_code, isin): {
        'pending_buy': float,   — trades on holding_date with VALUA_DATE > holding_date
        'pending_sell': float,
        'other_buy': float,     — trades on other dates, used as fallback to explain breaks
        'other_sell': float,
    }}

    Primary: only trades with TRXN_DATE == holding_date and VALUA_DATE > holding_date
    Fallback: all other trades in the file, accumulated separately for break analysis
    Cols: B=CLIENT_CODE, C=TRXN_DATE, D=TRXN_TYPE (P/S), G=QUANTITY,
          I=VALUA_DATE, K=ISINCODE
    """
    def _parse_dt(val):
        if isinstance(val, datetime): return val
        if isinstance(val, str) and val.strip():
            for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
                try: return datetime.strptime(val.strip(), fmt)
                except ValueError: pass
        return None

    result = defaultdict(lambda: {
        'pending_buy':  0.0, 'pending_sell': 0.0,
        'other_buy':    0.0, 'other_sell':   0.0,
    })
    try:
        hold_dt = datetime.strptime(holding_date, '%Y-%m-%d')
        wb = _open_wb(path)
        ws = wb[wb.sheetnames[0]]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                continue
            client    = str(row[1] or '').strip()   # B
            trxn_date = row[2]                       # C — trade date
            trxn_type = str(row[3] or '').strip()   # D — P or S
            qty       = float(row[6] or 0)          # G
            val_date  = row[8]                       # I — settlement date
            isin      = str(row[10] or '').strip()  # K

            if not client or not isin or qty == 0:
                continue

            trxn_dt = _parse_dt(trxn_date)
            val_dt  = _parse_dt(val_date)
            if val_dt is None:
                continue

            key = (client, isin)

            # Note: the prior T0-buy bucket (val_date == hold_date for
            # purchases) was removed when the 0096 SettlementDate
            # column G was changed to always emit T+1 — VALUA_DATE
            # never lands on hold_date for new uploads, and the
            # logical=0/saleable<0 case it explained no longer arises.
            # Legacy data with VALUA_DATE == hold_date now flows
            # through the normal "already settled" branch below.
            is_pending = val_dt > hold_dt
            if not is_pending:
                # Already settled — no impact on break analysis
                continue

            # Primary pending: trade date <= holdings date (booked by or on holdings date)
            # Other pending:   trade date > holdings date (future-dated, booked after snapshot)
            is_primary = trxn_dt is None or trxn_dt.date() <= hold_dt.date()

            if is_primary:
                if trxn_type == 'P':
                    result[key]['pending_buy']  += qty
                elif trxn_type == 'S':
                    result[key]['pending_sell'] += qty
            else:
                if trxn_type == 'P':
                    result[key]['other_buy']  += qty
                elif trxn_type == 'S':
                    result[key]['other_sell'] += qty

        wb.close()
        today_count = sum(1 for v in result.values()
                          if v['pending_buy'] > 0 or v['pending_sell'] > 0)
        other_count = sum(1 for v in result.values()
                          if v['other_buy'] > 0 or v['other_sell'] > 0)
        logger.info(f"WS Transactions: {today_count} pending (T+2/T+3), "
                    f"{other_count} positions with other-date trades")
    except Exception as e:
        logger.error(f"Failed to load WS transactions: {e}", exc_info=True)
    return dict(result)


def load_pool_master_names(path: str) -> Dict[str, str]:
    """
    Load Z13_PoolMaster for display names.
    Returns {ws_scheme_code: display_name}
    Col A = MAPINID (scheme code), Col E = REMARKS (display name)
    """
    names = {}
    try:
        wb = _open_wb(path)
        ws = wb[wb.sheetnames[0]]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                continue
            code = str(row[0] or '').strip()
            name = str(row[4] or '').strip()
            if code:
                names[code] = name or code
        wb.close()
    except Exception as e:
        logger.warning(f"Could not load pool master names: {e}")
    return names


# ── Recon Engine ──────────────────────────────────────────────────────────── #

def validate_file_dates(date_str: str,
                        ws_holdings_path: Optional[str],
                        ws_transactions_path: Optional[str],
                        custodian_records) -> List[str]:
    """
    Cross-check that all files contain data for the same holding date.
    Returns a list of error messages (empty = all good).
    """
    errors = []
    expected = datetime.strptime(date_str, '%Y-%m-%d')
    expected_dmy = expected.strftime('%d/%m/%Y')   # DD/MM/YYYY as used in files

    # --- Z13_Holding: check PORTFOLIO_DATE (col E, index 4) ---
    if ws_holdings_path and os.path.exists(ws_holdings_path):
        try:
            wb = _open_wb(ws_holdings_path)
            ws = wb[wb.sheetnames[0]]
            dates_found = set()
            for row in ws.iter_rows(min_row=2, max_row=51, values_only=True):
                if not row or row[0] is None:
                    continue
                val = row[4]
                if isinstance(val, datetime):
                    dates_found.add(val.strftime('%d/%m/%Y'))
                elif isinstance(val, str) and val.strip():
                    dates_found.add(val.strip())
            wb.close()
            dates_found.discard('')
            if dates_found and expected_dmy not in dates_found:
                errors.append(
                    f"Z13_Holding: file contains date(s) {', '.join(sorted(dates_found))} "
                    f"but selected holdings date is {expected_dmy}"
                )
        except Exception as e:
            errors.append(f"Z13_Holding: could not read dates — {e}")

    # --- Z13_TradeTrans: warn if holding date not present but don't block ---
    # The file legitimately contains trades from T-1 and T+1 (settlement cycles).
    # We only warn if the holding date is entirely absent from the file.
    if ws_transactions_path and os.path.exists(ws_transactions_path):
        try:
            wb = _open_wb(ws_transactions_path)
            ws = wb[wb.sheetnames[0]]
            dates_found = set()
            for row in ws.iter_rows(min_row=2, max_row=51, values_only=True):
                if not row or row[0] is None:
                    continue
                val = row[2]
                if isinstance(val, datetime):
                    dates_found.add(val.strftime('%d/%m/%Y'))
                elif isinstance(val, str) and val.strip():
                    dates_found.add(val.strip())
            wb.close()
            dates_found.discard('')
            if dates_found and expected_dmy not in dates_found:
                # Non-blocking warning — other-date trades will be used as fallback
                logger.warning(
                    f"Z13_TradeTrans: holding date {expected_dmy} not found in file "
                    f"(contains: {', '.join(sorted(dates_found))}). "
                    f"Other-date trades will be used as fallback break analysis."
                )
        except Exception as e:
            logger.warning(f"Z13_TradeTrans: could not read dates — {e}")

    # --- Custodian records: check holding_date field ---
    if custodian_records:
        cust_dates = set(r.holding_date for r in custodian_records if r.holding_date)
        if cust_dates and expected_dmy not in cust_dates:
            # Non-blocking: log warning but allow recon to proceed
            # (parser uses date_str which has offset already applied)
            logger.warning(
                f"Custodian files: holding date(s) in records are {', '.join(sorted(cust_dates))}, "
                f"expected {expected_dmy} — proceeding anyway"
            )

    return errors


class ReconEngine:

    # Categories
    CLEAN             = 'clean'
    PENDING_EXPLAINED = 'pending_explained'
    UNEXPLAINED       = 'unexplained'
    MINOR_BREAK       = 'minor_break'
    CUSTODY_ONLY      = 'custody_only'
    WS_ONLY           = 'ws_only'
    UNVERIFIED        = 'unverified'
    # Pool total matches sum of WS investors, but custodian holds the units
    # at pool level only — i.e. GSW has not communicated per-investor
    # allocation to the custodian. Quantity-wise reconciled, allocation gap.
    INVESTOR_ALLOC_NOT_COMMUNICATED = 'investor_alloc_not_communicated'

    CATEGORY_LABELS = {
        CLEAN:             '✓ Clean Match',
        PENDING_EXPLAINED: '~ Pending Explained',
        UNEXPLAINED:       '✗ Unexplained Break',
        MINOR_BREAK:       '⚠ Minor Break (< 1 unit)',
        CUSTODY_ONLY:      '✗ Custody Only (Break)',
        WS_ONLY:           '✗ WS Only (Break)',
        UNVERIFIED:        '? Unverified (Missing Custodian Data)',
        INVESTOR_ALLOC_NOT_COMMUNICATED:
            '! Investor Allocation Not Communicated to Custodian',
    }

    def __init__(self, mappings_config: dict):
        self.mappings = mappings_config.get('strategy_mappings', [])

    def _get_strategy(self, client: str, broker_code: str,
                      source: str) -> Optional[str]:
        """Map (source, broker_code, client) to WS scheme code."""
        for m in self.mappings:
            if m.get('source') == source and m.get('broker_code') == broker_code:
                for rule in m.get('conditional_rules', []):
                    # Exact match
                    if rule.get('if_client_id') == client:
                        return rule['then_ws_scheme_code']
                    # Prefix match: if_client_id_prefix allows FOF → route all FOF* clients
                    pfx = rule.get('if_client_id_prefix', '')
                    if pfx and client.startswith(pfx):
                        return rule['then_ws_scheme_code']
                return m.get('default_ws_scheme_code')
        return None

    def run(self, records: List[HoldingRecord],
            ws_holdings_path: Optional[str],
            ws_transactions_path: Optional[str],
            pool_master_path: Optional[str],
            output_dir: str,
            date_str: str,
            kotak_custody_path: Optional[str] = None,
            pool_mapin_codes: set = None,
            prev_custody: Optional[Dict[Tuple[str, str], dict]] = None,
            prev_date_str: Optional[str] = None,
            etf_isins: Optional[set] = None) -> Tuple[str, List[str]]:
        """
        Run the full reconciliation and write the Excel report.

        `prev_custody` is an optional {(client, isin): {logical, saleable, ...}}
        snapshot from the previous business day (see core/prev_custody.py).
        When provided, break rows with no current-day trade explanation are
        enriched with a "Custody dropped by N since <prev_date_str>" note so
        un-booked sells stand out at a glance.

        Returns (output_path, warnings)
        """
        warnings = []

        # --- Date validation across all files ---
        date_errors = validate_file_dates(
            date_str, ws_holdings_path, ws_transactions_path, records
        )
        if date_errors:
            return None, [f"DATE MISMATCH — {e}" for e in date_errors], {}

        # Load WS master files
        ws_holdings     = {}
        ws_isin_names   = {}   # isin → instrument_code fallback for WS-only names
        if ws_holdings_path and os.path.exists(ws_holdings_path):
            ws_holdings   = load_ws_holdings(ws_holdings_path)
            ws_isin_names = load_ws_isin_names(ws_holdings_path)
            # Exclude advisory holdings (client codes containing 'ADV') from reconciliation
            adv_excluded = sum(1 for (client, _) in list(ws_holdings) if 'ADV' in client.upper())
            ws_holdings   = {k: v for k, v in ws_holdings.items() if 'ADV' not in k[0].upper()}
            if adv_excluded:
                logger.info(f"Excluded {adv_excluded} advisory (ADV) positions from WS holdings")
        else:
            warnings.append("WS Holdings file (Z13_Holding) not found — "
                            "upload it to masters/ before running recon.")

        ws_transactions = {}
        if ws_transactions_path and os.path.exists(ws_transactions_path):
            ws_transactions = load_ws_transactions(ws_transactions_path, date_str)
        else:
            warnings.append("WS Transactions file (Z13_TradeTrans) not found — "
                            "pending transaction analysis will be skipped.")

        pool_names = {}
        if pool_master_path and os.path.exists(pool_master_path):
            pool_names = load_pool_master_names(pool_master_path)

        # Load Kotak custody remapping (strategy pool code → investor client code)
        kotak_remap: Dict[Tuple[str, str], str] = {}
        if kotak_custody_path and os.path.exists(kotak_custody_path):
            try:
                from core.exporter import load_kotak_custody
                kotak_remap = load_kotak_custody(kotak_custody_path)
            except Exception as e:
                logger.warning(f"Could not load Kotak custody for recon: {e}")

        # Build custodian lookup: (client, isin) → record
        # Apply Kotak trade-day remapping: replace strategy pool code with investor code
        cust_lookup: Dict[Tuple[str, str], dict] = {}
        # Pool-aggregate capture: when the custodian reports a pool-level row
        # (e.g. Axis GOLDEQUPMS with no per-client breakdown), we still drop it
        # from the main recon key-space BUT remember the ISIN totals. A later
        # pass uses them to validate matching WS rows — without this, WS
        # positions under that pool look "Unverified" for ever because the
        # pool aggregate never finds a matching (client, isin) key on the WS side.
        pool_aggregates: Dict[str, dict] = {}
        for rec in records:
            client_id = rec.client_id
            # Skip advisory client codes — excluded from reconciliation entirely
            if 'ADV' in client_id.upper():
                continue
            # Skip pool-level rows — the custodian (e.g. Axis) sometimes records
            # block deal deliveries at the pool MAPIN level (e.g. GOLDETEPMS) in
            # addition to the individual client rows. These pool-level rows duplicate
            # the individual client positions; WS tracks at client level only.
            if pool_mapin_codes and client_id.upper() in pool_mapin_codes:
                _agg = pool_aggregates.setdefault(rec.isin, {
                    'logical':  0.0,
                    'saleable': 0.0,
                    'client':   client_id,
                    'source':   rec.source,
                    'security_name': rec.security_name,
                })
                _agg['logical']  += rec.logical_holding
                _agg['saleable'] += rec.saleable_holding
                logger.debug(f'Skipping pool-level custodian row (captured '
                             f'for post-pass): client={client_id} isin={rec.isin}')
                continue
            # Remap Kotak pool codes to investor codes
            if rec.source == 'kotak' and kotak_remap:
                remapped = kotak_remap.get((client_id, rec.isin))
                if remapped:
                    client_id = remapped

            # Remap custodian client_id to WS scheme code when they differ.
            # e.g. Axis uses GOLDFALPMS but WS uses ALPH00001 — the mapping
            # in pools_hub defines ws_scheme_code which is the WS client code.
            ws_code = self._get_strategy(rec.client_id, rec.broker_code, rec.source)
            if ws_code and ws_code != client_id:
                # Only remap if the custodian code has no WS match but the ws_code does
                if (client_id, rec.isin) not in ws_holdings and (ws_code, rec.isin) in ws_holdings:
                    logger.debug(f'Remapping custodian {client_id} → WS {ws_code} for {rec.isin}')
                    client_id = ws_code

            key = (client_id, rec.isin)
            if key in cust_lookup:
                cust_lookup[key]['logical']  += rec.logical_holding
                cust_lookup[key]['saleable'] += rec.saleable_holding
            else:
                cust_lookup[key] = {
                    'client':        client_id,
                    'isin':          rec.isin,
                    'security_name': rec.security_name,
                    'source':        rec.source,
                    'broker_code':   rec.broker_code,
                    'ws_scheme':     ws_code or '',
                    'logical':       rec.logical_holding,
                    'saleable':      rec.saleable_holding,
                }

        # All keys across both datasets
        all_keys = set(cust_lookup.keys()) | set(ws_holdings.keys())

        # ── MF (non-ETF) identification ──────────────────────────────────
        # All Indian fund units — both AMC-only MFs and exchange-traded ETFs —
        # carry ISINs starting with "INF". ETFs settle T+1 on the exchange and
        # should NOT get the MF timing-lag allowance; AMC MFs settle T+2/3 and
        # should. The discriminator is Z8's NSEMAPPING column (passed in as
        # etf_isins): populated for ETFs, empty for AMC MF units.
        #
        # Fallback to the legacy name-contains-"ETF" check when Z8 is missing
        # or the ISIN isn't listed there — covers brand-new ETFs that haven't
        # yet landed in the master file.
        _etf_isins_u = {i.upper() for i in (etf_isins or set())}
        def _is_mf(isin: str, sec_name: str = '') -> bool:
            u = (isin or '').upper()
            if not u.startswith('INF'):
                return False
            if u in _etf_isins_u:
                return False   # Z8 says it's an ETF
            # No Z8 signal — fall back to name heuristic
            return 'ETF' not in (sec_name or '').upper()

        # Build set of MF ISINs with recent trades (within holding_date to T+2).
        # Used to explain MF WS-Only and Custody-Only positions.
        _mf_recent_trades: set = set()  # set of (client, isin) with MF trades
        _mf_trade_dates: dict = {}      # (client, isin) → trade_date string
        try:
            _tt_path_for_mf = ws_transactions  # already loaded
            for (cl, isn), txn_data in _tt_path_for_mf.items():
                if isn.upper().startswith('INF'):
                    # Any pending activity means MF settlement in progress
                    if (txn_data.get('pending_buy', 0) > 0 or
                        txn_data.get('pending_sell', 0) > 0 or
                        txn_data.get('other_buy', 0) > 0 or
                        txn_data.get('other_sell', 0) > 0):
                        _mf_recent_trades.add((cl, isn))
        except Exception:
            pass

        # Build isin → security_name lookup from custodian records.
        # Used to fill names for WS-only rows (advisory etc.) where we have no cust record.
        # Falls back to instrument code from Z13_Holding if still blank.
        isin_name_lookup: Dict[str, str] = {}
        for rec_data in cust_lookup.values():
            isin = rec_data.get('isin', '')
            name = rec_data.get('security_name', '')
            if isin and name and isin not in isin_name_lookup:
                isin_name_lookup[isin] = name
        # Pool aggregates are skipped from cust_lookup but still carry the real
        # custodian-side security name — seed the lookup so WS-only rows under
        # a pooled MAPIN pick up the proper fund name instead of the WS shortcode.
        for _isin, _agg in pool_aggregates.items():
            _name = _agg.get('security_name', '')
            if _isin and _name and _isin not in isin_name_lookup:
                isin_name_lookup[_isin] = _name
        # Merge WS instrument codes as fallback for ISINs not seen in custodian data
        for isin, instr_code in ws_isin_names.items():
            if isin not in isin_name_lookup:
                isin_name_lookup[isin] = instr_code

        # Track which WS scheme codes have any custodian records
        # Used to distinguish genuine WS Only breaks from unverifiable positions
        covered_schemes = set()
        for rec_data in cust_lookup.values():
            ws_code = rec_data.get('ws_scheme', '')
            if ws_code:
                covered_schemes.add(ws_code)

        # Categorise each position
        results = {
            self.CLEAN:             [],
            self.PENDING_EXPLAINED: [],
            self.UNEXPLAINED:       [],
            self.MINOR_BREAK:       [],
            self.CUSTODY_ONLY:      [],
            self.WS_ONLY:           [],
            self.UNVERIFIED:        [],
            self.INVESTOR_ALLOC_NOT_COMMUNICATED: [],
        }

        for key in sorted(all_keys):
            client, isin = key
            in_cust = key in cust_lookup
            in_ws   = key in ws_holdings

            pend    = ws_transactions.get(key, {})
            pend_b  = pend.get('pending_buy',  0.0)
            pend_s  = pend.get('pending_sell', 0.0)

            if in_cust and not in_ws:
                rec = cust_lookup[key]
                _logical_break  = rec['logical']  - (pend_b - pend_s)
                _saleable_break = rec['saleable']
                _row = {
                    **rec,
                    'ws_qty':         0,
                    'ws_adjusted':    pend_b - pend_s,
                    'pending_buy':    pend_b,
                    'pending_sell':   pend_s,
                    'logical_break':  _logical_break,
                    'saleable_break': _saleable_break,
                }
                _sec = isin_name_lookup.get(isin, rec.get('security_name', ''))
                _row['security_name'] = _sec

                if _logical_break == 0 and (pend_b > 0 or pend_s > 0):
                    _row['category'] = self.PENDING_EXPLAINED
                    _row['note'] = 'Pending trades explain custodian-only position'
                    results[self.PENDING_EXPLAINED].append(_row)
                elif abs(_logical_break) < 1 and abs(_saleable_break) < 1:
                    _row['category'] = self.MINOR_BREAK
                    results[self.MINOR_BREAK].append(_row)
                elif _logical_break == 0 and _saleable_break == 0:
                    _row['category'] = self.CLEAN
                    results[self.CLEAN].append(_row)
                elif _is_mf(isin, _sec) and (client, isin) in _mf_recent_trades:
                    # MF with recent redemption — WS already removed units but
                    # custody demat still holds them (settlement pending T+2/3)
                    _row['category'] = self.PENDING_EXPLAINED
                    _row['note'] = 'MF redemption pending custody debit (T+2/3 settlement)'
                    results[self.PENDING_EXPLAINED].append(_row)
                else:
                    _row['category'] = self.CUSTODY_ONLY
                    results[self.CUSTODY_ONLY].append(_row)
            elif in_ws and not in_cust:
                ws_qty = ws_holdings[key]
                ws_adj = ws_qty + pend_b - pend_s

                # Determine strategy for this WS position by looking up
                # what scheme code this client belongs to in WS holdings
                # (we don't have a direct mapping from client→scheme in WS-only rows)
                # Mark as UNVERIFIED if we have zero custodian records for ANY strategy
                # that this client could belong to — simpler: if we had any fetch errors
                # or zero total custodian records, mark unverified.
                # Best proxy: check if covered_schemes is empty (no custodian data at all)
                # or if client code pattern suggests a known-missing source
                # Unverified = WS client code never appears in any custodian record
                # meaning we simply have no custody data for this client at all
                cust_clients = set(k[0] for k in cust_lookup.keys())
                is_unverified = client not in cust_clients and len(cust_clients) > 0

                row = {
                    'client':         client,
                    'isin':           isin,
                    'security_name':  '',
                    'source':         '',
                    'broker_code':    '',
                    'ws_scheme':      '',
                    'logical':        0,
                    'saleable':       0,
                    'ws_qty':         ws_qty,
                    'ws_adjusted':    ws_adj,
                    'pending_buy':    pend_b,
                    'pending_sell':   pend_s,
                    'logical_break':  -ws_qty,
                    'saleable_break': -ws_qty,
                }
                # Enrich security name for MF check
                _sec = isin_name_lookup.get(isin, '')
                row['security_name'] = _sec

                if is_unverified:
                    row['category'] = self.UNVERIFIED
                    results[self.UNVERIFIED].append(row)
                elif abs(row['logical_break']) < 1 and abs(row['saleable_break']) < 1:
                    row['category'] = self.MINOR_BREAK
                    results[self.MINOR_BREAK].append(row)
                elif pend_b > 0 or pend_s > 0:
                    # Threshold matches the main-branch MINOR_BREAK cutoff
                    # at line ~665 (< 1). Previously 0.01, which made WS-only
                    # classification asymmetric with custody-only.
                    if abs(-ws_qty + (pend_b - pend_s)) < 1:
                        row['category'] = self.PENDING_EXPLAINED
                        row['note'] = 'Pending trades explain WS-only position'
                        results[self.PENDING_EXPLAINED].append(row)
                    else:
                        row['category'] = self.WS_ONLY
                        results[self.WS_ONLY].append(row)
                elif _is_mf(isin, _sec) and (client, isin) in _mf_recent_trades:
                    # MF with recent trade activity — custody credit pending T+2/3
                    row['category'] = self.PENDING_EXPLAINED
                    row['note'] = 'MF units pending custody credit (T+2/3 settlement)'
                    results[self.PENDING_EXPLAINED].append(row)
                elif _is_mf(isin, _sec):
                    # MF without recent trades — still flag as MF but it's a break
                    row['category'] = self.WS_ONLY
                    row['note'] = 'MF position — no recent trade found to explain'
                    results[self.WS_ONLY].append(row)
                else:
                    row['category'] = self.WS_ONLY
                    results[self.WS_ONLY].append(row)
            else:
                rec    = cust_lookup[key]
                ws_qty = ws_holdings[key]

                # ── Step 1: raw comparison (no trade adjustments) ────────────────────
                # Compare custodian logical holdings directly against WS holdings.
                # Trade data is only consulted if this raw comparison shows a break.
                raw_logical_break  = rec['logical']  - ws_qty
                raw_saleable_break = rec['saleable'] - ws_qty

                row = {
                    **rec,
                    'ws_qty':         ws_qty,
                    'ws_adjusted':    ws_qty,  # default: no adjustment applied
                    'pending_buy':    0.0,
                    'pending_sell':   0.0,
                    'other_buy':      0.0,
                    'other_sell':     0.0,
                    'logical_break':  raw_logical_break,
                    'saleable_break': raw_saleable_break,
                }

                if raw_logical_break == 0:
                    # ── Perfect logical match — saleable not reconciled ───────────────
                    row['category'] = self.CLEAN
                    results[self.CLEAN].append(row)

                elif abs(raw_logical_break) < 1:
                    # ── Minor logical difference — no trades needed ───────────────────
                    row['category'] = self.MINOR_BREAK
                    results[self.MINOR_BREAK].append(row)

                else:
                    # ── Step 2: raw break — now try trade data to explain it ─────────
                    pend_b_val = pend.get('pending_buy',  0.0)
                    pend_s_val = pend.get('pending_sell', 0.0)
                    other_buy  = pend.get('other_buy',    0.0)
                    other_sell = pend.get('other_sell',   0.0)

                    # WS adjusted = WS qty + pending sells (still in cust) - pending buys (still in cust)
                    # WS books T-date; custodian doesn't settle until value date.
                    # Pending SELL: WS deducted, custodian still shows → ws_adj adds it back
                    # Pending BUY:  WS added, custodian not settled yet → ws_adj subtracts
                    ws_adj         = ws_qty + pend_s_val - pend_b_val
                    logical_break  = rec['logical']  - ws_adj
                    saleable_break = rec['saleable'] - ws_qty

                    row.update({
                        'ws_adjusted':  ws_adj,
                        'pending_buy':  pend_b_val,
                        'pending_sell': pend_s_val,
                        'other_buy':    other_buy,
                        'other_sell':   other_sell,
                        'logical_break':  logical_break,
                        'saleable_break': saleable_break,
                    })

                    if logical_break == 0:
                        # Trade adjustment resolved the logical break — clean match.
                        # (Saleable may still differ due to settlement timing but
                        #  we reconcile on logical holdings only.)
                        if pend_b_val > 0 or pend_s_val > 0:
                            row['category'] = self.PENDING_EXPLAINED
                            results[self.PENDING_EXPLAINED].append(row)
                        else:
                            row['category'] = self.CLEAN
                            results[self.CLEAN].append(row)

                    elif abs(logical_break) < 1:
                        row['category'] = self.MINOR_BREAK
                        results[self.MINOR_BREAK].append(row)

                    else:
                        # ── Step 3: still unexplained — last resort: other-date trades ──
                        # (old-cycle unsettled trades where trade date ≠ value date)
                        other_adj = ws_qty + other_sell - other_buy
                        if abs(rec['logical'] - other_adj) < 0.01 and (other_buy > 0 or other_sell > 0):
                            row['category'] = self.PENDING_EXPLAINED
                            row['note'] = 'Explained by other-date transactions'
                            results[self.PENDING_EXPLAINED].append(row)
                        else:
                            row['category'] = self.UNEXPLAINED
                            results[self.UNEXPLAINED].append(row)

        # ── Pool-aggregate reconciliation ─────────────────────────────────
        # When the custodian delivers only a pool-level row for an ISIN
        # (no per-investor breakdown) but WS tracks each underlying client
        # separately, compare the pool total to the sum of WS investors:
        #
        #   • Equal  → quantity is reconciled BUT the custodian has no record
        #              of which investor owns what. This is a GSW→Custodian
        #              communication gap — flag as INVESTOR_ALLOC_NOT_COMMUNICATED.
        #              These rows are kept visible (NOT moved to Clean) so ops
        #              can chase the allocation update with the custodian.
        #   • Differ → genuine quantity break — leave rows in Unverified/WS_Only
        #              and let the existing classification stand.
        if pool_aggregates:
            from collections import defaultdict as _dd_pa
            _by_isin: dict = _dd_pa(list)
            for _cat_key in (self.UNVERIFIED, self.WS_ONLY):
                for _r in list(results.get(_cat_key, [])):
                    _by_isin[_r.get('isin', '')].append((_cat_key, _r))
            _agg_moved = 0
            for _isin, _group in _by_isin.items():
                if _isin not in pool_aggregates:
                    continue
                _agg = pool_aggregates[_isin]
                _ws_sum = sum(float(r.get('ws_qty', 0) or 0) for _, r in _group)
                # Tight tolerance — expect WS clients to sum exactly to pool qty
                if abs(_ws_sum - _agg['logical']) > 0.01:
                    continue
                _peers = sorted({_r.get('client', '') for _, _r in _group})
                _peer_csv = ', '.join(_peers)
                _note = (
                    f"Pool '{_agg['client']}' at {_agg['source']} holds "
                    f"{_agg['logical']:.4f} units; WS allocates this across "
                    f"{len(_peers)} investor(s) ({_peer_csv}) and the totals "
                    f"match. Custodian has NOT been told which investor owns "
                    f"what — raise allocation update with custodian."
                )
                _agg_name = _agg.get('security_name', '')
                for _cat_key, _r in _group:
                    # Quantity tally is reconciled — surface zero breaks but
                    # keep the row visible in its own bucket (not Clean).
                    _r['source']   = _agg['source']
                    _r['logical']  = _r.get('ws_qty', 0) or 0
                    _r['saleable'] = _r.get('ws_qty', 0) or 0
                    _r['logical_break']  = 0
                    _r['saleable_break'] = 0
                    _r['ws_adjusted']    = _r.get('ws_qty', 0) or 0
                    if _agg_name:
                        _r['security_name'] = _agg_name
                    _r['pool_code']      = _agg.get('client', '')
                    _r['pool_total']     = _agg['logical']
                    _r['affected_clients'] = _peer_csv
                    _r['note'] = _note
                    _r['category'] = self.INVESTOR_ALLOC_NOT_COMMUNICATED
                    results[self.INVESTOR_ALLOC_NOT_COMMUNICATED].append(_r)
                    try:
                        results[_cat_key].remove(_r)
                    except ValueError:
                        pass
                    _agg_moved += 1
            if _agg_moved:
                logger.info(
                    f"Pool-aggregate recon: flagged {_agg_moved} row(s) as "
                    f"Investor Allocation Not Communicated (sum-matches "
                    f"{len(pool_aggregates)} pool-level aggregate(s))"
                )

        # ── Likely-pending-sell annotation ────────────────────────────────
        # For every break / WS-Only row with no current-day trade explanation,
        # check whether the previous business day's custody had the position.
        # A custody-side drop with no matching WS trade is the signature of
        # a sell that's been executed but not yet booked in WS — most common
        # operational break.
        likely_sell_count = 0
        # Record whether prev-day snapshot was usable — the summary
        # banner uses this to explicitly say "detection disabled" when
        # yesterday's files aren't on disk, rather than just going quiet.
        self._prev_snapshot_status = 'ok' if prev_custody else 'missing'
        if prev_custody:
            label = prev_date_str or "previous business day"
            for cat in (self.UNEXPLAINED, self.WS_ONLY, self.CUSTODY_ONLY):
                for r in results.get(cat, []):
                    # Don't overwrite an existing explanation (pending trade,
                    # MF settlement, etc. — those are more specific).
                    if r.get('note'):
                        continue
                    key = (r.get('client', ''), r.get('isin', ''))
                    prev = prev_custody.get(key)
                    if not prev:
                        continue
                    prev_qty = float(prev.get('logical', 0) or 0)
                    today_qty = float(r.get('logical', 0) or 0)
                    # Only annotate drops (prev > today). An increase is its own
                    # story (fresh buy not yet booked in WS) — handled separately
                    # by the pending_buy path we already have.
                    if prev_qty <= today_qty:
                        continue
                    drop = prev_qty - today_qty
                    # Require that WS still reflects the pre-drop level: the
                    # pattern of an un-booked sell is WS_qty ≈ prev_custody_qty
                    # AND today_custody_qty < prev. If WS has already caught up
                    # (ws_qty close to today_qty) the drop is explained elsewhere.
                    ws_qty = float(r.get('ws_qty', 0) or 0)
                    # Symmetric tolerance: 5% of prev, floor 1 unit. Without a
                    # floor, prev_qty ≤ 2 would always be flagged even when
                    # WS already matches today's reduced custody.
                    tol = max(1.0, prev_qty * 0.05)
                    if abs(ws_qty - prev_qty) >= tol:
                        # WS is materially different from prev — not a clean
                        # un-booked sell pattern
                        continue
                    r['prev_custody_qty'] = prev_qty
                    r['unbooked_sell'] = True
                    r['note'] = (
                        f"Sell not booked in WS — custody dropped by "
                        f"{drop:.0f} since {label}; no matching WS trade"
                    )
                    likely_sell_count += 1
            if likely_sell_count:
                logger.info(
                    f"Flagged {likely_sell_count} break(s) as un-booked "
                    f"sells (custody dropped since {label})"
                )
        self._likely_sell_count = likely_sell_count  # picked up in summary

        # Write report
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        from datetime import datetime as _dt
        date_fmt  = date_str.replace('-', '')
        ts        = _dt.now().strftime('%H%M%S')
        filename  = f"Recon_{date_fmt}_{ts}.xlsx"
        out_path  = os.path.join(output_dir, filename)

        self._write_report(out_path, results, pool_names, date_str)

        total = sum(len(v) for v in results.values())
        logger.info(
            f"Recon complete: {total} positions — "
            f"clean={len(results[self.CLEAN])}, "
            f"pending={len(results[self.PENDING_EXPLAINED])}, "
            f"breaks={len(results[self.UNEXPLAINED])}, "
            f"cust_only={len(results[self.CUSTODY_ONLY])}, "
            f"ws_only={len(results[self.WS_ONLY])}, "
            f"alloc_not_communicated="
            f"{len(results[self.INVESTOR_ALLOC_NOT_COMMUNICATED])}"
        )
        return out_path, warnings, results

    # ── Excel report writer ───────────────────────────────────────────────── #

    def _write_report(self, out_path: str, results: dict,
                      pool_names: dict, date_str: str):
        wb = openpyxl.Workbook()

        # Summary sheet first
        self._write_summary(wb, results, pool_names, date_str)

        # Detail sheets
        sheet_configs = [
            (self.UNEXPLAINED,       'Unexplained Breaks',    C_RED_FG,    C_RED_BG),
            (self.MINOR_BREAK,       'Minor Breaks',          'B06820',    'FFF8E1'),
            (self.CUSTODY_ONLY,      'Custody Only',          C_BLUE_FG,   C_BLUE_BG),
            (self.WS_ONLY,           'WS Only',               C_ORANGE_FG, C_ORANGE_BG),
            (self.INVESTOR_ALLOC_NOT_COMMUNICATED,
                                     'Allocation Not Communicated',
                                                              '8E3FA0',    'F4ECF8'),
            (self.UNVERIFIED,        'Unverified',            '607D8B',    'ECEFF1'),
            (self.PENDING_EXPLAINED, 'Pending Explained',     C_AMBER_FG,  C_AMBER_BG),
            (self.CLEAN,             'Clean Matches',         C_GREEN_FG,  C_GREEN_BG),
        ]

        for cat, sheet_name, fg, bg in sheet_configs:
            rows = results[cat]
            self._write_detail_sheet(wb, sheet_name, rows, fg, bg,
                                     show_pending=(cat != self.CLEAN))

        wb.save(out_path)

    def _write_summary(self, wb, results: dict, pool_names: dict, date_str: str):
        ws = wb.active
        ws.title = 'Summary'

        # Title
        ws.merge_cells('A1:H1')
        title_cell = ws['A1']
        title_cell.value = f'Holdings Reconciliation Report — {date_str}'
        title_cell.font  = Font(bold=True, size=14, color=C_WHITE, name='Calibri')
        title_cell.fill  = PatternFill('solid', fgColor=C_NAVY)
        title_cell.alignment = Alignment(horizontal='center', vertical='center')
        ws.row_dimensions[1].height = 30

        # Overall counts
        ws['A3'] = 'Category'
        ws['B3'] = 'Positions'
        ws['C3'] = 'Status'
        for col in ['A', 'B', 'C']:
            c = ws[f'{col}3']
            c.font = Font(bold=True, color=C_WHITE, name='Calibri', size=10)
            c.fill = PatternFill('solid', fgColor=C_NAVY)
            c.alignment = Alignment(horizontal='center')

        summary_rows = [
            (self.UNEXPLAINED,       C_RED_FG,    C_RED_BG,    '✗ Requires immediate attention — break with no explanation'),
            (self.CUSTODY_ONLY,      C_RED_FG,    C_RED_BG,    '✗ In custodian but not in WS — unexplained break'),
            (self.WS_ONLY,           C_RED_FG,    C_RED_BG,    '✗ In WS but not in custodian — unexplained break'),
            (self.INVESTOR_ALLOC_NOT_COMMUNICATED,
                                     '8E3FA0',    'F4ECF8',    '! Pool total matches WS sum, but custodian has no per-investor allocation — raise with custodian'),
            (self.MINOR_BREAK,       'B06820',    'FFF8E1',    '⚠ Low urgency — difference < 1 unit'),
            (self.UNVERIFIED,        '607D8B',    'ECEFF1',    '? Cannot verify — custodian file missing'),
            (self.PENDING_EXPLAINED, C_AMBER_FG,  C_AMBER_BG,  '~ Difference due to pending trades'),
            (self.CLEAN,             C_GREEN_FG,  C_GREEN_BG,  '✓ Fully reconciled'),
        ]

        for r_idx, (cat, fg, bg, status) in enumerate(summary_rows, 4):
            count = len(results[cat])
            ws.cell(r_idx, 1, self.CATEGORY_LABELS[cat]).font = Font(
                color=fg, name='Calibri', size=10, bold=True)
            ws.cell(r_idx, 1).fill = PatternFill('solid', fgColor=bg)
            ws.cell(r_idx, 2, count).font = Font(color=fg, name='Calibri', size=10, bold=True)
            ws.cell(r_idx, 2).fill = PatternFill('solid', fgColor=bg)
            ws.cell(r_idx, 2).alignment = Alignment(horizontal='center')
            ws.cell(r_idx, 3, status).font = Font(color=fg, name='Calibri', size=10)
            ws.cell(r_idx, 3).fill = PatternFill('solid', fgColor=bg)

        total = sum(len(v) for v in results.values())
        # TOTAL row sits one row after the last category row (summary_rows
        # starts at row 4, so total row index = 4 + len(summary_rows))
        _total_row = 4 + len(summary_rows)
        ws.cell(_total_row, 1, 'TOTAL').font = Font(bold=True, name='Calibri', size=10)
        ws.cell(_total_row, 2, total).font   = Font(bold=True, name='Calibri', size=10)
        ws.cell(_total_row, 2).alignment = Alignment(horizontal='center')

        # Un-booked sell banner, or diagnostic if prev snapshot was missing
        likely = getattr(self, '_likely_sell_count', 0) or 0
        snapshot_status = getattr(self, '_prev_snapshot_status', '')
        note = ''
        color = ''
        if likely:
            note = (f"⚠ {likely} of the breaks above are sells not yet "
                    f"booked in WS — custody dropped day-over-day with no "
                    f"matching trade. See the Note column on each sheet.")
            color = 'B06820'
        elif snapshot_status == 'missing':
            note = ("⚠ Prev-day custody snapshot unavailable — un-booked "
                    "sell detection disabled. Yesterday's raw custody files "
                    "were not on disk (check KEYSTONE_DATA_DIR persistence).")
            color = 'C0392B'
        if note:
            ws.cell(_total_row, 4, note).font = Font(
                color=color, name='Calibri', size=10, italic=True)
            ws.merge_cells(start_row=_total_row, start_column=4,
                           end_row=_total_row, end_column=8)
            ws.cell(_total_row, 4).alignment = Alignment(
                horizontal='left', vertical='center', wrap_text=True)
            ws.row_dimensions[_total_row].height = 28

        # Strategy breakdown for breaks
        _bk_title_row = _total_row + 1
        _bk_hdr_row   = _total_row + 2
        _bk_data_row  = _total_row + 3
        ws.cell(_bk_title_row, 1,
                'Strategy Breakdown — All Breaks (Unexplained + Custody Only '
                '+ WS Only + Allocation Not Communicated)').font = Font(
            bold=True, size=11, name='Calibri', color=C_NAVY)

        for col_idx, hdr in enumerate(
            ['Strategy', 'Client', 'ISIN', 'Custodian Logical',
             'WS Adjusted', 'Break', 'Custodian Saleable', 'WS Qty'], 1):
            c = ws.cell(_bk_hdr_row, col_idx, hdr)
            c.font = Font(bold=True, color=C_WHITE, name='Calibri', size=10)
            c.fill = PatternFill('solid', fgColor=C_RED_FG)
            c.alignment = Alignment(horizontal='center')

        r = _bk_data_row
        all_breaks = (results[self.UNEXPLAINED]
                      + results[self.CUSTODY_ONLY]
                      + results[self.WS_ONLY]
                      + results[self.INVESTOR_ALLOC_NOT_COMMUNICATED])
        for row in all_breaks:
            ws.cell(r, 1, pool_names.get(row.get('ws_scheme',''), row.get('ws_scheme','') or '—'))
            ws.cell(r, 2, row['client'])
            ws.cell(r, 3, row['isin'])
            ws.cell(r, 4, row.get('logical', 0))
            ws.cell(r, 5, row.get('ws_adjusted', 0))
            ws.cell(r, 6, row.get('logical_break', 0))
            ws.cell(r, 7, row.get('saleable', 0))
            ws.cell(r, 8, row.get('ws_qty', 0))
            bg = C_RED_BG if r % 2 == 0 else 'FFFFFF'
            for col in range(1, 9):
                ws.cell(r, col).fill = PatternFill('solid', fgColor=bg)
                ws.cell(r, col).font = Font(name='Calibri', size=10)
            brk = row.get('logical_break', 0)
            if brk != 0:
                ws.cell(r, 6).font = Font(bold=True, color=C_RED_FG,
                                          name='Calibri', size=10)
            # Show category label in col 1 font style
            cat = row.get('category', '')
            if cat == self.CUSTODY_ONLY:
                ws.cell(r, 2).font = Font(italic=True, name='Calibri', size=10, color='1F3864')
            r += 1

        if not all_breaks:
            ws.cell(_bk_data_row, 1, 'No breaks — all positions reconciled ✓')
            ws.cell(_bk_data_row, 1).font = Font(
                color=C_GREEN_FG, bold=True, name='Calibri')

        # Column widths
        ws.column_dimensions['A'].width = 36
        ws.column_dimensions['B'].width = 14
        ws.column_dimensions['C'].width = 16
        for col in 'DEFGH':
            ws.column_dimensions[col].width = 18

    def _write_detail_sheet(self, wb, sheet_name: str, rows: list,
                            fg: str, bg: str, show_pending: bool):
        ws = wb.create_sheet(sheet_name)

        headers = [
            'Client Code', 'ISIN', 'Security Name', 'Source',
            'Cust Logical', 'Cust Saleable', 'WS Qty',
        ]
        if show_pending:
            headers += ['Pending Buy', 'Pending Sell', 'WS Adjusted',
                        'Logical Break', 'Saleable Break', 'Note']
        # Allocation-gap rows carry pool context — surface it as extra cols
        # so ops can see the pool aggregate alongside each affected investor.
        show_pool_cols = any(r.get('pool_code') for r in rows)
        if show_pool_cols:
            headers += ['Pool Code', 'Pool Total', 'Affected Investors']

        header_fill = PatternFill('solid', fgColor=fg)
        header_font = Font(bold=True, color=C_WHITE, name='Calibri', size=10)

        for c_idx, h in enumerate(headers, 1):
            cell = ws.cell(1, c_idx, h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center')

        data_font = Font(name='Calibri', size=10)
        alt_fill  = PatternFill('solid', fgColor=C_ALT)

        for r_idx, row in enumerate(rows, 2):
            fill = alt_fill if r_idx % 2 == 0 else None
            values = [
                row['client'], row['isin'], row.get('security_name', ''),
                row.get('source', ''),
                row.get('logical', 0), row.get('saleable', 0),
                row.get('ws_qty', 0),
            ]
            if show_pending:
                _sal_brk = row.get('saleable_break', 0)
                _note = row.get('note', '')
                # Flag outstanding saleable breaks in Pending Explained
                if not _note and _sal_brk != 0 and row.get('category') == 'pending_explained':
                    _note = f'Saleable break {_sal_brk:+.0f} — T+1 settlement pending'
                values += [
                    row.get('pending_buy', 0), row.get('pending_sell', 0),
                    row.get('ws_adjusted', 0),
                    row.get('logical_break', 0), _sal_brk, _note,
                ]
            if show_pool_cols:
                values += [row.get('pool_code', ''),
                           row.get('pool_total', ''),
                           row.get('affected_clients', '')]
            # Indices of logical_break and saleable_break columns when present.
            # 7 base cols + 4 pending cols (pending_buy, pending_sell, ws_adj,
            # logical_break) → logical_break is col 11, saleable_break col 12.
            _brk_cols = (11, 12) if show_pending else ()
            for c_idx, val in enumerate(values, 1):
                cell = ws.cell(r_idx, c_idx, val)
                cell.font = data_font
                if fill:
                    cell.fill = fill
                # Highlight non-zero breaks in red
                if c_idx in _brk_cols:
                    if isinstance(val, (int, float)) and val != 0:
                        cell.font = Font(bold=True, color=C_RED_FG,
                                         name='Calibri', size=10)

        # Column widths — base 7 + 6 (pending block) + 3 (pool block)
        widths = [16, 16, 40, 10, 16, 16, 16, 14, 14, 16, 14, 14, 30,
                  16, 16, 50]
        for i, w in enumerate(widths[:len(headers)], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        ws.freeze_panes = 'A2'

        if not rows:
            ws.cell(2, 1, f'No records in this category.')
            ws.cell(2, 1).font = Font(italic=True, color='888888', name='Calibri')
