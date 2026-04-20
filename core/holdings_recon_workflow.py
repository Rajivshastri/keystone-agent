"""
Holdings reconciliation workflow.

Orchestrates: parse custodian files → load WS masters → run recon engine →
write Excel report → return counts + output path.

This is the workflow companion to `bank_recon_workflow.run_bank_recon` and
`trade_recon_workflow.run_trade_recon`. It captures the logic that used to
live inline in `app.py` api_load / api_recon so that both the Keystone
agent and the legacy in-house Flask app can drive the same pipeline.

The function is pure — no Flask state, no logging sinks. Callers pass in
their own `log_fn(msg, level='info')` callable.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, List, Optional

from parsers import get_parser
from parsers.base import HoldingRecord


LogFn = Callable[..., None]


# ── Filename date helpers ──────────────────────────────────────────────── #


def _date_variants(date_str: str) -> set[str]:
    """Return filename date variants for a given YYYY-MM-DD date string."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    d = dt.strftime("%d")
    m = dt.strftime("%m")
    y = dt.strftime("%Y")
    d_nz = str(int(d))
    m_nz = str(int(m))
    return {
        f"{d}{m}{y}",
        f"{y}{m}{d}",
        f"{d}_{m}_{y}",
        f"{d}-{m}-{y}",
        f"{d}{m}{y[2:]}",
        f"{d_nz}_{m_nz}_{y}",
        f"{d_nz}_{m}_{y}",
    }


def _filter_files_by_date(
    files: list,
    date_str: str,
    offset_days: int = 0,
    take_last_only: bool = False,
) -> list:
    """Filter files whose filename contains a date variant for (date + offset)."""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=offset_days)
        variants = _date_variants(target.strftime("%Y-%m-%d"))
        matched = [f for f in files if any(v in os.path.basename(f) for v in variants)]
        if not matched:
            return []
        if take_last_only and len(matched) > 1:
            matched = [sorted(matched)[-1]]
        return matched
    except Exception:
        return []


def _kotak_source_folder(broker_code: str) -> str:
    return f"kotak_{broker_code}"


# ── Kotak broker codes from pools hub ──────────────────────────────────── #


def _get_kotak_broker_codes(mappings: list[dict]) -> list[str]:
    return [m["broker_code"] for m in mappings if m.get("source") == "kotak"]


# ── File loading pipeline ─────────────────────────────────────────────── #


