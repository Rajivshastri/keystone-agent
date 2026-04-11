"""
WS Bank Book XLS parser.
Parses the WealthSpectrum Bank Book export in .xls format.

Format (per account block):
  BANK BOOK
  From DD/MM/YYYY to DD/MM/YYYY
  Scheme : N Name
  [blank]
  BANKPREFIX-ACCNO   BANKNAME          ← account identifier line
  [separator]
  [headers]
  Cash - Received/Paid
  DD/MM/YYYY  [blank]  [blank]  Opening Balance  0  0  0  0  opening_bal
  DD/MM/YYYY  tran_acct  desc  security  buy_sell  0  0  dep_with  balance
  ...
  Total Cash - Received/Paid   ...   closing_bal
  Bank Total                   ...   closing_bal
  [page marker]
  [next block starts]
"""

import re
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


def _num(val: str) -> float:
    """Parse a number string, stripping commas and handling empty/dash."""
    if not val or val.strip() in ('', '-', '0.00', '0'):
        try:
            return float(str(val).replace(',', ''))
        except Exception:
            return 0.0
    try:
        return float(str(val).strip().replace(',', ''))
    except Exception:
        return 0.0


def _parse_date(val: str) -> str:
    """Convert DD/MM/YYYY to DD/MM/YYYY (keep as-is, normalise slashes)."""
    if not val:
        return ''
    # xlrd may give float for dates; we just use the string directly
    v = str(val).strip()
    # DD/MM/YYYY → DD-MMM-YYYY normalisation
    MONTHS = ['JAN','FEB','MAR','APR','MAY','JUN',
               'JUL','AUG','SEP','OCT','NOV','DEC']
    m = re.match(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', v)
    if m:
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), m.group(3)
        if 1 <= mm <= 12:
            return f'{dd:02d}-{MONTHS[mm-1]}-{yyyy}'
    return v


def parse_xls(file_path: str):
    """
    Parse a WS Bank Book .xls file.
    Returns a WSBankBookParseResult-compatible object with .accounts, .date_from, .date_to.
    """
    try:
        import xlrd
    except ImportError:
        raise ImportError("xlrd is required for .xls Bank Book files: pip install xlrd")

    from parsers.ws_bank_book import WSAccount, WSTransaction, WSBankBook

    result = WSBankBook()
    wb = xlrd.open_workbook(file_path)

    for sheet_idx in range(wb.nsheets):
        ws = wb.sheet_by_index(sheet_idx)
        rows = [ws.row_values(i) for i in range(ws.nrows)]

        # Extract date range from header
        for row in rows[:5]:
            v = str(row[1]).strip() if len(row) > 1 else ''
            m = re.search(r'From\s+(\d{1,2}/\d{1,2}/\d{4})\s+to\s+(\d{1,2}/\d{1,2}/\d{4})', v, re.I)
            if m:
                result.date_from = _parse_date(m.group(1))
                result.date_to   = _parse_date(m.group(2))

        current_scheme = ''
        i = 0
        while i < len(rows):
            row = rows[i]
            col1 = str(row[1]).strip() if len(row) > 1 else ''

            # Scheme line: "Scheme : N Name"
            if col1.startswith('Scheme :'):
                m = re.match(r'Scheme\s*:\s*\d+\s*(.*)', col1)
                current_scheme = m.group(1).strip() if m else col1
                i += 1
                continue

            # Account identifier: "BANKPREFIX-ACCNO   BANKNAME" (no spaces in account part)
            # Pattern: starts with LETTERS-DIGITS (bank account format)
            acct_match = re.match(r'^([A-Z]{2,10}-[\w\.]+)\s{2,}(\w+)', col1)
            if acct_match and current_scheme:
                ws_account = acct_match.group(1)
                # Parse bank prefix from the account key
                parts = ws_account.split('-', 1)
                bank_prefix = parts[0] if parts else ''
                account_no  = parts[1] if len(parts) > 1 else ws_account

                acct = WSAccount(
                    scheme_code    = '',
                    scheme_name    = current_scheme,
                    ws_account     = ws_account,
                    bank_prefix    = bank_prefix,
                    account_no     = account_no,
                    bank_name      = bank_prefix,
                    custodian_acct = '',
                )

                # Scan forward through data rows for this account block
                j = i + 1
                while j < len(rows):
                    drow = rows[j]
                    d0 = str(drow[1]).strip() if len(drow) > 1 else ''
                    d2 = str(drow[2]).strip() if len(drow) > 2 else ''
                    d3 = str(drow[3]).strip() if len(drow) > 3 else ''
                    d4 = str(drow[4]).strip() if len(drow) > 4 else ''
                    d6 = str(drow[6]).strip() if len(drow) > 6 else ''
                    d10= str(drow[10]).strip() if len(drow) > 10 else ''
                    d11= str(drow[11]).strip() if len(drow) > 11 else ''

                    # Stop at next account / new BANK BOOK block / Page marker
                    if d0 in ('BANK BOOK',) or re.match(r'^[A-Z]{2,10}-[\w\.]+\s{2,}\w+', d0):
                        break
                    if str(drow[12]).strip().startswith('Page') if len(drow) > 12 else False:
                        j += 1
                        break

                    # Opening Balance row: col4 == 'Opening Balance'
                    if d4 == 'Opening Balance' or d3 == 'Opening Balance':
                        bal_str = d11 or d10
                        acct.opening_balance = _num(bal_str)
                        acct.closing_balance = _num(bal_str)  # will be overwritten by Bank Total
                        j += 1
                        continue

                    # Skip headers, separators, totals
                    if d0 in ('Set Date', 'Cash - Received/Paid', '') or \
                       d0.startswith('Total') or d0.startswith('Bank Total') or \
                       d0.startswith('<---') or d0.startswith('Page'):
                        # "Bank Total" row has the closing balance
                        if d0.startswith('Bank Total'):
                            acct.closing_balance = _num(d11 or d10)
                        j += 1
                        continue

                    # Grand closing: "Total" + d4 == "Closing Balance" (last row)
                    if d0 == 'Total' and d4 == 'Closing Balance':
                        # This is the overall total, not per-account — skip
                        j += 1
                        continue

                    # Transaction row: col0 = date (DD/MM/YYYY)
                    date_str_raw = d0
                    if re.match(r'\d{1,2}/\d{1,2}/\d{4}', date_str_raw):
                        set_date = _parse_date(date_str_raw)
                        description = d3 or d2
                        # dep_with column (col10) is the cash movement
                        dep_with = _num(d10)
                        # buy_sell (col6) for securities movements
                        buy_sell = _num(d6)
                        amount = dep_with if dep_with != 0.0 else buy_sell

                        if amount != 0.0 and description not in ('Opening Balance',):
                            txn = WSTransaction(
                                scheme_code    = '',
                                scheme_name    = current_scheme,
                                ws_account     = ws_account,
                                bank_prefix    = bank_prefix,
                                account_no     = account_no,
                                set_date       = set_date,
                                description    = description,
                                security       = d4,
                                amount         = amount,
                                balance        = _num(d11 or d10),
                                custodian_acct = '',
                            )
                            acct.transactions.append(txn)

                    j += 1

                result.accounts.append(acct)
                i = j
                continue

            i += 1

    logger.info(f'WSBankBook XLS: parsed {len(result.accounts)} accounts '
                f'({result.date_from} → {result.date_to})')
    return result
