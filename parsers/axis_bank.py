"""
Axis Bank Parser
----------------
Input:  Two AES-256 password-protected ZIPs per email strategy.
        Delivered from custody.co@axis.bank.in
        Subject contains: "Bank Balance & Transaction"

        ZIP 1 — name contains "BankBalance"
          Inside: GOLDETEPMS_BankBalance_DDMMYYYY.xlsx
          Columns (0-based):
            0  SOL_ID
            1  ACCT_NO
            2  NAME
            3  ID
            4  CLIENT_CODE
            5  C_GROUP
            6  NVL(CLOSING_BALANCE,0)   ← closing balance
            7  SCHM_CODE
            8  ACCT_RPT_CODE
            9  LIEN_AMT
           10  NVL(EFFECT_AVAIL_BAL,0)  ← effective available balance
           11  ASONDATE                 ← date string DD/MM/YYYY
           12  UN_CLR_BAL_AMT
           ...

        ZIP 2 — name contains "BankTransaction"
          Inside: GOLDETEPMS_BankTransaction_DDMMYYYY.xlsx
          Columns:
            0  SOL_ID
            1  ACCT_NO
            2  NAME
            3  ID
            4  CLIENT_CODE
            5  C_GROUP
            6  PART_TRAN_TYPE   ← 'D' debit / 'C' credit
            7  TRAN             ← transaction serial
            8  TRAN_DATE
            9  TRAN_AMT
           10  NARRATION
           ...

Both ZIPs use AES-256 encryption (7z only); password = AALCG4181G.
"""
import io
import logging
import os
import subprocess
import tempfile

from .bank_base import BankAccount, BankTransaction, ParseResult, normalise_date

logger = logging.getLogger(__name__)


