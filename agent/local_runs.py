"""Local run artifact tracker.

Keystone's whitelist guarantees that the control plane never receives
the raw recon report — only metadata (filename, size, sha256). But the
operator who is physically sitting at the agent machine usually wants
to *open* the report after reviewing the summary in the browser. This
module records, for every successful run, the absolute path of the
engine-written output file on local disk.

The local UI exposes `GET /api/runs/{job_id}/download` which looks up
the row by job_id (the control-plane-assigned job identifier that
travels with every RunPush) and streams the file. The Run Detail page
on the control plane renders a button that points at this URL — the
button only works when the operator's browser is on the same machine
as the agent, which is exactly the intended audience.

No TTL: rows stay forever. A future phase can add a prune job. For
now, an operator who wants to clean up can delete the local_runs.db
file — it gets rebuilt on next run.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir

LOCAL_RUNS_FILE = "local_runs.db"


def _db_path() -> Path:
    return data_dir() / LOCAL_RUNS_FILE


@dataclass
class LocalRun:
    job_id: str
    run_type: str
    recon_date: str
    output_path: str
    created_at: str


class LocalRunStore:
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
                CREATE TABLE IF NOT EXISTS local_runs (
                    job_id       TEXT PRIMARY KEY,
                    run_type     TEXT NOT NULL,
                    recon_date   TEXT NOT NULL,
                    output_path  TEXT NOT NULL,
                    created_at   TEXT NOT NULL
                )
                """
            )

    def record(
        self, job_id: str, run_type: str, recon_date: str, output_path: str
    ) -> None:
        if not job_id or not output_path:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO local_runs "
                "(job_id, run_type, recon_date, output_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (job_id, run_type, recon_date, output_path, now),
            )

    def get(self, job_id: str) -> LocalRun | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT job_id, run_type, recon_date, output_path, created_at "
                "FROM local_runs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return LocalRun(
            job_id=str(row[0]),
            run_type=str(row[1]),
            recon_date=str(row[2]),
            output_path=str(row[3]),
            created_at=str(row[4]),
        )


_store: LocalRunStore | None = None


def get_store() -> LocalRunStore:
    global _store
    if _store is None:
        _store = LocalRunStore()
    return _store
