"""Runtime path resolution for the agent.

PyInstaller freezes the agent into a onefolder bundle whose data
files live under ``sys._MEIPASS``. That location is read-only (a
PyInstaller extraction cache) and is wiped on every agent upgrade,
so it is not a valid home for files that operators edit.

The bundled ``config/`` snapshot is therefore treated as *seed data*.
On first access we copy it into the agent's writable data dir and
return the writable path thereafter. Both the job runner (read) and
the templates library (read+write) go through ``config_dir()`` so
they always see the same files.

In dev (unfrozen) mode there is no bundle — ``config/`` lives next
to ``agent/``, ``core/``, ``parsers/`` in the repo tree and is
edited directly. No copy step.
"""
from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

from .config import data_dir

logger = logging.getLogger(__name__)

_seeded = False


def _frozen_root() -> Path | None:
    """Return the PyInstaller _MEIPASS root, or None if running from source."""
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        return Path(mei)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def _bundled_config_dir() -> Path | None:
    """Location of the read-only bundled config snapshot inside a frozen build."""
    root = _frozen_root()
    if root is None:
        return None
    candidate = root / "config"
    return candidate if candidate.exists() else None


def _dev_config_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config"


def _writable_config_dir() -> Path:
    return data_dir() / "config"


def _seed_writable_from_bundle() -> None:
    """Copy the bundled config snapshot into the writable dir on first run.

    Runs exactly once per process. Only files that do not already
    exist in the writable location are copied — so operator edits via
    the templates UI are never overwritten by a later agent upgrade.
    """
    global _seeded
    if _seeded:
        return
    bundle = _bundled_config_dir()
    if bundle is None:
        _seeded = True
        return
    writable = _writable_config_dir()
    writable.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in bundle.glob("*.json"):
        dst = writable / src.name
        if not dst.exists():
            try:
                shutil.copy2(src, dst)
                copied += 1
            except OSError as e:
                logger.warning(f"Failed to seed {src.name}: {e}")
    if copied:
        logger.info(f"Seeded {copied} config file(s) from bundle into {writable}")
    _seeded = True


def config_dir() -> Path:
    """Where all agent config JSON files live at runtime.

    - Frozen: ``<data_dir>/config`` (writable, seeded from bundle on first run)
    - Dev:    ``<repo>/config``     (edited directly)
    """
    if _frozen_root() is not None:
        _seed_writable_from_bundle()
        return _writable_config_dir()
    return _dev_config_dir()
