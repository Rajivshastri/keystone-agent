"""
core/benchmark_master.py — local cache + WS sync for the WS Benchmark Master.

The Benchmark Master is operator-managed in WS via /fincrm/viewBenchmarks.do.
We mirror it locally at config/benchmarks.json so the Settings → Benchmarks
tab and the Pool/Scheme creation forms can pick a benchmark without making
a round-trip to WS each time.

Cache shape (config/benchmarks.json):
  {
    "_synced_at": "2026-04-29T16:50:00+05:30",
    "rows": [
      {"code": "NIFTY50TRI", "description": "Nifty 50 TRI",
       "nse_ref": "", "bse_ref": "", "internal_ref": "", "index_type": "Standard BM"},
      ...
    ]
  }

Refresh flow:
  refresh_from_ws()  → ws_list_benchmarks → save_local_benchmarks
  create()           → ws_create_benchmark → refresh_from_ws (via session reuse)
  update()           → ws_update_benchmark → refresh_from_ws

Local cache is authoritative for *reads*; writes always go through WS first.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CACHE_FILENAME = 'benchmarks.json'


def _config_dir() -> Path:
    env = os.environ.get('KEYSTONE_CONFIG_DIR')
    if env:
        return Path(env)
    return Path(__file__).parent.parent / 'config'


def _cache_path() -> Path:
    return _config_dir() / CACHE_FILENAME


def load_local_benchmarks() -> dict:
    """Return cached benchmarks dict ({_synced_at, rows}). Empty shape if absent."""
    p = _cache_path()
    if not p.exists():
        return {'_synced_at': '', 'rows': []}
    try:
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {'_synced_at': '', 'rows': []}
        data.setdefault('_synced_at', '')
        data.setdefault('rows', [])
        return data
    except Exception as e:
        logger.warning(f'failed to read {p}: {e}')
        return {'_synced_at': '', 'rows': []}


def save_local_benchmarks(rows: List[dict], synced_at: str = '') -> Path:
    """Atomic write of the cache file. Returns the path written."""
    from core.timeutils import ist_now_str
    if not synced_at:
        synced_at = ist_now_str()
    payload = {'_synced_at': synced_at, 'rows': rows or []}
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{CACHE_FILENAME}.', suffix='.tmp',
                                dir=str(p.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def refresh_from_ws(*, session=None, base=None,
                    auth_cache_path: str = '') -> dict:
    """Fetch the live list from WS and overwrite the local cache.

    Returns: {ok, count, synced_at, rows}.
    Caller can pass an open session to avoid a fresh login.
    """
    import ws_uploader
    listing = ws_uploader.ws_list_benchmarks(
        session=session, base=base, auth_cache_path=auth_cache_path,
    )
    rows = listing.get('rows', []) or []
    save_local_benchmarks(rows)
    return {
        'ok':         True,
        'count':      len(rows),
        'synced_at':  load_local_benchmarks().get('_synced_at', ''),
        'rows':       rows,
        'session':    listing.get('session'),
        'base':       listing.get('base'),
    }


def create(code: str, description: str, *,
            short_name: str = '', nse_ref: str = '', series: str = '',
            bse_ref: str = '', internal_ref: str = '', value_source: str = '',
            ref6: str = '', ref7: str = '', ref8: str = '', ref9: str = '',
            session=None, base=None,
            auth_cache_path: str = '') -> dict:
    """Create a benchmark on WS and refresh the local cache."""
    import ws_uploader
    res = ws_uploader.ws_create_benchmark(
        code=code, description=description, short_name=short_name,
        nse_ref=nse_ref, series=series, bse_ref=bse_ref,
        internal_ref=internal_ref, value_source=value_source,
        ref6=ref6, ref7=ref7, ref8=ref8, ref9=ref9,
        session=session, base=base, auth_cache_path=auth_cache_path,
    )
    if res.get('status') == 'created':
        try:
            refresh_from_ws(session=res.get('session'), base=res.get('base'))
        except Exception as e:
            logger.warning(f'cache refresh after create failed: {e}')
    # Strip the verbose response_text before returning to UI.
    return {k: v for k, v in res.items() if k != 'response_text'}


def update(code: str, description: str, *,
            short_name: str = '', nse_ref: str = '', series: str = '',
            bse_ref: str = '', internal_ref: str = '', value_source: str = '',
            ref6: str = '', ref7: str = '', ref8: str = '', ref9: str = '',
            session=None, base=None,
            auth_cache_path: str = '') -> dict:
    """Modify an existing benchmark on WS and refresh the local cache."""
    import ws_uploader
    res = ws_uploader.ws_update_benchmark(
        code=code, description=description, short_name=short_name,
        nse_ref=nse_ref, series=series, bse_ref=bse_ref,
        internal_ref=internal_ref, value_source=value_source,
        ref6=ref6, ref7=ref7, ref8=ref8, ref9=ref9,
        session=session, base=base, auth_cache_path=auth_cache_path,
    )
    if res.get('status') == 'modified':
        try:
            refresh_from_ws(session=res.get('session'), base=res.get('base'))
        except Exception as e:
            logger.warning(f'cache refresh after update failed: {e}')
    return {k: v for k, v in res.items() if k != 'response_text'}


def lookup(code: str) -> Optional[Dict]:
    """Fast cache lookup by benchmark code (case-insensitive)."""
    if not code:
        return None
    target = code.strip().upper()
    for row in load_local_benchmarks().get('rows', []) or []:
        if (row.get('code') or '').strip().upper() == target:
            return row
    return None
