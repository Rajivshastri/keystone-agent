"""
core/broker_aliases.py — single source of truth for broker pool aliases.

After the 2026-Q2 migration, all broker→pool alias codes (UCC, client codes,
trading codes) live in broker_map.json under brokers[].pool_aliases. This
module is the I/O layer used by both the Settings → Brokers tab and the
Edit Pool tab so they stay in lockstep.

Shape on disk (broker_map.json):
  brokers: [
    {dealer_code, name, ..., pool_aliases: [{pool_id, alias_code, note}, ...]},
    ...
  ]

Shape returned per-pool by ``aliases_for_pool``:
  [{broker_dealer_code, broker_name, alias_code, note}, ...]

The UI flips between the broker-centric view (group by broker) and the
pool-centric view (group by pool) but both edit the same array.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _config_dir() -> Path:
    env = os.environ.get('KEYSTONE_CONFIG_DIR')
    if env:
        return Path(env)
    return Path(__file__).parent.parent / 'config'


def _broker_map_path() -> Path:
    return _config_dir() / 'broker_map.json'


def load_broker_map() -> dict:
    p = _broker_map_path()
    if not p.exists():
        return {'brokers': [], 'trade_sources': []}
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def save_broker_map(bm: dict) -> None:
    p = _broker_map_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.broker_map.', suffix='.json.tmp', dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(bm, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def aliases_for_pool(bm: dict, pool_id: str) -> List[Dict]:
    """Return all aliases that point to ``pool_id``, with broker context.

    Output shape: [{broker_dealer_code, broker_name, alias_code, note}, ...].
    Sorted by broker_dealer_code then alias_code for stable rendering.
    """
    pid = (pool_id or '').strip()
    if not pid:
        return []
    out: List[Dict] = []
    for b in bm.get('brokers', []) or []:
        for a in b.get('pool_aliases', []) or []:
            if (a.get('pool_id') or '').strip() == pid:
                out.append({
                    'broker_dealer_code': b.get('dealer_code', ''),
                    'broker_name':        b.get('name', ''),
                    'alias_code':         a.get('alias_code', ''),
                    'note':               a.get('note', ''),
                })
    out.sort(key=lambda x: (x['broker_dealer_code'], x['alias_code']))
    return out


def replace_aliases_for_pool(bm: dict, pool_id: str,
                              new_aliases: List[Dict]) -> dict:
    """Replace every alias targeting ``pool_id`` with the supplied list.

    Each entry in ``new_aliases`` must include ``broker_dealer_code`` and
    ``alias_code``. ``note`` is optional. Brokers referenced in ``new_aliases``
    must already exist in the broker map — unknown dealer codes are
    rejected (caller should create the broker via the Brokers tab first).

    Returns the mutated broker_map dict (caller is responsible for saving).
    """
    pid = (pool_id or '').strip()
    if not pid:
        raise ValueError('pool_id required')

    # First strip existing entries for this pool from every broker.
    for b in bm.get('brokers', []) or []:
        before = b.get('pool_aliases', []) or []
        b['pool_aliases'] = [a for a in before
                             if (a.get('pool_id') or '').strip() != pid]

    # Index brokers by dealer_code so we can append efficiently.
    by_dealer = {(b.get('dealer_code') or '').strip().upper(): b
                 for b in bm.get('brokers', []) or []}

    rejected = []
    for entry in new_aliases or []:
        dc = (entry.get('broker_dealer_code') or '').strip().upper()
        code = (entry.get('alias_code') or '').strip()
        note = (entry.get('note') or '').strip()
        if not dc or not code:
            rejected.append({'reason': 'missing_dealer_or_code', 'entry': entry})
            continue
        broker = by_dealer.get(dc)
        if broker is None:
            rejected.append({'reason': 'unknown_dealer', 'dealer_code': dc, 'entry': entry})
            continue
        broker.setdefault('pool_aliases', []).append({
            'pool_id':    pid,
            'alias_code': code,
            'note':       note,
        })

    if rejected:
        # Caller surfaces this — we don't silently drop rows.
        raise ValueError(f'rejected {len(rejected)} alias entry(ies): {rejected}')

    return bm


def all_pool_alias_index(bm: Optional[dict] = None) -> Dict[str, List[Dict]]:
    """Return {pool_id: [aliases...]} for all pools — used for bulk views."""
    if bm is None:
        bm = load_broker_map()
    idx: Dict[str, List[Dict]] = {}
    for b in bm.get('brokers', []) or []:
        for a in b.get('pool_aliases', []) or []:
            pid = (a.get('pool_id') or '').strip()
            if not pid:
                continue
            idx.setdefault(pid, []).append({
                'broker_dealer_code': b.get('dealer_code', ''),
                'broker_name':        b.get('name', ''),
                'alias_code':         a.get('alias_code', ''),
                'note':               a.get('note', ''),
            })
    for k in idx:
        idx[k].sort(key=lambda x: (x['broker_dealer_code'], x['alias_code']))
    return idx
