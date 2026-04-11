"""
ICICI Bank Parser
-----------------
Input:  Single .txt file (may cover multiple accounts).
        Delivered by email from corp.stmnts@icicibank.com
        Subject: "Account Statement from ICICI Bank"

Format:
  - One or more account blocks per file.
  - Each block:
      Header line:  "ICICI Bank account Statement from DD-MM-YYYY to DD-MM-YYYY."
      Column line:  Account Number|Tran Date|Tran Particular|Inst Num|Dr Tran Amt|Cr Tran Amt|Bal Amt|Deposit Branch
      Separator:    dashes
      Data rows:    pipe-delimited
      Footer line:  "Closing Balance as on ..."

  - The B/F (brought forward) row contains the opening balance in the Bal Amt column.
  - Closing balance is taken from the footer line.
"""
import re
import logging
from typing import List, Optional

from .bank_base import BankAccount, BankTransaction, ParseResult, normalise_date

logger = logging.getLogger(__name__)

# "Closing Balance as on 21-03-2026 04:07:24 is INR.2530800.34 ..."
RE_CLOSING = re.compile(
    r'Closing Balance as on\s+(\S+)\s+\S+\s+is\s+INR[.\s]*([\d,]+\.?\d*)',
    re.IGNORECASE,
)
# "ICICI Bank account Statement from DD-MM-YYYY to DD-MM-YYYY"
RE_HEADER  = re.compile(r'Statement from\s+([\d\-]+)\s+to\s+([\d\-]+)', re.IGNORECASE)


class ICICIBankParser:

    def parse_file(self, file_path: str, **kwargs) -> ParseResult:
        result = ParseResult(source='icici')
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as fh:
                text = fh.read()
            # CSV format: same content as pipe-delimited TXT but comma/quote wrapped.
            # Detected by absence of '|' and presence of quoted CSV columns.
            # Convert to pipe-delimited so _parse_text handles it uniformly.
            if '|' not in text and '"Account Number"' in text:
                text = self._csv_to_pipe(text)
            self._parse_text(text, result)
        except Exception as e:
            result.error = str(e)
            logger.error(f'ICICI parse error: {e}', exc_info=True)
        return result

    def _csv_to_pipe(self, text: str) -> str:
        """
        Convert ICICI CSV format to pipe-delimited format expected by _parse_text.
        CSV cols: Account Number, Tran Date, Tran Particular, Tran Remarks,
                  Inst Num, Orig Sol Id, Currency Code, Dr Tran Amt, Cr Tran Amt,
                  Bal Amt, Deposit Branch
        Pipe cols: Acct|Date|Narration|InstNum|Dr|Cr|Bal
        """
        import csv, io
        lines_out = []
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if not row:
                lines_out.append('')
                continue
            raw = row[0].strip() if row else ''
            # Pass through header/footer lines unchanged
            if 'ICICI Bank account Statement' in raw:
                lines_out.append(raw)
                continue
            if 'Closing Balance' in raw or 'closing balance' in raw.lower():
                lines_out.append(raw)
                continue
            if raw.startswith('Account Number') or raw.startswith('"Account Number"'):
                lines_out.append('Account Number|Tran Date|Tran Particular|Inst Num|Dr Tran Amt|Cr Tran Amt|Bal Amt')
                continue
            if raw.startswith('---') or raw.startswith('---------') or set(raw.replace('-','').strip()) == set():
                lines_out.append('---')
                continue
            # Data rows: [acct, date, particular, remarks, inst, sol_id, ccy, dr, cr, bal, branch]
            if len(row) >= 10 and row[0].startswith('Ac No:'):
                acct    = row[0].replace('Ac No:', '').strip()
                date    = row[1].strip()
                narr    = (row[2].strip() + ' ' + row[3].strip()).strip()
                inst    = row[4].strip()
                dr      = row[7].strip()
                cr      = row[8].strip()
                bal     = row[9].strip()
                lines_out.append(f'{acct}|{date}|{narr}|{inst}|{dr}|{cr}|{bal}')
            else:
                # Pass other lines through as-is
                lines_out.append(','.join(row))
        return '\n'.join(lines_out)

    def parse_text(self, text: str) -> ParseResult:
        result = ParseResult(source='icici')
        try:
            self._parse_text(text, result)
        except Exception as e:
            result.error = str(e)
            logger.error(f'ICICI parse error: {e}', exc_info=True)
        return result

    # ------------------------------------------------------------------ #

    def _parse_text(self, text: str, result: ParseResult):
        lines = text.splitlines()

        current_account: Optional[BankAccount] = None
        statement_date  = ''
        in_data         = False

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            # ── New account block header ────────────────────────────────
            hm = RE_HEADER.search(stripped)
            if 'ICICI Bank account Statement' in stripped and hm:
                if current_account is not None:
                    result.accounts.append(current_account)
                statement_date = self._fmt(hm.group(2))  # "to" date
                current_account = None
                in_data = False
                continue

            # ── Column header line (contains "Account Number|Tran Date")
            if 'Account Number' in stripped and 'Tran Date' in stripped:
                in_data = False   # next non-separator row is data
                continue

            # ── Separator line ──────────────────────────────────────────
            if set(stripped.replace('|', '').replace(' ', '').replace('-', '')) == set():
                continue
            if stripped.startswith('---'):
                in_data = True
                continue

            # ── Closing balance footer ──────────────────────────────────
            cm = RE_CLOSING.search(stripped)
            if cm and current_account is not None:
                current_account.closing_balance = float(cm.group(2).replace(',', ''))
                current_account.as_on_date = self._fmt(cm.group(1).split()[0])
                in_data = False
                continue

            # ── Data rows ───────────────────────────────────────────────
            if '|' in stripped:
                parts = [p.strip() for p in stripped.split('|')]
                # Need at least: acct_no, date, particular, inst, dr, cr, bal
                if len(parts) < 7:
                    continue
                try:
                    acct_no   = parts[0].strip()
                    tran_date = parts[1].strip()
                    narration = parts[2].strip()
                    inst_num  = parts[3].strip()
                    dr_str    = parts[4].strip()
                    cr_str    = parts[5].strip()
                    bal_str   = parts[6].strip()

                    if not acct_no or not tran_date:
                        continue

                    # Initialise account on first data row
                    if current_account is None or current_account.account_no != acct_no:
                        if current_account is not None and current_account.account_no != acct_no:
                            result.accounts.append(current_account)
                        current_account = BankAccount(
                            account_no=acct_no,
                            source='icici',
                            as_on_date=statement_date,
                        )

                    dr  = self._num(dr_str)
                    cr  = self._num(cr_str)
                    bal = self._num(bal_str)

                    # B/F row → opening balance
                    if 'B/F' in narration.upper():
                        current_account.opening_balance = bal
                        continue

                    current_account.transactions.append(BankTransaction(
                        account_no  = acct_no,
                        tran_date   = normalise_date(tran_date),
                        description = narration,
                        debit       = dr,
                        credit      = cr,
                        balance     = bal,
                        ref_num     = inst_num,
                        source      = 'icici',
                    ))

                except Exception as row_err:
                    logger.debug(f'ICICI row skip: {row_err} — {stripped}')
                    continue

        # Append last account
        if current_account is not None:
            result.accounts.append(current_account)

        logger.info(f'ICICI: parsed {len(result.accounts)} account(s)')

    @staticmethod
    def _num(s: str) -> float:
        if not s:
            return 0.0
        try:
            return float(s.replace(',', ''))
        except ValueError:
            return 0.0

    @staticmethod
    def _fmt(date_str: str) -> str:
        return normalise_date(date_str)
