"""
Client Bank Details Loader
---------------------------
Reads Z13_ClientDPBankDetails.xlsx and builds a direct lookup table:
  '{BANKCODE}-{BANK_ACCOUNT}' → MAPID

This is the authoritative source for mapping every WS Bank Book account
to its strategy pool.  Completely replaces the previous fallback chain
(custodian_acct NSDL lookup + scheme-name partial match).

Columns used:
  MAPINID       — Pool Master MAPID (e.g. GOLDETEPMS, GWPJ0004, MYSTICV)
  BANKCODE      — Bank prefix (e.g. ICICI, HDFC, AXIS, KMBL, SBI ...)
  BANK_ACCOUNT  — Client bank account number
  CLIENTCODE    — WS client code (e.g. ETER000001, GWPJ0002)
  NAME          — Investor name
  SCHEMEID      — WS scheme number

For multi-strategy investors, the same bank account can appear under
multiple scheme sections in the WS Bank Book.  The client file records
the 'primary' MAPID; the WS scheme context is the authoritative
attribution for each balance/transaction entry.
"""
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class ClientBankDetails:

    def __init__(self):
        # ws_key -> {mapid, clientcode, name, schemeid}
        self._map: Dict[str, dict] = {}
        self._loaded = False

    # ------------------------------------------------------------------ #

    def load(self, xlsx_path: str) -> 'ClientBankDetails':
        try:
            import openpyxl
            _ext_cbd = str(xlsx_path).lower().rsplit(".",1)[-1]
            if _ext_cbd == "xls":
                import xlrd as _xlrd_cbd
                _wb_cbd = _xlrd_cbd.open_workbook(xlsx_path)
                _ws_cbd = _wb_cbd.sheet_by_index(0)
                rows = [tuple(_ws_cbd.cell_value(rx,cx) for cx in range(_ws_cbd.ncols)) for rx in range(_ws_cbd.nrows)]
            else:
                wb = openpyxl.load_workbook(xlsx_path, read_only=True)
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                wb.close()
            if not rows:
                logger.warning('ClientBankDetails: empty file')
                return self

            headers = [str(h).strip() if h else '' for h in rows[0]]
            col = {h: i for i, h in enumerate(headers)}

            required = {'MAPINID', 'BANKCODE', 'BANK_ACCOUNT'}
            missing  = required - set(col.keys())
            if missing:
                logger.error(f'ClientBankDetails missing columns: {missing}')
                return self

            count = 0
            for row in rows[1:]:
                if not row or row[col['MAPINID']] is None:
                    continue

                def s(c): return str(row[col[c]] or '').strip() if c in col else ''

                mapid      = s('MAPINID')
                bankcode   = s('BANKCODE')
                bank_acct  = s('BANK_ACCOUNT')
                clientcode = s('CLIENTCODE')
                name       = s('NAME')
                schemeid   = row[col['SCHEMEID']] if 'SCHEMEID' in col else None

                if not (mapid and bankcode and bank_acct):
                    continue

                ws_key = f'{bankcode}-{bank_acct}'
                self._map[ws_key] = {
                    'mapid':      mapid,
                    'clientcode': clientcode,
                    'name':       name,
                    'schemeid':   schemeid,
                }
                count += 1

            self._loaded = True
            logger.info(f'ClientBankDetails: loaded {count} entries '
                        f'({len(set(v["mapid"] for v in self._map.values()))} unique MAPIDs)')

        except Exception as e:
            logger.error(f'ClientBankDetails load error: {e}', exc_info=True)
        return self

    # ------------------------------------------------------------------ #

    def mapid(self, ws_bank_key: str) -> Optional[str]:
        """Look up MAPID for a WS Bank Account key (e.g. 'HDFC-00121000040654')."""
        entry = self._map.get(ws_bank_key)
        return entry['mapid'] if entry else None

    def client_info(self, ws_bank_key: str) -> Optional[dict]:
        return self._map.get(ws_bank_key)

    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def all_entries(self) -> dict:
        return dict(self._map)

    def to_json(self) -> dict:
        """Serialise for storage in config dir."""
        return self._map

    def load_from_dict(self, d: dict) -> 'ClientBankDetails':
        self._map = d
        self._loaded = bool(d)
        return self
