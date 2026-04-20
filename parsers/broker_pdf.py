"""
Broker PDF Contract Note Parser
---------------------------------
Parses broker contract notes received by email.

Strategy:
1. Try structural extraction with pdfplumber (works for well-formatted PDFs
   like Equirus that embed proper text).
2. If quality is low (garbled CID encoding), fall back to Claude API vision
   which can read the PDF as an image.

Extracted fields per contract note:
  cn_no, trade_date, settlement_date, isin, security_name, side,
  qty, wap (price), brokerage_per_share, stt_total, ucc/mapin
"""
import io
import json
import logging
import os
import re
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Anthropic API endpoint
ANTHROPIC_API = 'https://api.anthropic.com/v1/messages'
CLAUDE_MODEL   = 'claude-sonnet-4-20250514'


# STT is printed two different ways on CNs:
#   - long form "Securities Transaction Tax ... 235.00"  (Equirus / IIFL / Haitong)
#   - short form "STT 235.00" on its own line           (Emkay)
# This combined regex matches either; group 1 OR group 2 holds the amount.
_STT_RE = re.compile(
    r'(?:Securit\w*\s+Tr[xa]\w*\s+Tax[^0-9]*([\d,]+\.?\d*))'
    r'|(?:(?:^|\n)\s*STT\s+([\d,]+\.?\d*))',
    re.IGNORECASE,
)


def _parse_stt_amount(ctx: str) -> float:
    """Extract STT from a CN's tax/levy section context. Returns 0.0 if absent.

    Handles both "Securities Transaction Tax" long form and the bare "STT"
    line label that Emkay's contract note uses. Silently degrades on parse
    failure so upstream callers always get a clean float.
    """
    if not ctx:
        return 0.0
    m = _STT_RE.search(ctx)
    if not m:
        return 0.0
    raw = m.group(1) or m.group(2) or ''
    try:
        return float(raw.replace(',', ''))
    except (TypeError, ValueError):
        return 0.0


# ── Data classes ──────────────────────────────────────────────────────────── #

@dataclass
class ContractNoteTrade:
    """One trade line within a contract note."""
    isin:                str
    security_name:       str
    side:                str    # 'Buy' or 'Sell'
    qty:                 float
    wap:                 float  # weighted average price (before brokerage)
    brokerage_per_share: float
    total_value:         float  # gross trade value
    exchange:            str = 'NSE'
    stt_total:           float = 0.0  # total STT in ₹ as read directly from the CN


@dataclass
class ContractNote:
    """One contract note (one PDF page or one CN block)."""
    cn_no:           str
    trade_date:      str   # DD/MM/YYYY
    settlement_date: str   # DD/MM/YYYY
    ucc:             str   # UCC / Mapin from the broker's client header
    broker_name:     str
    broker_sebi:     str   # SEBI registration number (used as broker code in 0096)
    trades:          List[ContractNoteTrade] = field(default_factory=list)
    net_amount:      float = 0.0   # Net Amount Receivable (negative=client pays, positive=client receives)
    source_file:     str = ''
    extraction_method: str = ''  # 'pdfplumber' or 'claude_api'


@dataclass
class BrokerPDFResult:
    contract_notes: List[ContractNote] = field(default_factory=list)
    error:          str = ''
    warnings:       List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error


# ── Main parser ───────────────────────────────────────────────────────────── #

class BrokerPDFParser:
    """
    Parse broker contract note PDFs.
    Tries pdfplumber first, falls back to Claude API.
    """

    def parse_file(self, pdf_path: str,
                   broker_sebi_code: str = '',
                   broker_name: str = '',
                   ticker_to_isin: dict = None) -> BrokerPDFResult:
        result = BrokerPDFResult()
        ext = Path(pdf_path).suffix.lower()

        # ── XLS/XLSX/CSV broker block deal files (Nuvama, IIFL, Equirus 0096) ────
        if ext in ('.xls', '.xlsx', '.csv'):
            cns = _extract_from_xls(pdf_path, broker_sebi_code, broker_name,
                                    ticker_to_isin=ticker_to_isin)
            if cns:
                for cn in cns:
                    cn.source_file = str(pdf_path)
                result.contract_notes = cns
            else:
                result.warnings.append(
                    f"No contract notes extracted from {Path(pdf_path).name} (XLS). "
                    "Columns may not match expected 0096 format."
                )
            return result

        # ── PDF files ─────────────────────────────────────────────────────────────
        try:
            import pdfplumber
        except ImportError:
            result.error = (
                f"pdfplumber not installed — cannot parse {Path(pdf_path).name}. "
                "Run: pip install pdfplumber"
            )
            return result

        try:
            with pdfplumber.open(pdf_path) as pdf:
                full_text = '\n'.join(
                    page.extract_text() or '' for page in pdf.pages
                )
                page_tables = [page.extract_tables() for page in pdf.pages]
        except Exception as e:
            result.error = f"Could not open PDF: {e}"
            return result

        # Detect encoding quality — if >20% of chars are CID codes → use Claude
        cid_count = full_text.count('(cid:')
        total_len  = max(len(full_text), 1)
        cid_ratio  = cid_count * 6 / total_len
        use_claude = cid_ratio > 0.15

        if use_claude:
            logger.info(f"BrokerPDFParser: CID ratio {cid_ratio:.0%} — using Claude API for {Path(pdf_path).name}")
            cns = _extract_via_claude(pdf_path, broker_sebi_code, broker_name)
        else:
            logger.info(f"BrokerPDFParser: using pdfplumber for {Path(pdf_path).name}")
            # Log first 500 chars of extracted text for debugging ICICI-style formats
            _first_500 = full_text[:500].replace('\n', '\\n')
            logger.info(f"BrokerPDFParser: text preview: {_first_500}")
            cns = _extract_via_pdfplumber(full_text, page_tables, broker_sebi_code, broker_name)

        if cns:
            for cn in cns:
                cn.source_file = str(pdf_path)
            result.contract_notes = cns
        elif not use_claude:
            # pdfplumber returned 0 CNs but file is text-readable — try Claude API
            logger.info(f"BrokerPDFParser: pdfplumber extracted 0 CNs, trying Claude API for {Path(pdf_path).name}")
            cns = _extract_via_claude(pdf_path, broker_sebi_code, broker_name)
            if cns:
                for cn in cns:
                    cn.source_file = str(pdf_path)
                result.contract_notes = cns
            else:
                result.warnings.append(
                    f"No contract notes extracted from {Path(pdf_path).name}. "
                    "Manual review may be needed."
                )
        else:
            result.warnings.append(
                f"No contract notes extracted from {Path(pdf_path).name}. "
                "Manual review may be needed."
            )

        return result

# ── XLS/XLSX 0096-format extraction ──────────────────────────────────────── #

