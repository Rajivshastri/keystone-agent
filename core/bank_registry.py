"""
Bank registry — single source of truth for the list of custodian-bank
sources Keystone knows about.

Tier 3 of the multi-tenant cleanup. The bank-recon hot path has
multiple call sites that hardcoded the 4-custodian assumption
('icici', 'hdfc', 'kotak', 'axis'). This module reads ``config/
sources.json`` and returns whichever rows are flagged ``is_bank=true``,
so adding a 5th custodian becomes a sources.json edit + writing the
new parser (PARSER_REGISTRY pattern, already config-driven).

Each entry:
  {
    'name':            'icici_bank',          (unique key — also folder)
    'short':           'icici',               (canonical short label)
    'parser':          'icici_bank',          (PARSER_REGISTRY key)
    'bank_full_name':  'ICICI Bank Ltd.',     (Bank Master form text)
    'sender_email':    [...],                  (whatever sources.json has)
  }

Per-bank RESOLUTION logic (Axis uses C_GROUP, HDFC uses zip alias,
ICICI uses pool_map, Kotak uses kotak_client_id) deliberately stays
in code — those are intrinsic to each bank's file format and can't
be reduced to data without losing semantics. Adding a 5th custodian
therefore requires extending ``_resolve_cust_mapid`` in
``core/bank_vs_ws_recon.py`` with the new bank's hint type.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _sources_path() -> Path:
    cfg_dir = os.environ.get('KEYSTONE_CONFIG_DIR')
    if cfg_dir:
        return Path(cfg_dir) / 'sources.json'
    return Path(__file__).parent.parent / 'config' / 'sources.json'


def _load_sources() -> List[Dict]:
    p = _sources_path()
    if not p.exists():
        return []
    try:
        return (json.loads(p.read_text(encoding='utf-8')).get('sources') or [])
    except Exception as e:
        logger.warning(f'sources.json read failed: {e}')
        return []


def enabled_banks() -> List[Dict]:
    """Return one normalised dict per active bank source. The
    ``short`` key strips ``_bank`` and ``_bank_balance`` suffixes so
    callers that key on the canonical custodian short-label
    ('axis' / 'hdfc' / 'icici' / 'kotak') keep working.

    De-duplicates by ``short`` so the HDFC twin (hdfc_bank +
    hdfc_bank_balance) appears once. The first row encountered wins —
    ``hdfc_bank`` ranks before ``hdfc_bank_balance`` in the seed file
    so that's preserved.
    """
    out: List[Dict] = []
    seen: set = set()
    for src in _load_sources():
        if not src.get('is_bank'):
            continue
        if not src.get('active', True):
            continue
        nm = (src.get('name') or '').strip()
        if not nm:
            continue
        # Normalise to a custodian short-label.
        short = nm
        for suf in ('_bank_balance', '_bank'):
            if short.endswith(suf):
                short = short[:-len(suf)]
                break
        if short in seen:
            continue
        seen.add(short)
        out.append({
            'name':           nm,
            'short':          short,
            'parser':         src.get('parser') or nm,
            'bank_full_name': (src.get('bank_full_name') or '').strip(),
            'sender_email':   src.get('sender_email'),
        })
    return out


def bank_folder_names() -> List[str]:
    """Folder names under ``data/{date}/raw/`` to scan for bank
    statements — typically ['axis_bank', 'hdfc_bank', 'icici_bank',
    'kotak_bank']. Used by core.recon_exclusions.scan_bank_pools and
    anywhere else that walks the raw-files tree."""
    return [src['name'] for src in _load_sources()
            if src.get('is_bank') and src.get('active', True)]


def bank_short_names() -> List[str]:
    """Canonical short labels — 'axis', 'hdfc', 'icici', 'kotak'.
    Used by callers that index bank-pool maps from PoolsHub."""
    return [b['short'] for b in enabled_banks()]


def bank_full_name(short: str) -> Optional[str]:
    """Lookup by canonical short label."""
    if not short:
        return None
    target = short.strip().lower()
    for b in enabled_banks():
        if b['short'].lower() == target:
            return b['bank_full_name'] or None
    return None
