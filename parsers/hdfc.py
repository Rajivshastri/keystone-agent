"""
HDFC Parser
-----------
File:     HTML disguised as .xls, inside a password-protected zip
Password: On the zip (not the file)
Strategy: Extracted from HTML metadata Table 9 ("Client Code : GWPJ")
Quantity: Total Saleable (col 9), Book Position (col 6)
Numbers:  Indian comma formatting (1,00,000) — stripped automatically
"""
import logging
import re
from bs4 import BeautifulSoup
from .base import BaseParser, HoldingRecord, ParseResult

logger = logging.getLogger(__name__)

# Column indices (0-based) in the main data table
COL_CLIENT_CODE  = 0
COL_CLIENT_NAME  = 1
COL_INSTR_CODE   = 2
COL_INSTR_NAME   = 3
COL_ISIN         = 4
COL_FACE_VALUE   = 5
COL_BOOK_POS     = 6   # logical holding
COL_PHYS_SALE    = 7
COL_DEMAT_NET    = 8
COL_TOTAL_SALE   = 9   # saleable holding
COL_HOLDING_DATE = 14


class HDFCParser(BaseParser):
    source_name = 'hdfc'

    def parse_file(self, file_path: str, date_str: str,
                   file_password: str = '') -> ParseResult:
        result = ParseResult(source=self.source_name, file_path=file_path)
        holding_date = self.format_date(date_str)

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            soup = BeautifulSoup(content, 'lxml')
            tables = soup.find_all('table')

            # --- Extract broker code from metadata tables ---
            broker_code = self._extract_broker_code(tables)
            if not broker_code:
                result.error = "Could not find Client Code in HDFC file metadata"
                return result

            result.broker_codes_found = [broker_code]

            # --- Find the main data table (largest table with header row) ---
            data_table = self._find_data_table(tables)
            if data_table is None:
                result.error = "Could not find holdings data table in HDFC file"
                return result

            rows = data_table.find_all('tr')
            if not rows:
                result.error = "No rows found in HDFC data table"
                return result

            # First row is the header — skip it
            for tr in rows[1:]:
                cells = [td.get_text(strip=True) for td in tr.find_all(['td', 'th'])]
                if len(cells) < 10:
                    continue

                client_code = cells[COL_CLIENT_CODE].strip()
                isin        = cells[COL_ISIN].strip()
                instr_code  = cells[COL_INSTR_CODE].strip()
                instr_name  = cells[COL_INSTR_NAME].strip()
                face_value  = cells[COL_FACE_VALUE].strip()

                # Skip non-data rows (empty, total rows, etc.)
                if not client_code or not isin or len(isin) < 5:
                    continue
                # Previously: `if not client_code.startswith(broker_code[:2]):`
                # silently dropped multi-strategy / cross-prefix files.
                # The empty / short-ISIN checks above are sufficient.

                logical  = self.clean_number(cells[COL_BOOK_POS])
                saleable = self.clean_number(cells[COL_TOTAL_SALE])

                result.records.append(HoldingRecord(
                    broker_code      = broker_code,
                    client_id        = client_code,
                    security_code    = instr_code,
                    security_name    = instr_name,
                    isin             = isin,
                    logical_holding  = logical,
                    saleable_holding = saleable,
                    holding_date     = holding_date,
                    face_value       = face_value,
                    source           = self.source_name,
                ))

            logger.info(f"HDFC: parsed {result.record_count} records, "
                        f"broker code: {broker_code}")

        except Exception as e:
            result.error = str(e)
            logger.error(f"HDFC parse error: {e}", exc_info=True)

        return result

    def _extract_broker_code(self, tables) -> str:
        """
        Find the broker/client code from HDFC HTML metadata.
        Looks for a table cell containing 'Client Code :' pattern.
        """
        for table in tables:
            text = table.get_text()
            if 'Client Code' in text and ':' in text:
                # Match patterns like "Client Code : GWPJ"
                match = re.search(r'Client\s+Code\s*:\s*([A-Z0-9]+)', text)
                if match:
                    return match.group(1).strip()
        return ''

    def _find_data_table(self, tables):
        """
        Find the main holdings data table — the largest table
        whose first row contains 'Client Code' and 'ISIN' headers.
        """
        best = None
        best_rows = 0
        for table in tables:
            rows = table.find_all('tr')
            if len(rows) < 2:
                continue
            header_text = rows[0].get_text()
            if 'Client Code' in header_text and ('ISIN' in header_text or 'Instrument' in header_text):
                if len(rows) > best_rows:
                    best = table
                    best_rows = len(rows)
        return best
