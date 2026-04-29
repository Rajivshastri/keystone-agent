"""Previous-business-day custody snapshot loader.

When today's recon produces Breaks / WS-Only rows for positions that
custody *used to* have yesterday, the most likely explanation is an
un-booked sell on the WS side. This module re-parses the prior day's
custody files and returns a `{(client_id, isin): qty}` lookup so the
recon engine can enrich those rows with a "Custody dropped from N since
<prev_date>" note.

Intentionally small and defensive — a prev-day load failure must not
break the main recon, so callers can either pass the result in or ignore
it if the dict is empty / a specific (client, isin) is missing.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Extension whitelist — custodian files are always one of these. We skip
# .zip (extracted copy already sits alongside), .csv (usually an
# auto-converted sidecar), and raw .txt exports (bank only).
_CUSTODY_EXTS = (".xlsx", ".xls")


def _find_custody_files(raw_root: Path, source_name: str) -> List[Path]:
    """Return all custodian files for one source under raw/{source_name}/.

    Walks recursively so Kotak's per-strategy subfolders (MYSTIC_WEVA,
    MYSTIC_WEMO) are picked up without special-casing.
    """
    src_dir = raw_root / source_name
    if not src_dir.exists():
        return []
    out: List[Path] = []
    for p in src_dir.rglob("*"):
        if not p.is_file():
            continue
        name = p.name.lower()
        if not name.endswith(_CUSTODY_EXTS):
            continue
        # Skip WS master files accidentally dropped in raw/
        if name.startswith("z13_") or name.startswith("z8_"):
            continue
        out.append(p)
    return out


def load_custody_snapshot(
    date_str: str,
    workdir: Path,
    sources_path: Optional[Path] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Dict[Tuple[str, str], Dict[str, float]]:
    """Parse every active non-bank custody file for `date_str` and return
    a lookup keyed by (client_id, isin).

    Returns:
        {(client_id, isin): {"logical": float, "saleable": float,
                             "sources": set[str]}}

    Missing sources don't raise — they log-and-skip. This is deliberate:
    the prev-day snapshot is a hint, not a gate, so one custodian's bad
    file shouldn't blind-spot the whole annotation pass.
    """
    log = log_fn if log_fn else (lambda m: logger.info(m))
    workdir = Path(workdir)
    raw_root = workdir / "data" / date_str / "raw"
    if not raw_root.exists():
        log(f"Prev custody: no raw/ dir for {date_str} ({raw_root})")
        return {}

    # Lazy import — this module is consumed from both Flask and the agent.
    from parsers import get_parser

    if sources_path is None:
        # Respect KEYSTONE_CONFIG_DIR on Azure; fall back to bundled config.
        cfg_dir = os.environ.get("KEYSTONE_CONFIG_DIR")
        sources_path = (
            Path(cfg_dir) / "sources.json"
            if cfg_dir
            else workdir / "config" / "sources.json"
        )
    try:
        sources = json.loads(Path(sources_path).read_text()).get("sources", [])
    except Exception as e:
        log(f"Prev custody: could not read sources.json ({e}) — skipping")
        return {}

    snapshot: Dict[Tuple[str, str], Dict[str, float]] = {}

    for source in sources:
        if not source.get("active", True):
            continue
        if source.get("is_bank", False):
            continue

        sname = source["name"]
        parser_name = source.get("parser", sname)
        from core.secret_resolver import resolve_from_source_dict
        password = resolve_from_source_dict(source, "file_password")

        files = _find_custody_files(raw_root, sname)
        if not files:
            continue

        try:
            parser = get_parser(parser_name)
        except Exception as e:
            log(f"Prev custody: no parser for {sname!r} ({e}) — skipping")
            continue

        parsed_rows = 0
        for fpath in files:
            try:
                result = parser.parse_file(str(fpath), date_str, password)
            except Exception as e:
                log(f"Prev custody: {sname} {fpath.name} parse failed ({e})")
                continue
            if getattr(result, "error", None):
                continue
            for rec in getattr(result, "records", []) or []:
                client = (getattr(rec, "client_id", "") or "").strip()
                isin = (getattr(rec, "isin", "") or "").strip().upper()
                if not client or not isin:
                    continue
                entry = snapshot.setdefault(
                    (client, isin),
                    {"logical": 0.0, "saleable": 0.0, "sources": set()},
                )
                try:
                    entry["logical"] += float(getattr(rec, "logical_holding", 0) or 0)
                    entry["saleable"] += float(getattr(rec, "saleable_holding", 0) or 0)
                except (TypeError, ValueError):
                    pass
                entry["sources"].add(sname)
                parsed_rows += 1
        if parsed_rows:
            log(f"Prev custody ({date_str}) {sname}: {parsed_rows} rows loaded")

    return snapshot


def prev_business_day(date_str: str) -> str:
    """Return the prior Mon-Fri as YYYY-MM-DD. Holiday-unaware —
    callers should fall back further if the snapshot turns up empty.
    """
    from datetime import datetime, timedelta

    d = datetime.strptime(date_str, "%Y-%m-%d")
    d -= timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")
