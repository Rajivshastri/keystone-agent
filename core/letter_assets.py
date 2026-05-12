"""
core/letter_assets.py — operator-uploaded signature + stamp images.

The Kotak broker invitation requires a stamped + signed letter (most
other custodians accept the standard strategy DOCX as-is). This module
stores the firm's signature and stamp images so the letter generator
in :mod:`core.kotak_letter` can stamp them onto the page at PDF
render time.

Files live at ``data/letter_assets/{signature,stamp}.png``. They're
stored as PNG regardless of the operator's upload format — Pillow
handles the conversion on save so downstream readers don't have to
sniff format. ``data/letter_assets/`` is ``KEYSTONE_DATA_DIR``-aware
so a redeploy on Azure doesn't blow them away (the live data tree at
``/home/keystone-data`` survives App Service swaps).

Public API
==========
    save(kind, src_path) -> dict              # accept any image format
    path_for(kind) -> Path | None             # None if not uploaded yet
    info(kind) -> dict                        # exists / size / modified
    remove(kind) -> bool

``kind`` is exactly one of ``'signature'`` or ``'stamp'``. Anything
else raises ``ValueError`` so a typo at the API layer can't write
junk into the directory.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

KINDS = ('signature', 'stamp')


def _root() -> Path:
    """``data/letter_assets/`` under the deploy's data tree. Created on
    first write."""
    base = os.environ.get('KEYSTONE_DATA_DIR') or str(Path(__file__).parent.parent)
    p = Path(base) / 'data' / 'letter_assets'
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ensure_kind(kind: str) -> str:
    k = (kind or '').strip().lower()
    if k not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}; got {kind!r}")
    return k


def _file_for(kind: str) -> Path:
    return _root() / f'{_ensure_kind(kind)}.png'


def save(kind: str, src_path: str) -> Dict[str, Any]:
    """Open ``src_path`` (any Pillow-readable image format) and write
    a normalised PNG into ``data/letter_assets/<kind>.png``. Existing
    file is overwritten atomically (write to .tmp + rename).

    Returns ``{ok, path, size, width, height}``."""
    k = _ensure_kind(kind)
    from PIL import Image
    target = _file_for(k)
    tmp = target.with_suffix('.png.tmp')
    with Image.open(src_path) as im:
        # Preserve transparency where the source had it (signatures
        # frequently come through as transparent-background PNGs);
        # convert other modes to RGBA for consistency.
        if im.mode not in ('RGB', 'RGBA', 'L'):
            im = im.convert('RGBA')
        im.save(tmp, 'PNG', optimize=True)
        w, h = im.size
    os.replace(tmp, target)
    return {'ok': True, 'kind': k, 'path': str(target),
            'size': target.stat().st_size, 'width': w, 'height': h}


def path_for(kind: str) -> Optional[Path]:
    """Return the file path if uploaded, else None. Lets callers do a
    quick exists-check without knowing where the directory lives."""
    p = _file_for(kind)
    return p if p.exists() else None


def info(kind: str) -> Dict[str, Any]:
    """Metadata for the UI status line (does it exist, how big, when
    was it last touched). Never raises — returns ``exists: False`` on
    any read error so the form keeps rendering even if disk is flaky."""
    p = _file_for(kind)
    if not p.exists():
        return {'kind': _ensure_kind(kind), 'exists': False}
    try:
        st = p.stat()
        return {
            'kind':         _ensure_kind(kind),
            'exists':       True,
            'size':         st.st_size,
            'modified':     datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                                .strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
    except Exception as e:
        logger.warning(f'letter_assets.info({kind}) failed: {e}')
        return {'kind': _ensure_kind(kind), 'exists': False}


def remove(kind: str) -> bool:
    """Delete the file. Returns True on success, False when missing
    or the unlink failed (logged at WARNING)."""
    p = _file_for(kind)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except Exception as e:
        logger.warning(f'letter_assets.remove({kind}) failed: {e}')
        return False
