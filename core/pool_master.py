"""
Pool Master Loader
------------------
Reads Z13_PoolMaster.xlsx and builds four lookup tables used by the
bank reconciliation engine.

Columns in the xlsx (0-based):
  0  MAPINID        e.g. MYSTICM, GOLDETEPMS, 204496800
  1  DP             NSDL
  2  DPID           e.g. IN301348
  3  DPCLIENTID     e.g. 20886076
  4  REMARKS        Strategy description
  5  REFCODE1..10   REFCODE6 = Kotak client account number (9000032175)
  15 FIRMID         WS exports this column populated with 0.0 in the
                    files we receive — value is NOT used. The MAPINID
                    (col 0) doubles as the custodian alias / firmid.
                    If WS ever ships a populated col 15 the ingest
                    will need updating; until then `info['firmid']`
                    is set to the MAPINID.
  16 POOLID         WS Pool/Scheme code (float 1.0, 2.0 …)

Lookup tables built:
  nsdl_to_mapid      "NSDL-IN301348-20886076" -> "MYSTICM"
  firmid_to_mapid    "GOLDETEPMS"             -> "GOLDETEPMS"
  kotak_client_to_mapid "9000032175"          -> "204496800"
  mapid_to_info      "MYSTICM" -> {name, firmid, kotak_client, pool_id}

ICICI pool accounts have no automatic key — they must be provided in
icici_pool_map: { "000405165526": "MYSTICM", ... }
"""
import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class PoolMaster:

    def __init__(self):
        self.nsdl_to_mapid:         Dict[str, str] = {}   # "NSDL-DPID-CLIENTID" -> mapid
        self.firmid_to_mapid:       Dict[str, str] = {}   # firmid -> mapid
        self.kotak_client_to_mapid: Dict[str, str] = {}   # kotak client id -> mapid
        self.mapid_to_info:         Dict[str, dict] = {}  # mapid -> full info
        self._loaded = False
        # Tripwire: WS historically ships col 15 (FIRMID) as 0.0 for every
        # row and we use MAPINID (col 0) as the firmid. If WS ever turns
        # col 15 on, we want to KNOW about it instead of silently ignoring
        # it. _col15_warned ensures we log once per load(), not per row.
        self._col15_warned = False

    # ------------------------------------------------------------------ #

    def load(self, xlsx_path: str) -> 'PoolMaster':
        try:
            ext = str(xlsx_path).lower().rsplit('.', 1)[-1]
            if ext == 'xls':
                import xlrd as _xlrd
                _wb = _xlrd.open_workbook(xlsx_path)
                _ws = _wb.sheet_by_index(0)
                rows = [tuple(_ws.cell_value(rx, cx) for cx in range(_ws.ncols))
                        for rx in range(_ws.nrows)]
            else:
                import openpyxl
                wb = openpyxl.load_workbook(xlsx_path, read_only=True)
                ws = wb.active
                rows = list(ws.iter_rows(values_only=True))
                wb.close()
            if not rows:
                logger.warning('PoolMaster: empty workbook')
                return self

            # Skip header row (first row has column names)
            for row in rows[1:]:
                if not row or row[0] is None:
                    continue
                self._ingest_row(row)

            self._loaded = True
            logger.info(
                f'PoolMaster: loaded {len(self.mapid_to_info)} entries — '
                f'{len(self.nsdl_to_mapid)} NSDL keys, '
                f'{len(self.firmid_to_mapid)} firmid keys, '
                f'{len(self.kotak_client_to_mapid)} Kotak client keys'
            )
        except Exception as e:
            logger.error(f'PoolMaster load error: {e}', exc_info=True)
        return self

    def load_from_rows(self, rows) -> 'PoolMaster':
        """Load from an iterable of tuples (for testing / in-memory use)."""
        for row in rows:
            self._ingest_row(row)
        self._loaded = True
        return self

    # ------------------------------------------------------------------ #

    def _ingest_row(self, row):
        def s(v): return str(v).strip() if v is not None else ''

        mapid      = s(row[0])
        dp         = s(row[1])
        dpid       = s(row[2])
        clientid   = s(row[3])
        name       = s(row[4])
        # REFCODE1-10 are cols 5-14
        refcodes   = [s(row[i]) if i < len(row) else '' for i in range(5, 15)]
        kotak_client = refcodes[5]   # REFCODE6 = col 10 = refcodes index 5
        # FIRMID (col 15) is stored as 0.0 in this file — not used
        # The MAPINID itself IS the firmid-equivalent (e.g. GOLDETEPMS, GWPJ0004, MYSTICM)
        firmid_col15 = row[15] if len(row) > 15 else None
        pool_id      = row[16] if len(row) > 16 else None

        # Tripwire — if WS ever populates col 15 with a real firmid, we
        # want a loud warning. Log once per load (not per row). The
        # value is still ignored for now — flip to active use only after
        # an operator confirms it should override MAPINID.
        if firmid_col15 not in (None, '', 0, 0.0) and not self._col15_warned:
            try:
                _is_zero = float(firmid_col15) == 0.0
            except (TypeError, ValueError):
                _is_zero = False
            if not _is_zero:
                logger.warning(
                    f"PoolMaster: col 15 (FIRMID) is now populated "
                    f"(first seen on MAPINID={mapid!r}, value={firmid_col15!r}). "
                    f"Currently IGNORED — MAPINID is still used as the firmid. "
                    f"If WS expects col 15 to override MAPINID, update "
                    f"core/pool_master.py:_ingest_row to use it."
                )
                self._col15_warned = True

        if not mapid:
            return

        info = {
            'mapid':        mapid,
            'name':         name,
            'firmid':       mapid,          # MAPINID doubles as firmid
            'kotak_client': kotak_client,
            'pool_id':      pool_id,
            'dp':           dp,
            'dpid':         dpid,
            'clientid':     clientid,
        }
        self.mapid_to_info[mapid] = info

        # NSDL key: "NSDL-IN301348-20886076"
        if dp and dpid and clientid:
            nsdl_key = f'{dp}-{dpid}-{clientid}'
            self.nsdl_to_mapid[nsdl_key] = mapid

        # firmid key: use MAPINID (non-numeric codes like GOLDETEPMS, GWPJ0004)
        # Skip pure-numeric MAPIDs (like '204496800') — those are Kotak rows
        if mapid and not mapid.replace('.', '', 1).isdigit():
            self.firmid_to_mapid[mapid] = mapid

        # Also register any non-empty REFCODE values as alternative firmid keys
        # (catches cases where MAPINID differs from C_GROUP)
        for rc in refcodes:
            if rc and not rc.replace('.', '', 1).isdigit():
                if rc not in self.firmid_to_mapid:
                    self.firmid_to_mapid[rc] = mapid

        # Kotak client account key (10-digit number starting with 9)
        if kotak_client and kotak_client.isdigit():
            self.kotak_client_to_mapid[kotak_client] = mapid

    # ------------------------------------------------------------------ #
    #  Lookup helpers                                                       #
    # ------------------------------------------------------------------ #

    def mapid_from_nsdl(self, nsdl_key: str) -> Optional[str]:
        """nsdl_key = 'NSDL-IN301348-20886076'"""
        return self.nsdl_to_mapid.get(nsdl_key)

    def mapid_from_firmid(self, firmid: str) -> Optional[str]:
        """firmid = 'GOLDETEPMS', 'GWPJ0004', 'MYSTICM' …"""
        return self.firmid_to_mapid.get(firmid)

    def mapid_from_kotak_client(self, client_id: str) -> Optional[str]:
        """client_id = '9000032175'"""
        return self.kotak_client_to_mapid.get(client_id)

    def strategy_name(self, mapid: str) -> str:
        return self.mapid_to_info.get(mapid, {}).get('name', mapid)

    def is_loaded(self) -> bool:
        return self._loaded

    # ------------------------------------------------------------------ #
    #  Custodian-side mapping                                               #
    # ------------------------------------------------------------------ #

    def resolve_custodian_account(
        self,
        bank:            str,         # 'ICICI' | 'HDFC' | 'AXIS' | 'KOTAK'
        account_no:      str,         # custodian pool account number
        c_group:         str = '',    # Axis: C_GROUP column
        zip_alias:       str = '',    # HDFC: zip filename prefix (e.g. 'GWPJ')
        kotak_client_id: str = '',    # Kotak: client account number
        icici_pool_map:  dict = None, # {'000405165526': 'MYSTICM', ...}
    ) -> Optional[str]:
        """
        Resolve a custodian pool account to a MAPID.
        Returns None if the account is an artificial (unmappable) account.
        """
        bank_upper = bank.upper()

        if bank_upper == 'AXIS' and c_group:
            mapid = self.mapid_from_firmid(c_group)
            if mapid:
                return mapid

        if bank_upper == 'HDFC' and zip_alias:
            # Try exact alias first, then try prefix match against firmids
            mapid = self.mapid_from_firmid(zip_alias)
            if mapid:
                return mapid
            # Some firmids are like GWPJ0004 — match by prefix
            for fid, mid in self.firmid_to_mapid.items():
                if fid.startswith(zip_alias):
                    return mid

        if bank_upper == 'KOTAK' and kotak_client_id:
            mapid = self.mapid_from_kotak_client(kotak_client_id)
            if mapid:
                return mapid

        if bank_upper == 'ICICI' and icici_pool_map:
            mapid = icici_pool_map.get(account_no)
            if mapid:
                return mapid

        # No match -> artificial account
        return None
