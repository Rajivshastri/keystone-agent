"""
Recon Progress Channel
----------------------
Lightweight on-disk progress sidecar so the UI can render a real
progress bar while a recon (or its preceding JIT fetch) is running.
The file at data/{date}/recon_progress.json is updated as each step
transitions; the UI polls /api/recon-progress?date={date} every ~1.5s.

Shape:
  {
    "date":           "2026-04-25",
    "recon_type":     "bank" | "holdings" | "trade",
    "stage":          "fetch" | "parse" | "reconcile" | "export"
                      | "done" | "failed",
    "current_source": "icici",     // optional, for fetch stage
    "completed":      3,
    "total":          9,
    "label":          "Fetching ICICI bank statements",
    "started_at":     "2026-04-25T08:41:00Z",
    "updated_at":     "2026-04-25T08:42:13Z",
    "log_tail":       ["…", "…"],   // last ~10 lines
    "error":          null           // populated when stage == "failed"
  }

The file is rewritten atomically each update (write to .tmp then
rename) so a polling reader never sees a partial JSON.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Tuple, Dict

logger = logging.getLogger(__name__)

PROGRESS_FILENAME = 'recon_progress.json'
LOG_TAIL_MAX = 10

# Global lock around progress-file writes. Without this, the heartbeat
# thread can race recon_progress.finish() and clobber stage='done' back
# to whatever stage was last on disk:
#   T0  heartbeat reads payload (stage='reconcile')
#   T1  main thread writes stage='done' + stops heartbeat
#   T2  heartbeat finishes its write (stage='reconcile' from T0 read,
#        new updated_at) — overwriting T1's done-state
# UI poll never sees 'done' → progress bar stuck at the last step.
# Serialising all writes through this lock closes the race; reads
# remain unlocked since they're idempotent.
_write_lock = threading.Lock()

# Default staleness threshold: 5 min without a heartbeat = treat as crashed.
# The heartbeat thread bumps updated_at every 30 s, so 5 min = 10 missed
# beats — well above any legitimate runtime gap (longest synchronous step
# in a recon today is the WS download phase at ~60 s).
STALE_AFTER_SECONDS = 300
HEARTBEAT_INTERVAL_SECONDS = 30


def _path(data_dir: Path | str, date_str: str) -> Path:
    return Path(data_dir) / 'data' / date_str / PROGRESS_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def begin(data_dir: Path | str, date_str: str, recon_type: str,
          total_steps: int = 0, label: str = '') -> None:
    """Initialise the progress file for a new run. Wipes any prior
    state for the same date+type so polling clients see a clean
    started_at marker."""
    p = _path(data_dir, date_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'date':       date_str,
        'recon_type': recon_type,
        'stage':      'fetch' if total_steps > 0 else 'parse',
        'completed':  0,
        'total':      total_steps,
        'label':      label or f'Starting {recon_type} reconciliation',
        'started_at': _now(),
        'updated_at': _now(),
        'log_tail':   [],
        'error':      None,
    }
    with _write_lock:
        _atomic_write(p, payload)


def update(data_dir: Path | str, date_str: str,
           stage: Optional[str] = None,
           current_source: Optional[str] = None,
           completed: Optional[int] = None,
           total: Optional[int] = None,
           label: Optional[str] = None,
           log_line: Optional[str] = None,
           error: Optional[str] = None) -> None:
    """Patch fields on the existing progress file. Best-effort — a
    missing file is treated as a fresh begin() with whatever fields
    the caller passed (so out-of-order updates don't silently drop)."""
    p = _path(data_dir, date_str)
    # Serialise read-modify-write with the heartbeat thread. See
    # _write_lock comment at module top for the race this prevents.
    with _write_lock:
        try:
            existing = _read(p) or {}
        except Exception:
            existing = {}

        payload = dict(existing) if existing else {
            'date':       date_str,
            'started_at': _now(),
            'log_tail':   [],
        }
        if stage is not None:          payload['stage']          = stage
        if current_source is not None: payload['current_source'] = current_source
        if completed is not None:      payload['completed']      = int(completed)
        if total is not None:          payload['total']          = int(total)
        if label is not None:          payload['label']          = label
        if error is not None:          payload['error']          = error
        payload['updated_at'] = _now()

        if log_line:
            tail = list(payload.get('log_tail') or [])
            tail.append(str(log_line))
            payload['log_tail'] = tail[-LOG_TAIL_MAX:]

        _atomic_write(p, payload)

    # Terminal stages auto-stop any running heartbeat for this date so the
    # caller doesn't have to remember stop_heartbeat() at every return path.
    if payload.get('stage') in ('done', 'failed'):
        try:
            stop_heartbeat(data_dir, date_str)
        except Exception:
            pass


def finish(data_dir: Path | str, date_str: str,
           label: str = 'Complete') -> None:
    update(data_dir, date_str, stage='done', label=label)


def fail(data_dir: Path | str, date_str: str,
         error: str, label: str = 'Failed') -> None:
    update(data_dir, date_str, stage='failed', label=label, error=error)


def read(data_dir: Path | str, date_str: str) -> Optional[dict]:
    """Return the progress payload for a date, or None if absent."""
    return _read(_path(data_dir, date_str))


# ── Stale-lock detection + auto-release ──────────────────────────────────── #
#
# A recon lock is "stale" when its updated_at hasn't moved in too long —
# typically because the worker crashed, the request was killed mid-flight
# (e.g. mobile tab backgrounded), or the App Service instance restarted.
# Before rejecting a new run with "already in progress", callers should
# call auto_release_if_stale() so the stale lock is converted to
# stage='failed' and the new run can proceed.

def is_stale(payload: Optional[dict],
             max_age_seconds: int = STALE_AFTER_SECONDS) -> bool:
    """True if the in-flight lock hasn't been heartbeated in too long."""
    if not payload:
        return False
    if payload.get('stage') in ('done', 'failed', None):
        return False
    ts = payload.get('updated_at') or payload.get('started_at')
    if not ts:
        return False
    try:
        last = datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
    except (ValueError, TypeError):
        return False
    age = (datetime.utcnow() - last).total_seconds()
    return age > max_age_seconds


def auto_release_if_stale(data_dir: Path | str, date_str: str,
                          max_age_seconds: int = STALE_AFTER_SECONDS) -> bool:
    """If the lock is stale, mark it failed and return True.

    Returns:
      True  — lock was stale and has been released; caller can proceed.
      False — no stale lock to release (either no lock at all, or the
              existing lock is healthy and the caller should reject).
    """
    payload = read(data_dir, date_str)
    if payload and payload.get('stage') not in ('done', 'failed', None) \
       and is_stale(payload, max_age_seconds):
        ts = payload.get('updated_at') or payload.get('started_at') or '?'
        update(data_dir, date_str, stage='failed',
               label='Auto-released stale lock',
               error=f'Previous run did not heartbeat since {ts} '
                     f'(>{max_age_seconds}s) — likely crashed or '
                     'mobile-disconnected.')
        return True
    return False


# ── Heartbeat thread ────────────────────────────────────────────────────── #
#
# A background daemon thread that bumps `updated_at` periodically while a
# recon is running. Without this, slow synchronous steps (e.g. the
# ReconEngine call) leave updated_at unchanged for the whole run — a
# well-behaved long recon could otherwise look stale to the auto-release
# check. The heartbeat self-terminates when stage flips to done/failed.

class _HeartbeatThread(threading.Thread):
    """Internal — use start_heartbeat()/stop_heartbeat() instead."""
    def __init__(self, data_dir: Path | str, date_str: str,
                 interval_seconds: int):
        super().__init__(name=f'recon-heartbeat-{date_str}', daemon=True)
        self.data_dir = data_dir
        self.date_str = date_str
        self.interval = max(1, int(interval_seconds))
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                # Hold the global write lock for the entire read-modify-
                # write so update()/finish() on the main thread can't
                # interleave a stage='done' between our read and our
                # write. Without this, the heartbeat clobbers the
                # terminal stage back to its pre-finish value and the
                # UI poll never sees stage='done' (root cause of the
                # 'EoD complete but UI stuck at step 1/10' bug).
                with _write_lock:
                    p = _path(self.data_dir, self.date_str)
                    payload = _read(p)
                    if not payload:
                        continue
                    # Self-terminate on terminal stage — caller may have
                    # forgotten stop_heartbeat() but we shouldn't keep
                    # bumping a finished lock.
                    if payload.get('stage') in ('done', 'failed'):
                        return
                    payload['updated_at'] = _now()
                    _atomic_write(p, payload)
            except Exception as e:
                logger.debug(f'heartbeat tick failed: {e}')


_heartbeats: Dict[Tuple[str, str], _HeartbeatThread] = {}
_heartbeats_lock = threading.Lock()


def start_heartbeat(data_dir: Path | str, date_str: str,
                    interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS) -> None:
    """Start a background heartbeat for this date. Idempotent — calling
    twice for the same date is a no-op (returns the existing thread)."""
    key = (str(data_dir), date_str)
    with _heartbeats_lock:
        existing = _heartbeats.get(key)
        if existing and existing.is_alive():
            return
        ht = _HeartbeatThread(data_dir, date_str, interval_seconds)
        ht.start()
        _heartbeats[key] = ht


def claim_heartbeat(data_dir: Path | str, date_str: str,
                    interval_seconds: int = HEARTBEAT_INTERVAL_SECONDS) -> bool:
    """Atomic test-and-set claim on the heartbeat registry.

    Used as the single-flight gate at recon-engine entry points. A worker
    that wins the claim is the active runner for this date; a worker
    that loses sees a competing recon already in flight and should
    return 409.

    Returns:
      True  — claim acquired; caller is the active runner. Heartbeat
              has been started.
      False — another live heartbeat already exists for this date.
              Caller MUST NOT proceed.

    Note: the registry is per-process. On Azure (1 worker × 4 threads),
    that catches all in-process races. Cross-worker concurrency is
    defended by the disk-side stale-lock check (auto_release_if_stale)
    rather than this claim.
    """
    key = (str(data_dir), date_str)
    with _heartbeats_lock:
        existing = _heartbeats.get(key)
        if existing and existing.is_alive():
            return False
        ht = _HeartbeatThread(data_dir, date_str, interval_seconds)
        ht.start()
        _heartbeats[key] = ht
        return True


def is_heartbeat_active(data_dir: Path | str, date_str: str) -> bool:
    """True when a heartbeat is currently running in this process for
    the given date. Used in tests + diagnostics."""
    key = (str(data_dir), date_str)
    with _heartbeats_lock:
        ht = _heartbeats.get(key)
    return bool(ht and ht.is_alive())


def stop_heartbeat(data_dir: Path | str, date_str: str) -> None:
    """Stop the heartbeat for this date if one is running. Safe to call
    when no heartbeat exists."""
    key = (str(data_dir), date_str)
    with _heartbeats_lock:
        ht = _heartbeats.pop(key, None)
    if ht:
        ht.stop()


# ── Decision prompt (continue / abort) ───────────────────────────── #
#
# When JIT fetch hits an error, the recon loop pauses and writes
# stage='awaiting_decision' to the progress file. The UI sees the
# stage, renders a modal with Continue / Abort buttons + the error
# text, and POSTs the operator's choice to /api/recon-decision. The
# backend writes data/{date}/recon_decision.json; the recon loop
# polls for it (with timeout) and resumes or aborts.

DECISION_FILENAME = 'recon_decision.json'
DECISION_POLL_SECONDS = 1.0
DECISION_TIMEOUT_SECONDS = 180  # 3 min — fits Azure's 230s request budget


def _decision_path(data_dir: Path | str, date_str: str) -> Path:
    return Path(data_dir) / 'data' / date_str / DECISION_FILENAME


def request_decision(data_dir: Path | str, date_str: str,
                     prompt: str, error: str = '') -> None:
    """Pause the recon and surface a Continue/Abort prompt to the UI.
    Clears any prior decision file so a stale answer can't sneak in."""
    p = _decision_path(data_dir, date_str)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            pass
    update(data_dir, date_str, stage='awaiting_decision',
           label=prompt, error=error)


def write_decision(data_dir: Path | str, date_str: str,
                   action: str, by: str = 'operator') -> None:
    """Record an operator decision. action is 'continue' or 'abort'."""
    if action not in ('continue', 'abort'):
        raise ValueError(f'invalid action: {action!r}')
    p = _decision_path(data_dir, date_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'action':     action,
        'by':         by,
        'decided_at': _now(),
    }
    _atomic_write(p, payload)


def read_decision(data_dir: Path | str, date_str: str) -> Optional[dict]:
    """Read a decision payload. Returns None when no decision recorded."""
    return _read(_decision_path(data_dir, date_str))


def await_decision(data_dir: Path | str, date_str: str,
                   timeout_seconds: float = DECISION_TIMEOUT_SECONDS,
                   poll_seconds: float = DECISION_POLL_SECONDS) -> Optional[dict]:
    """Block until an operator decision is recorded or timeout elapses.

    Returns the decision dict, or None on timeout (caller should treat
    that as 'abort' for safety — better to fail visibly than push
    through with stale data after the operator wandered off).
    """
    import time as _time
    deadline = _time.time() + max(1.0, float(timeout_seconds))
    while _time.time() < deadline:
        d = read_decision(data_dir, date_str)
        if d:
            # Consume the file so a re-prompt later in the same run
            # doesn't see this decision again.
            try:
                _decision_path(data_dir, date_str).unlink()
            except Exception:
                pass
            return d
        _time.sleep(max(0.1, float(poll_seconds)))
    return None


# ── internals ──────────────────────────────────────────────────────── #

def _read(p: Path) -> Optional[dict]:
    if not p.exists():
        return None
    try:
        with p.open(encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.debug(f'Progress file unreadable ({p}): {e}')
        return None


def _atomic_write(p: Path, payload: dict) -> None:
    """Write to a tempfile in the same dir, then rename. Avoids the
    poll endpoint reading a half-written file."""
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_name = tempfile.mkstemp(prefix='.recon_progress_',
                                         suffix='.tmp', dir=str(p.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(payload, f, separators=(',', ':'))
            os.replace(tmp_name, str(p))
        except Exception:
            # Best-effort cleanup of the temp file on rare write failures.
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
            raise
    except Exception as e:
        logger.warning(f'Progress write failed for {p}: {e}')
