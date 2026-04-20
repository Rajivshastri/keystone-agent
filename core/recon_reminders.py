"""
Recon Reminder Queue
--------------------
Persistent tracker for pending "final email" reminders on holdings and bank
reconciliations. When an initial alert email is sent but the final (with-notes)
email is not sent within an hour, a reminder goes out — and then every hour
until the final email is actually sent.

State is kept in a single JSON file so reminders survive server restarts.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

REMINDER_INTERVAL = timedelta(hours=1)
STALE_AFTER       = timedelta(days=7)   # prune entries older than this


class ReconReminderStore:
    """Thread-safe JSON-backed store of pending reminders keyed by (type, date)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # ── File I/O ────────────────────────────────────────────────────── #

    def _load(self) -> dict:
        if not self.path.exists():
            return {'pending': {}}
        try:
            with open(self.path) as f:
                data = json.load(f) or {}
            if 'pending' not in data or not isinstance(data.get('pending'), dict):
                data['pending'] = {}
            return data
        except Exception as e:
            logger.warning(f'recon_reminders: load failed — starting empty: {e}')
            return {'pending': {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        tmp.replace(self.path)

    @staticmethod
    def _key(recon_type: str, date_str: str) -> str:
        return f'{recon_type}:{date_str}'

    # ── Public API ──────────────────────────────────────────────────── #

    def record_initial_sent(self, recon_type: str, date_str: str,
                            recipients: List[str],
                            attachment_path: Optional[str] = None) -> None:
        """Register that an initial alert went out — a reminder is now owed in 1h."""
        now = datetime.utcnow()
        entry = {
            'type':            recon_type,
            'date':            date_str,
            'initial_sent_at': now.isoformat(timespec='seconds'),
            'next_due_at':     (now + REMINDER_INTERVAL).isoformat(timespec='seconds'),
            'last_reminder_at': None,
            'reminder_count':  0,
            'recipients':      list(recipients or []),
            'attachment_path': attachment_path or '',
        }
        with self._lock:
            data = self._load()
            data['pending'][self._key(recon_type, date_str)] = entry
            self._save(data)
        logger.info(f'Reminder queued: {recon_type}/{date_str} — next due '
                    f'{entry["next_due_at"]} UTC')

    def mark_final_sent(self, recon_type: str, date_str: str) -> bool:
        """Clear a pending reminder once the final email has gone out."""
        with self._lock:
            data = self._load()
            removed = data['pending'].pop(self._key(recon_type, date_str), None)
            if removed is not None:
                self._save(data)
        if removed is not None:
            logger.info(f'Reminder cleared: {recon_type}/{date_str} '
                        f'(sent {removed.get("reminder_count", 0)} reminder(s))')
            return True
        return False

    def mark_reminder_sent(self, recon_type: str, date_str: str) -> None:
        """Bump count and reschedule the next reminder 1h from now."""
        now = datetime.utcnow()
        with self._lock:
            data = self._load()
            entry = data['pending'].get(self._key(recon_type, date_str))
            if entry is None:
                return
            entry['last_reminder_at'] = now.isoformat(timespec='seconds')
            entry['next_due_at']      = (now + REMINDER_INTERVAL).isoformat(timespec='seconds')
            entry['reminder_count']   = int(entry.get('reminder_count', 0)) + 1
            self._save(data)

    def get_due(self) -> List[dict]:
        """Return pending entries whose next_due_at has passed (UTC now)."""
        now = datetime.utcnow()
        due = []
        with self._lock:
            data = self._load()
            for key, entry in list(data['pending'].items()):
                try:
                    due_at = datetime.fromisoformat(entry.get('next_due_at', ''))
                except ValueError:
                    continue
                if now >= due_at:
                    due.append(dict(entry))
        return due

    def prune_stale(self) -> int:
        """Drop entries older than STALE_AFTER so the file doesn't grow forever."""
        cutoff = datetime.utcnow() - STALE_AFTER
        removed = 0
        with self._lock:
            data = self._load()
            for key in list(data['pending'].keys()):
                entry = data['pending'][key]
                try:
                    sent_at = datetime.fromisoformat(entry.get('initial_sent_at', ''))
                except ValueError:
                    continue
                if sent_at < cutoff:
                    data['pending'].pop(key)
                    removed += 1
            if removed:
                self._save(data)
        if removed:
            logger.info(f'recon_reminders: pruned {removed} stale entrie(s)')
        return removed

    def list_pending(self) -> List[dict]:
        with self._lock:
            data = self._load()
            return list(data['pending'].values())

    def clear_all(self) -> int:
        """Remove every pending reminder. Returns the count cleared.

        Operator-initiated bulk wipe — fired by the CP's "Clear all
        reminders" button via the reminder_clear_all command. Safe to
        call on a missing/empty store; returns 0 in that case.
        """
        with self._lock:
            data = self._load()
            cleared = len(data.get('pending', {}))
            data['pending'] = {}
            self._save(data)
        if cleared:
            logger.info(f'recon_reminders: cleared {cleared} entrie(s) via clear_all')
        return cleared