def _extract_from_nsdl_format(rows: list,
                               header_idx: int,
                               broker_sebi: str,
                               broker_name: str,
                               file_path: str) -> List[ContractNote]:
    """
    Parse NSDL STeADY format XLS/XLSX (35 columns).
    Used for NUVAMA_EQ_*.XLSX and SRK317CNSTAT*.xls files.

    Key columns:
      ECN No | ECN Date | ISIN Code | Security Name | Transaction Type |
      Sett. Date | Qty | Net Rate | Brokerage Amount | Brokerage Rate |
      SEBI Regn No. | Broker Name | Client Exchange Code/UCC | Scheme Name
    """
    from collections import defaultdict
    from datetime import datetime

    header = [str(c or '').strip().lower() for c in rows[header_idx]]

    def col(names):
        for name in names:
            for i, h in enumerate(header):
                if name.lower() in h:
                    return i
        return None

    C_ECN    = col(['ecn no'])
    C_DATE   = col(['ecn date'])
    C_ISIN   = col(['isin code', 'isin'])
    C_SEC    = col(['security name'])
    C_TXN    = col(['transaction type'])
    C_SETT   = col(['sett. date', 'settlement date'])
    C_QTY    = col(['qty', 'quantity'])
    C_RATE   = col(['net rate', 'rate', 'price'])
    C_BROK   = col(['brokerage amount'])
    C_BROK_R = col(['brokerage rate'])
    C_STT    = col(['service transaction tax', 'stt'])
    C_SEBI   = col(['sebi regn', 'sebi'])
    C_BNAME  = col(['broker name'])
    C_UCC    = col(['client exchange code', 'ucc'])

    def cell(row, c, default=''):
        if c is None or c >= len(row): return default
        v = row[c]
        if v is None: return default
        if hasattr(v, 'strftime'): return v.strftime('%d/%m/%Y')
        return str(v).strip()

    def flt(row, c, default=0.0):
        v = cell(row, c)
        try: return float(str(v).replace(',', '')) if v else default
        except: return default

    # Group rows by ECN No — each ECN = one contract note
    groups = defaultdict(list)
    for row in rows[header_idx + 1:]:
        if not row or all(c is None or str(c).strip() == '' for c in row):
            continue
        ecn = cell(row, C_ECN)
        if not ecn or ecn.lower() in ('nan', 'ecn no', ''):
            continue
        groups[ecn].append(row)

    if not groups:
        return []

    cns = []
    for ecn_no, group_rows in groups.items():
        first = group_rows[0]
        trade_date  = cell(first, C_DATE)
        sett_date   = cell(first, C_SETT)
        ucc         = cell(first, C_UCC)
        b_sebi      = cell(first, C_SEBI) or broker_sebi
        b_name      = cell(first, C_BNAME) or broker_name

        trades = []
        for row in group_rows:
            txn = cell(row, C_TXN).upper()
            side = 'Sell' if txn in ('SELL', 'S', 'SL+', 'SL') else 'Buy'
            qty  = flt(row, C_QTY)
            rate = flt(row, C_RATE)
            brok = flt(row, C_BROK)
            brok_rate = flt(row, C_BROK_R)
            stt  = flt(row, C_STT)
            if qty <= 0:
                continue
            brok_per_share = brok_rate if brok_rate > 0 else (round(brok / qty, 6) if qty > 0 and brok > 0 else 0.0)
            trades.append(ContractNoteTrade(
                isin                = cell(row, C_ISIN),
                security_name       = cell(row, C_SEC),
                side                = side,
                qty                 = qty,
                wap                 = rate,
                brokerage_per_share = brok_per_share,
                total_value         = round(qty * rate, 2),
                exchange            = 'NSE',
                stt_total           = stt,
            ))

        if not trades:
            continue

        cns.append(ContractNote(
            cn_no           = ecn_no,
            trade_date      = trade_date,
            settlement_date = sett_date,
            ucc             = ucc,
            broker_name     = b_name,
            broker_sebi     = b_sebi,
            trades          = trades,
            extraction_method = 'nsdl_xlsx',
            source_file     = str(file_path),
        ))

    logger.info(f"NSDL XLSX parser: {len(cns)} CNs, "
                f"{sum(len(c.trades) for c in cns)} trades from {Path(file_path).name}")
    return cns


def _extract_from_xls(file_path: str,
                       broker_sebi: str,
                       broker_name: str,
                       ticker_to_isin: dict = None) -> List[ContractNote]:
    """
    Parse broker XLS/XLSX files. Detects two formats:

    1. NSDL STeADY format (NUVAMA_EQ_*.XLSX, SRK317CNSTAT*.xls):
       35-column NSDL download with ECN No, ISIN Code, Transaction Type, UCC, etc.
       Each row = one trade from one contract note (grouped by ECN No).

    2. 0096 block deal format (NUVAMA_0096_*.xls, GOLDSTANDARD_*.XLS):
       Broker Code | Dummy | Script Code | Exchange | Txn Type | ...
       Each row = one trade. Grouped by (Mapin, Trade Date, Remarks/CN No).
    """
    rows = _read_xls_rows(file_path)
    if not rows:
        return []
    if ticker_to_isin is None:
        ticker_to_isin = {}

    # ── Structural execution-report guard ────────────────────────────────
    # Exchange execution/fill reports (SAUDA, trade blotter) contain BOTH
    # an OrderNo column AND a TradeNo column because they record individual
    # exchange fills, not consolidated contract notes.
    # A real CN file (from any broker) only has order-level or CN-level rows
    # and will never expose raw TradeNo in its settlement confirmation.
    # Detect and reject early — these files must not enter CN parsing.
    _hdr0 = [str(v).upper().replace(' ', '').replace('_', '') for v in rows[0]]
    _has_order_no = any('ORDERNO' in h or h == 'ORDERDATETIME' for h in _hdr0)
    _has_trade_no = any('TRADENO' in h or h == 'TRADEDATETIME' for h in _hdr0)
    if _has_order_no and _has_trade_no:
        logger.info(
            f'Skipping {Path(file_path).name}: detected as exchange execution report '
            f'(has both OrderNo and TradeNo columns — not a contract note)'
        )
        return []

    # Detect format by checking header content
    # NSDL format has 'ECN No' and 'ISIN Code' in its header row
    for i, row in enumerate(rows[:10]):
        row_s = ' '.join(str(c or '').lower() for c in row)
        if 'ecn no' in row_s and 'isin code' in row_s:
            logger.info(f"Detected NSDL STeADY format in {Path(file_path).name}")
            return _extract_from_nsdl_format(rows, i, broker_sebi, broker_name, file_path)

    # Fall through to 0096 format

    # Find header row
    header_idx = None
    for i, row in enumerate(rows):
        row_s = ' '.join(str(c or '').lower() for c in row)
        if ('broker' in row_s and 'script' in row_s) or \
           ('mapin' in row_s) or \
           ('quantity' in row_s and 'price' in row_s):
            header_idx = i
            break

    if header_idx is None:
        # No header found — try treating first non-empty row as data
        # using positional columns matching the standard 0096 format
        header_idx = 0

    headers = [str(c or '').strip().lower() for c in rows[header_idx]]

    def find_col(keywords):
        for kw in keywords:
            for i, h in enumerate(headers):
                if kw in h:
                    return i
        return None

    col_broker   = find_col(['broker'])
    col_script   = find_col(['script', 'security code', 'isin'])
    col_exchange = find_col(['exchange'])
    col_txn      = find_col(['txn', 'transaction type', 'buy/sell', 'side'])
    col_date     = find_col(['trade date', 'transaction date', 'date'])
    col_sett     = find_col(['settlement date', 'sett'])
    col_qty      = find_col(['quantity', 'qty'])
    col_price    = find_col(['price', 'rate', 'wap'])
    col_brok     = find_col(['brokerage', 'brok'])
    col_stt      = find_col(['stt', 'transaction tax'])
    col_mapin    = find_col(['mapin', 'ucc', 'client'])
    col_remarks  = find_col(['remarks', 'cn', 'contract'])

    def get(row, col, default=''):
        if col is None or col >= len(row):
            return default
        v = row[col]
        if v is None:
            return default
        # Handle datetime objects from openpyxl/xlrd directly
        if hasattr(v, 'strftime'):
            return v.strftime('%d/%m/%Y')
        return str(v).strip()

    def flt(row, col, default=0.0):
        v = get(row, col)
        try:
            return float(str(v).replace(',', '')) if v else default
        except (ValueError, TypeError):
            return default

    # Group rows into contract notes by (mapin, trade_date, cn_no)
    from collections import defaultdict
    groups = defaultdict(list)

    for row in rows[header_idx + 1:]:
        if not row or all(c is None or str(c).strip() == '' for c in row):
            continue
        qty = flt(row, col_qty)
        if qty <= 0:
            continue

        mapin      = get(row, col_mapin)
        trade_date = _normalise_date(get(row, col_date))
        cn_no      = get(row, col_remarks) or 'UNKNOWN'
        key        = (mapin, trade_date, cn_no)
        groups[key].append(row)

    if not groups:
        return []

    cns = []
    for (mapin, trade_date, cn_no), group_rows in groups.items():
        trades = []
        for row in group_rows:
            txn_raw = get(row, col_txn, 'BY-').upper()
            side    = 'Sell' if txn_raw in ('SL+', 'S', 'SELL') else 'Buy'
            qty     = flt(row, col_qty)
            price   = flt(row, col_price)
            brok    = flt(row, col_brok)
            stt     = flt(row, col_stt)
            script  = get(row, col_script)
            exch    = get(row, col_exchange) or 'NSE'

            _isin = script if (len(script) == 12 and script.startswith('IN')) else ''
            if not _isin and script and ticker_to_isin:
                _isin = ticker_to_isin.get(script.upper(), '')
            trades.append(ContractNoteTrade(
                isin                = _isin,
                security_name       = script,
                side                = side,
                qty                 = qty,
                wap                 = price,
                brokerage_per_share = round(brok / qty, 6) if qty > 0 and brok > 1 else brok,
                total_value         = round(qty * price, 2),
                exchange            = exch,
                stt_total           = stt,
            ))

        sebi = get(group_rows[0], col_broker) if col_broker is not None else broker_sebi
        sett = _normalise_date(get(group_rows[0], col_sett)) if col_sett is not None else ''

        cns.append(ContractNote(
            cn_no             = cn_no,
            trade_date        = trade_date,
            settlement_date   = sett,
            ucc               = mapin,
            broker_name       = broker_name,
            broker_sebi       = sebi or broker_sebi,
            trades            = trades,
            extraction_method = 'xls_0096',
        ))

    logger.info(f"XLS 0096 parser: {len(cns)} CN groups, {sum(len(c.trades) for c in cns)} trades from {Path(file_path).name}")
    return cns


