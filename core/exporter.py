"""
Exporter
--------
Generates the WealthSpectrum Holdings Upload file.

Upload format (9 columns):
  A: Client code     — UCC / Client Code / Cln Code from broker file
  B: Security type   — BLANK
  C: Security code   — SYMBOLID from WS Security Master (looked up via ISIN)
  D: Security name   — BLANK
  E: ISIN            — ISIN code
  F: Logical holding — total position
  G: Saleable hold.  — saleable position
  H: Holding date    — DD/MM/YYYY from custody file
  I: Face value      — BLANK

One row per client per security. No aggregation across clients.

Mandatory WS master files (all must be in masters/ for the date):
  1. Z13_PoolMaster.xlsx       — WS scheme codes (validates coverage)
  2. Z13_SecurityDetail.xlsx   — ISIN -> SYMBOLID lookup
  3. Z13_0_..._KotakWM_...xlsx — Kotak trade-day client code remapping
"""
import logging
import os
from typing import List, Dict, Optional, Tuple
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

from parsers.base import HoldingRecord

logger = logging.getLogger(__name__)


def load_security_master(path: str) -> Dict[str, dict]:
    """
    Load Z13_SecurityDetail.xlsx.
    Returns {isin: {'symbolid': str, 'face_value': float}}
    Col A = SYMBOLID, Col G = ISINCODE, Col I = FACEVAL
    """
    logger.info(f"Loading security master: {os.path.basename(path)}")
    lookup: Dict[str, dict] = {}
    try:
        _ext_e = str(path).lower().rsplit(".",1)[-1]
        if _ext_e == "xls":
            import xlrd as _xlrd_e
            _wb_e = _xlrd_e.open_workbook(path)
            _ws_e = _wb_e.sheet_by_index(0)
            _rows_e = (tuple(_ws_e.cell_value(rx,cx) for cx in range(_ws_e.ncols)) for rx in range(1,_ws_e.nrows))
        else:
            _wb_ox = openpyxl.load_workbook(path, read_only=True, data_only=True)
            _rows_e = _wb_ox[_wb_ox.sheetnames[0]].iter_rows(min_row=2, values_only=True)
        for row in _rows_e:
            if not row or row[0] is None:
                continue
            symbolid = str(row[0] or '').strip()
            isin     = str(row[6] or '').strip()
            face_val = row[8]
            if isin and symbolid:
                lookup[isin] = {
                    'symbolid':   symbolid,
                    'face_value': float(face_val) if face_val else 0.0,
                }
        if _ext_e == "xls":
            _wb_e.release_resources()
        else:
            _wb_ox.close()
        logger.info(f"Security master loaded: {len(lookup):,} securities")
    except Exception as e:
        logger.error(f"Failed to load security master: {e}", exc_info=True)
    return lookup


def load_kotak_custody(path: str) -> Dict[Tuple[str, str], str]:
    """
    Load the Kotak custody/transactions file.
    Returns {(df_client_code, isin): rf_client_code}
    Col A = DF CLIENT CODE, Col B = RF CLIENT CODE, Col C = ISIN
    Supports both .xls (xlrd) and .xlsx (openpyxl).
    """
    logger.info(f"Loading Kotak custody file: {os.path.basename(path)}")
    mapping: Dict[Tuple[str, str], str] = {}
    try:
        ext = str(path).lower().rsplit('.', 1)[-1]
        if ext == 'xls':
            import xlrd as _xlrd
            wb = _xlrd.open_workbook(path)
            ws = wb.sheet_by_index(0)
            rows = (tuple(ws.cell_value(rx, cx) for cx in range(ws.ncols))
                    for rx in range(1, ws.nrows))
        else:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            ws = wb[wb.sheetnames[0]]
            rows = ws.iter_rows(min_row=2, values_only=True)
        for row in rows:
            if not row or row[0] is None or row[0] == '':
                continue
            df_code = str(row[0] or '').strip()
            rf_code = str(row[1] or '').strip()
            isin    = str(row[2] or '').strip()
            if df_code and rf_code and isin:
                mapping[(df_code, isin)] = rf_code
        logger.info(f"Kotak custody loaded: {len(mapping)} trade-day remappings")
    except Exception as e:
        logger.error(f"Failed to load Kotak custody file: {e}", exc_info=True)
    return mapping


