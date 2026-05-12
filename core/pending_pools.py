"""
core/pending_pools.py — stash for pool-creation runs that are waiting
on something out-of-band before they can complete on WS.

The classic case is HDFC: the CML email arrives with the bank's UCC,
but the CP Code is issued LATER in a separate communication after
the bank/demat accounts are opened. Operator can't run the full
WS create chain (IA → Scheme → Bank → Pool) until CP is in hand,
but the CML PDF + Schedule-A + parsed values are valuable to capture
right now so they don't get lost while waiting.

Storage layout — one JSON file per pending record at:

    data/pending_pools/<uuid>.json

Shape:

    {
      "id":               "<uuid>",
      "created_at":       "<ISO 8601 UTC>",
      "updated_at":       "<ISO 8601 UTC>",
      "status":           "pending_cp",
      "reason":           "Awaiting HDFC CP Code",
      "label":            "<IA name + custodian short>",  # for the list
      "form_data":        { … the full _pcCollectForm payload … },
      "cml_token":        "<temp-upload token from parse-cml>",
      "schedule_a_token": "<temp-upload token from parse-schedule-a>",
    }

Operator drops the CML, fills what they can, hits "Stash for CP".
The form_data + tokens get persisted; the tokens keep the PDFs
alive on disk until the resume happens. When CP arrives, operator
clicks the pending record → form opens prefilled → types the CP →
hits "Create Pool" which routes through the regular create endpoint.
The pending record is deleted on successful create.

Public surface — keep small + boring; wraps file I/O so callers
don't think about paths.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _root() -> Path:
    """Resolve data/pending_pools/ under the deploy's data dir, mirroring
    the rest of the date-bucketed data tree (KEYSTONE_DATA_DIR aware)."""
    base = os.environ.get('KEYSTONE_DATA_DIR') or str(Path(__file__).parent.parent)
    p = Path(base) / 'data' / 'pending_pools'
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _path_for(pid: str) -> Path:
    """File path for a record id. Defensive against path traversal — the
    id must be a plain UUID-like string, no slashes."""
    safe = ''.join(c for c in (pid or '') if c.isalnum() or c in '-_')
    if not safe:
        raise ValueError(f'invalid pending-pool id: {pid!r}')
    return _root() / f'{safe}.json'


def stash(form_data: Dict[str, Any],
          *,
          status: str = 'pending_cp',
          reason: str = '',
          cml_token: str = '',
          schedule_a_token: str = '',
          label: str = '') -> Dict[str, Any]:
    """Persist a new pending record. Returns the stored dict including the
    auto-generated id. Caller owns the form_data shape — we don't validate;
    /api/pool-creator/resume hands it back to /api/pool-creator/create
    verbatim."""
    pid = uuid.uuid4().hex
    rec: Dict[str, Any] = {
        'id':               pid,
        'created_at':       _now_iso(),
        'updated_at':       _now_iso(),
        'status':           status or 'pending_cp',
        'reason':           reason or '',
        'label':            label or _derive_label(form_data),
        'form_data':        form_data or {},
        'cml_token':        cml_token or '',
        'schedule_a_token': schedule_a_token or '',
    }
    p = _path_for(pid)
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding='utf-8')
    tmp.replace(p)
    logger.info(f'pending_pools: stashed {pid} (status={status}, label={rec["label"]!r})')
    return rec


def update(pid: str, **patch: Any) -> Optional[Dict[str, Any]]:
    """Merge patch into an existing record. Returns the updated dict, or
    None if the id isn't found. updated_at is bumped automatically; id
    + created_at are immutable."""
    p = _path_for(pid)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        logger.warning(f'pending_pools: read failed for {pid}: {e}')
        return None
    for k, v in patch.items():
        if k in ('id', 'created_at'):
            continue
        rec[k] = v
    rec['updated_at'] = _now_iso()
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(rec, indent=2, sort_keys=True), encoding='utf-8')
    tmp.replace(p)
    return rec


def get(pid: str) -> Optional[Dict[str, Any]]:
    """Read one record by id. None when missing or unparseable."""
    p = _path_for(pid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        logger.warning(f'pending_pools: read failed for {pid}: {e}')
        return None


def list_all(*, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return every pending record, optionally filtered to a single
    status. Sorted newest-first by created_at so the operator's most
    recent stash lands at the top of the pending list."""
    root = _root()
    if not root.exists():
        return []
    out: List[Dict[str, Any]] = []
    for fp in root.glob('*.json'):
        try:
            rec = json.loads(fp.read_text(encoding='utf-8'))
        except Exception as e:
            logger.warning(f'pending_pools: skipping unreadable {fp.name}: {e}')
            continue
        if status and rec.get('status') != status:
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get('created_at', ''), reverse=True)
    return out


def remove(pid: str) -> bool:
    """Delete a record. Returns True on success, False when missing."""
    p = _path_for(pid)
    if not p.exists():
        return False
    try:
        p.unlink()
        logger.info(f'pending_pools: removed {pid}')
        return True
    except Exception as e:
        logger.warning(f'pending_pools: remove failed for {pid}: {e}')
        return False


def _derive_label(form_data: Dict[str, Any]) -> str:
    """Best-effort short label for the pending list — uses IA name and
    custodian short when available; falls back to pool_id."""
    ia    = (form_data.get('ia_name')   or '').strip()
    cust  = (form_data.get('custodian') or '').strip().upper()
    pool  = (form_data.get('pool_id')   or '').strip()
    if ia and cust:
        return f'{ia} ({cust})'
    if ia:
        return ia
    if pool:
        return pool
    return '(unnamed pending pool)'