def _read_xls_rows(file_path: str) -> list:
    """Read all rows from XLS or XLSX into a list of tuples."""
    ext = Path(file_path).suffix.lower()
    try:
        if ext == '.xls':
            try:
                import xlrd
                wb = xlrd.open_workbook(file_path)
                ws = wb.sheet_by_index(0)
                return [tuple(ws.cell_value(r, c) for c in range(ws.ncols))
                        for r in range(ws.nrows)]
            except ImportError:
                pass  # fall through to openpyxl
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active
        rows = [tuple(cell for cell in row) for row in ws.iter_rows(values_only=True)]
        wb.close()
        return rows
    except Exception as e:
        logger.warning(f"_read_xls_rows failed for {file_path}: {e}")
        return []


# ── pdfplumber extraction ─────────────────────────────────────────────────── #

def _extract_via_pdfplumber(full_text: str,
                             page_tables: list,
                             broker_sebi: str,
                             broker_name: str) -> List[ContractNote]:
    """
    Structural extraction for clean PDFs (Equirus-style).
    Handles multi-CN PDFs (one CN per 2 pages).
    """
    cns = []

    # Extract SEBI registration number from the full text (lives in the header,
    # not inside individual CN blocks). Covers both:
    #   ICICI:  'SEBI Reg No: INZ000183631'
    #   Motilal: 'SEBIRegn.No.:BSE/NSE/MCX/NCDEX : INZ000158836'
    _sebi_m = (re.search(r'SEBI Reg No[:\s]+(INZ\w+)', full_text, re.IGNORECASE) or
               re.search(r'SEBIRegn\.No\..*?(INZ\w+)', full_text, re.IGNORECASE) or
               re.search(r'\b(INZ\d{9})\b', full_text))
    if _sebi_m:
        broker_sebi = _sebi_m.group(1)

    # Split text into CN blocks.
    # Strategy: try "CONTRACT NOTE CUM TAX INVOICE" first (Equirus-style) —
    # this keeps the client header (UCC, name) together with its trade data.
    # Fall back to "CONTRACT NOTE NO." for ICICI/Motilal where the tax invoice
    # header doesn't repeat per CN.
    cn_blocks = re.split(r'(?=CONTRACT NOTE CUM TAX INVOICE)', full_text, flags=re.IGNORECASE)
    cn_blocks = [b for b in cn_blocks if b.strip()]
    if len(cn_blocks) <= 1:
        # ICICI: "CONTRACT NOTE \n ICICI SECURITIES" — split on header boundary
        cn_blocks = re.split(r'(?=CONTRACT NOTE\s*\n)', full_text, flags=re.IGNORECASE)
        cn_blocks = [b for b in cn_blocks if b.strip()]
    if len(cn_blocks) <= 1:
        cn_blocks = re.split(r'CONTRACT NOTE NO\.?\s*', full_text, flags=re.IGNORECASE)
    if len(cn_blocks) <= 1:
        cn_blocks = re.split(r'(?=TRADE DATE\s)', full_text, flags=re.IGNORECASE)

    # For each block, try to extract a complete CN.
    # ICICI PDFs contain "CONTRACT NOTE NO." TWICE per CN — once in the client
    # header and once in the summary table.  Splitting on that delimiter puts the
    # trade row in block N and the STT/levy section in block N+1.  We extend each
    # block with the following block to let _parse_trade_table find the STT line.
    #
    # HOWEVER, for brokers like Equirus where "CONTRACT NOTE NO." appears only
    # ONCE per CN, each block is already complete.  Blindly extending with
    # next_block then pulls the NEXT CN's entire trade table into the parse,
    # producing phantom duplicate trades.
    #
    # Fix: extend with next_block only when the current block lacks levy markers
    # (Securities Transaction Tax / Total Brokerage) — i.e. it is genuinely
    # incomplete and needs the following block to find the STT section.
    _LEVY_MARKERS = re.compile(
        r'Securities Transaction Tax|Total Brokerage|STT\s*\(|Net Amount',
        re.IGNORECASE
    )
    for i, block in enumerate(cn_blocks):
        if not block.strip():
            continue
        # Only extend when the block looks incomplete (no levy/STT section)
        if _LEVY_MARKERS.search(block):
            parse_text = block           # block is self-contained — don't extend
        else:
            next_block = cn_blocks[i + 1] if i + 1 < len(cn_blocks) else ''
            parse_text = block + next_block
        cn = _parse_cn_block(parse_text, broker_sebi, broker_name)
        if cn and cn.cn_no and cn.trades:
            cn.extraction_method = 'pdfplumber'
            cns.append(cn)
        elif cn and cn.cn_no and not cn.trades:
            logger.warning(f'CN {cn.cn_no} (UCC={cn.ucc}): no trades extracted — '
                           f'block has {len(parse_text)} chars, ISINs in block: '
                           f'{re.findall(r"IN[A-Z0-9]{10}", parse_text)}')

    # If block splitting failed or any CN has empty trades, try table extraction
    _any_empty = any(not cn.trades for cn in cns)
    if not cns or _any_empty:
        _table_cns = _extract_from_tables(page_tables, broker_sebi, broker_name)
        if _table_cns:
            if not cns:
                cns = _table_cns
            else:
                # Merge: for CNs with empty trades, fill from table results
                _table_by_isin = {}
                for tc in _table_cns:
                    for tt in tc.trades:
                        _table_by_isin[tt.isin] = tt
                for cn in cns:
                    if not cn.trades:
                        # Try to find trades from table extraction using ISINs in the block
                        _block_isins = re.findall(r'\b(IN[EF][A-Z0-9]{9})\b', cn._block_text if hasattr(cn, '_block_text') else '')
                        for _bi in _block_isins:
                            if _bi in _table_by_isin:
                                cn.trades.append(_table_by_isin[_bi])
                # Remove CNs still with no trades
                cns = [cn for cn in cns if cn.trades]

    return cns