class Exporter:

    HEADERS = [
        'Scheme code', 'Security type', 'Security code', 'Security name',
        'ISIN code', 'Logical holding', 'Saleable holding', 'Holding date',
        'Face value'
    ]

    def __init__(self, mappings_config: dict):
        self.mappings = mappings_config.get('strategy_mappings', [])

    def resolve_ws_scheme_code(self, broker_code: str, client_id: str,
                                source: str) -> Optional[str]:
        for mapping in self.mappings:
            if (mapping.get('source') == source and
                    mapping.get('broker_code') == broker_code):
                for rule in mapping.get('conditional_rules', []):
                    if rule.get('if_client_id') == client_id:
                        return rule['then_ws_scheme_code']
                    pfx = rule.get('if_client_id_prefix', '')
                    if pfx and client_id.startswith(pfx):
                        return rule['then_ws_scheme_code']
                return mapping.get('default_ws_scheme_code')
        return None

    def get_all_configured_scheme_codes(self) -> List[dict]:
        seen = set()
        result = []
        # Pass 1: real pool mappings, with full metadata (including
        # parent_pool_id for sub-accounts). This MUST run before the
        # conditional-rule pass — otherwise a parent's `if_client_id` rule
        # whose target scheme code matches a sub-account would shadow the
        # sub-account's own entry and strip its parent linkage.
        for mapping in self.mappings:
            code = mapping.get('default_ws_scheme_code')
            if code and code not in seen:
                seen.add(code)
                result.append({
                    'ws_scheme_code': code,
                    'display_name':   mapping.get('display_name', code),
                    'source':         mapping.get('source', ''),
                    'broker_code':    mapping.get('broker_code', ''),
                    'pool_id':        mapping.get('id', ''),
                    'parent_pool_id': mapping.get('parent_pool_id', ''),
                    'is_sub_account': bool(mapping.get('is_sub_account')),
                })
        # Pass 2: scheme codes that only appear as conditional-rule targets.
        for mapping in self.mappings:
            for rule in mapping.get('conditional_rules', []):
                ccode = rule.get('then_ws_scheme_code')
                if ccode and ccode not in seen:
                    seen.add(ccode)
                    result.append({
                        'ws_scheme_code': ccode,
                        'display_name':   ccode,
                        'source':         mapping.get('source', ''),
                        'broker_code':    mapping.get('broker_code', ''),
                        'pool_id':        '',
                        'parent_pool_id': '',
                        'is_sub_account': False,
                    })
        return result

    def export(self, records: List[HoldingRecord],
               output_dir: str, date_str: str,
               security_master_path: Optional[str] = None,
               kotak_custody_path: Optional[str] = None,
               ) -> Tuple[str, List[str]]:
        """
        Generate the WS Holdings upload file.
        One row per client per security.
        Columns B, D, H are blank.
        Column A = client code, Column C = SYMBOLID from security master.
        """
        warnings = []

        # Load security master
        sec_master: Dict[str, dict] = {}
        if security_master_path and os.path.exists(security_master_path):
            sec_master = load_security_master(security_master_path)
        else:
            warnings.append(
                "Security Detail master not found — SYMBOLID column will be blank. "
                "Upload Z13_SecurityDetail.xlsx to masters/."
            )

        # Load Kotak custody remapping
        kotak_remap: Dict[Tuple[str, str], str] = {}
        if kotak_custody_path and os.path.exists(kotak_custody_path):
            kotak_remap = load_kotak_custody(kotak_custody_path)
        else:
            warnings.append(
                "Kotak custody file not found — trade-day client code remapping skipped. "
                "Upload the Kotak transactions file to masters/."
            )

        # Build output rows
        rows = []
        missing_isins = set()
        kotak_remaps_applied = 0

        for rec in records:
            isin      = rec.isin.strip()
            client_id = rec.client_id

            # Kotak trade-day remap
            if rec.source == 'kotak' and kotak_remap:
                remapped = kotak_remap.get((client_id, isin))
                if remapped:
                    client_id = remapped
                    kotak_remaps_applied += 1

            # ISIN -> SYMBOLID lookup
            sec_info  = sec_master.get(isin, {})
            symbolid  = sec_info.get('symbolid', '')
            if not symbolid and isin:
                missing_isins.add(isin)

            rows.append({
                'client_id':     client_id,
                'security_code': symbolid,
                'isin':          isin,
                'logical':       rec.logical_holding,
                'saleable':      rec.saleable_holding,
                'holding_date':  rec.holding_date,
            })

        if missing_isins:
            warnings.append(
                f"{len(missing_isins)} ISIN(s) not found in security master "
                f"(SYMBOLID will be blank): "
                f"{', '.join(sorted(missing_isins)[:10])}"
                + (" …and more" if len(missing_isins) > 10 else "")
            )

        if kotak_remaps_applied:
            logger.info(f"Kotak: applied {kotak_remaps_applied} trade-day client code remappings")

        # Sort by client_id then ISIN
        rows.sort(key=lambda r: (r['client_id'], r['isin']))

        # Write Excel
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        date_fmt = date_str.replace('-', '')
        filename = f"WS_Holdings_{date_fmt}.xlsx"
        out_path = os.path.join(output_dir, filename)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Holdings Upload'

        header_fill = PatternFill('solid', fgColor='1B2A4A')
        header_font = Font(bold=True, color='FFFFFF', name='Calibri', size=10)
        for col_idx, header in enumerate(self.HEADERS, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.fill      = header_fill
            cell.font      = header_font
            cell.alignment = Alignment(horizontal='center')

        alt_fill  = PatternFill('solid', fgColor='F5F7FA')
        data_font = Font(name='Calibri', size=10)

        for row_idx, row in enumerate(rows, 2):
            fill = alt_fill if row_idx % 2 == 0 else None
            values = [
                row['client_id'],     # A — client code
                '',                   # B — blank
                row['security_code'], # C — SYMBOLID
                '',                   # D — blank
                row['isin'],          # E — ISIN
                row['logical'],       # F — logical holding
                row['saleable'],      # G — saleable holding
                row['holding_date'],  # H — holding date DD/MM/YYYY
                '',                   # I — blank
            ]
            for col_idx, val in enumerate(values, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                cell.font = data_font
                if fill:
                    cell.fill = fill

        col_widths = [16, 8, 16, 8, 16, 16, 16, 8, 12]
        for i, width in enumerate(col_widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width

        ws.freeze_panes = 'A2'
        wb.save(out_path)

        logger.info(f"Exported {len(rows)} rows -> {filename}")
        return out_path, warnings

    def get_coverage(self, records: List[HoldingRecord]) -> dict:
        coverage = {}
        # Sub-account → parent linkage, used after the record loop to
        # propagate "file present" from a parent pool to its sub-pools that
        # share the same physical custody file (e.g. Aristos NRO Mustafa).
        sub_to_parent_pool: dict = {}
        pool_to_scheme:    dict = {}
        for cfg in self.get_all_configured_scheme_codes():
            ws_code = cfg['ws_scheme_code']
            coverage[ws_code] = {
                'ws_scheme_code': ws_code,
                'display_name':   cfg['display_name'],
                'source':         cfg['source'],
                'broker_code':    cfg['broker_code'],
                'record_count':   0,
                'covered':        False,
            }
            if cfg.get('pool_id'):
                pool_to_scheme[cfg['pool_id']] = ws_code
            if cfg.get('is_sub_account') and cfg.get('parent_pool_id'):
                sub_to_parent_pool[ws_code] = cfg['parent_pool_id']
        for rec in records:
            ws_code = self.resolve_ws_scheme_code(
                rec.broker_code, rec.client_id, rec.source
            )
            if not ws_code:
                continue
            if ws_code in coverage:
                coverage[ws_code]['record_count'] += 1
                coverage[ws_code]['covered'] = True
            else:
                coverage[ws_code] = {
                    'ws_scheme_code': ws_code,
                    'display_name':   ws_code,
                    'source':         rec.source,
                    'broker_code':    rec.broker_code,
                    'record_count':   1,
                    'covered':        True,
                }
        # Inherit coverage: a sub-account is "covered" whenever its parent
        # pool's custody file was loaded, even if the sub-account holds no
        # positions today.
        for sub_scheme, parent_pool_id in sub_to_parent_pool.items():
            if coverage[sub_scheme]['covered']:
                continue
            parent_scheme = pool_to_scheme.get(parent_pool_id)
            if parent_scheme and coverage.get(parent_scheme, {}).get('covered'):
                coverage[sub_scheme]['covered'] = True
                coverage[sub_scheme]['inherited_from'] = parent_scheme
        return coverage
