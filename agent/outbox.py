"""Local outbox for run-push messages.

When the control plane is unreachable, the runner persists run summaries
to a tiny SQLite file under the agent's data dir. The poll loop drains
the outbox on every successful poll: each row is retried, and on 2xx
the row is deleted.

Phase 1 scope is deliberately small:
  - single table `outbox_runs`
  - append-only writes
  - FIFO drain order
  - no TTL — a stuck row stays forever until it succeeds or is manually
    deleted via the local UI (future feature)

Phase 2 will add:
  - dead-letter after N attempts
  - heartbeat outbox (health pings can also fail mid-blip)
  - per-tenant quota so a misbehaving agent can't fill disk
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir

logger = logging.getLogger(__name__)


OUTBOX_FILE = "outbox.db"


def _outbox_path() -> Path:
    return data_dir() / OUTBOX_FILE


@dataclass
class OutboxItem:
    id: int
    created_at: str
    attempts: int
    last_error: str
    payload: dict


class RunOutbox:
    """Thread-safe SQLite-backed outbox. Fast, simple, survives restarts."""

    def __init__(self) -> None:
        self._path = _outbox_path()
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_runs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at  TEXT    NOT NULL,
                    attempts    INTEGER NOT NULL DEFAULT 0,
                    last_error  TEXT    NOT NULL DEFAULT '',
                    payload     TEXT    NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outbox_runs_created "
                "ON outbox_runs(created_at)"
            )

    def enqueue(self, payload: dict) -> int:
        """Append a run-push payload. Returns the new row id."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO outbox_runs(created_at, payload) VALUES (?, ?)",
                (now, json.dumps(payload, default=str)),
            )
            return int(cur.lastrowid or 0)

    def peek(self, limit: int = 16) -> list[OutboxItem]:
        """Return up to `limit` oldest items (FIFO)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, created_at, attempts, last_error, payload "
                "FROM outbox_runs "
                "ORDER BY created_at ASC, id ASC "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            OutboxItem(
                id=int(r[0]),
                created_at=str(r[1]),
                attempts=int(r[2]),
                last_error=str(r[3]),
                payload=json.loads(r[4]),
            )
            for r in rows
        ]

    def bump_attempt(self, item_id: int, error: str) -> None:
        """Record a failed attempt against a row without removing it."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE outbox_runs SET attempts = attempts + 1, last_error = ? "
                "WHERE id = ?",
                (error[:4000], item_id),
            )

    def delete(self, item_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM outbox_runs WHERE id = ?", (item_id,))

    def count(self) -> int:
        with self._lock, self._connect() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM outbox_runs").fetchone()[0]
            )


_store: RunOutbox | None = None


def get_outbox() -> RunOutbox:
    global _store
    if _store is None:
        _store = RunOutbox()
    return _store
