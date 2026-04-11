"""
Kotak Parser
------------
File:     .xlsx inside a password-protected zip (zip also contains a .csv bank statement)
Password: On the zip (not the file)
Strategy: Extracted from the ZIP filename suffix (e.g. MYSTIC_WEVA, MYSTIC_WEMO)
Sheet:    Sheet1 — Holdings
          Sheet2 — Pending Deals (ignored)
          Sheet3 — Settled Transactions (ignored)
Layout:   Row 1 = title, Row 2 = blank, Row 3 = sub-headers, Row 4 = column headers
          Data rows 5 to (max_row - 2); last 2 rows = totals + disclaimer
Quantity: Settled Position (col 6), Saleable (col 16)
"""
import logging
import os
import re
import openpyxl
from .base import BaseParser, HoldingRecord, ParseResult

logger = logging.getLogger(__name__)

# Column indices (0-based) in Sheet1 data rows
COL_CLN_CODE     = 0
COL_CLN_NAME     = 1
COL_INSTR_CODE   = 2
COL_ISIN         = 3
COL_INSTR_NAME   = 4
COL_DEMAT_PHYS   = 5
COL_SETTLED      = 6   # free settled quantity
COL_PEND_PURCH   = 7
COL_PEND_SALE    = 8
COL_BLOCKED      = 9
COL_MARKET_DATE  = 13
COL_MARKET_PRICE = 14
COL_SALEABLE     = 16  # settled - pending sale
COL_CONTRACTUAL  = 17


class KotakParser(BaseParser):
    source_name = 'kotak'

    def parse_file(self, file_path: str, date_str: str,
                   file_password: str = '') -> ParseResult:
        result = ParseResult(source=self.source_name, file_path=file_path)
        holding_date = self.format_date(date_str)

        try:
            # Strategy code is in the parent folder name or the zip filename
            # The caller sets the broker_code via the folder name convention
            broker_code = self._extract_broker_code_from_path(file_path)
            if not broker_code:
                result.error = "Could not determine strategy from Kotak file path"
                return result

            result.broker_codes_found = [broker_code]

            wb = openpyxl.load_workbook(file_path)
            if 'Sheet1' not in wb.sheetnames:
                result.error = f"Expected 'Sheet1' not found. Sheets: {wb.sheetnames}"
                return result

            ws = wb['Sheet1']
            all_rows = list(ws.iter_rows(values_only=True))
            wb.close()

            # Rows 1-4: title/sub-headers/column headers — skip
            # Last 2 rows: totals row + disclaimer row — skip
            data_rows = all_rows[4:-2]

            for row in data_rows:
                if not row or row[COL_CLN_CODE] is None:
                    continue

                client_code = str(row[COL_CLN_CODE]).strip()
                isin        = str(row[COL_ISIN] or '').strip()
                instr_code  = str(row[COL_INSTR_CODE] or '').strip()
                instr_name  = str(row[COL_INSTR_NAME] or '').strip()

                if not client_code or not isin or len(isin) < 5:
                    continue

                settled    = self.clean_number(row[COL_SETTLED])
                pend_purch = self.clean_number(row[COL_PEND_PURCH])
                blocked    = self.clean_number(row[COL_BLOCKED])
                saleable   = self.clean_number(row[COL_SALEABLE])

                logical    = settled + pend_purch + blocked

                # Skip rows with zero logical and zero saleable
                if logical == 0 and saleable == 0:
                    continue

                result.records.append(HoldingRecord(
                    broker_code      = broker_code,
                    client_id        = client_code,
                    security_code    = instr_code,
                    security_name    = instr_name,
                    isin             = isin,
                    logical_holding  = logical,
                    saleable_holding = saleable,
                    holding_date     = holding_date,
                    source           = self.source_name,
                ))

            logger.info(f"Kotak: parsed {result.record_count} records, "
                        f"broker code: {broker_code}")

        except Exception as e:
            result.error = str(e)
            logger.error(f"Kotak parse error: {e}", exc_info=True)

        return result

    def _extract_broker_code_from_path(self, file_path: str) -> str:
        """
        Extract strategy code from the directory path.
        Convention: the source subdirectory is named after the broker_code.
        e.g.  .../raw/kotak_MYSTIC_WEVA/G0264 16032026.xlsx
              → broker_code = MYSTIC_WEVA

        Falls back to extracting from the zip name if the folder name
        contains the strategy suffix.
        """
        # Try parent folder name: kotak_MYSTIC_WEVA
        folder = os.path.basename(os.path.dirname(file_path))
        if '_' in folder:
            parts = folder.split('_', 1)
            if len(parts) == 2 and parts[1]:
                return parts[1]  # e.g. MYSTIC_WEVA

        # Try grandparent folder (in case of nesting)
        grandparent = os.path.basename(
            os.path.dirname(os.path.dirname(file_path))
        )
        if '_' in grandparent:
            parts = grandparent.split('_', 1)
            if len(parts) == 2 and parts[1]:
                return parts[1]

        # Last resort: try to match known Kotak patterns in the path
        match = re.search(r'(MYSTIC_\w+)', file_path)
        if match:
            return match.group(1)

        return ''