def _parse_cn_block(text: str, broker_sebi: str, broker_name: str) -> Optional[ContractNote]:
    """Parse a single contract note text block."""

    def find(pattern, default=''):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else default

    def find_float(pattern, default=0.0):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(',', ''))
            except (ValueError, TypeError):
                pass
        return default

    # CN number — handle ICICI (NE26M/0534457) and Motilal (': 778085') formats
    cn_no = (find(r'^[\s:]*([A-Z]{2}\d+[A-Z]/\d+)') or   # NE26M/0534457 (ICICI: 2 letters+digit+letter+slash+digits)
             find(r'^[\s:]*([A-Z]+\d+/\d+)') or            # CNM26/0562323 (IIFL: letters+digits+slash+digits)
             find(r'^[\s:]*(\d{4,})') or                    # 778085, 50333 (4+ digits)
             find(r'Contract Note No[.\s:]+([\w/]+\d+)') or # header label (most brokers)
             find(r'Contract No\s*[:.\s]+([\d/]+\d+)') or  # Nuvama: 'Contract No : 423742'
             find(r'^[\s]*(\w+\d+)'))
    if not cn_no or len(cn_no) < 4:
        return None

    # Trade date — multiple formats:
    #   ICICI:   'Trade Date 23-MAR-2026'
    #   Motilal: 'Trade Date : 23 Mar 2026'
    #   Haitong: 'TRADE DATE Apr 09,2026'
    #   Equirus: 'TRADE DATE 09/04/2026'
    _raw_td = (find(r'Trade Date\s*[:\s]*\s*(\d+-[A-Za-z]+-\d+)') or
               find(r'Trade Date\s*[:\s]*\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})') or
               find(r'TRADE DATE[:\s]+([A-Za-z]+\s+\d{1,2},?\s*\d{4})') or
               find(r'TRADE DATE[:\s]+(\S+)'))
    trade_date  = _normalise_date(_raw_td)
    # Settlement date — match the same set of formats as trade_date:
    #   "15 Apr 2026"   (day month year)
    #   "Apr 15, 2026"  (month day, year — Haitong format)
    #   "Apr 15,2026"   (no space after comma)
    #   "Apr 15 2026"   (no comma)
    # The last \S+ pattern is a greedy fallback but only grabs ONE token,
    # which is why "Apr 15, 2026" was truncating to just "Apr" before.
    _raw_sd = (find(r'Settlement Date\s*[:\s]+\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})') or
               find(r'SETTLEMENT DATE[.:\s]+([A-Za-z]+\s+\d{1,2},?\s*\d{4})') or
               find(r'Settlement Date\s*[:\s]+\s*([A-Za-z]+\s+\d{1,2},?\s*\d{4})') or
               find(r'SETTLEMENT DATE[.:]*\s*(\S+)'))
    settle_date = _normalise_date(_raw_sd)

    # UCC — multiple broker formats
    ucc = (find(r'Unique Client Code of Client[:\s]+(\S+)') or   # ICICI: 25281
           find(r'UCC\s*/\s*MAPIN ID[:\s]+(\S+)') or            # Motilal: HDFC00001816
           find(r'UCC of Client[:\s]+(\S+)') or                   # generic
           find(r'Participant code[:\s]+(\S+)'))

    # SEBI registration number
    sebi = find(r'SEBI Reg(?:n|istration)?(?:\.?\s*No\.?)?[:\s]+(INZ\w+)') or broker_sebi

    # Trade table — look for ISIN + qty + WAP pattern
    trades = _parse_trade_table(text)

    if not trades:
        return None

    # Net settlement amount — various broker formats:
    #   Equirus: "Net Amount Receivable From Client / (payable by Client) (Rs.) -122959.80"
    #   Haitong: "Net amount receivable by Client (Rs.) 94595.21"
    #   Nuvama:  "Net amount receivable by Client (Rs.) -222054.50"
    # Negative = client pays (buy), Positive = client receives (sell)
    _net_amt = (
        find_float(r'Net\s+[Aa]mount\s+[Rr]eceivable\s+(?:From|by)\s+Client[^-\d]*([-]?[\d,]+\.?\d*)', 0.0) or
        find_float(r'Net\s+[Aa]mount\s+[Pp]ayable\s+(?:to|by)\s+Client[^-\d]*([-]?[\d,]+\.?\d*)', 0.0) or
        find_float(r'Net\s+[Aa]mount\s+[Rr]eceivable.*?(-?[\d,]+\.?\d*)', 0.0)
    )

    return ContractNote(
        cn_no           = cn_no,
        trade_date      = trade_date,
        settlement_date = settle_date,
        ucc             = ucc,
        broker_name     = broker_name or find(r'^([A-Z][A-Z ]+(?:PRIVATE|LTD|LIMITED)\b)', ''),
        broker_sebi     = sebi,
        trades          = trades,
        net_amount      = _net_amt,
    )


