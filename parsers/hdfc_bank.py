"""
HDFC Bank Parser
----------------
HDFC sends TWO separate emails:

1. "Bank Account transactions as on Yesterday"
   - Attachment: zip → fcat_hdfccustodystmt.xlsx
   - Contains: transaction rows (debit/credit, no balance)
   - Source name: hdfc_bank

2. "End of Day Bank Account Balances as on Yesterday"
   - Attachment: zip → fcat_hdfccustodybalance.xlsx
   - Contains: account-level opening and closing balances
   - Source name: hdfc_bank_balance

The bank recon route merges both files: balance file provides opening/closing,
transaction file provides the individual debit/credit rows.

XLSX layouts:

fcat_hdfccustodystmt.xlsx (Sheet "Sheet"):
  Headers: cod_acct_no | nam_branch_shrt | amt_txn | cod_drcr | dat_value |
           dat_txn | dat_post | txt_txn_desc
  cod_drcr: 'D' = debit, 'C' = credit

fcat_hdfccustodybalance.xlsx:
  Headers vary — common: cod_acct_no | dat_value | bal_book | bal_open | ...
  or: account_no | opening_balance | closing_balance | ...
  Parser reads flexibly by scanning for balance-related column names.
"""
import io
import logging
import subprocess
import tempfile
import os

from .bank_base import BankAccount, BankTransaction, ParseResult, normalise_date

logger = logging.getLogger(__name__)

EXPECTED_COLS = {
    'cod_acct_no', 'amt_txn', 'cod_drcr', 'dat_txn', 'txt_txn_desc',
}


