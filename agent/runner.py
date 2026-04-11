"""Job runner — dispatches poll jobs to the reconciliation engines.

The poll loop hands us a PollJob; we figure out which engine to run,
invoke it with the agent's workdir, and build a RunPush pydantic model
that the poll loop then ships to the control plane (directly or via
the outbox).

Key invariants:
  1. The runner is the ONLY place in the agent that touches both
     the engine and the protocol layer. If you want to send something
     to the control plane, you add a whitelist key and make the runner
     populate it here. The poll loop does not serialise RunPush
     directly.
  2. Exceptions inside an engine are caught and turned into a
     RunPush with status=failed and the exception message as a log
     line. The job never crashes the poll loop.
  3. Every dispatch captures a collected log (list of dicts) which is
     passed through whitelist.filter_log_lines before upload.
"""
from __future__ import annotations

import logging
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AgentSettings, load_settings


def _load_config_json(name: str) -> dict:
    """Load a JSON config file from the agent repo's `config/` directory.

    Phase 3 replaces this with a per-tenant config store under the
    workdir. For now we read the shipped defaults alongside the engine
    code so the runner has something to point the workflows at.
    """
    import json as _json

    # config/ lives at the repo root next to core/ and parsers/
    candidate = Path(__file__).resolve().parent.parent / "config" / name
    if not candidate.exists():
        return {}
    try:
        with open(candidate) as f:
            return _json.load(f)  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001
        return {}
from .protocol import (
    LogLine,
    PollJob,
    ReconType,
    RunPush,
    RunStatus,
)
from .whitelist import (
    attachment_metadata,
    derive_counts_from_results,
    filter_log_lines,
)

logger = logging.getLogger(__name__)


# ── Internal helpers ───────────────────────────────────────────────── #


class _LogCollector:
    """Captures log messages from the engine for upload via RunPush.

    The bank and trade workflows accept a `log_fn(msg[, level])` callable.
    The holdings engine logs via the standard `logging` module. We give
    both of them this collector; for the stdlib logger path we attach
    it as a handler for the duration of the run.
    """

    def __init__(self) -> None:
        self._entries: list[dict[str, Any]] = []

    def __call__(self, msg: Any, level: str = "info") -> None:
        self._entries.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "level": level if level in ("debug", "info", "warning", "error") else "info",
                "msg": str(msg),
            }
        )

    def as_log_lines(self) -> list[LogLine]:
        return filter_log_lines(self._entries)


def _job_date(job: PollJob, default_today: bool = True) -> str:
    """Extract the YYYY-MM-DD recon date from a job payload."""
    date = job.payload.get("date")
    if isinstance(date, str) and len(date) == 10:
        return date
    if default_today:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raise ValueError(f"Job {job.id} missing 'date' in payload")