def _parse_trade_table(text: str) -> List[ContractNoteTrade]:
    """
    Extract trade rows from contract note text.
    Looks for ISIN pattern followed by security name, qty, WAP, brokerage.
    """
    trades = []

    # Match rows like: INE379A01028  ITC HOTELS  500  155.0000  0.1550  155.1550  77577.50
    # Equirus has 5 numbers: qty, WAP, brokerage, WAP-after-brok, total.
    # Other brokers have 4: qty, WAP, brokerage, total.
    # Try 5-number match first (Equirus), fall back to 4-number.
    isin_pattern_5 = re.compile(
        r'(IN[A-Z0-9]{10})\s+'          # ISIN
        r'(.+?)\s+'                       # Security name (non-greedy)
        r'(\d[\d,]*)\s+'                  # Quantity
        r'(\d[\d,]*\.\d+)\s+'            # WAP
        r'(\d[\d,]*\.\d+)\s+'            # Brokerage per share
        r'(\d[\d,]*\.\d+)\s+'            # WAP after brokerage (skip)
        r'(\d[\d,]*\.\d+)',              # Total value (actual)
        re.IGNORECASE
    )
    isin_pattern = re.compile(
        r'(IN[A-Z0-9]{10})\s+'          # ISIN
        r'(.+?)\s+'                       # Security name (non-greedy)
        r'(\d[\d,]*)\s+'                  # Quantity
        r'(\d[\d,]*\.\d+)\s+'            # WAP
        r'(\d[\d,]*\.\d+)\s+'            # Brokerage per share
        r'(\d[\d,]*\.\d+)',              # Total value
        re.IGNORECASE
    )

    # ── Pass 0: Motilal format ─────────────────────────────────────────────────
    # Format: ISIN - NAME Side qty gross_price total brok_rate brok_total net_rate net_total
    # 'Buy'/'Sell' appears explicitly between name and quantity.
    motilal_pattern = re.compile(
        r'(IN[A-Z0-9]{10})\s*-?\s*'          # ISIN
        r'((?:(?!Buy|Sell).)+?)\s+'             # Name (stops before side keyword)
        r'(Buy|Sell)\s+'                        # Side (explicit)
        r'([\d,]+)\s+'                          # Quantity
        r'([\d,]+\.\d+)\s+'                    # Gross price / WAP
        r'([\d,]+\.\d+)\s+'                    # Total amount (skip)
        r'([\d,]+\.\d+)',                       # Brokerage rate per unit
        re.IGNORECASE | re.DOTALL
    )
    for m0 in motilal_pattern.finditer(text):
        _isin0 = m0.group(1).strip()
        if not (_isin0.startswith('INE') or _isin0.startswith('INF')):
            continue
        try:
            sec_name  = m0.group(2).strip().rstrip('-').strip()
            side      = m0.group(3).capitalize()
            qty       = float(m0.group(4).replace(',', ''))
            wap       = float(m0.group(5).replace(',', ''))
            brok_rate = float(m0.group(7).replace(',', ''))
            context0  = text[max(0, m0.start()-50):m0.end()+300]
            stt_raw0  = _parse_stt_amount(context0)
            exch0 = 'BSE' if 'BSE' in context0.upper() and 'NSE' not in context0.upper() else 'NSE'
            trades.append(ContractNoteTrade(
                isin=m0.group(1).strip(), security_name=sec_name, side=side,
                qty=qty, wap=wap, brokerage_per_share=brok_rate,
                total_value=round(qty * wap, 2), exchange=exch0, stt_total=stt_raw0,
            ))
        except (ValueError, ZeroDivisionError): continue

    # ── Pass 1: ISIN-first pattern (most brokers) ─────────────────────────────
    # Log all ISINs visible in the text so we can diagnose missed trades
    _all_isins = re.findall(r'\b(IN[A-Z0-9]{10})\b', text)
    if _all_isins:
        _matched_isins = {t.isin for t in trades}
        _missing = [i for i in set(_all_isins) if i not in _matched_isins]
        if _missing:
            logger.debug(f'_parse_trade_table: ISINs in text but not yet matched: {_missing}')

    # Try 5-number pattern first (Equirus: qty, wap, brok, wap_after_brok, total)
    # then fall back to 4-number pattern (ICICI/others: qty, wap, brok, total)
    # Tuple: (isin, name, qty, wap, brok, wap_after_brok, total, match_obj)
    _pass1_matches = []
    for m5 in isin_pattern_5.finditer(text):
        _pass1_matches.append((m5.group(1), m5.group(2), m5.group(3),
                               m5.group(4), m5.group(5),
                               m5.group(6),   # wap_after_brok
                               m5.group(7), m5))
    _matched_by_5 = {x[0].strip() for x in _pass1_matches}
    for m4 in isin_pattern.finditer(text):
        if m4.group(1).strip() not in _matched_by_5:
            _pass1_matches.append((m4.group(1), m4.group(2), m4.group(3),
                                   m4.group(4), m4.group(5),
                                   '',          # no wap_after_brok
                                   m4.group(6), m4))

    for (_isin_raw, _name_raw, _qty_raw, _wap_raw, _brok_raw,
         _wap_after_raw, _total_raw, m) in _pass1_matches:
        if not (_isin_raw.strip().startswith('INE') or _isin_raw.strip().startswith('INF')):
            continue  # Only valid Indian ISINs (INE/INF)
        if any(t.isin == _isin_raw.strip() for t in trades): continue
        try:
            qty      = float(_qty_raw.replace(',', ''))
            wap      = float(_wap_raw.replace(',', ''))
            brok     = float(_brok_raw.replace(',', ''))
            total    = float(_total_raw.replace(',', ''))
            wap_after = float(_wap_after_raw.replace(',', '')) if _wap_after_raw else 0.0

            # Always build context (used by STT/exchange detection below)
            context = text[max(0, m.start()-200):m.end()+500]

            # Determine side using a 3-tier approach:
            #
            # 1. WAP vs WAP-after-brokerage (most reliable when available):
            #    Buy: WAP-after-brok > WAP (brokerage added to cost)
            #    Sell: WAP-after-brok < WAP (brokerage deducted from proceeds)
            #
            # 2. Net Obligation / Net Amount sign (definitive for the CN):
            #    Negative = buy (client pays), Positive = sell (client receives)
            #
            # 3. Keyword proximity (fallback for other broker formats)

            side = ''

            # Tier 1: WAP comparison
            if wap_after > 0 and wap > 0 and abs(wap_after - wap) > 0.0001:
                side = 'Buy' if wap_after > wap else 'Sell'

            # Tier 2: Net Obligation sign
            if not side:
                _net_obl = re.search(
                    r'(?:Net\s+Obligation|Net\s+Amount\s+Receivable)[^\d-]*([-]?[\d,]+\.\d+)',
                    context, re.IGNORECASE)
                if _net_obl:
                    _net_val = float(_net_obl.group(1).replace(',', ''))
                    side = 'Sell' if _net_val > 0 else 'Buy'

            # Tier 3: keyword proximity
            if not side:
                _post_match = text[m.start():min(len(text), m.end()+100)]
                _buy_near  = bool(re.search(r'\bBUY\b',        _post_match, re.IGNORECASE))
                _sell_near = bool(re.search(r'\bSELL\b|\bSL\+', _post_match, re.IGNORECASE))
                if _sell_near and not _buy_near:
                    side = 'Sell'
                elif _buy_near and not _sell_near:
                    side = 'Buy'
                else:
                    side = 'Buy'

            # Cross-check: if WAP comparison and Net Obligation disagree, log a warning
            if wap_after > 0 and wap > 0 and abs(wap_after - wap) > 0.0001:
                _wap_side = 'Buy' if wap_after > wap else 'Sell'
                if _wap_side != side:
                    logger.warning(
                        f'Side mismatch for {_isin_raw}: WAP says {_wap_side} '
                        f'but Net Obligation says {side} — using WAP')
                    side = _wap_side

            # STT — look nearby for "Securities Transaction Tax"
            # STT — long-form "Securities Transaction Tax" or bare "STT"
            stt_total = _parse_stt_amount(context)

            # Exchange
            exchange = 'BSE' if 'BSE' in context.upper() and 'NSE' not in context.upper() else 'NSE'

            trades.append(ContractNoteTrade(
                isin=m.group(1).strip(),
                security_name=m.group(2).strip(),
                side=side,
                qty=qty,
                wap=wap,
                brokerage_per_share=brok,
                total_value=total,
                exchange=exchange,
                stt_total=stt_total,
            ))
        except (ValueError, ZeroDivisionError):
            continue

    # ── Pass 1.4: ICICI name-first layouts ───────────────────────────────────
    # Must run BEFORE Pass 1.5 — otherwise Pass 1.5's forgiving number-hunt
    # picks up timestamps ("09:50:32" → qty=9) and Pay/Pay Out Obligation
    # before we get a chance to use the exact ICICI name-first match.
    _icici_pattern_p14 = re.compile(
        r'([A-Z][A-Z &.()\-]+?)\s*'
        r'(?:Buy|Sell)\s+'
        r'([\d,]+)\s+'
        r'([\d,]+\.\d+)\s+'
        r'([\d,]+\.\d+)\s+'
        r'([\d,]+\.\d+)\s+'
        r'([\d,]+\.\d+)'
        r'[\s\S]{0,80}?'
        r'(IN[A-Z0-9]{10})',
        re.IGNORECASE
    )
    _icici_concat_p14 = re.compile(
        r'([A-Z][A-Z &.()\-]+?)'
        r'(IN[EF][A-Z0-9]{9})\s+'
        r'[\s\S]{0,80}?'
        r'(?:Buy|Sell)\s+'
        r'([\d,]+)\s+'
        r'([\d,]+\.\d+)\s+'
        r'([\d,]+\.\d+)\s+'
        r'([\d,]+\.\d+)\s+'
        r'([\d,]+\.\d+)',
        re.IGNORECASE
    )
    for m in _icici_concat_p14.finditer(text):
        try:
            isin = m.group(2).strip()
            if not (isin.startswith('INE') or isin.startswith('INF')): continue
            if any(t.isin == isin for t in trades): continue
            sec_name = m.group(1).strip().rstrip('-').strip()
            qty      = float(m.group(3).replace(',', ''))
            wap      = float(m.group(4).replace(',', ''))
            brok     = float(m.group(5).replace(',', ''))
            total    = float(m.group(7).replace(',', ''))
            context  = text[max(0, m.start()-100):m.end()+500]
            side     = 'Sell' if re.search(r'\bSell\b', m.group(0), re.IGNORECASE) else 'Buy'
            stt_raw  = _parse_stt_amount(context)
            exchange = 'BSE' if 'BSE' in context.upper() and 'NSE' not in context.upper() else 'NSE'
            trades.append(ContractNoteTrade(
                isin=isin, security_name=sec_name, side=side, qty=qty, wap=wap,
                brokerage_per_share=brok, total_value=total, exchange=exchange,
                stt_total=stt_raw,
            ))
            logger.info(f'Pass 1.4 ICICI concat: {isin} {sec_name} {side} qty={qty} wap={wap}')
        except (ValueError, IndexError):
            continue
    for m in _icici_pattern_p14.finditer(text):
        try:
            isin = m.group(7).strip()
            if not (isin.startswith('INE') or isin.startswith('INF')): continue
            if any(t.isin == isin for t in trades): continue
            sec_name = m.group(1).strip().rstrip('-').strip()
            qty      = float(m.group(2).replace(',', ''))
            wap      = float(m.group(3).replace(',', ''))
            brok     = float(m.group(4).replace(',', ''))
            total    = float(m.group(6).replace(',', ''))
            context  = text[max(0, m.start()-100):m.end()+500]
            side     = 'Sell' if re.search(r'\bSell\b|\bSL\+', context, re.IGNORECASE) else 'Buy'
            stt_raw  = _parse_stt_amount(context)
            exchange = 'BSE' if 'BSE' in context.upper() and 'NSE' not in context.upper() else 'NSE'
            trades.append(ContractNoteTrade(
                isin=isin, security_name=sec_name, side=side, qty=qty, wap=wap,
                brokerage_per_share=brok, total_value=total, exchange=exchange,
                stt_total=stt_raw,
            ))
            logger.info(f'Pass 1.4 ICICI: {isin} {sec_name} {side} qty={qty} wap={wap}')
        except (ValueError, IndexError):
            continue

    # ── Pass 1.5: Forgiving ISIN extraction (catches SELL-only rows) ─────────
    # Equirus SELL rows: BUY columns are empty, numbers may be on the same
    # line or the next few lines.  Use a 300-char window after the ISIN.
    for _isin_m in re.finditer(r'\b(IN[A-Z0-9]{10})\b', text):
        _isin = _isin_m.group(1)
        if any(t.isin == _isin for t in trades):
            continue
        # Only accept valid Indian ISINs (INE/INF prefix). Skip SEBI/PMS
        # registration numbers (INZ/INP) and spurious matches like INDIAINE...
        if not (_isin.startswith('INE') or _isin.startswith('INF')):
            continue
        # Use 300 chars after ISIN to find name + numbers (handles multiline)
        _after = text[_isin_m.end():_isin_m.end()+300]
        # Security name: text on same line before ISIN
        _line_start = text.rfind('\n', 0, _isin_m.start()) + 1
        _sec_name = text[_line_start:_isin_m.start()].strip()
        if not _sec_name or len(_sec_name) < 3:
            _name_m = re.match(r'\s+(.+?)\s+\d', _after, re.DOTALL)
            _sec_name = re.sub(r'\s+', ' ', _name_m.group(1)).strip() if _name_m else ''
        # All decimal numbers (WAP, brokerage, totals)
        _nums = [float(n.replace(',', '')) for n in re.findall(r'[\d,]+\.\d+', _after)]
        # All integers (quantities)
        _ints = []
        for n in re.findall(r'\b(\d[\d,]*)\b', _after):
            if '.' not in n:
                try:
                    v = int(n.replace(',', ''))
                    if v > 0:
                        _ints.append(v)
                except ValueError:
                    pass
        if _ints and len(_nums) >= 2:
            qty = float(_ints[0])
            wap = _nums[0]
            # Sanity check: reject garbage matches where qty < 10 and wap > 100000
            # (likely picking up order numbers / obligations instead of trade data)
            if qty < 10 and wap > 100000:
                logger.debug(f'Pass 1.5 rejected: {_isin} qty={qty} wap={wap} (garbage)')
                continue
            qty = float(_ints[0])
            wap = _nums[0]
            brok = _nums[1] if len(_nums) > 1 else 0.0
            # WAP-after-brokerage is the 3rd decimal if we have 4+ decimals
            wap_after = _nums[2] if len(_nums) >= 4 else 0.0
            total = round(qty * wap, 2)
            # Use actual total from 5th decimal if available
            if len(_nums) >= 4:
                total = _nums[3]  # 4th decimal = total value
            # Determine side — same 3-tier approach as Pass 1
            _ctx = text[max(0, _isin_m.start()-200):min(len(text), _isin_m.end()+500)]
            _side = ''
            # Tier 1: WAP vs WAP-after-brokerage
            if wap_after > 0 and wap > 0 and abs(wap_after - wap) > 0.0001:
                _side = 'Buy' if wap_after > wap else 'Sell'
            # Tier 2: Net Obligation sign
            if not _side:
                _net_m = re.search(
                    r'(?:Net\s+Obligation|Net\s+Amount\s+Receivable)[^\d-]*([-]?[\d,]+\.\d+)',
                    _ctx, re.IGNORECASE)
                if _net_m:
                    _net_val = float(_net_m.group(1).replace(',', ''))
                    _side = 'Sell' if _net_val > 0 else 'Buy'
            # Tier 3: keyword proximity
            if not _side:
                _post = text[_isin_m.start():min(len(text), _isin_m.end()+150)]
                _has_sell = bool(re.search(r'\bSELL\b', _post, re.IGNORECASE))
                _has_buy  = bool(re.search(r'\bBUY\b', _post, re.IGNORECASE))
                _side = 'Sell' if _has_sell and not _has_buy else 'Buy'
            # STT — long-form "Securities Transaction Tax" or bare "STT"
            _stt = _parse_stt_amount(_ctx)
            _exch = 'BSE' if 'BSE' in _ctx.upper() and 'NSE' not in _ctx.upper() else 'NSE'
            trades.append(ContractNoteTrade(
                isin=_isin, security_name=_sec_name, side=_side,
                qty=qty, wap=wap, brokerage_per_share=brok,
                total_value=total, exchange=_exch, stt_total=_stt,
            ))
            logger.info(f'Pass 1.5 matched: {_isin} {_sec_name} {_side} qty={qty} wap={wap} '
                        f'raw_300={_after[:200]!r}')

    # If ISIN-first pattern found nothing, try ICICI name-first layout.
    # ICICI PDFs: security name + side + numbers on one line, ISIN on the next line.
    icici_pattern = re.compile(
        r'([A-Z][A-Z &.()\-]+?)\s*'          # Security name (ALL-CAPS with punctuation)
        r'(?:Buy|Sell)\s+'                     # Side keyword
        r'([\d,]+)\s+'                         # Quantity
        r'([\d,]+\.\d+)\s+'                   # Gross rate / WAP
        r'([\d,]+\.\d+)\s+'                   # Brokerage per unit
        r'([\d,]+\.\d+)\s+'                   # Net rate after brokerage
        r'([\d,]+\.\d+)'                      # Net total (before levies)
        r'[\s\S]{0,80}?'                        # intervening text (date fragments etc.)
        r'(IN[A-Z0-9]{10})',                     # ISIN follows on next line
        re.IGNORECASE
    )
    # ICICI layout: name+ISIN concatenated on one line, Buy/Sell + numbers on next.
    # Pattern: ...NAME(ISIN)\n...Buy/Sell qty wap brok net_rate total
    icici_concat_pattern = re.compile(
        r'([A-Z][A-Z &.()\-]+?)'                  # Security name
        r'(IN[EF][A-Z0-9]{9})\s+'                  # ISIN concatenated (flexible whitespace)
        r'[\s\S]{0,80}?'                            # date fragment (e.g. "30-MAR-\n2026")
        r'(?:Buy|Sell)\s+'                          # Side
        r'([\d,]+)\s+'                              # Quantity
        r'([\d,]+\.\d+)\s+'                         # WAP
        r'([\d,]+\.\d+)\s+'                         # Brokerage
        r'([\d,]+\.\d+)\s+'                         # Net rate
        r'([\d,]+\.\d+)',                           # Net total
        re.IGNORECASE
    )
    for m in icici_concat_pattern.finditer(text):
        try:
            isin     = m.group(2).strip()
            if any(t.isin == isin for t in trades):
                continue
            sec_name = m.group(1).strip().rstrip('-').strip()
            qty      = float(m.group(3).replace(',', ''))
            wap      = float(m.group(4).replace(',', ''))
            brok     = float(m.group(5).replace(',', ''))
            total    = float(m.group(7).replace(',', ''))
            context  = text[max(0, m.start()-100):m.end()+500]
            side     = 'Sell' if re.search(r'\bSell\b', m.group(0), re.IGNORECASE) else 'Buy'
            stt_raw  = _parse_stt_amount(context)
            exchange = 'BSE' if 'BSE' in context.upper() and 'NSE' not in context.upper() else 'NSE'
            trades = [t for t in trades if t.isin != isin]
            trades.append(ContractNoteTrade(
                isin=isin, security_name=sec_name, side=side, qty=qty, wap=wap,
                brokerage_per_share=brok, total_value=total, exchange=exchange,
                stt_total=stt_raw,
            ))
            logger.info(f'ICICI concat matched: {isin} {sec_name} {side} qty={qty} wap={wap}')
        except (ValueError, IndexError):
            continue

    # Also try original ICICI pattern (name + side + numbers, ISIN on next line)
    for m in icici_pattern.finditer(text):
        try:
            isin     = m.group(7).strip()
            if any(t.isin == isin for t in trades):
                continue
            sec_name = m.group(1).strip().rstrip('-').strip()
            qty      = float(m.group(2).replace(',', ''))
            wap      = float(m.group(3).replace(',', ''))
            brok     = float(m.group(4).replace(',', ''))
            total    = float(m.group(6).replace(',', ''))
            context  = text[max(0, m.start()-100):m.end()+500]
            side     = 'Sell' if re.search(r'\bSell\b|\bSL\+', context, re.IGNORECASE) else 'Buy'
            stt_raw  = _parse_stt_amount(context)
            exchange = 'BSE' if 'BSE' in context.upper() and 'NSE' not in context.upper() else 'NSE'
            # Remove any bad Pass 1.5 match for this ISIN before adding the correct one
            trades = [t for t in trades if t.isin != isin]
            trades.append(ContractNoteTrade(
                isin=isin, security_name=sec_name, side=side, qty=qty, wap=wap,
                brokerage_per_share=brok,
                total_value=total, exchange=exchange, stt_total=stt_raw,
            ))
        except (ValueError, IndexError):
            continue

    return trades


