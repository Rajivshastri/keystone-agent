"""
Fetch Log
---------
Persistent audit log of all email fetch runs.
Stored as JSONL (one JSON record per line) at data/email_fetch_log.jsonl.

Each record:
  id          — short unique ID for the run
  trigger     — 'scheduler' | 'manual' | 'override'
  user        — email of the triggering user, or 'system' for scheduler
  started_at  — UTC ISO timestamp
  date_from   — earliest date searched (YYYY-MM-DD)
  date_to     — latest date searched (YYYY-MM-DD)
  ok          — number of files saved successfully
  errors      — number of errors
  log         — first 100 log lines from the run
"""
import json
import uuid
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Path relative to this file: core/ → parent → data/
_LOG_PATH = Path(__file__).parent.parent / 'data' / 'email_fetch_log.jsonl'


def _log_path() -> Path:
    return _LOG_PATH


def record_fetch(trigger: str, user: str,
                 date_from: str, date_to: str,
                 ok: int, errors: int,
                 log_lines: list) -> str:
    """
    Append a fetch record to the log. Returns the fetch_id.

    trigger:    'scheduler' | 'manual' | 'override'
    user:       email address or 'system'
    date_from:  start of search window 'YYYY-MM-DD'
    date_to:    end of search window 'YYYY-MM-DD'
    ok:         count of files saved successfully
    errors:     count of errors
    log_lines:  list of log message strings (capped to first 100)
    """
    fetch_id = str(uuid.uuid4())[:8]
    record = {
        'id':         fetch_id,
        'trigger':    trigger,
        'user':       user,
        'started_at': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
        'date_from':  date_from,
        'date_to':    date_to,
        'ok':         ok,
        'errors':     errors,
        'log':        (log_lines or [])[:100],
    }
    try:
        _log_path().parent.mkdir(parents=True, exist_ok=True)
        with open(_log_path(), 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
    except Exception as e:
        logger.warning(f"fetch_log: failed to write record: {e}")
    return fetch_id


def get_history(limit: int = 100) -> list:
    """
    Return the last `limit` fetch records, newest first.
    """
    path = _log_path()
    if not path.exists():
        return []
    records = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        logger.warning(f"fetch_log: failed to read history: {e}")
        return []
    # Return newest first, capped to limit
    return list(reversed(records[-limit:]))


def get_last_fetch() -> Optional[dict]:
    """
    Return the most recent fetch record, or None if the log is empty.
    """
    history = get_history(limit=1)
    return history[0] if history else None
