"""
Exchange Trade File Parser — NSE / BSE
----------------------------------------
Parses the exchange-provided trade data files (one per exchange per day).
Files arrive late at night by email (post-facto verification layer).

Both NSE and BSE files are password-protected XLS files:
  password = AALCG4181G (same as other custodian files)

When uploaded as PDFs (printed from password-protected XLS), the text
is CID-encoded but the table structure is parseable.

Exchange file columns (12):
  Sr No | Party Description | Member/Exchange Code | Segment |
  Security Name | Client/Member Code | Buy/Sell |
  Trade No | Trade Time (DD/MM/YY hH) | Quantity | Price | Trade Value

The post-facto check aggregates by ISIN × Side × day and compares
totals to the dealer file.
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class ExchangeTrade:
    sr_no:        str
    party:        str    # participant description
    member_code:  str    # exchange member code
    segment:      str
    security:     str    # security name / symbol (may contain ISIN prefix)
    isin:         str    # extracted ISIN code (INE...)
    client_code:  str    # UCC or member code
    side:         str    # 'Buy' or 'Sell'
    trade_no:     str    # unique trade ID from exchange
    trade_time:   str    # timestamp
    trade_date:   str    # DD/MM/YYYY extracted from trade_time
    qty:          float
    price:        float
    trade_value:  float
    exchange:     str = 'NSE'  # NSE or BSE


@dataclass
class ExchangeParseResult:
    trades:   List[ExchangeTrade] = field(default_factory=list)
    exchange: str = ''
    error:    str = ''
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


class ExchangeFileParser:
    # Password for NSE/BSE trade confirmation PDFs and encrypted XLS files
    EXCHANGE_PDF_PASSWORD = 'AALCG4181G'

    """Parse NSE or BSE daily trade files (XLS or PDF print)."""

    def parse_file(self, file_path: str, exchange: str = '') -> ExchangeParseResult:
        """
        Auto-detect format (XLS or PDF) and parse accordingly.
        exchange: 'NSE' or 'BSE' — if blank, inferred from filename.
        """
        result = ExchangeParseResult()
        path   = Path(file_path)
        ext    = path.suffix.lower()

        # Infer exchange from filename if not specified
        if not exchange:
            fn = path.name.upper()
            exchange = 'BSE' if 'BSE' in fn else 'NSE'
        result.exchange = exchange

        if ext in ('.xls', '.xlsx'):
            self._parse_xls(file_path, exchange, result)
        elif ext == '.pdf':
            self._parse_pdf(file_path, exchange, result)
        else:
            result.error = f"Unsupported exchange file format: {ext}"


        # Stamp file-level date on trades with empty trade_date.
        # Exchange rows often carry time-only (09:50:26) in the trade_time column,
        # so _extract_date returns '' — but the filename encodes the date.
        # Without this, stale trades from old files all pass the date filter
        # because the filter treats empty trade_date as 'today'.
        _file_date = _extract_date_from_filename(file_path)
        if _file_date:
            for et in result.trades:
                if not et.trade_date:
                    et.trade_date = _file_date

        return result

    # ── XLS parsing ──────────────────────────────────────────────────────── #

    def _parse_xls(self, file_path: str, exchange: str, result: ExchangeParseResult):
        """Parse password-protected XLS (needs 7-Zip or msoffcrypto)."""
        try:
            import pandas as pd
            ext = Path(file_path).suffix.lower()
            engine = 'xlrd' if ext == '.xls' else 'openpyxl'
            df = pd.read_excel(file_path, engine=engine, header=None)
            self._process_dataframe(df, exchange, result)
        except Exception as e:
            # Try with password decryption
            try:
                import msoffcrypto, io, xlrd, pandas as pd
                PASSWORD = 'AALCG4181G'
                with open(file_path, 'rb') as f:
                    office_file = msoffcrypto.OfficeFile(f)
                    office_file.load_key(password=PASSWORD)
                    decrypted = io.BytesIO()
                    office_file.decrypt(decrypted)
                decrypted.seek(0)
                df = pd.read_excel(decrypted, engine='xlrd', header=None)
                self._process_dataframe(df, exchange, result)
            except Exception as e2:
                result.error = f"Cannot parse XLS (try uploading as PDF): {e} | {e2}"

    def _process_dataframe(self, df, exchange: str, result: ExchangeParseResult):
        """Find header row and extract trades from dataframe."""
        import pandas as pd

        # Find header row — look for 'Buy' or 'Qty' or 'Trade No'
        header_idx = None
        for i, row in df.iterrows():
            row_s = ' '.join(str(c).lower() for c in row if pd.notna(c))
            if ('buy' in row_s and 'qty' in row_s) or 'trade no' in row_s:
                header_idx = i
                break
        if header_idx is None:
            # Try first row as header
            header_idx = 0

        df.columns = [str(c).strip() for c in df.iloc[header_idx]]
        df = df.iloc[header_idx + 1:].reset_index(drop=True)

        for _, row in df.iterrows():
            trade = self._row_to_trade(row, df.columns, exchange)
            if trade:
                result.trades.append(trade)

    def _row_to_trade(self, row, columns, exchange: str) -> 'ExchangeTrade | None':
        """Map a dataframe row to an ExchangeTrade."""
        import pandas as pd

        def get(keywords):
            for col in columns:
                if any(kw in col.lower() for kw in keywords):
                    v = row.get(col)
                    return '' if pd.isna(v) else str(v).strip()
            return ''

        def flt(keywords, default=0.0):
            for col in columns:
                if any(kw in col.lower() for kw in keywords):
                    v = row.get(col)
                    if pd.notna(v):
                        try:
                            return float(str(v).replace(',', ''))
                        except ValueError:
                            pass
            return default

        sr_no     = get(['sr', 'sno', 'no.'])
        trade_no  = get(['trade no', 'tradeno'])
        qty       = flt(['qty', 'quantity'])
        price     = flt(['price', 'rate'])

        if not trade_no and (qty == 0 or price == 0):
            return None

        side_raw = get(['buy', 'side', 'b/s'])
        side     = 'Sell' if side_raw.lower() in ('s', 'sell', 'sl+') else 'Buy'

        trade_time = get(['time', 'trade time'])
        trade_date = _extract_date(trade_time)

        sec_raw = get(['security', 'name', 'symbol', 'isin'])
        # Extract ISIN from security field — NSE format: "INE121A01024 CIFC" or just ISIN
        isin_match = re.search(r'(IN[A-Z0-9]{10})', sec_raw)
        isin_val   = isin_match.group(1) if isin_match else get(['isin'])
        # Clean security name — remove ISIN prefix if present
        sec_name   = re.sub(r'^IN[A-Z0-9]{10}\s*', '', sec_raw).strip() or sec_raw

        return ExchangeTrade(
            sr_no       = sr_no,
            party       = get(['party', 'description', 'participant']),
            member_code = get(['member code', 'exchange code']),
            segment     = get(['segment']),
            security    = sec_name,
            isin        = isin_val,
            client_code = get(['client', 'ucc']),
            side        = side,
            trade_no    = trade_no,
            trade_time  = trade_time,
            trade_date  = trade_date,
            qty         = qty,
            price       = price,
            trade_value = flt(['value', 'trade value']),
            exchange    = exchange,
        )

    # ── PDF parsing (CID-encoded print) ──────────────────────────────────── #

    def _parse_pdf(self, file_path: str, exchange: str, result: ExchangeParseResult):
        """
        Parse password-protected NSE/BSE trade confirmation PDFs.
        Password: AALCG4181G. Tries line-based table extraction first,
        falls back to text/word extraction for whitespace-layout PDFs.
        """
        try:
            import pdfplumber
        except ImportError:
            result.error = "pdfplumber not installed"
            return

        try:
            all_rows = []
            # Try with known password first, then without (some PDFs not encrypted)
            for open_kwargs in [{'password': self.EXCHANGE_PDF_PASSWORD}, {}]:
                try:
                    with pdfplumber.open(file_path, **open_kwargs) as pdf:
                        for page in pdf.pages:
                            tables = page.extract_tables(
                                {'vertical_strategy': 'lines', 'horizontal_strategy': 'lines'}
                            )
                            if tables:
                                for table in tables:
                                    for row in table:
                                        all_rows.append([str(c or '').strip() for c in row])
                            else:
                                text = page.extract_text()
                                if text:
                                    for line in text.split('\n'):
                                        parts = line.split()
                                        if len(parts) >= 6:
                                            all_rows.append(parts)
                    break  # opened successfully
                except Exception:
                    all_rows = []
                    continue

            if not all_rows:
                result.warnings.append(
                    f"No data extracted from exchange PDF (possibly wrong password "
                    f"or unsupported format): {Path(file_path).name}"
                )
                return

            # Find first data row with >=8 non-empty cells
            header_idx = 0
            for i, row in enumerate(all_rows):
                if sum(1 for c in row if len(c) > 2) >= 8:
                    header_idx = i
                    break

            for row in all_rows[header_idx:]:
                if len(row) < 6:
                    continue
                trade = self._parse_pdf_row(row, exchange)
                if trade:
                    result.trades.append(trade)

        except Exception as e:
            result.error = f"Exchange PDF parse error: {e}"

    def _parse_pdf_row(self, cells: list, exchange: str) -> 'ExchangeTrade | None':
        """
        Parse one row from the exchange PDF table.
        The table structure is consistent (12 cols) even though text is CID-encoded.
        We use position + numeric pattern matching.
        """
        if len(cells) < 10:
            return None

        def is_num(s):
            try:
                float(s.replace(',', ''))
                return True
            except ValueError:
                return False

        def to_flt(s):
            try:
                return float(s.replace(',', ''))
            except (ValueError, AttributeError):
                return 0.0

        # Position-based extraction (standard 12-col layout)
        # Verify this is a data row: col 0 should be a serial number (numeric)
        if not cells[0].isdigit() and not re.match(r'^\d+$', cells[0]):
            return None

        # Col 6 (index) = Buy/Sell indicator
        # In CID files it shows as a numeric buy/sell code; in clean files as Buy/Sell
        # We identify side from context: a row with price around 30-10000 range
        # and trade_no as a long numeric string
        trade_no   = cells[7] if len(cells) > 7 else ''
        trade_time = cells[8] if len(cells) > 8 else ''

        # Skip header-like rows (non-numeric quantities)
        qty_raw   = cells[9]  if len(cells) > 9  else '0'
        price_raw = cells[10] if len(cells) > 10 else '0'
        val_raw   = cells[11] if len(cells) > 11 else '0'

        if not is_num(qty_raw) or not is_num(price_raw):
            return None

        qty   = to_flt(qty_raw)
        price = to_flt(price_raw)
        val   = to_flt(val_raw)

        if qty <= 0 or price <= 0:
            return None

        # Side — cell 6: try to detect Buy/Sell
        side_raw = cells[6] if len(cells) > 6 else ''
        side = 'Sell' if side_raw.lower() in ('s', 'sell', 'sl+') else 'Buy'
        # For CID files the side column may be garbled — price context won't help.
        # Leave as 'Buy' default; the engine will try to reconcile both ways.

        trade_date = _extract_date(trade_time)

        sec_raw  = cells[4] if len(cells) > 4 else ''
        # NSE PDFs embed ISIN in cell 4: "INE121A01024 CIFC" or just the symbol
        isin_m   = re.search(r'(IN[A-Z0-9]{10})', sec_raw)
        isin_val = isin_m.group(1) if isin_m else ''
        sec_name = re.sub(r'^IN[A-Z0-9]{10}\s*', '', sec_raw).strip() or sec_raw

        return ExchangeTrade(
            sr_no       = cells[0],
            party       = cells[1] if len(cells) > 1 else '',
            member_code = cells[2] if len(cells) > 2 else '',
            segment     = cells[3] if len(cells) > 3 else '',
            security    = sec_name,
            isin        = isin_val,
            client_code = cells[5] if len(cells) > 5 else '',
            side        = side,
            trade_no    = trade_no,
            trade_time  = trade_time,
            trade_date  = trade_date,
            qty         = qty,
            price       = price,
            trade_value = val,
            exchange    = exchange,
        )


# ── Exchange aggregator ───────────────────────────────────────────────────── #

def aggregate_exchange_trades(trades: List[ExchangeTrade]) -> dict:
    """
    Aggregate exchange trades by (isin_or_security, side).
    Key uses ISIN when available so it can match against dealer ISIN.
    Falls back to security name when ISIN is blank.
    Returns {(isin_or_security, side): {qty, avg_px, trade_count, isin, security}}
    """
    from collections import defaultdict
    agg = defaultdict(lambda: {'qty': 0.0, 'value': 0.0, 'count': 0, 'isin': '', 'security': ''})
    for t in trades:
        # Prefer ISIN as key so Check 5 can match against dealer ISIN
        key_val = t.isin if t.isin else t.security
        key = (key_val, t.side)
        agg[key]['qty']      += t.qty
        agg[key]['value']    += t.trade_value or (t.qty * t.price)
        agg[key]['count']    += 1
        agg[key]['isin']      = t.isin or agg[key]['isin']
        agg[key]['security']  = t.security or agg[key]['security']

    result = {}
    for key, v in agg.items():
        result[key] = {
            'qty':         v['qty'],
            'avg_px':      round(v['value'] / v['qty'], 4) if v['qty'] > 0 else 0,
            'trade_count': v['count'],
            'isin':        v['isin'],
            'security':    v['security'],
        }
    return result


# ── Helpers ───────────────────────────────────────────────────────────────── #

def _extract_date_from_filename(filepath: str) -> str:
    """Extract DD/MM/YYYY date from exchange filenames like G587_23032026_xxx.xls"""
    name = Path(filepath).stem
    # DDMMYYYY: G587_23032026_PAN.xls  →  23/03/2026
    m = re.search(r'[_\-](\d{2})(\d{2})(\d{4})(?:[_\-\.]|$)', name)
    if m:
        d, mo, y = m.group(1), m.group(2), m.group(3)
        if 1 <= int(d) <= 31 and 1 <= int(mo) <= 12 and int(y) >= 2020:
            return f"{d}/{mo}/{y}"
    # YYYYMMDD: nse_alert_20260323.xls  →  23/03/2026
    m = re.search(r'[_\-](\d{4})(\d{2})(\d{2})(?:[_\-\.]|$)', name)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if 1 <= int(d) <= 31 and 1 <= int(mo) <= 12 and int(y) >= 2020:
            return f"{d}/{mo}/{y}"
    return ''


def _extract_date(s: str) -> str:
    """Extract DD/MM/YYYY from trade time strings like '11:12 18/03/2026'."""
    if not s:
        return ''
    m = re.search(r'(\d{2}/\d{2}/\d{4})', s)
    if m:
        return m.group(1)
    m = re.search(r'(\d{2}/\d{2}/\d{2})', s)   # DD/MM/YY
    if m:
        parts = m.group(1).split('/')
        try:
            yr = int(parts[2])
            yr = yr + 2000 if yr < 100 else yr
            return f"{parts[0]}/{parts[1]}/{yr}"
        except (IndexError, ValueError):
            pass
    return ''
