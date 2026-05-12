"""
Runtime State (cross-process)
-----------------------------
A small file-backed key/value store so the in-flight UI status, recon
results, and bank-recon results survive a worker boundary.

Why this exists
---------------
Keystone runs gunicorn with multiple workers (the recon-reminder
scheduler depends on ≥2 workers). The `_state` dict in app.py is
process-local, so:

* /api/recon completes on worker A, writes `_state['recon_results']`
* /api/recon/email lands on worker B → empty results → email fails

The fix is to persist the post-completion summary to disk via atomic
write (temp + rename), and have all readers look it up here. Writers
update both `_state` (fast same-worker reads) and disk
(cross-worker reads).

Storage layout: `data/_state/{key}.json` — one file per key, so a
write to one key never blocks or clobbers another. Atomic-rename
means readers never see a partial JSON.

Cache: each instance keeps an mtime-keyed cache so consecutive reads
that didn't get bumped on disk avoid re-parsing.

Scope: only intended for things UI/email sends need across workers.
Big in-memory blobs (the full `records` list, ws_strategies) stay
in-process. UI doesn't need them after recon completes — just the
summary.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

STATE_DIRNAME = '_state'


class RuntimeState:

    def __init__(self, data_dir: Path | str):
        self._dir = Path(data_dir) / 'data' / STATE_DIRNAME
        self._cache: dict = {}
        self._cache_mtime: dict = {}
        self._lock = threading.Lock()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"runtime_state: could not create {self._dir}: {e}")

    # ------------------------------------------------------------------ #

    def _path(self, key: str) -> Path:
        # Defensive: keys must be filename-safe
        safe = ''.join(c if c.isalnum() or c in '_-' else '_' for c in key)
        return self._dir / f"{safe}.json"

    # ------------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        """Read a key. Falls back to `default` when absent or unreadable.

        Mtime-cache: if the file's mtime hasn't changed since last
        read, returns the cached object without re-parsing JSON.
        """
        p = self._path(key)
        try:
            st = p.stat()
        except FileNotFoundError:
            return default
        except OSError as e:
            logger.debug(f"runtime_state.get({key}): stat failed: {e}")
            return default

        with self._lock:
            cached_mtime = self._cache_mtime.get(key)
            if cached_mtime == st.st_mtime_ns:
                return self._cache.get(key, default)

        try:
            with p.open('r', encoding='utf-8') as f:
                val = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"runtime_state.get({key}): read failed: {e}")
            return default

        with self._lock:
            self._cache[key] = val
            self._cache_mtime[key] = st.st_mtime_ns
        return val

    def set(self, key: str, value: Any) -> None:
        """Atomic write. JSON-serialisable values only."""
        p = self._path(key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f'.{p.stem}_',
                                             suffix='.tmp',
                                             dir=str(p.parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(value, f, default=str)
                os.replace(tmp_name, str(p))
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"runtime_state.set({key}) failed: {e}")
            return

        # Refresh cache on success so the writing process sees its own
        # write without re-reading.
        try:
            mtime = p.stat().st_mtime_ns
        except OSError:
            mtime = None
        with self._lock:
            self._cache[key] = value
            if mtime is not None:
                self._cache_mtime[key] = mtime

    def delete(self, key: str) -> None:
        p = self._path(key)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.debug(f"runtime_state.delete({key}): {e}")
        with self._lock:
            self._cache.pop(key, None)
            self._cache_mtime.pop(key, None)

    def clear(self) -> None:
        """Remove every key in the state directory. Used by /api/clear."""
        if not self._dir.exists():
            return
        for f in self._dir.iterdir():
            if f.is_file() and f.suffix == '.json':
                try:
                    f.unlink()
                except OSError:
                    pass
        with self._lock:
            self._cache.clear()
            self._cache_mtime.clear()
