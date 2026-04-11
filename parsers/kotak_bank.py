"""
Kotak Bank Parser
-----------------
Input:  Password-protected ZIP (same zip as the holdings file).
        Zip filename: GOLDSTANDARD_WEALTH_PVT_LTD_MYSTIC_WEVA.zip
        Password:     AALCG4181G
        Delivered from Custodysettlement@kotak.com
        Subject:      "Daily Settlement Report"

        Inside the zip:
          ├── 6651091725.csv          ← bank statement (account no = filename)
          └── G0264 DDMMYYYY.xlsx    ← holdings (handled by holdings app)

CSV Layout (no standard header row — fixed structure):
  Row 1:  Customer Account No, <ACCT_NO>, Name, <NAME>, Account Currency, INR
  Row 2:  Statement from, <FROM_DATE>, To, <TO_DATE>
  Row 3:  Opening Balance, <AMOUNT>
  Row 4:  Closing Balance, <AMOUNT>
  Row 5:  Clearing Balance, <AMOUNT>
  Row 6:  (blank)
  Row 7:  TranDate, TranParticular, RefNum, TranCcy, TranAmt, TranBal
  Row 8+: Data rows

  TranAmt is signed (negative = debit).
"""
import csv
import io
import logging
import os
import tempfile

from .bank_base import BankAccount, BankTransaction, ParseResult, normalise_date

logger = logging.getLogger(__name__)