def load_custodian_records(
    date_str: str,
    fm,  # core.file_manager.FileManager
    sources: list[dict],
    mappings: list[dict],
    log_fn: LogFn,
) -> List[HoldingRecord]:
    """
    Walk the raw/{source}/ folders for every active, non-bank source and
    parse the files with their registered parser. Returns the concatenated
    list of HoldingRecord objects.

    Kotak is special-cased: real data lives under raw/kotak_{broker_code}/
    per trading strategy; this helper loops over the configured broker
    codes from pools_hub and aggregates.
    """
    all_records: List[HoldingRecord] = []

    for source in sources:
        if not source.get("active", True):
            continue
        if source.get("is_bank", False):
            continue

        sname = source["name"]
        parser_name = source.get("parser", sname)
        file_password = source.get("file_password", "")

        if sname == "kotak":
            kotak_codes = _get_kotak_broker_codes(mappings)
            if not kotak_codes:
                log_fn(f"  {sname}: no broker codes configured in mappings", "warning")
                continue
            for broker_code in kotak_codes:
                folder = _kotak_source_folder(broker_code)
                files = fm.list_source_files(date_str, folder)
                xlsx_files = [f for f in files if f.lower().endswith(".xlsx")]

                # Fallback: scan the flat raw/kotak/ folder for xlsx files
                # matching this broker code by filename heuristic.
                if not xlsx_files:
                    flat_files = fm.list_source_files(date_str, "kotak")
                    file_id_map = {
                        "MYSTIC_WEVA": ["G0264", "MYSTICWEVA", "MYSTIC_WEVA", "WEVA"],
                        "MYSTIC_WEMO": ["G0265", "MYSTICWEMO", "MYSTIC_WEMO", "WEMO"],
                    }
                    hints = file_id_map.get(broker_code.upper(), [broker_code])
                    for f in flat_files:
                        fn = os.path.basename(f).upper().replace(" ", "").replace("-", "").replace("_", "")
                        if f.lower().endswith(".xlsx") and any(
                            h.replace("_", "").replace(" ", "") in fn for h in hints
                        ):
                            xlsx_files.append(f)

                if not xlsx_files:
                    log_fn(f"  {sname}/{broker_code}: no .xlsx file found", "warning")
                    continue
                for f in xlsx_files:
                    _parse_one(f, date_str, sname, parser_name, file_password, all_records, log_fn)
            continue

        # Generic (non-kotak) source
        files = fm.list_source_files(date_str, sname)
        if not files:
            log_fn(f"  {sname}: no files found in raw/{sname}/", "warning")
            continue
        # Custodian holding files are xlsx/xls — exclude zips and raw CSVs.
        take_last_only = source.get("take_last_only", False)
        date_filter = source.get("date_filter", False)
        offset_days = source.get("offset_days", 0)
        data_files = [
            f
            for f in files
            if not f.lower().endswith(".zip") and not f.lower().endswith(".csv")
        ]
        if not data_files:
            log_fn(f"  {sname}: no data files found (only zips/csvs)", "info")
            continue

        if date_filter:
            filtered = _filter_files_by_date(data_files, date_str, offset_days=offset_days)
            if filtered:
                orig_count = len(data_files)
                data_files = filtered
                if len(filtered) < orig_count:
                    log_fn(f"  {sname}: date-filtered to {len(filtered)}/{orig_count} file(s)")
            else:
                log_fn(
                    f"  {sname}: date-filter found no matching files — skipping "
                    f"(expected date variants for {date_str} + {offset_days}d)",
                    "warning",
                )
                continue

        if take_last_only and len(data_files) > 1:
            data_files = [sorted(data_files)[-1]]
            log_fn(f"  {sname}: multiple files found, using last: {os.path.basename(data_files[0])}")

        for f in data_files:
            _parse_one(f, date_str, sname, parser_name, file_password, all_records, log_fn)

    return all_records


def _parse_one(
    file_path: str,
    date_str: str,
    source_name: str,
    parser_name: str,
    file_password: str,
    all_records: list,
    log_fn: LogFn,
) -> None:
    """Parse one file and append its records to `all_records`. Silent on
    bank parsers (which return None from the registry)."""
    fname = os.path.basename(file_path)
    try:
        parser = get_parser(parser_name)
        if parser is None:
            return
        log_fn(f"  Parsing [{source_name}]: {fname}")
        result = parser.parse_file(file_path, date_str, file_password)
        if result.error:
            log_fn(f"    ERROR: {result.error}", "error")
            return
        log_fn(
            f"    OK — {result.record_count} records "
            f"| broker codes: {result.broker_codes_found}"
        )
        all_records.extend(result.records)
    except Exception as e:  # noqa: BLE001
        log_fn(f"    EXCEPTION parsing {fname}: {e}", "error")


# ── Top-level orchestration ───────────────────────────────────────────── #


class HoldingsReconResult:
    """Tiny bag returned by run_holdings_recon."""

    def __init__(
        self,
        output_path: Optional[str],
        results: dict[str, Any],
        warnings: list[str],
        record_count: int,
    ) -> None:
        self.output_path = output_path
        self.results = results
        self.warnings = warnings
        self.record_count = record_count


