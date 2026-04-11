"""Field whitelist enforcement for run-summary push.

The runner is forbidden from sending raw reconciliation data to the
control plane. This module is the one place where engine output is
translated into protocol-safe `counts`, `attachments_meta`, and
`log_lines` — every other part of the runner routes through here.

Rules:
  - Counts may only contain numeric or short-string values.
  - Counts keys are drawn from a flow-specific whitelist.
  - Attachment metadata is explicitly filtered: filename, size_bytes,
    sha256 only. Never the full path and never the content.
  - Log lines are truncated to 4000 chars each, 1000 lines max.

If an engine grows a new count category, it MUST be added to this
module before it will propagate upstream. Fail-closed by design.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Iterable

from .protocol import LogLine, ReconType

logger = logging.getLogger(__name__)

# Per-recon-type whitelists. Any key not listed here is silently dropped.
HOLDINGS_COUNT_KEYS: frozenset[str] = frozenset({
    "clean",
    "unexplained",
    "minor_break",
    "custody_only",
    "ws_only",
    "advisory",
    "unverified",
    "pending_explained",
    "comments_count",
    "total_positions",
})

BANK_COUNT_KEYS: frozenset[str] = frozenset({
    "total_pools",
    "clean",
    "breaks",
    "covered",
    "not_in_ws",
    "not_mapped",
    "balance_breaks",
    "transaction_breaks",
    "no_statement",
})

TRADE_COUNT_KEYS: frozenset[str] = frozenset({
    "total_orders",
    "c1_breaks",
    "c2_breaks",
    "c3_breaks",
    "total_0096_rows",
    "exchange_breaks",
    "broker_cn_count",
    "nsdl_row_count",
    "dropped_rejected",
})

_KEYSETS: dict[ReconType, frozenset[str]] = {
    "holdings": HOLDINGS_COUNT_KEYS,
    "bank": BANK_COUNT_KEYS,
    "trade": TRADE_COUNT_KEYS,
}


def filter_counts(recon_type: ReconType, raw: dict[str, Any]) -> dict[str, int | str]:
    """Drop everything not in the whitelist and coerce values to int/str.

    Non-numeric, non-string values are dropped. Strings are truncated to
    64 chars. Dicts, lists, None, floats that are not whole numbers —
    all dropped. This is intentional: if an engine returns a nested
    structure, we don't ship it.
    """
    allowed = _KEYSETS.get(recon_type, frozenset())
    out: dict[str, int | str] = {}
    for key, value in raw.items():
        if key not in allowed:
            continue
        if isinstance(value, bool):
            # bool is a subclass of int — coerce explicitly to avoid
            # "False" ever being interpreted as 0 somewhere downstream
            out[key] = 1 if value else 0
        elif isinstance(value, int):
            out[key] = value
        elif isinstance(value, float):
            # Only keep whole-number floats, as ints. Fractional counts
            # don't make sense for the fields we're whitelisting.
            if value.is_integer():
                out[key] = int(value)
        elif isinstance(value, str):
            out[key] = value[:64]
        # Everything else silently dropped
    dropped = set(raw.keys()) - allowed
    if dropped:
        logger.debug(f"whitelist.filter_counts dropped keys: {sorted(dropped)}")
    return out


def derive_counts_from_results(
    recon_type: ReconType, results: dict[str, Any]
) -> dict[str, int | str]:
    """Translate an engine's `results` dict (which for holdings is
    {category: [rows]}) into count-shaped data for the protocol.

    Holdings engine returns a dict like:
      {'clean': [row, row, ...], 'unexplained': [...], ...}
    We translate to:
      {'clean': N, 'unexplained': M, ...}

    For bank and trade engines we expect the engine to already return
    a counts-shaped dict (via its to_dict() method), so we just
    whitelist-filter it.
    """
    if recon_type == "holdings":
        counts: dict[str, Any] = {}
        for k, v in results.items():
            if isinstance(v, list):
                counts[k] = len(v)
            elif isinstance(v, (int, float, str)):
                counts[k] = v
        # Add a grand total so the history screen can show a cheap
        # "N positions reconciled" without summing on the server side.
        counts["total_positions"] = sum(
            len(v) for v in results.values() if isinstance(v, list)
        )
        return filter_counts(recon_type, counts)
    return filter_counts(recon_type, results)


def attachment_metadata(path: str | Path | None) -> dict[str, Any]:
    """Produce safe attachment metadata for the protocol.

    Only filename, size, and sha256 — never the full path and never
    the content. Non-existent paths return an empty dict. The control
    plane uses this only for display ("Download the report locally
    from the agent").
    """
    if not path:
        return {}
    p = Path(path)
    if not p.exists() or not p.is_file():
        return {}
    try:
        size = p.stat().st_size
    except OSError:
        return {}
    sha = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                sha.update(chunk)
    except OSError:
        return {}
    return {
        "filename": p.name,
        "size_bytes": size,
        "sha256": sha.hexdigest(),
    }


def filter_log_lines(lines: Iterable[Any]) -> list[LogLine]:
    """Truncate and coerce a sequence of log entries for upload.

    Accepts either plain strings (treated as info-level), pre-made
    LogLine objects, or dict-like entries with {ts, level, msg} fields.
    Any shape we can't recognise is dropped.
    """
    from datetime import datetime, timezone

    out: list[LogLine] = []
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for entry in lines:
        ts = now_iso
        level: Any = "info"
        msg: Any = None
        if isinstance(entry, LogLine):
            out.append(LogLine(
                ts=entry.ts,
                level=entry.level,
                msg=entry.msg[:4000],
            ))
            continue
        if isinstance(entry, str):
            msg = entry
        elif isinstance(entry, dict):
            ts = str(entry.get("ts", now_iso))
            level = entry.get("level", "info")
            msg = entry.get("msg")
        else:
            continue
        if not msg:
            continue
        if level not in ("debug", "info", "warning", "error"):
            level = "info"
        out.append(LogLine(ts=ts, level=level, msg=str(msg)[:4000]))
        if len(out) >= 1000:
            break
    return out