def _file_manager(workdir: str):
    """Construct a core.file_manager.FileManager rooted at the agent's workdir.

    Every recon engine expects a FileManager instance. FileManager's
    constructor takes the base directory; all other paths are derived
    from it.
    """
    from core.file_manager import FileManager

    base = Path(workdir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return FileManager(str(base))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _keystone_rename(
    engine_output_path: str | None,
    recon_type: ReconType,
    recon_date: str,
) -> str | None:
    """Rename an engine-written output file with a Keystone-prefixed name.

    Engines write files with the in-house convention
        Recon_20260410_154653.xlsx
        TradeRecon_20260410_154653.xlsx
        0096_20260410_154653.xls
    which collides with outputs from legacy runs sitting in the same
    folder. We rename them to
        Keystone_Holdings_2026-04-10_154653.xlsx
        Keystone_Trade_2026-04-10_154653.xlsx
        Keystone_Trade_0096_2026-04-10_154653.xls
    so operators can immediately tell which pipeline produced which file
    and can safely leave Keystone and legacy outputs coexisting.

    Returns the new path (or None if the source path was None / missing).
    """
    if not engine_output_path:
        return None
    src = Path(engine_output_path)
    if not src.exists():
        return None

    ext = src.suffix
    stem = src.stem
    # Extract the trailing HHMMSS timestamp from the original filename
    # (last underscore-separated token), fall back to "now" if absent.
    parts = stem.split("_")
    time_tag = parts[-1] if parts and parts[-1].isdigit() else datetime.now().strftime("%H%M%S")

    type_label = {"holdings": "Holdings", "bank": "Bank", "trade": "Trade"}[recon_type]

    # Disambiguate the two trade-recon artifacts — TradeRecon_* is the
    # main report, 0096_* is the WS upload file.
    if recon_type == "trade" and stem.startswith("0096"):
        new_stem = f"Keystone_Trade_0096_{recon_date}_{time_tag}"
    elif recon_type == "trade" and stem.lower().startswith("traderecon"):
        new_stem = f"Keystone_Trade_{recon_date}_{time_tag}"
    else:
        new_stem = f"Keystone_{type_label}_{recon_date}_{time_tag}"

    dst = src.with_name(new_stem + ext)
    if dst.exists():
        # Very unlikely, but guarantee uniqueness by appending a counter.
        counter = 1
        while dst.exists():
            dst = src.with_name(f"{new_stem}_{counter}{ext}")
            counter += 1
    src.rename(dst)
    return str(dst)


def _failed_push(
    job: PollJob,
    recon_type: ReconType,
    recon_date: str,
    log: _LogCollector,
    err: BaseException,
) -> RunPush:
    log(f"Runner error: {type(err).__name__}: {err}", level="error")
    logger.exception(f"Job {job.id} failed")
    return RunPush(
        job_id=job.id,
        type=recon_type,
        recon_date=recon_date,
        status="failed",
        counts={},
        attachments_meta={},
        reminder_count=0,
        log_lines=log.as_log_lines(),
    )


# ── Public dispatch ────────────────────────────────────────────────── #


def execute_job(job: PollJob) -> RunPush:
    """Top-level dispatch. Map a PollJob to a RunPush.

    Raises no exceptions — every failure path returns a `status=failed`
    RunPush so the caller's push-to-cloud loop is trivial.
    """
    if job.type == "holdings_recon":
        return _run_holdings(job)
    if job.type == "bank_recon":
        return _run_bank(job)
    if job.type == "trade_recon":
        return _run_trade(job)
    if job.type == "fetch_emails":
        return _run_fetch_emails(job)
    if job.type == "ws_download":
        return _run_ws_download(job)
    if job.type == "diagnostic_bundle":
        return _run_diagnostic_bundle(job)
    # Unknown type — record it as a failure and let the server see it
    log = _LogCollector()
    return _failed_push(
        job, "holdings", datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        log, ValueError(f"Unknown job type: {job.type}"),
    )


# ── Holdings ───────────────────────────────────────────────────────── #


def _run_holdings(job: PollJob) -> RunPush:
    log = _LogCollector()
    try:
        date_str = _job_date(job)
    except ValueError as e:
        return _failed_push(job, "holdings", _now_iso()[:10], log, e)

    log(f"Starting holdings recon for {date_str}")
    settings = load_settings()
    try:
        fm = _file_manager(settings.workdir)

        from core.holdings_recon_workflow import run_holdings_recon
        from core.pools_hub import PoolsHub

        hub = PoolsHub.load()
        mappings_cfg = {
            "strategy_mappings": hub.mappings_dict().get("strategy_mappings", [])
        }
        sources = _load_config_json("sources.json").get("sources", [])

        # Pool-level MAPIN/custodian codes to skip in custodian files
        # (pools with skip_pool_row_in_custodian=True, currently Axis only).
        pool_mapin_codes: set[str] = set()
        for pool in getattr(hub, "_pools", []) or []:
            if pool.get("skip_pool_row_in_custodian"):
                for fld in ("mapin", "custodian_code"):
                    v = str(pool.get(fld, "")).strip().upper()
                    if v:
                        pool_mapin_codes.add(v)

        result = run_holdings_recon(
            date_str=date_str,
            fm=fm,
            sources=sources,
            mappings_cfg=mappings_cfg,
            pool_mapin_codes=pool_mapin_codes,
            log_fn=log,
        )

        for w in result.warnings:
            log(w, level="warning")

        if not result.record_count:
            log("No custodian records — cannot run holdings recon", level="error")
            return RunPush(
                job_id=job.id,
                type="holdings",
                recon_date=date_str,
                status="failed",
                counts={},
                attachments_meta={},
                reminder_count=0,
                log_lines=log.as_log_lines(),
            )

        if result.output_path is None:
            log("Holdings engine returned no output — run failed", level="error")
            return RunPush(
                job_id=job.id,
                type="holdings",
                recon_date=date_str,
                status="failed",
                counts=derive_counts_from_results("holdings", result.results),
                attachments_meta={},
                reminder_count=0,
                log_lines=log.as_log_lines(),
            )

        # Retag the engine-written file with Keystone_Holdings_{date}_{time}
        renamed = _keystone_rename(result.output_path, "holdings", date_str)
        log(f"Holdings recon complete — report at {renamed}")

        counts = derive_counts_from_results("holdings", result.results)
        status: RunStatus = _holdings_status_from_counts(counts)
        return RunPush(
            job_id=job.id,
            type="holdings",
            recon_date=date_str,
            status=status,
            counts=counts,
            attachments_meta=attachment_metadata(renamed),
            reminder_count=0,
            log_lines=log.as_log_lines(),
        )
    except Exception as e:  # noqa: BLE001
        return _failed_push(job, "holdings", date_str, log, e)


def _holdings_status_from_counts(counts: dict[str, int | str]) -> RunStatus:
    def _n(k: str) -> int:
        v = counts.get(k, 0)
        return v if isinstance(v, int) else 0

    unexplained = _n("unexplained")
    custody_only = _n("custody_only")
    ws_only = _n("ws_only")
    if unexplained or custody_only or ws_only:
        return "breaks_found"
    return "all_clear"


# ── Bank ───────────────────────────────────────────────────────────── #


def _run_bank(job: PollJob) -> RunPush:
    log = _LogCollector()
    try:
        date_str = _job_date(job)
    except ValueError as e:
        return _failed_push(job, "bank", _now_iso()[:10], log, e)

    log(f"Starting bank recon for {date_str}")
    settings = load_settings()
    try:
        fm = _file_manager(settings.workdir)
        from core.bank_recon_workflow import run_bank_recon, BankReconError
        from core.pools_hub import PoolsHub

        # Bank recon wants sources.json + password for zip extraction
        sources = _load_config_json("sources.json").get("sources", [])
        password = ""
        for s in sources:
            if s.get("name") in ("hdfc", "hdfc_bank") and s.get("zip_password"):
                password = s["zip_password"]
                break

        bank_history: dict = {}  # Phase 2 will persist this under workdir
        try:
            summary, balance_summary, parse_log = run_bank_recon(
                date_str,
                fm,
                sources,
                password,
                bank_history,
                log,  # log_fn — our collector is callable
                bank_dates=[date_str],
            )
        except BankReconError as be:
            for line in getattr(be, "parse_log", []) or []:
                log(str(line))
            return _failed_push(job, "bank", date_str, log, be)

        for line in parse_log or []:
            log(str(line))

        summary_dict = summary.to_dict()
        log(
            f"Bank recon complete — {summary_dict.get('total_pools', 0)} pools, "
            f"{summary_dict.get('clean', 0)} clean, {summary_dict.get('breaks', 0)} breaks"
        )

        # Write the Keystone-tagged Excel report. The exporter is new
        # in Phase 2 — the legacy in-house app never had a working
        # bank writer. Failure to export must NOT fail the run; we
        # still push the summary to the control plane either way.
        bank_output_path: str | None = None
        try:
            from core.bank_recon_exporter import export_bank_recon

            out_dir = Path(str(fm.output_dir(date_str)))
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%H%M%S")
            raw_path = str(out_dir / f"BankRecon_{date_str.replace('-', '')}_{ts}.xlsx")
            balance_dict = balance_summary.to_dict() if balance_summary else None
            export_bank_recon(
                summary_dict,
                raw_path,
                balance_check=balance_dict,
                recon_date=date_str,
            )
            bank_output_path = _keystone_rename(raw_path, "bank", date_str)
            if bank_output_path:
                log(f"Bank recon report: {bank_output_path}")
        except Exception as exp_err:  # noqa: BLE001
            log(f"Bank export failed: {exp_err}", level="warning")
            bank_output_path = None

        counts = derive_counts_from_results("bank", summary_dict)
        breaks = counts.get("breaks", 0)
        status: RunStatus = "breaks_found" if isinstance(breaks, int) and breaks > 0 else "all_clear"

        return RunPush(
            job_id=job.id,
            type="bank",
            recon_date=date_str,
            status=status,
            counts=counts,
            attachments_meta=attachment_metadata(bank_output_path),
            reminder_count=0,
            log_lines=log.as_log_lines(),
        )
    except Exception as e:  # noqa: BLE001
        return _failed_push(job, "bank", date_str, log, e)


# ── Trade ──────────────────────────────────────────────────────────── #


def _run_trade(job: PollJob) -> RunPush:
    log = _LogCollector()
    try:
        date_str = _job_date(job)
    except ValueError as e:
        return _failed_push(job, "trade", _now_iso()[:10], log, e)

    log(f"Starting trade recon for {date_str}")
    settings = load_settings()
    try:
        fm = _file_manager(settings.workdir)
        from core.trade_recon_workflow import run_trade_recon
        from core.pools_hub import PoolsHub

        hub = PoolsHub.load()
        pool_map_dict = hub.pool_map_dict()
        pool_map_raw = _load_config_json("pool_map.json")
        broker_map = _load_config_json("broker_map.json")
        out_dir = str(fm.output_dir(date_str))
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        result = run_trade_recon(
            date_str,
            fm,
            broker_map=broker_map,
            pool_map_dict=pool_map_dict,
            pool_map_raw=pool_map_raw,
            out_dir=out_dir,
            log_fn=log,
        )

        # run_trade_recon returns a TradeReconResult whose .summary is a
        # TradeReconSummary object with a .to_dict() method. Coerce to a
        # plain dict so the whitelist/filter code below can index it.
        raw_summary = getattr(result, "summary", None)
        if raw_summary is None:
            summary_dict: dict = {}
        elif hasattr(raw_summary, "to_dict"):
            summary_dict = raw_summary.to_dict()
        elif isinstance(raw_summary, dict):
            summary_dict = raw_summary
        else:
            summary_dict = {}
        log(
            f"Trade recon complete — orders: {summary_dict.get('total_orders', 0)}, "
            f"c1_breaks: {summary_dict.get('c1_breaks', 0)}, "
            f"c2_breaks: {summary_dict.get('c2_breaks', 0)}, "
            f"c3_breaks: {summary_dict.get('c3_breaks', 0)}"
        )

        counts = derive_counts_from_results("trade", summary_dict)

        def _n(k: str) -> int:
            v = counts.get(k, 0)
            return v if isinstance(v, int) else 0

        status: RunStatus = (
            "breaks_found"
            if (_n("c1_breaks") + _n("c2_breaks") + _n("c3_breaks")) > 0
            else "all_clear"
        )

        # Rename both trade output files with the Keystone_Trade prefix.
        # recon_path is the TradeRecon_*.xlsx summary; xlsx_0096 is the WS
        # upload file. We ship the main recon file in attachments_meta —
        # the 0096 file lives alongside it on disk for the operator.
        recon_path = getattr(result, "recon_path", None)
        xlsx_0096 = getattr(result, "xlsx_0096", None)
        renamed_recon = _keystone_rename(recon_path, "trade", date_str)
        renamed_0096 = _keystone_rename(xlsx_0096, "trade", date_str)
        if renamed_recon:
            log(f"Trade recon report: {renamed_recon}")
        if renamed_0096:
            log(f"Trade 0096 upload file: {renamed_0096}")
        att = attachment_metadata(renamed_recon)

        return RunPush(
            job_id=job.id,
            type="trade",
            recon_date=date_str,
            status=status,
            counts=counts,
            attachments_meta=att,
            reminder_count=0,
            log_lines=log.as_log_lines(),
        )
    except Exception as e:  # noqa: BLE001
        return _failed_push(job, "trade", date_str, log, e)


# ── Non-recon jobs (lightweight) ───────────────────────────────────── #


def _run_fetch_emails(job: PollJob) -> RunPush:
    """Trigger an email fetch and record it as a holdings run summary.

    Phase 1 reports this as a no-op success — the fetch itself is a
    Phase 2 concern wrapped around core/email_ingestor.py. For now we
    just acknowledge the job so the control plane can see the agent
    received it.
    """
    log = _LogCollector()
    date_str = _job_date(job)
    log("Email fetch jobs are Phase 2 — acknowledging without action")
    return RunPush(
        job_id=job.id,
        type="holdings",
        recon_date=date_str,
        status="all_clear",
        counts={},
        attachments_meta={},
        reminder_count=0,
        log_lines=log.as_log_lines(),
    )


def _run_ws_download(job: PollJob) -> RunPush:
    log = _LogCollector()
    date_str = _job_date(job)
    log("WS download jobs are Phase 2 — acknowledging without action")
    return RunPush(
        job_id=job.id,
        type="holdings",
        recon_date=date_str,
        status="all_clear",
        counts={},
        attachments_meta={},
        reminder_count=0,
        log_lines=log.as_log_lines(),
    )


def _run_diagnostic_bundle(job: PollJob) -> RunPush:
    log = _LogCollector()
    date_str = _job_date(job)
    log("Diagnostic bundle jobs are Phase 5 — acknowledging without action")
    return RunPush(
        job_id=job.id,
        type="holdings",
        recon_date=date_str,
        status="all_clear",
        counts={},
        attachments_meta={},
        reminder_count=0,
        log_lines=log.as_log_lines(),
    )