class KotakBankParser:

    def parse_zip(self, zip_path: str, password: str = 'AALCG4181G') -> ParseResult:
        """Extract the CSV (and xlsx) from the zip and parse. Uses Python zipfile —
        Kotak zips use standard ZIP encryption, not AES-256."""
        import zipfile as _zipfile
        result = ParseResult(source='kotak')
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pwd_bytes = password.encode('utf-8') if password else None
                try:
                    with _zipfile.ZipFile(zip_path, 'r') as zf:
                        for member in zf.infolist():
                            if member.filename.lower().endswith(('.csv', '.xlsx')):
                                member.filename = os.path.basename(member.filename)
                                zf.extract(member, path=tmp, pwd=pwd_bytes)
                except Exception as e:
                    result.error = f'Zip extraction failed: {e}'
                    return result

                csv_files  = [f for f in os.listdir(tmp) if f.endswith('.csv')]
                xlsx_files = [f for f in os.listdir(tmp) if f.endswith('.xlsx')]

                if not csv_files:
                    result.error = 'No .csv found in Kotak zip'
                    return result

                # Extract Kotak client ID from holdings xlsx (Sheet2 client column)
                kotak_client_id = ''
                if xlsx_files:
                    kotak_client_id = self._extract_kotak_client_id(
                        os.path.join(tmp, xlsx_files[0])
                    )

                for csv_file in csv_files:
                    sub = self.parse_file(os.path.join(tmp, csv_file))
                    if sub.ok:
                        for acct in sub.accounts:
                            if kotak_client_id:
                                acct.kotak_client_id = kotak_client_id
                        result.accounts.extend(sub.accounts)
                    else:
                        result.warnings.append(f'{csv_file}: {sub.error}')

        except Exception as e:
            result.error = str(e)
            logger.error(f'Kotak zip error: {e}', exc_info=True)
        return result

    @staticmethod
    def _extract_kotak_client_id(xlsx_path: str) -> str:
        """
        Extract the Kotak pool client account number from the holdings xlsx.
        Sheet2 (Pending Deals) carries the client code in the first data row,
        and Sheet3 (Settled Transactions) also carries it.
        The client code is the Kotak internal pool number e.g. '9000032175'.
        """
        try:
            import openpyxl
            wb = openpyxl.load_workbook(xlsx_path, read_only=True)
            for sheet_name in ['Sheet2', 'Sheet3', 'Sheet1']:
                if sheet_name not in wb.sheetnames:
                    continue
                ws = wb[sheet_name]
                for row in ws.iter_rows(min_row=2, max_row=20, values_only=True):
                    if not row or row[0] is None:
                        continue
                    val = str(row[0]).strip()
                    # Kotak client IDs are 10-digit numbers starting with 9
                    if val.isdigit() and len(val) >= 9 and val.startswith('9'):
                        return val
        except Exception as _e:
            pass
        finally:
            try: wb.close()
            except: pass
        return ''

    def parse_file(self, file_path: str, **kwargs) -> ParseResult:
        result = ParseResult(source='kotak')
        try:
            # Account number is the CSV filename (without extension)
            acct_no_from_file = os.path.splitext(os.path.basename(file_path))[0]

            with open(file_path, 'r', encoding='utf-8-sig', errors='replace') as fh:
                text = fh.read()

            self._parse_csv_text(text, acct_no_from_file, result)

        except Exception as e:
            result.error = str(e)
            logger.error(f'Kotak parse error: {e}', exc_info=True)
        return result

    def parse_csv_text(self, text: str, acct_no_hint: str = '') -> ParseResult:
        result = ParseResult(source='kotak')
        try:
            self._parse_csv_text(text, acct_no_hint, result)
        except Exception as e:
            result.error = str(e)
        return result

    # ------------------------------------------------------------------ #

    def _parse_csv_text(self, text: str, acct_no_hint: str, result: ParseResult):
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader]

        acct_no      = acct_no_hint
        acct_name    = ''
        opening_bal  = 0.0
        closing_bal  = 0.0
        from_date    = ''
        to_date      = ''
        transactions = []

        in_data = False
        has_opening = False   # True only if we see an explicit 'Opening Balance' row

        for row in rows:
            if not row or all(c.strip() == '' for c in row):
                continue

            vals = [c.strip() for c in row]

            # Row 1: Customer Account No, <ACCT_NO>, Name, <NAME>, ...
            if vals[0].lower().startswith('customer account'):
                if len(vals) > 1 and vals[1]:
                    acct_no = vals[1]
                if len(vals) > 3 and vals[3]:
                    acct_name = vals[3]
                continue

            # Row 2: Statement from, <DATE>, To, <DATE>
            if vals[0].lower().startswith('statement from'):
                if len(vals) > 1:
                    from_date = normalise_date(vals[1])
                if len(vals) > 3:
                    to_date = normalise_date(vals[3])
                continue

            # Row 3: Opening Balance
            if vals[0].lower().startswith('opening balance'):
                opening_bal = self._num(vals[1]) if len(vals) > 1 else 0.0
                has_opening = True
                continue

            # Row 4: Closing Balance
            if vals[0].lower().startswith('closing balance'):
                closing_bal = self._num(vals[1]) if len(vals) > 1 else 0.0
                continue

            # Row 5: Clearing Balance — skip
            if vals[0].lower().startswith('clearing balance'):
                continue

            # Alternative format: 'Balance as on <date>' with no Opening Balance
            if vals[0].lower().startswith('balance as on'):
                closing_bal = self._num(vals[1]) if len(vals) > 1 else 0.0
                continue

            # Column header row
            if vals[0].lower() == 'trandate':
                in_data = True
                continue

            # Data rows
            if in_data and vals[0]:
                try:
                    tran_date   = normalise_date(vals[0])
                    description = vals[1] if len(vals) > 1 else ''
                    ref_num     = vals[2] if len(vals) > 2 else ''
                    # ccy       = vals[3]
                    amt_raw     = vals[4] if len(vals) > 4 else ''
                    bal_raw     = vals[5] if len(vals) > 5 else ''

                    amt = self._num_signed(amt_raw)
                    bal = self._num(bal_raw)

                    debit  = abs(amt) if amt < 0 else 0.0
                    credit = amt      if amt > 0 else 0.0

                    transactions.append(BankTransaction(
                        account_no  = acct_no,
                        tran_date   = tran_date,
                        description = description,
                        debit       = debit,
                        credit      = credit,
                        balance     = bal,
                        ref_num     = ref_num,
                        source      = 'kotak',
                    ))
                except Exception as row_err:
                    logger.debug(f'Kotak row skip: {row_err}')
                    continue

        account = BankAccount(
            account_no           = acct_no,
            account_name         = acct_name,
            opening_balance      = opening_bal,
            closing_balance      = closing_bal,
            as_on_date           = to_date,
            source               = 'kotak',
            transactions         = transactions,
            has_opening_balance  = has_opening,
        )
        # Kotak client ID comes from the zip filename suffix or sheet data
        # e.g. zip GOLDSTANDARD_WEALTH_PVT_LTD_MYSTIC_WEVA -> client 9000032175
        # This is also available in the holdings sheet2 client column
        # For now stash as empty; caller can set it from the Kotak xlsx sheet data
        account.kotak_client_id = ''
        result.accounts.append(account)
        logger.info(f'Kotak: {acct_no} — {len(transactions)} txn(s)')

    @staticmethod
    def _num(v) -> float:
        try:
            return float(str(v).replace(',', ''))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _num_signed(v) -> float:
        """Parse a signed amount string (negative = debit)."""
        try:
            return float(str(v).replace(',', ''))
        except (TypeError, ValueError):
            return 0.0