def _extract_from_tables(page_tables: list,
                          broker_sebi: str,
                          broker_name: str) -> List[ContractNote]:
    """Extract from raw table structures (fallback for pdfplumber table mode)."""
    cns = []
    _table_trades = []  # collect all trades from tables
    for page_idx, tables in enumerate(page_tables):
        for table in tables:
            for row in table:
                if not row:
                    continue
                # Look for ISIN in any cell — exact match OR embedded in text
                for i, cell in enumerate(row):
                    cell_s = str(cell or '').strip()
                    # Exact ISIN cell
                    _isin_exact = re.match(r'^(IN[EF][A-Z0-9]{9})$', cell_s)
                    # ISIN embedded in text (e.g. "JAMMU & KASHMIR BANK LTD-INE168A01041")
                    _isin_embed = re.search(r'(IN[EF][A-Z0-9]{9})', cell_s) if not _isin_exact else None
                    if not _isin_exact and not _isin_embed:
                        continue
                    _isin_match = _isin_exact or _isin_embed
                    try:
                        isin = _isin_match.group(1)
                        # Security name: for embedded ISIN, text before the ISIN
                        if _isin_embed:
                            sec = cell_s[:_isin_embed.start()].rstrip('-').strip()
                        else:
                            sec = str(row[i+1] or '').strip() if i+1 < len(row) else ''

                        def _safe_float(idx):
                            if idx < len(row) and row[idx]:
                                try: return float(str(row[idx]).replace(',',''))
                                except ValueError: pass
                            return 0.0

                        # Scan ALL remaining cells for Buy/Sell keyword and numbers
                        _side_found = ''
                        _nums_in_row = []
                        for j in range(len(row)):
                            _cv = str(row[j] or '').strip()
                            if _cv.lower() in ('buy', 'b'):
                                _side_found = 'Buy'
                            elif _cv.lower() in ('sell', 's'):
                                _side_found = 'Sell'
                            try:
                                _fv = float(_cv.replace(',', ''))
                                if _fv != 0:
                                    _nums_in_row.append(_fv)
                            except ValueError:
                                pass

                        # Need at least qty and wap
                        if len(_nums_in_row) < 2:
                            continue
                        # Heuristic: qty is the first integer-like number,
                        # wap is the first decimal that's a plausible price
                        qty = _nums_in_row[0]
                        wap = _nums_in_row[1]
                        brok = _nums_in_row[2] if len(_nums_in_row) > 2 else 0.0
                        total = _nums_in_row[-1] if len(_nums_in_row) > 3 else round(qty * wap, 2)
                        side = _side_found or 'Buy'

                        if qty > 0 and wap > 0:
                            _table_trades.append(ContractNoteTrade(
                                isin=isin, security_name=sec, side=side,
                                qty=qty, wap=wap, brokerage_per_share=brok,
                                total_value=total,
                            ))
                            logger.info(f'Table extraction: {isin} {sec} {side} qty={qty} wap={wap}')
                    except (ValueError, IndexError):
                        pass
    # Return table trades as a single CN (no CN number from tables)
    if _table_trades:
        logger.info(f'Table extraction: {len(_table_trades)} trade(s) found')
        # Create a dummy CN to carry the trades — the caller will merge
        _dummy_cn = ContractNote(
            cn_no='TABLE', trade_date='', settlement_date='',
            ucc='', broker_name=broker_name, broker_sebi=broker_sebi,
            trades=_table_trades,
        )
        cns.append(_dummy_cn)
    return cns


