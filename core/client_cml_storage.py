"""
core/client_cml_storage.py — persist client CML PDFs from upload through
client-record save.

The client-onboarding flow uploads a CML PDF, parses it, and lets the
operator review the extracted fields before pressing Save. The PDF itself
needs to live on disk so downstream features (the welcome email after
authorize-ws, audit, re-parse-with-edits) can attach or re-read it.

Two-stage storage matches the pool-creator flow's pattern:

1. **Stage** — at ``/api/clients/ingest-cml`` time the PDF is copied to
   ``data/_client_uploads/<token>.pdf`` keyed by a UUID. The token rides
   back to the frontend in the extracted dict and is sent through to
   ``/api/clients/save`` in the JSON body.

2. **Commit** — at ``/api/clients/save`` time, when a token is present,
   the staged PDF is moved to ``data/clients/<row_id>/cml.pdf``. Old
   stale staged PDFs (older than 24h) get garbage-collected on each
   save so the temp dir doesn't accumulate.

KEYSTONE_DATA_DIR-aware so the live data tree on Azure (currently
``/home/keystone-data/``) is the source of truth across redeploys.

Public API
==========
    stage(src_path) -> token                    # copy to staging slot
    commit(token, row_id) -> Path | None        # move to client/<id>/
    cml_path(row_id) -> Path | None             # read existing
    cleanup_stale(older_than_hours=24) -> int   # GC orphan staged PDFs
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _data_root() -> Path:
    """``data/`` under the deploy's data tree (KEYSTONE_DATA_DIR aware)."""
    base = os.environ.get('KEYSTONE_DATA_DIR') or str(Path(__file__).parent.parent)
    p = Path(base) / 'data'
    p.mkdir(parents=True, exist_ok=True)
    return p


def _stage_dir() -> Path:
    p = _data_root() / '_client_uploads'
    p.mkdir(parents=True, exist_ok=True)
    return p


def _client_dir(row_id: int) -> Path:
    p = _data_root() / 'clients' / str(int(row_id))
    p.mkdir(parents=True, exist_ok=True)
    return p


def stage(src_path: str) -> str:
    """Copy ``src_path`` into the staging dir keyed by a fresh UUID
    token. Returns the token so the caller can hand it to the frontend
    and round-trip it through the save endpoint."""
    token = uuid.uuid4().hex[:16]
    dst = _stage_dir() / f'{token}.pdf'
    shutil.copyfile(src_path, dst)
    return token


def commit(token: str, row_id: int) -> Optional[Path]:
    """Move the staged PDF for ``token`` into the client's permanent
    slot. Returns the new path or None if the token doesn't resolve
    (stale / garbage-collected). The destination filename is fixed
    (``cml.pdf``) so resends don't have to grep for it."""
    if not token or row_id is None:
        return None
    src = _stage_dir() / f'{(token or "").strip()}.pdf'
    if not src.exists():
        logger.info(f'client_cml_storage.commit: token {token!r} not found in staging')
        return None
    dst = _client_dir(int(row_id)) / 'cml.pdf'
    try:
        shutil.move(str(src), str(dst))
        return dst
    except Exception as e:
        logger.warning(f'client_cml_storage.commit failed: {e}')
        return None


def cml_path(row_id: int) -> Optional[Path]:
    """Return the persisted CML path for a saved client, or None when
    the client was created before CML persistence was added (legacy
    rows have no cml.pdf)."""
    if row_id is None:
        return None
    p = _client_dir(int(row_id)) / 'cml.pdf'
    return p if p.exists() else None


def cleanup_stale(older_than_hours: int = 24) -> int:
    """Delete staged PDFs older than ``older_than_hours``. Returns the
    count removed. Called opportunistically from ``commit`` so the
    staging dir doesn't grow forever when the operator abandons a
    client mid-onboarding."""
    cutoff = time.time() - (older_than_hours * 3600)
    removed = 0
    sd = _stage_dir()
    if not sd.exists():
        return 0
    for p in sd.glob('*.pdf'):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception as e:
            logger.debug(f'client_cml_storage.cleanup_stale skip {p.name}: {e}')
    if removed:
        logger.info(f'client_cml_storage.cleanup_stale: removed {removed} stale staged PDF(s)')
    return removed
