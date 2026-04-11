"""Server-side scratch table for template upload drafts.

When an operator uploads a CSV on the Templates page, we parse and diff
it immediately but don't commit yet — we first show a preview. The old
flow round-tripped the full CSV through a hidden form field on the
preview page, which was fragile for large uploads and awkward with
quoting. This module replaces that with a tiny SQLite-backed scratch
store: the upload stashes the CSV under a random token, the preview URL
carries only the token, and the commit endpoint looks the draft up.

Drafts are single-tenant (the agent is already per-tenant) and expire
after DRAFT_TTL_SECONDS. A sweep runs on every read so stale rows don't
accumulate.
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir

DRAFT_TTL_SECONDS = 60 * 60  # 1 hour
DRAFTS_FILE = "template_drafts.db"


def _db_path() -> Path:
    return data_dir() / DRAFTS_FILE


@dataclass
class Draft:
    token: str
    slug: str
    csv_text: str
    created_at: str


class TemplateDraftStore:
    """Thread-safe SQLite store for in-flight template uploads."""

    def __init__(self) -> None:
        self._path = _db_path()
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
                CREATE TABLE IF NOT EXISTS template_drafts (
                    token       TEXT PRIMARY KEY,
                    slug        TEXT NOT NULL,
                    csv_text    TEXT NOT NULL,
                    created_at  TEXT NOT NULL
                )
                """
            )

    def put(self, slug: str, csv_text: str) -> str:
        token = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO template_drafts(token, slug, csv_text, created_at) "
                "VALUES (?, ?, ?, ?)",
                (token, slug, csv_text, now),
            )
        self._sweep()
        return token

    def get(self, token: str) -> Draft | None:
        self._sweep()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT token, slug, csv_text, created_at "
                "FROM template_drafts WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None
        return Draft(
            token=str(row[0]),
            slug=str(row[1]),
            csv_text=str(row[2]),
            created_at=str(row[3]),
        )

    def delete(self, token: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM template_drafts WHERE token = ?", (token,))

    def _sweep(self) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - DRAFT_TTL_SECONDS
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(
            timespec="seconds"
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM template_drafts WHERE created_at < ?", (cutoff_iso,)
            )


_store: TemplateDraftStore | None = None


def get_store() -> TemplateDraftStore:
    global _store
    if _store is None:
        _store = TemplateDraftStore()
    return _store