# ── Claude API extraction ─────────────────────────────────────────────────── #

def _extract_via_claude(pdf_path: str,
                         broker_sebi: str,
                         broker_name: str) -> List[ContractNote]:
    """
    Use Claude API (vision) to extract contract note data from PDFs
    with garbled/encoded fonts that pdfplumber cannot read.
    Converts each page to an image, sends to claude-sonnet with a
    structured extraction prompt.
    """
    try:
        import base64
        from pdf2image import convert_from_path
        import anthropic as _anth_check  # noqa — verify SDK is installed
    except ImportError:
        logger.warning("pdf2image or anthropic SDK not available — cannot use Claude API fallback")
        return []

    try:
        images = convert_from_path(pdf_path, dpi=150, fmt='jpeg')
    except Exception as e:
        logger.warning(f"PDF to image conversion failed: {e}")
        return []

    # Group pages into pairs (each CN typically spans 2 pages)
    page_groups = [images[i:i+2] for i in range(0, len(images), 2)]
    all_cns = []

    for group in page_groups:
        image_content = []
        for img in group:
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=85)
            b64 = base64.b64encode(buf.getvalue()).decode()
            image_content.append({
                'type': 'image',
                'source': {
                    'type': 'base64',
                    'media_type': 'image/jpeg',
                    'data': b64,
                }
            })

        image_content.append({
            'type': 'text',
            'text': (
                'This is a broker contract note from India. '
                'Extract all trade information and return ONLY a JSON object with this exact schema:\n'
                '{\n'
                '  "cn_no": "contract note number",\n'
                '  "trade_date": "DD/MM/YYYY",\n'
                '  "settlement_date": "DD/MM/YYYY",\n'
                '  "ucc": "UCC or Mapin of client shown on this note",\n'
                '  "broker_name": "full broker name",\n'
                '  "broker_sebi": "SEBI registration number",\n'
                '  "trades": [\n'
                '    {\n'
                '      "isin": "12-char ISIN",\n'
                '      "security_name": "name",\n'
                '      "side": "Buy or Sell",\n'
                '      "qty": 500,\n'
                '      "wap": 155.0,\n'
                '      "brokerage_per_share": 0.155,\n'
                '      "stt_total": 78.0,\n'
                '      "exchange": "NSE or BSE"\n'
                '    }\n'
                '  ]\n'
                '}\n'
                'Return ONLY the JSON, no other text.'
            )
        })

        try:
            import anthropic as _anthropic
            _client = _anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env
            _msg = _client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=1000,
                messages=[{'role': 'user', 'content': image_content}],
            )
            raw = _msg.content[0].text.strip()
            # Strip markdown fences if present
            raw = re.sub(r'^```[a-z]*\s*', '', raw, flags=re.MULTILINE)
            raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE)
            data = json.loads(raw)

            trades = []
            for t in data.get('trades', []):
                qty = float(t.get('qty', 0))
                stt_total = float(t.get('stt_total', 0))
                trades.append(ContractNoteTrade(
                    isin=t.get('isin', ''),
                    security_name=t.get('security_name', ''),
                    side=t.get('side', 'Buy'),
                    qty=qty,
                    wap=float(t.get('wap', 0)),
                    brokerage_per_share=float(t.get('brokerage_per_share', 0)),
                    total_value=qty * float(t.get('wap', 0)),
                    exchange=t.get('exchange', 'NSE'),
                    stt_total=stt_total,
                ))

            if trades:
                all_cns.append(ContractNote(
                    cn_no           = str(data.get('cn_no', '')),
                    trade_date      = _normalise_date(str(data.get('trade_date', ''))),
                    settlement_date = _normalise_date(str(data.get('settlement_date', ''))),
                    ucc             = str(data.get('ucc', '')),
                    broker_name     = data.get('broker_name', broker_name),
                    broker_sebi     = data.get('broker_sebi', broker_sebi),
                    trades          = trades,
                    extraction_method = 'claude_api',
                ))

        except Exception as e:
            logger.warning(f"Claude API extraction failed for a page group: {e}")
            continue

    return all_cns


