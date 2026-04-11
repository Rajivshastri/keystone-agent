"""
WS Order Log Parser (Z13_OrderLog.xlsx)
----------------------------------------
Replaces Z13_TradeTrans as the source of WS orders for trade reconciliation.

The file is a wide export (145 columns). The first 108 columns are client /
account metadata. Trade-specific data begins at column DE (index 108).

Key trade columns (DE onwards):
  ORDERDATE     date the order was placed
  ORDERID       unique WS order identifier
  CLIENTID      WS client ID (repeated; also at col A)
  CLIENTNAME    client name
  SYMBOLCODE    WS internal instrument code (e.g. EQ24662 = CIFC)
  SYMBOLNAME    full security name ("Cholamandalam Investment & Finance Co. Ltd.")
  TRANSTYPE     "Buy" or "Sell"
  QUANTITY      shares ordered
  RATEFLAG      "Limit" or "MKT"
  RATE          limit price or last market rate
  AUTHORIZED_BY user who authorized
  AUTHORIZED_DATE datetime of authorization
  STATUS        "Authorized" | "Deleted" | "Rejected due to Error"
  ISINCODE      12-char ISIN
  BROKERCODE    broker short code (e.g. "DIRECT")
  BROKERACID    broker account / Mapin (e.g. "GWPJ0008", "MYSM00001")

Client-level columns (A onwards, same row):
  POOLID / POOLNAME    WS pool
  SCHEMEID / SCHEMENAME

Usage
-----
result = OrderLogParser().parse_file(path)
# result.orders  — list of dicts with normalised keys matching what the engine expects
# result.isin_to_instr — {ISIN: SYMBOLCODE} map
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import openpyxl

logger = logging.getLogger(__name__)

# Status values that represent a live, authorised order
AUTHORIZED_STATUSES = {'authorized', 'authorised'}


@dataclass
class OrderLogResult:
    orders:        List[dict]         = field(default_factory=list)
    isin_to_instr: Dict[str, str]     = field(default_factory=dict)
    error:         str                = ''
    warnings:      List[str]          = field(default_factory=list)
    total_rows:    int                = 0
    skipped_rows:  int                = 0

    @property
    def ok(self) -> bool:
        return not self.error


class OrderLogParser:
    """Parse Z13_OrderLog.xlsx for trade reconciliation."""

    def parse_file(self, file_path: str) -> OrderLogResult:
        result = OrderLogResult()
        path = Path(file_path)

        if not path.exists():
            result.error = f"File not found: {path.name}"
            return result

        try:
            ext = path.suffix.lower()
            if ext == '.xls':
                import xlrd as _xlrd
                _xwb = _xlrd.open_workbook(str(path))
                _xws = _xwb.sheet_by_index(0)
                rows = [tuple(_xws.cell_value(r, c) for c in range(_xws.ncols))
                        for r in range(_xws.nrows)]
            else:
                wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                wb.close()
        except Exception as e:
            result.error = f"Cannot open {path.name}: {e}"
            return result

        if not rows:
            result.error = "File is empty"
            return result

        # Build column index from header row
        hdr = {str(h or '').strip().upper(): i for i, h in enumerate(rows[0]) if h}

        # Verify this is an OrderLog (not a TradeTrans)
        if 'ORDERID' not in hdr or 'BROKERACID' not in hdr:
            result.error = (
                f"{path.name} does not appear to be a Z13_OrderLog file. "
                "Expected columns ORDERID and BROKERACID. "
                "Upload Z13_OrderLog.xlsx (not Z13_TradeTrans)."
            )
            return result

        # Convenience getter
        def cell(row, col_name, default=''):
            i = hdr.get(col_name)
            if i is None or i >= len(row):
                return default
            v = row[i]
            if v is None:
                return default
            if hasattr(v, 'strftime'):          # datetime cell
                return v.strftime('%d/%m/%Y')
            return str(v).strip()

        def flt(row, col_name, default=0.0):
            v = cell(row, col_name, '')
            try:
                return float(str(v).replace(',', '')) if v else default
            except (ValueError, TypeError):
                return default

        total = 0
        skipped = 0

        for row in rows[1:]:
            if not row or row[0] is None:
                continue
            total += 1

            status = cell(row, 'STATUS', '').lower()
            if status not in AUTHORIZED_STATUSES:
                skipped += 1
                continue

            isin       = cell(row, 'ISINCODE')
            symbol_code = cell(row, 'SYMBOLCODE')     # WS internal code e.g. EQ24662
            symbol_name = cell(row, 'SYMBOLNAME')     # full name e.g. "Cholamandalam... Ltd."
            transtype  = cell(row, 'TRANSTYPE')       # "Buy" / "Sell"
            qty        = flt(row, 'QUANTITY')
            rate       = flt(row, 'RATE')
            rateflag   = cell(row, 'RATEFLAG')        # "Limit" / "MKT"
            broker_acid = cell(row, 'BROKERACID')     # mapin / pool account (e.g. GWPJ0008)
            order_date = cell(row, 'ORDERDATE')
            order_id   = cell(row, 'ORDERID')
            client_id  = cell(row, 'CLIENTID')        # numeric WS client ID
            client_name = cell(row, 'CLIENTNAME')
            pool_name  = cell(row, 'POOLNAME')
            scheme_name = cell(row, 'SCHEMENAME')
            broker_code = cell(row, 'BROKERCODE')

            if not isin or qty <= 0:
                skipped += 1
                continue

            # Normalise TRANSTYPE to the internal P/S codes the engine expects
            # (same convention as the old TradeTrans TRXN_TYPE field)
            trxn_type_internal = 'P' if transtype.lower() == 'buy' else 'S'

            # BROKERACID is the mapin directly — no CBD lookup needed.
            # We store it as CLIENT_CODE so the engine's cbd_client_map fallback
            # (client_code used as-is when not found in CBD) returns the mapin.
            order_dict = {
                # Fields expected by the trade recon engine
                'CLIENT_CODE':     broker_acid,      # mapin / pool account
                'ISINCODE':        isin,
                'TRXN_TYPE':       trxn_type_internal,
                'QUANTITY':        qty,
                'TRAN_PRICE':      rate,
                'INSTRUMENT_CODE': symbol_code,
                'INSTRUMENT_NAME': symbol_name,
                'TRANS_ID':        order_id,
                # Extra fields (for logging, display, future use)
                'ORDER_DATE':      order_date,
                'ORDER_ID':        order_id,
                'CLIENT_ID':       client_id,
                'CLIENT_NAME':     client_name,
                'SYMBOL_CODE':     symbol_code,
                'SYMBOL_NAME':     symbol_name,
                'TRANSTYPE':       transtype,         # original "Buy"/"Sell"
                'RATEFLAG':        rateflag,
                'BROKER_ACID':     broker_acid,
                'BROKER_CODE':     broker_code,
                'POOL_NAME':       pool_name,
                'SCHEME_NAME':     scheme_name,
            }
            result.orders.append(order_dict)

            # Build ISIN → instrument_code map
            if isin and symbol_code and isin not in result.isin_to_instr:
                result.isin_to_instr[isin] = symbol_code

        result.total_rows  = total
        result.skipped_rows = skipped

        if not result.orders:
            result.warnings.append(
                f"No Authorized orders found in {path.name} "
                f"({total} rows total, {skipped} non-Authorized)."
            )

        logger.info(
            "OrderLogParser: %s → %d authorized orders, %d skipped, %d ISIN mappings",
            path.name, len(result.orders), skipped, len(result.isin_to_instr)
        )
        return result


def detect_order_log_format(headers: list) -> bool:
    """Return True if the given header list looks like a Z13_OrderLog file."""
    hdr_upper = {str(h or '').strip().upper() for h in headers}
    return 'ORDERID' in hdr_upper and 'BROKERACID' in hdr_upper
