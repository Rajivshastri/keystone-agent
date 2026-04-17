"""Masters service — on-demand merge of WS Z30 data with local extras.

The master data itself (name, PAN, address, bank details, etc.) lives
only in WealthSpectrum. We fetch it on demand by downloading the Z30
query XLS, parse the rows, and merge them with our local "extras" —
the fields we maintain at the agent that WS doesn't know about
(workflow state, provenance, WS tracking timestamps, internal tags).

Merge key: BOTH dp_id AND dp_client_id must match. PAN alone is not
enough — the same individual can have multiple DP accounts across
schemes.

Cache: 5-minute TTL. Bypass with refresh=True on any call.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


CACHE_TTL_SECONDS = 300   # 5 minutes


@dataclass
class MastersCacheEntry:
    rows: list[dict[str, Any]]
    fetched_at: float


_client_cache: MastersCacheEntry | None = None


# ── Column mapping from Z30_ClientDetail.xls header to our field names ─────
# These are WS-authoritative fields — we always display from WS, never
# from our local store.
_Z30_CLIENT_COLUMNS = {
    "CLIENTID":          "ws_client_id",
    "CLIENTNAME":        "name",
    "BIRTHDATE":         "birth_date",
    "CLIENTCODE":        "client_code",
    "CONTACTNAME":       "contact_name",
    "PHONE":             "phone",
    "MOBILE":            "mobile",
    "EMAILID":           "email",
    "H1PANNO":           "pan",
    "H1MIN":             "mapin",
    "H1TANNO":           "tan",
    "GROUPID":           "group_id",
    "GROUPNAME":         "group_name",
    "SCHEMENAME":        "scheme_name",
    "INTERMEDIARYNAME":  "intermediary",
    "BRANCHNAME":        "branch",
    "RELMGRNAME":        "rm_name",
    "H1ADD1":            "address1",
    "H1ADD2":            "address2",
    "H1CITY":            "city",
    "H1PIN":             "pin_code",
    "H1STATE":           "state",
    "BROKERACID":        "broker_account",
    "BANKCODE":          "bank_code",
    "BANKACID":          "bank_account",
    "DPCLIENTID":        "dp_client_id",
    "BILLGROUP":         "bill_group",
    "H1STATUS":          "status",
    "ASSETS":            "assets",
    "NETCAPITAL":        "net_capital",
    "TDSFLAG":           "tds_flag",
    "ACCOUNTINGTXN":     "accounting_txn",
    "STTTAKEN AS":       "stt_taken_as",
    "TRXNTAKEN AS":      "trxn_taken_as",
}


def _workdir() -> Path:
    """Agent workdir — same place trade/holdings/bank recons write.

    Resolution order:
      1. Agent settings.workdir (authoritative — what _pre_reconciliation uses)
      2. KEYSTONE_DATA_DIR env var (dev override)
      3. <source repo root>       (dev fallback only)
    """
    try:
        from agent.config import load_settings
        s = load_settings()
        if s.workdir:
            return Path(s.workdir)
    except Exception:
        pass
    base = os.environ.get("KEYSTONE_DATA_DIR") or str(Path(__file__).parent.parent)
    return Path(base)


def _ws_credentials() -> tuple[str, str]:
    """Read (ws_user, ws_pass) from the agent's stored settings.

    Falls back to FINCRM_USER / FINCRM_PASS env vars when running from
    source / dev mode without the agent settings store available.
    """
    # Prefer agent-managed settings (paired agent, persisted credentials)
    try:
        from agent.config import load_settings
        from agent.secrets import KEY_WS_PORTAL_PASSWORD, get_store
        from agent.setup import EK_WS_USERNAME

        settings = load_settings()
        extras = settings.extras or {}
        user = extras.get(EK_WS_USERNAME, "").strip()
        pwd = (get_store().get(KEY_WS_PORTAL_PASSWORD) or "").strip()
        if user and pwd:
            return user, pwd
    except Exception as e:
        log.debug(f"Agent settings unavailable, falling back to env: {e}")

    return (os.environ.get("FINCRM_USER", "").strip(),
            os.environ.get("FINCRM_PASS", "").strip())


def _find_latest_z30_client_detail() -> Path | None:
    """Return the most recently modified ClientDetail file on disk.

    Walks data/{YYYY-MM-DD}/masters/ directories looking for files
    matching either ``ClientDetail*.xls`` (the name WS writes when
    downloaded via ws_downloader) or ``Z30_ClientDetail*.xls`` (the
    historical name used by bulk-downloaded snapshots). Returns None
    if no file found — caller should trigger a download.
    """
    data_root = _workdir() / "data"
    if not data_root.exists():
        return None
    candidates: list[Path] = []
    patterns = ("ClientDetail*.xls", "Z30_ClientDetail*.xls")
    for date_dir in data_root.iterdir():
        if not date_dir.is_dir():
            continue
        masters_dir = date_dir / "masters"
        if not masters_dir.exists():
            continue
        for pat in patterns:
            for f in masters_dir.glob(pat):
                candidates.append(f)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _download_z30_client_detail() -> Path | None:
    """Trigger a WS download of the Client Details master.

    Uses run_all_downloads with a reports_filter of just ["Client Details"].
    Mirrors the credential-injection pattern used by _pre_reconciliation:
    ws_downloader reads FINCRM_USER/FINCRM_PASS from the environment,
    so we temporarily set them from the agent's stored settings before
    the call and restore afterwards.

    Returns the downloaded file path, or None on failure.
    """
    from datetime import datetime
    try:
        import ws_downloader as _wsd
    except ImportError:
        log.error("ws_downloader import failed — cannot download Z30_ClientDetail")
        return None

    ws_user, ws_pass = _ws_credentials()
    if not ws_user or not ws_pass:
        log.error("WS credentials not configured — cannot download Z30_ClientDetail")
        return None

    prev = {
        "FINCRM_USER": os.environ.get("FINCRM_USER"),
        "FINCRM_PASS": os.environ.get("FINCRM_PASS"),
    }
    os.environ["FINCRM_USER"] = ws_user
    os.environ["FINCRM_PASS"] = ws_pass
    try:
        result = _wsd.run_all_downloads(
            date_obj=datetime.now(),
            app_dir=_workdir(),
            reports_filter=["Client Details"],
        )
    except Exception as e:
        log.exception(f"Z30 client detail download failed: {e}")
        return None
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    if not result.get("success_count"):
        log.warning(f"Z30 client detail download returned no success: {result}")
        return None

    return _find_latest_z30_client_detail()


def _parse_z30_client_detail(path: Path) -> list[dict[str, Any]]:
    """Parse the Z30 XLS into a list of normalized dicts."""
    try:
        import xlrd
    except ImportError:
        log.error("xlrd not available — cannot parse Z30 XLS")
        return []

    try:
        wb = xlrd.open_workbook(str(path))
        ws = wb.sheet_by_index(0)
    except Exception as e:
        log.exception(f"Z30 XLS open failed: {e}")
        return []

    if ws.nrows < 1:
        return []

    headers = [str(ws.cell_value(0, c)).strip() for c in range(ws.ncols)]
    rows: list[dict[str, Any]] = []
    for r in range(1, ws.nrows):
        raw = {headers[c]: ws.cell_value(r, c) for c in range(ws.ncols)}
        norm: dict[str, Any] = {}
        for ws_col, our_key in _Z30_CLIENT_COLUMNS.items():
            val = raw.get(ws_col, "")
            if isinstance(val, float) and val.is_integer():
                val = str(int(val))
            elif not isinstance(val, str):
                val = str(val)
            norm[our_key] = val.strip()
        # dp_id isn't in this Z30 snapshot; WS stores it on a separate
        # master that we'll join later if needed. Default to blank.
        norm.setdefault("dp_id", "")
        if norm.get("pan") or norm.get("dp_client_id"):
            rows.append(norm)
    return rows


def _load_local_extras() -> dict[tuple[str, str], dict[str, Any]]:
    """Load local extras keyed by (dp_id, dp_client_id).

    Pulls from the existing clients SQLite table (the onboarding DB).
    Only returns the fields that we consider "extras" — everything else
    comes from WS.
    """
    try:
        from core.client_onboarding import _db_path  # reuse existing path logic
    except Exception:
        log.info("client_onboarding not available — no extras will be merged")
        return {}

    db_path = _db_path()
    if not Path(db_path).exists():
        return {}

    extras_cols = [
        "dp_id",
        "dp_client_id",
        "ws_ref_number",
        "ws_push_date",
        "ws_authorize_date",
        "ws_authorized",
        "cml_source_file",
        "pending_auth",
        "creator_token",
        "auth_token",
        "submitted_at",
        "auth_notes",
        "created_at",
        "updated_at",
    ]

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                f"SELECT {','.join(extras_cols)} FROM clients "
                f"WHERE dp_id IS NOT NULL AND dp_client_id IS NOT NULL"
            )
            out: dict[tuple[str, str], dict[str, Any]] = {}
            for row in cur.fetchall():
                d = dict(row)
                key = (d["dp_id"] or "", d["dp_client_id"] or "")
                out[key] = d
            return out
    except sqlite3.OperationalError as e:
        log.info(f"Local extras query skipped: {e}")
        return {}


def get_client_master(mode: str = "load") -> dict[str, Any]:
    """Return the merged client master view.

    mode:
      "load"  — parse the latest Z30_ClientDetail XLS already on disk
                (usually written by the most recent trade recon run).
                Returns an error result if no file exists.
      "fetch" — download a fresh Z30_ClientDetail from WS, then parse.
      "cache" — return the in-memory cache if within TTL, else behave
                like "load". Used by repeated UI reads.

    Returns:
      {
        "rows": [ ...merged dicts... ],
        "fetched_at": <epoch seconds>,
        "source_file": "/path/to/Z30_ClientDetail.xls" or None,
        "mode": "load" | "fetch" | "cache",
        "error": "..." (only on failure),
      }
    """
    global _client_cache
    now = time.time()

    if mode == "cache" and _client_cache is not None:
        if (now - _client_cache.fetched_at) < CACHE_TTL_SECONDS:
            return {
                "rows": _client_cache.rows,
                "fetched_at": _client_cache.fetched_at,
                "mode": "cache",
                "source_file": None,
            }
        mode = "load"

    if mode == "fetch":
        log.info("get_client_master(fetch): downloading fresh Z30_ClientDetail from WS")
        z30_path = _download_z30_client_detail()
        if z30_path is None:
            return {"rows": [], "fetched_at": now, "source_file": None,
                    "mode": "fetch",
                    "error": "WS download failed — check credentials and network"}
    else:  # load
        z30_path = _find_latest_z30_client_detail()
        if z30_path is None:
            return {"rows": [], "fetched_at": now, "source_file": None,
                    "mode": "load",
                    "error": "No Z30_ClientDetail file found on disk — "
                             "run trade recon first, or click Fetch to download"}

    ws_rows = _parse_z30_client_detail(z30_path)
    extras = _load_local_extras()

    merged: list[dict[str, Any]] = []
    for r in ws_rows:
        key = (r.get("dp_id", ""), r.get("dp_client_id", ""))
        extras_row = extras.get(key, {})
        r["extras"] = {k: v for k, v in extras_row.items()
                       if k not in ("dp_id", "dp_client_id") and v}
        merged.append(r)

    _client_cache = MastersCacheEntry(rows=merged, fetched_at=now)

    return {
        "rows": merged,
        "fetched_at": now,
        "source_file": str(z30_path),
        "mode": mode,
    }


def invalidate_client_cache() -> None:
    """Clear the in-memory cache. Next call fetches fresh."""
    global _client_cache
    _client_cache = None