# ── Helpers ───────────────────────────────────────────────────────────────── #

def _normalise_date(s) -> str:
    """Normalise various date formats to DD/MM/YYYY.
    Accepts strings, datetime objects, and datetime-like objects.
    """
    import re
    from datetime import datetime, date

    # Handle datetime / date objects directly
    if hasattr(s, 'strftime'):
        return s.strftime('%d/%m/%Y')

    s = str(s).strip().rstrip('.')
    if not s or s.lower() in ('none', 'nan', ''):
        return ''

    # Strip time component if present (e.g. '2026-03-20 00:00:00')
    s_date = s.split(' ')[0].split('T')[0]

    patterns = [
        ('%d/%m/%Y', r'^\d{2}/\d{2}/\d{4}$'),
        ('%d-%m-%Y', r'^\d{2}-\d{2}-\d{4}$'),
        ('%Y-%m-%d', r'^\d{4}-\d{2}-\d{2}$'),
        ('%d-%b-%Y', r'^\d{2}-[A-Za-z]{3}-\d{4}$'),
        ('%d %b %Y', r'^\d{1,2}\s[A-Za-z]{3}\s\d{4}$'),
        ('%b %d,%Y', r'^[A-Za-z]{3}\s\d{1,2},\d{4}$'),       # Apr 09,2026
        ('%b %d, %Y', r'^[A-Za-z]{3}\s\d{1,2},\s\d{4}$'),    # Apr 09, 2026
    ]
    for fmt, pat in patterns:
        if re.match(pat, s_date, re.IGNORECASE):
            try:
                return datetime.strptime(s_date, fmt).strftime('%d/%m/%Y')
            except ValueError:
                continue
        # Also try against original s
        if re.match(pat, s, re.IGNORECASE):
            try:
                return datetime.strptime(s, fmt).strftime('%d/%m/%Y')
            except ValueError:
                continue
    return s


def calculate_charges(qty: float, avg_px: float, brokerage_rate: float, side: str) -> dict:
    """
    Calculate all charges for a trade line to populate 0096.
    brokerage_rate: e.g. 0.001 for 0.10%
    Returns dict with keys: brokerage_per_share,
                             brokerage_total, stt_total

    Brokerage is stored to 5 decimal places to match the required upload
    format (e.g. 1.34506 not 1.35).
    """
    brok_per_share = round(avg_px * brokerage_rate, 5)  # 5dp required by upload format
    stt_total      = math.ceil(avg_px * qty * 0.001)   # 0.1% — round up (all taxes rounded up)
    return {
        'brokerage_per_share': brok_per_share,
        'brokerage_total':     round(brok_per_share * qty, 2),
        'stt_total':           stt_total,
    }