class HdfcBankParser:

    def parse_zip(self, zip_path: str, password: str = 'AALCG4181G') -> ParseResult:
        """Extract the XLSX from the ZIP and parse it.
        HDFC zips use standard ZIP encryption — Python zipfile handles them.
        7z fallback is kept for any edge-case AES-256 variants."""
        result = ParseResult(source='hdfc')
        try:
            import zipfile as zf
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    with zf.ZipFile(zip_path, 'r') as z:
                        z.extractall(tmp, pwd=password.encode() if password else None)
                except Exception:
                    # Fallback for AES-256 — find 7z properly on Windows
                    import shutil as _shutil, os as _os
                    seven_zip = (_shutil.which('7z') or _shutil.which('7z.exe') or
                                 next((p for p in [
                                     r'C:\Program Files\7-Zip\7z.exe',
                                     r'C:\Program Files (x86)\7-Zip\7z.exe',
                                 ] if _os.path.isfile(p)), None))
                    if seven_zip:
                        subprocess.run([seven_zip, 'e', zip_path, f'-p{password}',
                                        f'-o{tmp}', '-y'], capture_output=True, timeout=60)
                    else:
                        raise RuntimeError(
                            'HDFC zip could not be extracted with Python zipfile and '
                            '7-Zip was not found. Install 7-Zip if this zip uses AES-256.'
                        )

                xlsx_files = [f for f in os.listdir(tmp) if f.endswith('.xlsx')]
                if not xlsx_files:
                    result.error = 'No .xlsx found in HDFC zip'
                    return result

                # Derive account alias from zip filename
                zip_alias = os.path.splitext(os.path.basename(zip_path))[0]
                return self.parse_file(
                    os.path.join(tmp, xlsx_files[0]),
                    zip_alias=zip_alias,
                )
        except Exception as e:
            result.error = str(e)
            logger.error(f'HDFC zip error: {e}', exc_info=True)
        return result

    def parse_file(self, file_path: str, zip_alias: str = '', **kwargs) -> ParseResult:
        result = ParseResult(source='hdfc')
        try:
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            wb.close()
            if not rows:
                result.error = 'HDFC file is empty'
                return result

            # Build column index from header row
            headers = [str(c).strip().lower() if c else '' for c in rows[0]]
            col = {h: i for i, h in enumerate(headers)}

            missing = EXPECTED_COLS - set(col.keys())
            if missing:
                result.error = f'HDFC missing columns: {missing}'
                return result

            accounts: dict[str, BankAccount] = {}

            for row in rows[1:]:
                acct_raw = row[col['cod_acct_no']]
                if acct_raw is None:
                    continue
                acct_no = str(acct_raw).strip().lstrip("'")
                if not acct_no:
                    continue

                if acct_no not in accounts:
                    accounts[acct_no] = BankAccount(
                        account_no          = acct_no,
                        account_name        = zip_alias,
                        zip_alias           = zip_alias,
                        source              = 'hdfc',
                        has_opening_balance = False,  # HDFC txn file has no balance rows
                    )

                amt    = self._num(row[col['amt_txn']])
                drcr   = str(row[col['cod_drcr']] or '').strip().upper()
                debit  = amt if drcr == 'D' else 0.0
                credit = amt if drcr == 'C' else 0.0

                tran_date = normalise_date(row[col['dat_txn']])
                desc      = str(row[col['txt_txn_desc']] or '').strip()

                accounts[acct_no].transactions.append(BankTransaction(
                    account_no  = acct_no,
                    tran_date   = tran_date,
                    description = desc,
                    debit       = debit,
                    credit      = credit,
                    source      = 'hdfc',
                ))

            result.accounts = list(accounts.values())
            logger.info(f'HDFC: parsed {len(result.accounts)} account(s)')

        except Exception as e:
            result.error = str(e)
            logger.error(f'HDFC parse error: {e}', exc_info=True)

        return result

    @staticmethod
    def _num(v) -> float:
        try:
            return abs(float(str(v).replace(',', '')))
        except (TypeError, ValueError):
            return 0.0

    def parse_balance_zip(self, zip_path: str, password: str = 'AALCG4181G') -> ParseResult:
        """Parse fcat_hdfccustodybalance.xlsx from zip — provides opening/closing balances."""
        import zipfile as _zipfile
        result = ParseResult(source='hdfc')
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pwd_bytes = password.encode('utf-8') if password else None
                try:
                    with _zipfile.ZipFile(zip_path, 'r') as zf:
                        for member in zf.infolist():
                            if member.filename.lower().endswith('.xlsx'):
                                member.filename = os.path.basename(member.filename)
                                zf.extract(member, path=tmp, pwd=pwd_bytes)
                except Exception as e:
                    result.error = f'Zip extraction failed: {e}'; return result

                xlsx_files = [f for f in os.listdir(tmp) if f.endswith('.xlsx')]
                if not xlsx_files:
                    result.error = 'No .xlsx in HDFC balance zip'; return result

                result = self.parse_balance_file(os.path.join(tmp, xlsx_files[0]),
                                                 zip_alias=os.path.splitext(
                                                     os.path.basename(zip_path))[0])
        except Exception as e:
            result.error = str(e)
        return result

    def parse_balance_file(self, file_path: str, zip_alias: str = '') -> ParseResult:
        """
        Parse fcat_hdfccustodybalance.xlsx.
        Reads flexibly — looks for account, opening balance, and closing balance columns
        regardless of exact column names.
        """
        result = ParseResult(source='hdfc')
        try:
            import openpyxl
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            wb.close()
            if not rows:
                result.error = 'HDFC balance file is empty'; return result

            headers = [str(c).strip().lower().replace(' ', '_') if c else '' for c in rows[0]]
            col = {h: i for i, h in enumerate(headers)}

            # Flexible column detection
            acct_col    = self._find_col(col, ['cod_acct_no','account_no','account','acct_no'])
            open_col    = self._find_col(col, ['bal_open','opening_balance','open_bal','bal_opening','opn_bal'])
            close_col   = self._find_col(col, ['bal_book','closing_balance','close_bal','bal_closing','cls_bal','bal_clg'])
            date_col    = self._find_col(col, ['dat_value','value_date','dat_bal','as_on_date'])

            if acct_col is None:
                result.error = f'HDFC balance file: cannot find account column in {headers}'; return result

            accounts = {}
            for row in rows[1:]:
                if not row or row[acct_col] is None:
                    continue
                acct_no = str(row[acct_col]).strip().lstrip("'")
                if not acct_no or not acct_no[0].isdigit():
                    continue

                opening = self._num(row[open_col])  if open_col is not None else 0.0
                closing = self._num(row[close_col]) if close_col is not None else 0.0
                as_on   = normalise_date(row[date_col]) if date_col is not None else ''

                if acct_no not in accounts:
                    accounts[acct_no] = BankAccount(
                        account_no          = acct_no,
                        account_name        = zip_alias,
                        zip_alias           = zip_alias,
                        opening_balance     = opening,
                        closing_balance     = closing,
                        as_on_date          = as_on,
                        source              = 'hdfc',
                        has_opening_balance = open_col is not None,
                    )
                else:
                    # Update if same account appears twice (take most recent)
                    if closing:
                        accounts[acct_no].closing_balance = closing
                    if opening:
                        accounts[acct_no].opening_balance = opening

            result.accounts = list(accounts.values())
            logger.info(f'HDFC balance: {len(result.accounts)} account(s)')

        except Exception as e:
            result.error = str(e)
            logger.error(f'HDFC balance parse error: {e}', exc_info=True)

        return result

    @staticmethod
    def _find_col(col_dict: dict, candidates: list):
        """Find first matching column index from a list of candidate names."""
        for c in candidates:
            if c in col_dict:
                return col_dict[c]
        return None
