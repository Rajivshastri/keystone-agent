"""Templates library — bulk export/import of tabular config.

Operators don't want to hand-edit JSON to add a new pool or change a
bank account number. This module produces CSV snapshots of well-known
config files and accepts CSV uploads back, diffing them against the
current state and committing on confirmation.

Phase 3 ships the 'pool_accounts' template backed by
config/pools_hub.json. It covers the per-pool fields most often
changed by operations: bank details, dealer/demat accounts,
MAPIN/broker codes, active flag. List-valued fields (ws_scheme_names,
ws_overrides, broker_cn_aliases) are NOT in this template — they
live outside the CSV and get preserved verbatim on round-trip.

Other templates (clients.csv, recipients.csv, custodian_dispatch.csv)
can be added following the same pattern. Each registers a
TemplateSpec in TEMPLATES.
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Config files live alongside the core/ and parsers/ packages at the
# agent repo root in dev mode. In a PyInstaller bundle they are seeded
# from the read-only bundle into the writable data dir on first access.
# agent.paths.config_dir() handles both cases. Resolve per call rather
# than at import time so the lazy seed runs after data_dir() has been
# initialised.
from .paths import config_dir as _agent_config_dir


def _CONFIG_DIR() -> Path:  # noqa: N802 — keep old name for call sites
    return _agent_config_dir()


# ── Schema of one column ────────────────────────────────────────────── #


@dataclass
class ColumnSpec:
    name: str
    required: bool = False
    kind: str = "string"  # 'string' | 'int' | 'float' | 'bool'
    help: str = ""

    def coerce(self, raw: str) -> Any:
        s = (raw or "").strip()
        if self.kind == "string":
            return s
        if self.kind == "bool":
            return s.lower() in ("1", "true", "yes", "y", "t")
        if self.kind == "int":
            if s == "":
                return 0
            return int(s)
        if self.kind == "float":
            if s == "":
                return 0.0
            return float(s)
        if self.kind == "list_semi":
            if not s:
                return []
            return [part.strip() for part in s.split(";") if part.strip()]
        return s

    def to_csv_cell(self, v: Any) -> Any:
        if self.kind == "bool":
            return 1 if v else 0
        if self.kind == "list_semi":
            if v is None:
                return ""
            if isinstance(v, list):
                return ";".join(str(x) for x in v)
            return str(v)
        return v if v is not None else ""


# ── Template definition ─────────────────────────────────────────────── #


@dataclass
class TemplateSpec:
    slug: str
    title: str
    description: str
    columns: list[ColumnSpec]
    # Key column(s) used to match existing rows on import for diffing
    key: tuple[str, ...]

    # Per-template load/save hooks — each knows how to read and write
    # its backing JSON file. Returns a list of dicts, one per row.
    loader: Callable[[Path], list[dict[str, Any]]]
    saver: Callable[[Path, list[dict[str, Any]]], None]

    # Relative path under config/ — e.g. 'pools_hub.json'
    backing_file: str


# ── pool_accounts template ──────────────────────────────────────────── #

POOL_COLUMNS: list[ColumnSpec] = [
    ColumnSpec("pool_id", required=True, help="Short unique id for the pool"),
    ColumnSpec("display_name", required=True),
    ColumnSpec("active", kind="bool", required=True, help="1 to include in recons"),
    ColumnSpec("mapin", help="SEBI UCC / MAPIN code"),
    ColumnSpec("custodian_bank", help="Custodian bank label (ICICI / HDFC / Kotak / Axis)"),
    ColumnSpec("custodian_code", help="Custodian-side identifier"),
    ColumnSpec("bank", help="Operating bank for this pool"),
    ColumnSpec("bank_account", help="Bank account number"),
    ColumnSpec("pool_demat_account", help="Demat/DP account"),
    ColumnSpec("dealer_account", help="Dealer trading account"),
    ColumnSpec("kotak_client_id", help="Kotak WM client id if applicable"),
    ColumnSpec("ws_scheme_code", help="WS primary scheme code"),
]
POOL_KEY = ("pool_id",)


def _load_pools_hub(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f) or {}
    return list(data.get("pools", []))


def _save_pools_hub(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        with open(path) as f:
            data = json.load(f) or {}
    else:
        data = {"pools": []}
    data["pools"] = rows
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── sources template (sources.json) ─────────────────────────────────── #

SOURCE_COLUMNS: list[ColumnSpec] = [
    ColumnSpec("name", required=True, help="Short unique source id"),
    ColumnSpec("display_name", required=True),
    ColumnSpec("active", kind="bool", required=True),
    ColumnSpec("is_bank", kind="bool", help="1 for bank statement, 0 for holdings"),
    ColumnSpec("parser", required=True, help="Parser name from PARSER_REGISTRY"),
    ColumnSpec("sender_email", help="From-address filter"),
    ColumnSpec("subject_keyword", help="Substring the subject must contain"),
    ColumnSpec("subject_prefix"),
    ColumnSpec("zip_name_prefix"),
    ColumnSpec("zip_password"),
    ColumnSpec("file_password"),
    ColumnSpec("file_date_offset", kind="int", help="Days between email date and file date"),
    ColumnSpec("take_last_only", kind="bool"),
    ColumnSpec("attachment_type", help="zip / txt_or_csv / zip_aes256 / csv_in_zip"),
]
SOURCE_KEY = ("name",)


def _load_sources(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f) or {}
    return list(data.get("sources", []))


def _save_sources(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        with open(path) as f:
            data = json.load(f) or {}
    else:
        data = {"sources": []}
    data["sources"] = rows
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── strategy_mappings template (mappings.json) ──────────────────────── #

MAPPING_COLUMNS: list[ColumnSpec] = [
    ColumnSpec("id", required=True, help="Unique mapping row id"),
    ColumnSpec("source", required=True, help="Custodian source name"),
    ColumnSpec("broker_code", required=True),
    ColumnSpec("display_name"),
    ColumnSpec("default_ws_scheme_code", required=True),
]
MAPPING_KEY = ("id",)


def _load_mappings(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f) or {}
    return list(data.get("strategy_mappings", []))


def _save_mappings(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        with open(path) as f:
            data = json.load(f) or {}
    else:
        data = {"strategy_mappings": []}
    data["strategy_mappings"] = rows
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── custodian_dispatch template (custodian_dispatch.json) ───────────── #

DISPATCH_COLUMNS: list[ColumnSpec] = [
    ColumnSpec("custodian", required=True, help="Custodian key (ICICI / HDFC / KOTAK / AXIS)"),
    ColumnSpec("interface_type", required=True, help="WS Custody Interface label"),
    ColumnSpec("report_format", required=True, help="XX=XLSX, X=XLS, C=CSV"),
    ColumnSpec("email_to", kind="list_semi", help="Recipients, semicolon separated"),
    ColumnSpec("email_subject"),
    ColumnSpec("email_body"),
    ColumnSpec("send_from"),
]
DISPATCH_KEY = ("custodian",)


def _load_dispatch(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f) or {}
    out: list[dict[str, Any]] = []
    for key, cfg in (data.get("custodians") or {}).items():
        row = {"custodian": key}
        row.update(cfg or {})
        out.append(row)
    return out


def _save_dispatch(path: Path, rows: list[dict[str, Any]]) -> None:
    if path.exists():
        with open(path) as f:
            data = json.load(f) or {}
    else:
        data = {"custodians": {}}
    custodians: dict[str, Any] = dict(data.get("custodians") or {})
    for row in rows:
        key = row.get("custodian")
        if not key:
            continue
        existing = dict(custodians.get(key) or {})
        for col in DISPATCH_COLUMNS:
            if col.name == "custodian":
                continue
            existing[col.name] = row.get(col.name, existing.get(col.name))
        custodians[key] = existing
    data["custodians"] = custodians
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── calendar_dates template (calendar.json) ─────────────────────────── #

CALENDAR_COLUMNS: list[ColumnSpec] = [
    ColumnSpec("date", required=True, help="ISO date YYYY-MM-DD"),
    ColumnSpec(
        "kind",
        required=True,
        help="holiday / working_weekend / no_trade_day",
    ),
]
CALENDAR_KEY = ("date", "kind")

_CAL_BUCKETS = {
    "holiday": "holidays",
    "working_weekend": "working_weekends",
    "no_trade_day": "no_trade_days",
}


def _load_calendar(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path) as f:
        data = json.load(f) or {}
    out: list[dict[str, Any]] = []
    for kind, bucket in _CAL_BUCKETS.items():
        for d in data.get(bucket) or []:
            out.append({"date": d, "kind": kind})
    return out


def _save_calendar(path: Path, rows: list[dict[str, Any]]) -> None:
    data: dict[str, list[str]] = {b: [] for b in _CAL_BUCKETS.values()}
    for row in rows:
        kind = row.get("kind")
        date = row.get("date")
        bucket = _CAL_BUCKETS.get(kind or "")
        if not bucket or not date:
            continue
        if date not in data[bucket]:
            data[bucket].append(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ── Registry ────────────────────────────────────────────────────────── #

TEMPLATES: dict[str, TemplateSpec] = {
    "pool_accounts": TemplateSpec(
        slug="pool_accounts",
        title="Pool accounts",
        description=(
            "Per-pool bank, demat, dealer, MAPIN and WS scheme mappings. "
            "This is the master list the reconciliation engines walk for "
            "every pool you operate."
        ),
        columns=POOL_COLUMNS,
        key=POOL_KEY,
        loader=_load_pools_hub,
        saver=_save_pools_hub,
        backing_file="pools_hub.json",
    ),
    "sources": TemplateSpec(
        slug="sources",
        title="Email sources",
        description=(
            "Custodian and bank email sources the ingester watches. One "
            "row per inbound feed — holdings files, bank statements, bank "
            "balances. Parser name must match a registered parser."
        ),
        columns=SOURCE_COLUMNS,
        key=SOURCE_KEY,
        loader=_load_sources,
        saver=_save_sources,
        backing_file="sources.json",
    ),
    "strategy_mappings": TemplateSpec(
        slug="strategy_mappings",
        title="Strategy mappings",
        description=(
            "Broker-code → WS-scheme-code map. One row per (source, "
            "broker_code) pair. Conditional FoF rules live outside the "
            "template and are preserved verbatim on round-trip."
        ),
        columns=MAPPING_COLUMNS,
        key=MAPPING_KEY,
        loader=_load_mappings,
        saver=_save_mappings,
        backing_file="mappings.json",
    ),
    "custodian_dispatch": TemplateSpec(
        slug="custodian_dispatch",
        title="Custodian dispatch",
        description=(
            "Per-custodian trade-dispatch email config. Recipients live "
            "in the email_to column as a semicolon-separated list. The "
            "common/report_for defaults live outside the template."
        ),
        columns=DISPATCH_COLUMNS,
        key=DISPATCH_KEY,
        loader=_load_dispatch,
        saver=_save_dispatch,
        backing_file="custodian_dispatch.json",
    ),
    "calendar_dates": TemplateSpec(
        slug="calendar_dates",
        title="Calendar overrides",
        description=(
            "Holidays, working weekends, and no-trade days. One row per "
            "(date, kind) pair. The scheduler consults this to decide "
            "whether a scheduled run should fire on a given calendar day."
        ),
        columns=CALENDAR_COLUMNS,
        key=CALENDAR_KEY,
        loader=_load_calendar,
        saver=_save_calendar,
        backing_file="calendar.json",
    ),
}


# ── Public helpers ──────────────────────────────────────────────────── #


def list_templates() -> list[dict[str, Any]]:
    return [
        {
            "slug": t.slug,
            "title": t.title,
            "description": t.description,
            "columns": [
                {"name": c.name, "required": c.required, "kind": c.kind, "help": c.help}
                for c in t.columns
            ],
        }
        for t in TEMPLATES.values()
    ]


def get_template(slug: str) -> TemplateSpec | None:
    return TEMPLATES.get(slug)


def export_csv(slug: str) -> str:
    """Serialise the current config for `slug` as a CSV string."""
    t = TEMPLATES.get(slug)
    if t is None:
        raise KeyError(f"Unknown template: {slug}")
    rows = t.loader(_CONFIG_DIR() / t.backing_file)
    buf = io.StringIO()
    headers = [c.name for c in t.columns]
    w = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    w.writeheader()
    for row in rows:
        out_row: dict[str, Any] = {}
        for col in t.columns:
            out_row[col.name] = col.to_csv_cell(row.get(col.name, ""))
        w.writerow(out_row)
    return buf.getvalue()


@dataclass
class DiffResult:
    added: list[dict[str, Any]] = field(default_factory=list)
    changed: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    unchanged_count: int = 0
    errors: list[str] = field(default_factory=list)


def parse_and_diff(slug: str, csv_text: str) -> tuple[DiffResult, list[dict[str, Any]]]:
    """Parse a CSV upload, validate each row, and diff against current state.

    Returns (DiffResult, merged_rows). `merged_rows` is the full list that
    would be written if the operator confirms — including unchanged rows,
    changed rows with their new values, and new rows. Removed rows are
    reported in the diff but NOT automatically dropped — the commit step
    asks the operator whether to honour removals.
    """
    t = TEMPLATES.get(slug)
    if t is None:
        raise KeyError(f"Unknown template: {slug}")

    current_rows = t.loader(_CONFIG_DIR() / t.backing_file)
    current_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in current_rows:
        k = tuple(row.get(c) for c in t.key)
        current_by_key[k] = row

    diff = DiffResult()
    incoming_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}

    reader = csv.DictReader(io.StringIO(csv_text))
    for line_no, raw_row in enumerate(reader, start=2):
        parsed: dict[str, Any] = {}
        row_errors: list[str] = []
        for col in t.columns:
            raw = raw_row.get(col.name, "")
            try:
                parsed[col.name] = col.coerce(raw or "")
            except Exception as e:  # noqa: BLE001
                row_errors.append(f"line {line_no}: {col.name}: {e}")
            if col.required and not raw.strip():
                row_errors.append(f"line {line_no}: {col.name} is required")
        if row_errors:
            diff.errors.extend(row_errors)
            continue
        k = tuple(parsed.get(c) for c in t.key)
        incoming_by_key[k] = parsed

    # Classify each incoming row
    for k, incoming in incoming_by_key.items():
        existing = current_by_key.get(k)
        if existing is None:
            diff.added.append(incoming)
            continue
        # Merge: start from the existing row so list-valued fields
        # (ws_scheme_names, ws_overrides, ...) are preserved, then
        # overlay only the columns owned by this template.
        merged = dict(existing)
        for col in t.columns:
            merged[col.name] = incoming.get(col.name, existing.get(col.name))
        if _shallow_equal(existing, merged, [c.name for c in t.columns]):
            diff.unchanged_count += 1
        else:
            diff.changed.append((existing, merged))

    # Rows in current but missing from upload
    for k, existing in current_by_key.items():
        if k not in incoming_by_key:
            diff.removed.append(existing)

    # Build the merged full list (for commit). Use incoming order.
    merged_rows: list[dict[str, Any]] = []
    for k, incoming in incoming_by_key.items():
        existing = current_by_key.get(k) or {}
        merged = dict(existing)
        for col in t.columns:
            merged[col.name] = incoming.get(col.name, existing.get(col.name))
        merged_rows.append(merged)

    return diff, merged_rows


def _shallow_equal(a: dict, b: dict, keys: list[str]) -> bool:
    # Treat missing keys, None, and "" as equal so that adding new
    # optional columns to a template doesn't mark every existing row
    # as "changed" on the first round-trip.
    def norm(v: Any) -> Any:
        if v is None or v == "":
            return None
        return v

    for k in keys:
        if norm(a.get(k)) != norm(b.get(k)):
            return False
    return True


def commit_csv(
    slug: str,
    merged_rows: list[dict[str, Any]],
    *,
    keep_removed: bool = True,
) -> None:
    """Write the merged rows back to the backing file.

    If keep_removed=True (default), rows that exist in the current
    config but were missing from the upload are retained. Set to False
    to actually delete them.
    """
    t = TEMPLATES.get(slug)
    if t is None:
        raise KeyError(f"Unknown template: {slug}")

    existing = t.loader(_CONFIG_DIR() / t.backing_file)
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in existing:
        by_key[tuple(row.get(c) for c in t.key)] = row

    incoming_keys = set()
    for row in merged_rows:
        incoming_keys.add(tuple(row.get(c) for c in t.key))

    final: list[dict[str, Any]] = []
    # Start with merged incoming (overwrites existing entries)
    for row in merged_rows:
        final.append(row)
    if keep_removed:
        for k, row in by_key.items():
            if k not in incoming_keys:
                final.append(row)

    t.saver(_CONFIG_DIR() / t.backing_file, final)