def run_holdings_recon(
    date_str: str,
    fm,  # core.file_manager.FileManager
    sources: list[dict],
    mappings_cfg: dict,  # {"strategy_mappings": [...]}
    pool_mapin_codes: set[str] | None,
    log_fn: LogFn,
) -> HoldingsReconResult:
    """End-to-end holdings reconciliation.

    1. Walk custodian raw folders and parse every file with its registered parser
    2. Load WS Holdings / WS TradeTrans / PoolMaster / Kotak custody
    3. Run the ReconEngine
    4. Return output path, results dict (category → rows), warnings, record count.

    Raises nothing — any failure surfaces as an empty result with warnings.
    """
    from core.recon_engine import ReconEngine

    mappings = mappings_cfg.get("strategy_mappings", [])
    records = load_custodian_records(date_str, fm, sources, mappings, log_fn)
    log_fn(f"Total custodian records parsed: {len(records)}")

    if not records:
        return HoldingsReconResult(
            output_path=None,
            results={},
            warnings=["No custodian records parsed — cannot run holdings recon"],
            record_count=0,
        )

    engine = ReconEngine(mappings_cfg)
    ws_holdings_path = fm.get_ws_holdings(date_str)
    ws_transactions_path = fm.get_ws_transactions(date_str)
    pool_master_path = fm.get_strategy_master(date_str)
    kotak_custody_path = fm.get_kotak_custody(date_str)

    if not ws_holdings_path:
        return HoldingsReconResult(
            output_path=None,
            results={},
            warnings=["Z13_Holding / Holdings file not found in masters/"],
            record_count=len(records),
        )

    out_dir = str(fm.output_dir(date_str))
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    log_fn(f"WS Holdings: {os.path.basename(ws_holdings_path)}")
    if ws_transactions_path:
        log_fn(f"WS TradeTrans: {os.path.basename(ws_transactions_path)}")
    if pool_master_path:
        log_fn(f"Pool Master: {os.path.basename(pool_master_path)}")
    if kotak_custody_path:
        log_fn(f"Kotak custody: {os.path.basename(kotak_custody_path)}")

    # Previous-business-day custody snapshot — lets the recon engine
    # annotate break/WS-Only rows with "Likely pending sell — custody
    # dropped since <prev_date>" when yesterday had the position and
    # no WS trade explains today's gap. Non-fatal: missing snapshot
    # just disables the hint.
    prev_snapshot = {}
    prev_date = None
    try:
        from core.prev_custody import load_custody_snapshot, prev_business_day
        from agent.paths import config_dir as _agent_config_dir
        prev_date = prev_business_day(date_str)
        prev_snapshot = load_custody_snapshot(
            prev_date,
            workdir=fm.base_dir,
            sources_path=_agent_config_dir() / 'sources.json',
            log_fn=log_fn,
        )
    except Exception as _e:
        log_fn(f"Prev custody snapshot failed ({_e}) — annotation disabled")

    # Build ETF ISIN set from Z8_SecurityDetail (NSEMAPPING col). Engine uses
    # this to distinguish exchange-traded ETFs from AMC MF units — both carry
    # INF ISINs but only ETFs should bypass the MF timing-lag branch.
    etf_isins: set = set()
    try:
        _sec_master_p = fm.get_security_master(date_str)
        if _sec_master_p:
            from core.exporter import load_etf_isins as _load_etf
            etf_isins = _load_etf(_sec_master_p)
            log_fn(f"Security master: {len(etf_isins)} ETF ISIN(s) flagged via NSEMAPPING")
    except Exception as _e:
        log_fn(f"ETF ISIN load failed ({_e}) — falling back to name heuristic")

    out_path, warnings, results = engine.run(
        records=records,
        ws_holdings_path=ws_holdings_path,
        ws_transactions_path=ws_transactions_path,
        pool_master_path=pool_master_path,
        output_dir=out_dir,
        date_str=date_str,
        kotak_custody_path=kotak_custody_path,
        pool_mapin_codes=pool_mapin_codes,
        prev_custody=prev_snapshot,
        prev_date_str=prev_date,
        etf_isins=etf_isins,
    )

    return HoldingsReconResult(
        output_path=out_path,
        results=results or {},
        warnings=list(warnings or []),
        record_count=len(records),
    )
