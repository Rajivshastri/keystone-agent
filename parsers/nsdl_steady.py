"""
NSDL Steady File Parser
------------------------
Reads the NSDL contract notes status XLS file downloaded from
https://eservices.nsdl.com (manual download, re-uploadable).

File: ContractNotesStatus sheet
Key columns (35 total):
  Sr. No | ECN No | ECN Status | ECN Date | ISIN Code | Security Name |
  Transaction Type | Delivery Type | Exchange | Sett. Date | Qty |
  Net Amount | Net Rate | Brokerage Amount | Brokerage Rate |
  Service Tax | Stamp Duty | Service Transaction Tax |
  SEBI Regn No. | Broker Name | Client Exchange Code/UCC |
  Scheme Name | Custodian Name | Remarks
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class NSDLRecord:
    ecn_no:        str
    ecn_status:    str    # 'Received by FM', 'Received by STeADY', etc.
    ecn_date:      str    # DD/MM/YYYY trade date
    isin:          str
    security_name: str
    side:          str    # 'Buy' or 'Sell'
    exchange:      str
    settlement_date: str
    qty:           float
    net_rate:      float  # = dealer AvgPx
    brokerage:     float  # total brokerage
    brokerage_rate: float
    stt:           float  # Service Transaction Tax
    broker_sebi:   str    # SEBI Regn No.
    broker_name:   str
    ucc:           str    # Client Exchange Code / UCC (= Mapin)
    scheme_name:   str
    custodian:     str
    remarks:       str    # brokerage + GST breakdown


@dataclass
class NSDLParseResult:
    records:  List[NSDLRecord] = field(default_factory=list)
    error:    str = ''
    warnings: List[str] = field(default_factory=list)
    report_date: str = ''

    @property
    def ok(self) -> bool:
        return not self.error


class NSDLParser:
    """Parse the NSDL contract notes status file."""

    # Column name aliases (normalised lowercase, spaces→underscores)
    COL_ALIASES = {
        'ecn_no':          ['ecn_no', 'ecn no'],
        'ecn_status':      ['ecn_status', 'ecn status'],
        'ecn_date':        ['ecn_date', 'ecn date'],
        'isin_code':       ['isin_code', 'isin code'],
        'security_name':   ['security_name', 'security name'],
        'transaction_type':['transaction_type', 'transaction type'],
        'exchange':        ['exchange'],
        'sett_date':       ['sett._date', 'sett. date', 'sett_date', 'settlement date'],
        'qty':             ['qty'],
        'net_rate':        ['net_rate', 'net rate'],
        'brokerage_amount':['brokerage_amount', 'brokerage amount'],
        'brokerage_rate':  ['brokerage_rate', 'brokerage rate'],
        'service_tax':     ['service_tax', 'service tax'],
        'stamp_duty':      ['stamp_duty', 'stamp duty'],
        'stt':             ['service_transaction_tax', 'service transaction tax'],
        'sebi_regn_no':    ['sebi_regn_no.', 'sebi regn no.', 'sebi regn no'],
        'broker_name':     ['broker_name', 'broker name'],
        'ucc':             ['client_exchange_code/ucc', 'client exchange code/ucc',
                            'client_exchange_code_ucc'],
        'scheme_name':     ['scheme_name', 'scheme name'],
        'custodian_name':  ['custodian_name', 'custodian name'],
        'remarks':         ['remarks'],
    }

    def parse_file(self, file_path: str) -> NSDLParseResult:
        result = NSDLParseResult()
        import re

        ext = Path(file_path).suffix.lower()

        # Load all rows as raw values using openpyxl or xlrd
        try:
            if ext == '.xls':
                try:
                    import xlrd
                    wb = xlrd.open_workbook(file_path)
                    ws = wb.sheet_by_index(0)
                    all_rows = [
                        [ws.cell_value(r, c) for c in range(ws.ncols)]
                        for r in range(ws.nrows)
                    ]
                except ImportError:
                    # xlrd not available — try openpyxl (works for .xls in some cases)
                    import openpyxl
                    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
                    ws = wb.active
                    all_rows = [[cell for cell in row] for row in ws.iter_rows(values_only=True)]
                    wb.close()
            else:
                import openpyxl
                wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
                ws = wb.active
                all_rows = [[cell for cell in row] for row in ws.iter_rows(values_only=True)]
                wb.close()
        except Exception as e:
            result.error = f"Cannot open NSDL file: {e}"
            return result

        if not all_rows:
            result.error = "NSDL file is empty"
            return result

        def cell_str(v):
            if v is None:
                return ''
            s = str(v).strip()
            return '' if s.lower() == 'none' else s

        # Extract report date from first 6 rows
        for row in all_rows[:6]:
            row_str = ' '.join(cell_str(c) for c in row)
            m = re.search(r'(\d{1,2}[-/]\w+[-/]\d{4})', row_str)
            if m:
                result.report_date = m.group(1)
                break

        # Find header row — contains 'ECN No' or 'Sr. No'
        header_idx = None
        for i, row in enumerate(all_rows):
            row_str = ' '.join(cell_str(c) for c in row).lower()
            if 'ecn no' in row_str or ('sr' in row_str and 'no' in row_str and 'isin' in row_str):
                header_idx = i
                break

        if header_idx is None:
            result.error = "Could not find header row in NSDL file"
            return result

        # Build column index from header row
        header_row = [cell_str(c).lower().strip().replace(' ', '_').replace('/', '_').replace('.', '')
                      for c in all_rows[header_idx]]

        def find_col(aliases):
            for alias in aliases:
                alias_n = alias.lower().strip().replace(' ', '_').replace('/', '_').replace('.', '')
                for i, h in enumerate(header_row):
                    if alias_n in h or h in alias_n:
                        return i
            return None

        col_idx = {std: find_col(aliases) for std, aliases in self.COL_ALIASES.items()}

        def get_cell(row, key):
            idx = col_idx.get(key)
            if idx is None or idx >= len(row):
                return ''
            return cell_str(row[idx])

        def flt(v):
            try:
                return float(str(v).replace(',', '')) if v not in ('', None) else 0.0
            except (ValueError, TypeError):
                return 0.0

        count = 0
        service_tax_count = 0
        service_tax_total = 0.0
        stamp_duty_count  = 0
        stamp_duty_total  = 0.0
        for row in all_rows[header_idx + 1:]:
            if not row or all(c is None or str(c).strip() == '' for c in row):
                continue

            ecn_no = get_cell(row, 'ecn_no')
            isin   = get_cell(row, 'isin_code')

            if not ecn_no or not isin:
                continue
            if ecn_no.lower() in ('nan', 'sr. no', 'sr no', 'ecn no'):
                continue

            txn_type = get_cell(row, 'transaction_type').lower()
            side = 'Sell' if 'sell' in txn_type else 'Buy'

            record = NSDLRecord(
                ecn_no          = ecn_no,
                ecn_status      = get_cell(row, 'ecn_status'),
                ecn_date        = _normalise_date(get_cell(row, 'ecn_date')),
                isin            = isin,
                security_name   = get_cell(row, 'security_name'),
                side            = side,
                exchange        = get_cell(row, 'exchange') or 'NSE',
                settlement_date = _normalise_date(get_cell(row, 'sett_date')),
                qty             = flt(get_cell(row, 'qty')),
                net_rate        = flt(get_cell(row, 'net_rate')),
                brokerage       = flt(get_cell(row, 'brokerage_amount')),
                brokerage_rate  = flt(get_cell(row, 'brokerage_rate')),
                stt             = flt(get_cell(row, 'stt')),
                broker_sebi     = get_cell(row, 'sebi_regn_no'),
                broker_name     = get_cell(row, 'broker_name'),
                ucc             = get_cell(row, 'ucc'),
                scheme_name     = get_cell(row, 'scheme_name'),
                custodian       = get_cell(row, 'custodian_name'),
                remarks         = get_cell(row, 'remarks'),
            )
            result.records.append(record)
            count += 1

            svc_tax = flt(get_cell(row, 'service_tax'))
            if svc_tax != 0.0:
                service_tax_count += 1
                service_tax_total += svc_tax
            stamp = flt(get_cell(row, 'stamp_duty'))
            if stamp != 0.0:
                stamp_duty_count += 1
                stamp_duty_total += stamp

        if service_tax_count:
            result.warnings.append(
                f"NSDL: {service_tax_count} row(s) have non-zero Service Tax "
                f"(total Rs {service_tax_total:,.2f}). Expected all zero."
            )
        if stamp_duty_count:
            result.warnings.append(
                f"NSDL: {stamp_duty_count} row(s) have non-zero Stamp Duty "
                f"(total Rs {stamp_duty_total:,.2f}). Expected all zero."
            )

        if not result.records:
            result.warnings.append(
                f"No records found in NSDL file — file may be empty or header format unrecognised. "
                f"Parsed {len(all_rows) - header_idx - 1} data rows."
            )
        else:
            logger.info(f"NSDL parser: {count} records from {Path(file_path).name}")

        return result


def _normalise_date(s) -> str:
    """Normalise date to DD/MM/YYYY. Accepts strings and datetime objects."""
    import re
    from datetime import datetime
    if hasattr(s, 'strftime'):
        return s.strftime('%d/%m/%Y')
    s = str(s).strip().rstrip('.')
    if not s or s.lower() in ('none', 'nan', ''):
        return ''
    # Strip time component if present
    s_date = s.split(' ')[0].split('T')[0]
    patterns = [
        ('%d/%m/%Y', r'^\d{2}/\d{2}/\d{4}$'),
        ('%d-%m-%Y', r'^\d{2}-\d{2}-\d{4}$'),
        ('%Y-%m-%d', r'^\d{4}-\d{2}-\d{2}$'),
        ('%d-%b-%Y', r'^\d{2}-[A-Za-z]{3}-\d{4}$'),
    ]
    for fmt, pat in patterns:
        for candidate in (s_date, s):
            if re.match(pat, candidate, re.IGNORECASE):
                try:
                    return datetime.strptime(candidate, fmt).strftime('%d/%m/%Y')
                except ValueError:
                    continue
    return s
