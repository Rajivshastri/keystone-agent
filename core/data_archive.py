"""
Data Retention & Archive
------------------------
Per-date data folders (`data/YYYY-MM-DD/`) accumulate forever — every
custodian zip, every WS master, every recon report. Disk pressure shows
up as Azure deploy failures before it shows up anywhere else, so we
need a retention policy that operates on its own without operator
intervention.

Policy
------
* `archive_old_dates(days_old=90)` — finds every `data/YYYY-MM-DD/`
  whose directory name represents a date older than 90 days and zips
  it into `data/_archive/{YYYY}-Q{1-4}/{YYYY-MM-DD}.zip`. The source
  directory is removed only after the zip is written and validated.

* Quarter bucket = the calendar quarter of the date being archived
  (NOT the quarter at archive time). 2025-12-15 always lands in
  `_archive/2025-Q4/` regardless of when it gets archived.

* `archive_status()` — returns one row per quarter with date count and
  total size, for the UI.

* `auto_archive_if_due()` — runs at startup; cheap no-op if last
  archive was within 90 days, otherwise schedules a real run on a
  daemon thread so we don't slow boot.

Idempotent: a date already present in `_archive/` is skipped, not
re-zipped. The zip uses ZIP_DEFLATED at level 6 — the recon Excel
files are mostly text and compress to ~20% of original.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Iterator, List, Optional

logger = logging.getLogger(__name__)

ARCHIVE_DIRNAME = '_archive'
DATE_FOLDER_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
LAST_RUN_FILENAME = '.last_archive_run'


# ---------------------------------------------------------------------- #
#  Helpers                                                               #
# ---------------------------------------------------------------------- #

def _quarter_label(d: date) -> str:
    """date -> 'YYYY-Qn' bucket label."""
    q = (d.month - 1) // 3 + 1
    return f"{d.year}-Q{q}"


def _data_root(data_dir: Path | str) -> Path:
    """The `data/` directory under the project root (or KEYSTONE_DATA_DIR)."""
    return Path(data_dir) / 'data'


def _archive_root(data_dir: Path | str) -> Path:
    return _data_root(data_dir) / ARCHIVE_DIRNAME


def _parse_date_folder(name: str) -> Optional[date]:
    """Return `date(2026, 4, 29)` for '2026-04-29', else None."""
    if not DATE_FOLDER_RE.match(name):
        return None
    try:
        return datetime.strptime(name, '%Y-%m-%d').date()
    except ValueError:
        return None


def _zip_directory(src: Path, dest_zip: Path) -> int:
    """Zip src tree into dest_zip (DEFLATE level 6). Returns bytes written."""
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_zip.with_suffix(dest_zip.suffix + '.tmp')
    if tmp.exists():
        tmp.unlink()
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in src.rglob('*'):
            if f.is_file():
                arcname = f.relative_to(src.parent)
                zf.write(f, arcname=str(arcname))
    os.replace(tmp, dest_zip)
    return dest_zip.stat().st_size


def _dir_size_bytes(p: Path) -> int:
    total = 0
    for f in p.rglob('*'):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------- #
#  Public API                                                            #
# ---------------------------------------------------------------------- #

@dataclass
class ArchiveResult:
    archived: List[str]      # YYYY-MM-DD strings successfully archived
    skipped:  List[str]      # already-archived dates (zip exists)
    errors:   List[dict]     # [{'date': ..., 'error': ...}]
    started_at: str          # ISO UTC
    finished_at: str         # ISO UTC

    @property
    def ok(self) -> bool:
        return not self.errors


def archive_old_dates(data_dir: Path | str,
                      days_old: int = 90,
                      dry_run: bool = False) -> ArchiveResult:
    """Archive every per-date folder older than `days_old` days.

    Implementation:
      1. List `data/YYYY-MM-DD/` directories
      2. For each whose date is < (today - days_old), build the target
         zip path under `data/_archive/{YYYY}-Q{1-4}/`
      3. Skip if zip already exists
      4. Zip the folder, then remove the source dir
      5. Append a line to `data/_archive/.last_archive_run`

    Returns an ArchiveResult; never raises (errors recorded per-date).
    """
    started = datetime.utcnow().isoformat()
    archived: List[str] = []
    skipped: List[str] = []
    errors: List[dict] = []

    root = _data_root(data_dir)
    if not root.exists():
        logger.info(f"data_archive: nothing to do, {root} does not exist")
        return ArchiveResult(archived, skipped, errors, started,
                             datetime.utcnow().isoformat())

    today = date.today()
    cutoff = today.toordinal() - max(0, int(days_old))

    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == ARCHIVE_DIRNAME:
            continue
        d = _parse_date_folder(child.name)
        if d is None:
            continue
        if d.toordinal() > cutoff:
            continue  # too recent — keep

        zip_dest = _archive_root(data_dir) / _quarter_label(d) / f"{child.name}.zip"
        if zip_dest.exists():
            skipped.append(child.name)
            continue

        if dry_run:
            archived.append(child.name + ' (dry-run)')
            continue

        try:
            size = _zip_directory(child, zip_dest)
            # Validate the zip opens before deleting source
            with zipfile.ZipFile(zip_dest) as zf:
                if zf.testzip() is not None:
                    raise RuntimeError("zip integrity check failed")
            shutil.rmtree(child)
            archived.append(child.name)
            logger.info(f"data_archive: {child.name} -> {zip_dest.name} "
                        f"({size:,} bytes)")
        except Exception as e:
            errors.append({'date': child.name, 'error': str(e)})
            logger.warning(f"data_archive: {child.name} failed: {e}")
            # Clean up partial zip if it exists
            if zip_dest.exists():
                try:
                    zip_dest.unlink()
                except OSError:
                    pass

    # Mark the run timestamp regardless of whether anything was archived
    if not dry_run:
        try:
            mark = _archive_root(data_dir) / LAST_RUN_FILENAME
            mark.parent.mkdir(parents=True, exist_ok=True)
            mark.write_text(datetime.utcnow().isoformat() + '\n', encoding='utf-8')
        except Exception as e:
            logger.debug(f"data_archive: could not write last-run marker: {e}")

    return ArchiveResult(archived, skipped, errors, started,
                         datetime.utcnow().isoformat())


@dataclass
class QuarterStatus:
    quarter:    str    # 'YYYY-Qn'
    dates:      int    # number of zips in this bucket
    bytes:      int    # total size on disk


def archive_status(data_dir: Path | str) -> dict:
    """Return summary for the UI.

    {
      'archive_root': '.../data/_archive',
      'last_run':     'YYYY-MM-DDTHH:MM:SS' or '',
      'quarters':     [{'quarter': '2025-Q4', 'dates': 12, 'bytes': 18234567}, ...],
      'total_dates':  N,
      'total_bytes':  M,
    }
    """
    root = _archive_root(data_dir)
    out = {
        'archive_root': str(root),
        'last_run':     '',
        'quarters':     [],
        'total_dates':  0,
        'total_bytes':  0,
    }
    if not root.exists():
        return out

    last_run_path = root / LAST_RUN_FILENAME
    if last_run_path.exists():
        try:
            out['last_run'] = last_run_path.read_text(encoding='utf-8').strip()
        except OSError:
            pass

    for q_dir in sorted(root.iterdir()):
        if not q_dir.is_dir():
            continue
        if not re.match(r'^\d{4}-Q[1-4]$', q_dir.name):
            continue
        zips = [f for f in q_dir.iterdir() if f.is_file() and f.suffix == '.zip']
        if not zips:
            continue
        sz = sum(z.stat().st_size for z in zips)
        out['quarters'].append({
            'quarter': q_dir.name,
            'dates':   len(zips),
            'bytes':   sz,
        })
        out['total_dates'] += len(zips)
        out['total_bytes'] += sz
    return out


def stream_quarter_zip(data_dir: Path | str, quarter: str) -> Iterator[bytes]:
    """Generator that yields a fresh zip-of-zips for the requested quarter.

    Each yielded chunk is appended to the response by the Flask streamer.
    Uses a temp file so the entire payload doesn't have to fit in memory.
    """
    root = _archive_root(data_dir)
    q_dir = root / quarter
    if not q_dir.is_dir():
        raise FileNotFoundError(f"quarter not found: {quarter}")

    import tempfile
    tmp = tempfile.NamedTemporaryFile(prefix='ws_archive_', suffix='.zip',
                                      delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_STORED) as outer:
            # Each per-date zip is already compressed; outer wrapper is
            # STORED so we don't pay another compression pass.
            for inner in sorted(q_dir.iterdir()):
                if inner.is_file() and inner.suffix == '.zip':
                    outer.write(inner, arcname=f"{quarter}/{inner.name}")

        with tmp_path.open('rb') as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def auto_archive_if_due(data_dir: Path | str, days_old: int = 90,
                        run_interval_days: int = 30) -> bool:
    """Trigger a background archive run if the last one is older than
    `run_interval_days` (default 30). Non-blocking — schedules a daemon
    thread so app startup is not delayed by the zip pass.

    Returns True if a run was scheduled, False if not due.
    """
    root = _archive_root(data_dir)
    mark = root / LAST_RUN_FILENAME
    last_run = 0.0
    if mark.exists():
        try:
            last_run = mark.stat().st_mtime
        except OSError:
            last_run = 0.0

    seconds_since = time.time() - last_run
    if seconds_since < run_interval_days * 86400:
        return False

    def _bg():
        try:
            r = archive_old_dates(data_dir, days_old=days_old)
            logger.info(f"data_archive (auto): archived={len(r.archived)} "
                        f"skipped={len(r.skipped)} errors={len(r.errors)}")
        except Exception as e:
            logger.error(f"data_archive (auto) failed: {e}", exc_info=True)

    t = threading.Thread(target=_bg, daemon=True, name='data-archive-auto')
    t.start()
    logger.info(f"data_archive: scheduled auto-run "
                f"(last run {seconds_since/86400:.0f}d ago, "
                f"threshold {run_interval_days}d)")
    return True
