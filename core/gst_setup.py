"""
core/gst_setup.py — build the GST Parameter upload xlsx for a client.

Layout of the file (mapid=1033 / templateType=GST):

  A = 'GST'                        UTYPE
  B = numeric WS CLIENTID          CLIENTID
  C = feetype code                 FLD1  → CLIENT_GSTPARAM_M.FEETYPE
  D = client state code            FLD2  → CLIENTSTATECODE
  E = service location id          FLD3  → SERVLOCATEID
  F = firm GSTIN                   FLD4  → GSTREGNO
  G = valid_from (Excel date)      FLD5  → VALIDFROM

No header row. Single sheet 'Sheet1'. Plugs straight into the
upload_client_gst + gst_authorize_pending bridges in ws_uploader.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

_CONFIG_DIR = Path(__file__).resolve().parent.parent / 'config'


def _load_state_codes() -> tuple[dict, dict, dict]:
    """Return (code→name, name_lc→code, alias_lc→code)."""
    path = _CONFIG_DIR / 'ws_state_codes.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    codes = data.get('codes', {})
    aliases = data.get('aliases', {})
    name_to_code = {v.lower(): k for k, v in codes.items()}
    alias_to_code = {k.lower(): v for k, v in aliases.items()}
    return codes, name_to_code, alias_to_code


def _load_gst_config() -> dict:
    return json.loads((_CONFIG_DIR / 'gst_config.json').read_text(encoding='utf-8'))


_CODES, _NAME_TO_CODE, _ALIAS_TO_CODE = _load_state_codes()


def _normalize(s: str) -> str:
    """Trim, collapse internal whitespace, lower-case, drop punctuation that
    operators inconsistently include (commas, periods)."""
    if not s:
        return ''
    s = s.strip().lower()
    s = re.sub(r'[.,]+', ' ', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def state_name_to_code(state_value: str) -> Optional[str]:
    """Resolve a free-text state value to a WS state code.

    Resolution order:
      1. Already a code (2 uppercase letters that exist in our table)
      2. Exact case-insensitive match against the canonical state name
      3. Alias map (handles common misspellings / city names / abbreviations)

    Returns None when nothing matches — caller decides whether to fall
    back to firm_state_code or raise.
    """
    if not state_value:
        return None
    raw = state_value.strip()

    # 1) Direct code (e.g. "MH", "TN")
    if len(raw) == 2 and raw.upper() in _CODES:
        return raw.upper()

    n = _normalize(raw)

    # 2) Canonical state name match
    if n in _NAME_TO_CODE:
        return _NAME_TO_CODE[n]

    # 3) Alias map
    if n in _ALIAS_TO_CODE:
        return _ALIAS_TO_CODE[n]

    return None


def resolve_client_state_code(client: dict, *, fallback_to_firm: bool = True) -> tuple[str, str]:
    """Pick the WS state code for a client.

    Looks at the client's `state` field (primary address state) and runs
    it through ``state_name_to_code``. When that fails, falls back to
    the firm's state code so we still produce a usable GST row.

    Returns (state_code, source) where source is one of:
        'address'  — derived from the client's address state
        'firm'     — fell back to the firm default (no match)
    """
    cfg = _load_gst_config()
    firm_code = (cfg.get('firm_state_code') or 'MH').strip().upper()

    raw = (client.get('state') or '').strip()
    code = state_name_to_code(raw) if raw else None
    if code:
        return code, 'address'
    if fallback_to_firm:
        return firm_code, 'firm'
    return '', 'unmatched'


def build_gst_xlsx(*, client_id: int, state_code: str,
                    valid_from: str = '',
                    output_path: Path) -> Path:
    """Write a single-row GST upload xlsx for one client.

    Args:
        client_id: WS-assigned numeric CLIENTID (e.g. 100072).
        state_code: WS state code (e.g. 'MH'). Caller resolves this
            via ``resolve_client_state_code``.
        valid_from: ISO YYYY-MM-DD. Empty → use gst_config.valid_from_default.
        output_path: where to write the .xlsx (parent dir must exist).
    """
    from openpyxl import Workbook   # local import keeps the module-load
                                    # cheap when only the helpers are needed

    cfg = _load_gst_config()
    gstin     = cfg['firm_gstin']
    serv_id   = int(cfg['service_location_id'])
    fee_type  = cfg.get('fee_type', '*')
    iso_from  = valid_from or cfg.get('valid_from_default', '2025-04-01')

    try:
        valid_from_dt = datetime.strptime(iso_from[:10], '%Y-%m-%d')
    except ValueError as e:
        raise ValueError(
            f"valid_from must be YYYY-MM-DD, got {iso_from!r}: {e}"
        ) from None

    if not state_code or state_code not in _CODES:
        raise ValueError(
            f"state_code must be a known WS code (e.g. 'MH'), got {state_code!r}"
        )
    if not isinstance(client_id, int) or client_id <= 0:
        raise ValueError(f"client_id must be a positive int, got {client_id!r}")

    wb = Workbook()
    ws = wb.active
    ws.title = 'Sheet1'
    ws['A1'] = 'GST'
    ws['B1'] = client_id
    ws['C1'] = fee_type
    ws['D1'] = state_code
    ws['E1'] = serv_id
    ws['F1'] = gstin
    ws['G1'] = valid_from_dt
    ws['G1'].number_format = 'dd/mm/yyyy'   # WS expects DD/MM/YYYY

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(output_path))
    return output_path


def build_gst_xlsx_for_client(client: dict, *,
                                client_id: int,
                                output_path: Path,
                                valid_from: str = '') -> tuple[Path, str]:
    """High-level convenience: derive state code from client record and
    build the GST xlsx in one call.

    Returns (file_path, state_code_source) where state_code_source is
    'address' or 'firm' (see ``resolve_client_state_code``).
    """
    state_code, source = resolve_client_state_code(client)
    path = build_gst_xlsx(
        client_id    = client_id,
        state_code   = state_code,
        valid_from   = valid_from,
        output_path  = output_path,
    )
    return path, source