class AxisBankParser:

    def parse_zip_pair(
        self,
        balance_zip:  str,
        txn_zip:      str,
        password:     str = 'AALCG4181G',
    ) -> ParseResult:
        """Parse the balance ZIP and the transaction ZIP together."""
        result = ParseResult(source='axis')
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bal_dir = os.path.join(tmp, 'bal')
                txn_dir = os.path.join(tmp, 'txn')
                os.makedirs(bal_dir); os.makedirs(txn_dir)

                self._extract_aes_zip(balance_zip, password, bal_dir)
                if txn_zip:
                    self._extract_aes_zip(txn_zip, password, txn_dir)

                bal_files = self._xlsx_files(bal_dir)
                txn_files = self._xlsx_files(txn_dir)

                if not bal_files:
                    result.error = 'No .xlsx found in Axis balance zip'
                    return result

                # Parse balance file → accounts dict
                accounts = self._parse_balance(bal_files[0])

                # Parse transactions and attach
                if txn_files:
                    self._parse_transactions(txn_files[0], accounts)
                else:
                    result.warnings.append('Axis transaction zip contained no xlsx')

                result.accounts = list(accounts.values())
                logger.info(f'Axis: parsed {len(result.accounts)} account(s)')

        except Exception as e:
            result.error = str(e)
            logger.error(f'Axis parse error: {e}', exc_info=True)

        return result

    # ------------------------------------------------------------------ #

    def _extract_aes_zip(self, zip_path: str, password: str, dest: str):
        """
        Extract an AES-256 encrypted ZIP using pyzipper (pure Python).
        Handles AES-256 which Python's built-in zipfile does not support.
        Falls back to 7-Zip if pyzipper is not available.
        """
        import os as _os

        # Primary: pyzipper — pure Python, no external binary needed
        try:
            import pyzipper
            with pyzipper.AESZipFile(zip_path) as zf:
                zf.setpassword(password.encode('utf-8'))
                zf.extractall(dest)
            return
        except ImportError:
            pass  # Fall through to 7z
        except Exception as e:
            raise RuntimeError(f'pyzipper failed on {zip_path}: {e}')

        # Fallback: 7-Zip binary (Windows local installs, or if 7z in PATH)
        import shutil as _shutil
        seven_zip = _shutil.which('7z') or _shutil.which('7z.exe')
        if not seven_zip:
            candidates = [
                r'C:\Program Files\7-Zip\7z.exe',
                r'C:\Program Files (x86)\7-Zip\7z.exe',
                _os.path.expandvars(r'%ProgramFiles%\7-Zip\7z.exe'),
            ]
            seven_zip = next((p for p in candidates if _os.path.isfile(p)), None)

        if not seven_zip:
            raise RuntimeError(
                'Cannot extract Axis Bank AES-256 zip: pyzipper not installed and '
                '7-Zip not found. Install pyzipper: pip install pyzipper'
            )

        cmd = [seven_zip, 'e', zip_path, f'-p{password}', f'-o{dest}', '-y']
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(f'7z failed on {zip_path}: {r.stderr.decode(errors="replace")}')

    @staticmethod
    def _xlsx_files(folder: str):
        return [os.path.join(folder, f)
                for f in os.listdir(folder) if f.endswith('.xlsx')]

    def _parse_balance(self, file_path: str) -> dict:
        import openpyxl
        # Note: read_only=True fails on Axis files (no dimension record) — use full load
        wb = openpyxl.load_workbook(file_path)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            return {}

        headers = [str(c).strip() if c else '' for c in rows[0]]
        col = {h: i for i, h in enumerate(headers)}

        acct_col    = col.get('ACCT_NO', 1)
        name_col    = col.get('NAME', 2)
        bal_col     = col.get('NVL(CLOSING_BALANCE,0)', 6)
        date_col    = col.get('ASONDATE', 11)
        grp_col     = col.get('C_GROUP', 5)

        accounts = {}
        for row in rows[1:]:
            if row[acct_col] is None:
                continue
            acct_no = str(row[acct_col]).strip()
            if not acct_no:
                continue
            # Skip CAIRT accounts — these are Axis Bank's internal clearing accounts
            schm_col = col.get('SCHM_CODE', 7)
            if schm_col < len(row) and str(row[schm_col] or '').strip().upper() == 'CAIRT':
                continue
            closing = self._num(row[bal_col]) if bal_col < len(row) else 0.0
            name    = str(row[name_col] or '').strip() if name_col < len(row) else ''
            date    = normalise_date(row[date_col]) if date_col < len(row) else ''
            accounts[acct_no] = BankAccount(
                account_no           = acct_no,
                account_name         = name,
                closing_balance      = closing,
                as_on_date           = date,
                source               = 'axis',
                has_opening_balance  = False,   # Axis balance file has no opening bal
            )
            # Stash c_group for Pool Master resolution
            grp_val = str(row[grp_col] or '').strip() if grp_col < len(row) else ''
            accounts[acct_no].c_group = grp_val
        return accounts

    def _parse_transactions(self, file_path: str, accounts: dict):
        import openpyxl
        # Note: read_only=True fails on Axis files (no dimension record) — use full load
        wb = openpyxl.load_workbook(file_path)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            return

        headers = [str(c).strip() if c else '' for c in rows[0]]
        col = {h: i for i, h in enumerate(headers)}

        acct_col  = col.get('ACCT_NO', 1)
        name_col  = col.get('NAME', 2)
        drcr_col  = col.get('PART_TRAN_TYPE', 6)
        date_col  = col.get('TRAN_DATE', 8)
        amt_col   = col.get('TRAN_AMT', 9)
        narr_col  = col.get('NARRATION', 10)
        ref_col   = col.get('REF_NUM', 16) if 'REF_NUM' in col else None

        for row in rows[1:]:
            if not row or row[acct_col] is None:
                continue
            acct_no = str(row[acct_col]).strip()
            if not acct_no:
                continue

            # Create account stub if not in balance file
            if acct_no not in accounts:
                name = str(row[name_col] or '').strip() if name_col < len(row) else ''
                accounts[acct_no] = BankAccount(
                    account_no   = acct_no,
                    account_name = name,
                    source       = 'axis',
                )

            drcr  = str(row[drcr_col] or '').strip().upper() if drcr_col < len(row) else ''
            # Handle both single-letter ('D'/'C') and full-word ('Debit'/'Credit') formats
            is_debit  = drcr in ('D', 'DEBIT')
            is_credit = drcr in ('C', 'CREDIT')
            amt   = self._num(row[amt_col]) if amt_col < len(row) else 0.0
            debit = amt if is_debit  else 0.0
            credit= amt if is_credit else 0.0
            date  = normalise_date(row[date_col]) if date_col < len(row) else ''
            narr  = str(row[narr_col] or '').strip() if narr_col < len(row) else ''
            ref   = str(row[ref_col] or '').strip() if (ref_col and ref_col < len(row)) else ''

            accounts[acct_no].transactions.append(BankTransaction(
                account_no  = acct_no,
                tran_date   = date,
                description = narr,
                debit       = debit,
                credit      = credit,
                ref_num     = ref,
                source      = 'axis',
            ))

    @staticmethod
    def _num(v) -> float:
        try:
            return abs(float(str(v).replace(',', '')))
        except (TypeError, ValueError):
            return 0.0
