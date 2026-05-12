"""
core/fee_calc_cache.py — per-period fee-compute result cache.

A fee compute is non-trivial (downloads Daily AUM, walks day-by-day
across N accounts × ~90 days). Operators commonly re-open the same
period in the UI multiple times — caching the full ``compute_fees_daily``
output keeps that experience instant after the first run.

Cache layout (per period):

    ${KEYSTONE_DATA_DIR}/data/fee_runs/{from}_{to}.json
    {
      "period":                     {"from": "...", "to": "..."},
      "computed_at":                "2026-05-08T05:30:00Z",
      "fee_config_signature":       "<sha256>",
      "effective_dates_signature":  "<sha256>",
      "result":                     { ...full compute_fees_daily output... }
    }

Invalidation rule (per the spec):
  - Cache is INVALID if either fee_configs or effective_dates have
    changed since the cache entry was written. Both are hashed
    content-stably so the operator updating a single share rate or a
    single client's effective date triggers re-compute.
  - Cache is VALID otherwise — operator's "compute again for the same
    period with same inputs" returns the prior result instantly.

Rotation: keep newest 25 cache files by mtime, delete older.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ROTATE_KEEP = 25

# Bump when the compute_fees_daily output shape changes in a way
# downstream consumers (mailer, exporter) can't backfill from older
# cached results. Lookups with a different version return None →
# cache miss → recompute. Versions:
#  v2 — added per-role pct fields to by_account (gsw_pct / fm_pct
#       etc. + headline_pct) so the per-entity PDF mailer can show
#       share rates.
#  v3 — fee_config gained breakup_periods (time-varying breakup),
#       headline now sourced from CBG master FLATFEE per-day, line
#       items now per-segment so accounts can produce >1 row,
#       sum_aum + billgroup_description added to by_account, and
#       no_breakup added to unmatched.
SCHEMA_VERSION = 3


# ── Path resolution ──────────────────────────────────────────────────────── #

def _cache_dir() -> Path:
    """KEYSTONE_DATA_DIR-rooted (so the cache survives container redeploys
    and lives alongside the Daily AUM downloads under data/)."""
    base = os.environ.get("KEYSTONE_DATA_DIR") or str(
        Path(__file__).parent.parent
    )
    return Path(base) / "data" / "fee_runs"


def _file(from_str: str, to_str: str) -> Path:
    return _cache_dir() / f"{from_str}_{to_str}.json"


# ── Signature hashing ───────────────────────────────────────────────────── #
#
# Hash only the fields that affect compute output. Skipping derivation
# fields (_client_name, _scheme_name, _client_row_id) keeps cache
# stable when those change without affecting the math.

def _fee_config_signature(fee_configs: Dict[str, Dict[str, Any]]) -> str:
    """Hash the breakup_periods array per account. Headline rate is
    NOT hashed here — it's sourced from CBG master, whose own
    signature catches changes via _effective_dates_signature.
    Derivation fields (_client_name, _scheme_name, _client_row_id)
    are excluded so a name change without a math change keeps the
    cache valid."""
    sig = {
        k: {"breakup_periods": v.get("breakup_periods") or []}
        for k, v in (fee_configs or {}).items()
    }
    h = hashlib.sha256()
    h.update(json.dumps(sig, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()


def _effective_dates_signature(effective_dates: Dict) -> str:
    # Tuple keys aren't JSON-serialisable directly; coerce to repr() for
    # stable hashing across process restarts.
    sig = {repr(k): v for k, v in (effective_dates or {}).items()}
    h = hashlib.sha256()
    h.update(json.dumps(sig, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()


# ── Public API ───────────────────────────────────────────────────────────── #

def lookup(from_str: str, to_str: str,
           fee_configs: Dict[str, Dict[str, Any]],
           effective_dates: Dict) -> Optional[Dict[str, Any]]:
    """Return cached compute result if both signatures still match,
    else None."""
    p = _file(from_str, to_str)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"fee cache: unreadable {p}: {e}")
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    if data.get("fee_config_signature") != _fee_config_signature(fee_configs):
        return None
    if data.get("effective_dates_signature") != _effective_dates_signature(effective_dates):
        return None
    return data.get("result")


def summary(from_str: str, to_str: str) -> Optional[Dict[str, Any]]:
    """Light-read for the UI's pre-compute prompt — returns just
    metadata (when computed, headline totals).

    No freshness check here: signature comparison requires loading
    fee_configs + effective_dates (clients.db + 100KB XLS) which can
    take 1-2s on the production container. The compute endpoint
    re-validates via ``lookup()`` when the operator confirms — if
    the cache is stale, it transparently recomputes. From the
    operator's POV: 'use cached' is best-effort; if data has changed
    they get a fresh compute anyway."""
    p = _file(from_str, to_str)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    r = data.get("result") or {}
    totals = r.get("totals") or {}
    return {
        "computed_at": data.get("computed_at"),
        "total_fees":  totals.get("total_fees"),
        "accounts":    totals.get("accounts"),
    }


def save(from_str: str, to_str: str,
         result: Dict[str, Any],
         fee_configs: Dict[str, Dict[str, Any]],
         effective_dates: Dict) -> None:
    """Write cache + rotate."""
    p = _file(from_str, to_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version":            SCHEMA_VERSION,
        "period":                    {"from": from_str, "to": to_str},
        "computed_at":               datetime.now(timezone.utc).strftime(
                                          "%Y-%m-%dT%H:%M:%SZ"),
        "fee_config_signature":      _fee_config_signature(fee_configs),
        "effective_dates_signature": _effective_dates_signature(effective_dates),
        "result":                    result,
    }
    p.write_text(json.dumps(payload, default=str), encoding="utf-8")
    _rotate(ROTATE_KEEP)


def _rotate(keep: int) -> None:
    """Keep the ``keep`` newest cache files by mtime; delete the rest."""
    try:
        files = sorted(
            _cache_dir().glob("*.json"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
    except FileNotFoundError:
        return
    for f in files[keep:]:
        try:
            f.unlink()
        except Exception as e:
            logger.warning(f"fee cache rotate: could not delete {f}: {e}")
