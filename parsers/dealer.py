"""
Dealer Trade File Parser
------------------------
Parses the dealer's daily trade grid (Excel).
Sent from transactions@thegoldstandard.in to operations@thegoldstandard.in.

Columns (11):
  Create Time (As of) | Account | Side | Brkr Code | Name | Security |
  Qty | LmtPx | FillQty | AvgPx | ISIN

Key rules:
  - LmtPx = 0 or blank → market order (skip price check)
  - FillQty <= Qty always
  - Side: 'Buy' → BY-, 'Sell' → SL+
"""
import logging
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DealerTrade:
    create_time:  str
    account:      str    # dealer pool account (e.g. GSWP_WEVA_ICICI)
    side:         str    # 'Buy' or 'Sell'
    brkr_code:    str    # dealer short broker code (e.g. EQRS)
    name:         str    # security name
    security:     str    # dealer ticker symbol
    qty:          float  # ordered quantity
    lmt_px:       float  # limit price (0 = market)
    fill_qty:     float  # filled quantity
    avg_px:       float  # average fill price (before brokerage)
    isin:         str
    trade_date:   str = ''  # extracted from create_time (DD/MM/YYYY)
    mapin:        str = ''  # filled in by engine via Pool_Map

    @property
    def is_market_order(self) -> bool:
        return self.lmt_px == 0.0

    @property
    def is_partially_filled(self) -> bool:
        return self.fill_qty < self.qty

    @property
    def transaction_type(self) -> str:
        return 'BY-' if self.side.lower() == 'buy' else 'SL+'


@dataclass
class DealerParseResult:
    trades:   List[DealerTrade] = field(default_factory=list)
    error:    str = ''
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


class DealerParser:
    """Parse the dealer's daily Excel trade grid."""

    # Expected column headers (case-insensitive, spaces stripped)
    EXPECTED_COLS = {
        'createtime': 'create_time',
        'createtimeasof': 'create_time',
        'account': 'account',
        'side': 'side',
        'brkrcode': 'brkr_code',
        'name': 'name',
        'security': 'security',
        'qty': 'qty',
        'lmtpx': 'lmt_px',
        'fillqty': 'fill_qty',
        'avgpx': 'avg_px',
        'isin': 'isin',
    }

    def parse_file(self, file_path: str) -> DealerParseResult:
        result = DealerParseResult()
        ext = Path(file_path).suffix.lower()
        try:
            if ext == '.csv':
                rows = self._read_csv(file_path)
            else:
                import openpyxl
                wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
                ws = wb[wb.sheetnames[0]]
                rows = list(ws.iter_rows(values_only=True))
                wb.close()
        except Exception as e:
            result.error = f"Could not open dealer file: {e}"
            return result

        if not rows:
            result.error = "Dealer file is empty"
            return result

        # Find header row — first row with 'Account' or 'ISIN'
        header_idx = None
        for i, row in enumerate(rows):
            row_norm = [str(c or '').strip().lower().replace(' ', '').replace('(', '').replace(')', '')
                        for c in row]
            if 'account' in row_norm and 'isin' in row_norm:
                header_idx = i
                break

        if header_idx is None:
            result.error = "Could not find header row (expected 'Account' and 'ISIN' columns)"
            return result

        headers = [str(c or '').strip().lower().replace(' ', '').replace('(', '').replace(')', '')
                   for c in rows[header_idx]]
        col_map = {}
        for i, h in enumerate(headers):
            # Handle 'createtimeasof' → create_time
            h_clean = h.replace('asof', '').strip() if 'createtime' in h else h
            if h_clean in self.EXPECTED_COLS:
                col_map[self.EXPECTED_COLS[h_clean]] = i
            elif h in self.EXPECTED_COLS:
                col_map[self.EXPECTED_COLS[h]] = i

        required = ['account', 'side', 'brkr_code', 'isin', 'fill_qty', 'avg_px']
        missing = [r for r in required if r not in col_map]
        if missing:
            result.error = f"Dealer file missing columns: {missing}"
            return result

        def get(row, field_name, default=''):
            idx = col_map.get(field_name)
            if idx is None or idx >= len(row):
                return default
            return row[idx]

        def flt(v, default=0.0):
            try:
                return float(v) if v not in (None, '', 'None') else default
            except (TypeError, ValueError):
                return default

        for row in rows[header_idx + 1:]:
            if not row or all(c is None for c in row):
                continue

            account  = str(get(row, 'account', '') or '').strip()
            isin     = str(get(row, 'isin', '') or '').strip()
            if not account or not isin:
                continue

            create_time = str(get(row, 'create_time', '') or '').strip()
            trade_date  = _extract_date(create_time)

            trade = DealerTrade(
                create_time = create_time,
                account     = account,
                side        = str(get(row, 'side', '') or '').strip(),
                brkr_code   = str(get(row, 'brkr_code', '') or '').strip(),
                name        = str(get(row, 'name', '') or '').strip(),
                security    = str(get(row, 'security', '') or '').strip(),
                qty         = flt(get(row, 'qty', 0)),
                lmt_px      = flt(get(row, 'lmt_px', 0)),
                fill_qty    = flt(get(row, 'fill_qty', 0)),
                avg_px      = flt(get(row, 'avg_px', 0)),
                isin        = isin,
                trade_date  = trade_date,
            )
            result.trades.append(trade)

        if not result.trades:
            result.error = "No trade rows found in dealer file"

        logger.info(f"Dealer parser: {len(result.trades)} trades from {Path(file_path).name}")
        return result

    def _read_csv(self, file_path: str):
        """Read a CSV file and return rows as list of tuples (same as openpyxl)."""
        rows = []
        # Try common encodings
        for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
            try:
                with open(file_path, newline='', encoding=enc) as f:
                    reader = csv.reader(f)
                    for row in reader:
                        rows.append(tuple(c if c != '' else None for c in row))
                return rows
            except (UnicodeDecodeError, Exception):
                continue
        raise ValueError(f"Could not decode CSV file with any known encoding")


def _extract_date(create_time: str) -> str:
    """
    Extract DD/MM/YYYY from a create_time string like '11:12 18/03/2026'
    or '2026-03-18 11:12:00'.
    """
    import re
    if not create_time:
        return ''
    from datetime import datetime
    # DD/MM/YYYY
    m = re.search(r'(\d{1,2}/\d{2}/\d{4})', create_time)
    if m:
        return m.group(1).zfill(10)  # pad day if single-digit
    # YYYY-MM-DD
    m = re.search(r'(\d{4}-\d{2}-\d{2})', create_time)
    if m:
        try:
            return datetime.strptime(m.group(1), '%Y-%m-%d').strftime('%d/%m/%Y')
        except ValueError:
            pass
    # DD-MMM-YYYY (e.g. 23-MAR-2026)
    m = re.search(r'(\d{1,2}-[A-Za-z]{3}-\d{4})', create_time)
    if m:
        try:
            return datetime.strptime(m.group(1), '%d-%b-%Y').strftime('%d/%m/%Y')
        except ValueError:
            pass
    # DD-MM-YYYY
    m = re.search(r'(\d{2}-\d{2}-\d{4})', create_time)
    if m:
        try:
            return datetime.strptime(m.group(1), '%d-%m-%Y').strftime('%d/%m/%Y')
        except ValueError:
            pass
    return ''
