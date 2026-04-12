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
from .local_runs import get_store as _get_local_runs


def _load_config_json(name: str) -> dict:
    """Load a JSON config file from the bundled config directory.

    Phase 3 replaces this with a per-tenant config store under the
    workdir. For now we read the shipped defaults via agent.paths so
    the resolution works the same way in the dev tree and inside a
    PyInstaller-frozen bundle.
    """
    import json as _json

    from .paths import config_dir

    candidate = config_dir() / name
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
        if renamed:
            _get_local_runs().record(job.id, "holdings", date_str, renamed)

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
                _get_local_runs().record(job.id, "bank", date_str, bank_output_path)
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
            _get_local_runs().record(job.id, "trade", date_str, renamed_recon)
        if renamed_0096:
            log(f"Trade 0096 upload file: {renamed_0096}")
        att = attachment_metadata(renamed_recon)

        # Auto-dispatch: when trade recon is all_clear and a 0096 file
        # exists, upload it to WS and email custody interface files to
        # custodians. This is the "straight-through processing" path —
        # if any break exists, dispatch is skipped and the operator must
        # resolve breaks first.
        dispatch_counts: dict[str, int | str] = {}
        if status == "all_clear" and (renamed_0096 or xlsx_0096):
            dispatch_counts = _auto_dispatch_trades(
                file_0096=renamed_0096 or xlsx_0096 or "",
                date_str=date_str,
                workdir=settings.workdir,
                log=log,
            )

        return RunPush(
            job_id=job.id,
            type="trade",
            recon_date=date_str,
            status=status,
            counts={**counts, **dispatch_counts},
            attachments_meta=att,
            reminder_count=0,
            log_lines=log.as_log_lines(),
        )
    except Exception as e:  # noqa: BLE001
        return _failed_push(job, "trade", date_str, log, e)


def _auto_dispatch_trades(
    file_0096: str, date_str: str, workdir: str, log: _LogCollector
) -> dict[str, int | str]:
    """Upload 0096 to WS and email custody interface files to custodians.

    Called automatically after a trade recon that finishes all_clear.
    Credentials come from DPAPI (ws_portal_password) and env vars
    (FINCRM_USER / FINCRM_PASS) — same as _run_ws_download.
    """
    from .secrets import KEY_WS_PORTAL_PASSWORD, get_store
    from .setup import EK_WS_USERNAME

    settings = load_settings()
    extras = settings.extras or {}
    ws_user = extras.get(EK_WS_USERNAME, "").strip()
    ws_pass = (get_store().get(KEY_WS_PORTAL_PASSWORD) or "").strip()
    if not ws_user or not ws_pass:
        log("Dispatch skipped — WS credentials not configured", level="warning")
        return {"dispatch": "skipped_no_creds"}

    prev = {
        "FINCRM_USER": os.environ.get("FINCRM_USER"),
        "FINCRM_PASS": os.environ.get("FINCRM_PASS"),
    }
    os.environ["FINCRM_USER"] = ws_user
    os.environ["FINCRM_PASS"] = ws_pass

    try:
        from ws_uploader import dispatch_trades

        log(f"Auto-dispatch: uploading 0096 + sending custody emails for {date_str}")

        def progress(stage: str, detail: str) -> None:
            log(f"Dispatch {stage}: {detail}")

        result = dispatch_trades(
            file_0096=file_0096,
            date_str=date_str,
            progress_cb=progress,
        )

        if result.ok:
            n_cust = len(result.custodian_results)
            n_ok = sum(1 for c in result.custodian_results if c.get("email_ok"))
            log(f"Dispatch complete — upload ok, {n_ok}/{n_cust} custodian emails sent")
            return {
                "dispatch": "ok",
                "dispatch_custodians": n_cust,
                "dispatch_emails_ok": n_ok,
            }
        else:
            msg = getattr(result.upload_result, "detail", "unknown") if result.upload_result else "unknown"
            log(f"Dispatch failed — upload: {msg}", level="warning")
            return {"dispatch": f"failed: {msg}"}
    except Exception as e:  # noqa: BLE001
        log(f"Dispatch error: {e}", level="error")
        return {"dispatch": f"error: {e}"}
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── Non-recon jobs (lightweight) ───────────────────────────────────── #


def _build_trade_fetch_sources() -> list[dict]:
    """Build fetch source entries for dealer/NSDL/exchange/broker CN emails.

    The Flask app built these dynamically from broker_map.json each time
    it ran a fetch. We do the same so trade recon emails land under the
    right raw/{source}/ subfolder alongside holdings and bank feeds.
    """
    broker_map = _load_config_json("broker_map.json")
    fetch_sources: list[dict] = []

    for ts in broker_map.get("trade_sources", []):
        if not ts.get("active", True):
            continue
        fetch_sources.append({
            "name": ts.get("name", "trade"),
            "display_name": ts.get("display_name", ""),
            "sender_email": ts.get("sender_email", ""),
            "subject_keyword": ts.get("subject_keyword", ""),
            "file_prefix": ts.get("file_prefix", ""),
            "file_password": ts.get("file_password", ""),
            "attachment_type": "direct",
            "active": True,
            "is_bank": False,
            "file_date_offset": 0,
        })

    for b in broker_map.get("brokers", []):
        if not b.get("active", True):
            continue
        sender = b.get("sender_email", "").strip()
        domain = b.get("email_domain", "").strip()
        effective = sender if sender else ("@" + domain if domain else "")
        if not effective:
            continue
        fetch_sources.append({
            "name": f'broker_cn_{b.get("dealer_code", "").lower()}',
            "display_name": b["name"],
            "sender_email": effective,
            "subject_keyword": b.get("subject_keyword", "").strip(),
            "attachment_type": "direct",
            "active": True,
            "is_bank": False,
            "file_date_offset": 0,
        })

    return fetch_sources


def _run_fetch_emails(job: PollJob) -> RunPush:
    """Pull custodian/bank/trade emails via Microsoft Graph.

    Supports single-date and multi-day range:
      - Single: payload = {date: "2026-04-10"}
      - Range:  payload = {date_from: "2026-04-08", date_to: "2026-04-10"}

    Merges the static sources.json (custody/bank) with dynamic trade
    sources built from broker_map.json (dealer files, broker CNs,
    NSDL, exchange) — exactly as the Flask app did.
    """
    log = _LogCollector()
    payload = job.payload or {}

    # Support date range: if date_from + date_to are in payload, use range.
    # Otherwise fall back to single-date via the 'date' field.
    date_from = str(payload.get("date_from", "")).strip()
    date_to = str(payload.get("date_to", "")).strip()
    if date_from and date_to:
        date_str = date_to  # use the end date as the canonical recon_date
    else:
        try:
            date_str = _job_date(job)
        except ValueError as e:
            return _failed_push(job, "holdings", _now_iso()[:10], log, e)
        date_from = date_str
        date_to = date_str

    log(f"Starting email fetch for {date_from} → {date_to}")
    settings = load_settings()

    from .secrets import KEY_M365_CLIENT_SECRET, get_store
    from .setup import EK_M365_CLIENT_ID, EK_M365_MAILBOX, EK_M365_TENANT_ID

    extras = settings.extras or {}
    azure_cfg = {
        "tenant_id": extras.get(EK_M365_TENANT_ID, ""),
        "client_id": extras.get(EK_M365_CLIENT_ID, ""),
        "client_secret": get_store().get(KEY_M365_CLIENT_SECRET) or "",
        "mailbox": extras.get(EK_M365_MAILBOX, ""),
    }
    if not all(azure_cfg.values()):
        log(
            "M365 Graph credentials are not configured on this agent — "
            "complete the first-run wizard or open /setup and fill in "
            "Azure tenant/client/mailbox before scheduling fetch jobs.",
            level="error",
        )
        return _failed_push(job, "holdings", date_str, log, ValueError("m365_not_configured"))

    try:
        from core.email_ingestor import EmailIngestor

        fm = _file_manager(settings.workdir)

        # Merge custody/bank sources with trade sources
        sources = _load_config_json("sources.json").get("sources", [])
        trade_sources = _build_trade_fetch_sources()
        all_sources = sources + trade_sources
        log(f"Sources: {len(sources)} custody/bank + {len(trade_sources)} trade = {len(all_sources)} total")

        if not all_sources:
            log("No sources configured — nothing to fetch", level="warning")
            return RunPush(
                job_id=job.id,
                type="holdings",
                recon_date=date_str,
                status="all_clear",
                counts={"fetched": 0, "sources": 0},
                attachments_meta={},
                reminder_count=0,
                log_lines=log.as_log_lines(),
            )

        ingestor = EmailIngestor(azure_cfg)
        results = ingestor.fetch_for_range(
            date_from=date_from,
            date_to=date_to,
            sources=all_sources,
            file_manager=fm,
            log_callback=log,
        )

        ok_count = sum(1 for r in results if r.get("status") == "ok")
        err_count = sum(1 for r in results if r.get("status") == "error")
        skip_count = sum(1 for r in results if r.get("status") == "skipped")
        log(
            f"Fetch complete — ok={ok_count} skipped={skip_count} errors={err_count}"
        )
        status: RunStatus = "all_clear" if err_count == 0 else "breaks_found"
        return RunPush(
            job_id=job.id,
            type="holdings",
            recon_date=date_str,
            status=status,
            counts={
                "fetched": ok_count,
                "skipped": skip_count,
                "errors": err_count,
                "sources": len(all_sources),
            },
            attachments_meta={},
            reminder_count=0,
            log_lines=log.as_log_lines(),
        )
    except Exception as e:  # noqa: BLE001
        return _failed_push(job, "holdings", date_str, log, e)


def _run_ws_download(job: PollJob) -> RunPush:
    """Download WealthSpectrum master reports.

    Supports single-date and multi-day range:
      - Single: payload = {date: "2026-04-10"}
      - Range:  payload = {date_from: "2026-04-08", date_to: "2026-04-10"}

    For a range, downloads are run once per date stepping from date_from
    to date_to inclusive.
    """
    log = _LogCollector()
    payload = job.payload or {}

    date_from = str(payload.get("date_from", "")).strip()
    date_to = str(payload.get("date_to", "")).strip()
    if date_from and date_to:
        date_str = date_to
    else:
        try:
            date_str = _job_date(job)
        except ValueError as e:
            return _failed_push(job, "holdings", _now_iso()[:10], log, e)
        date_from = date_str
        date_to = date_str

    log(f"Starting WS download for {date_from} → {date_to}")
    settings = load_settings()

    from .secrets import KEY_WS_PORTAL_PASSWORD, get_store
    from .setup import EK_WS_USERNAME

    extras = settings.extras or {}
    ws_user = extras.get(EK_WS_USERNAME, "").strip()
    ws_pass = (get_store().get(KEY_WS_PORTAL_PASSWORD) or "").strip()
    if not ws_user or not ws_pass:
        log(
            "WealthSpectrum portal credentials are not configured — "
            "open /setup on the local UI and fill in the WS portal "
            "username + password.",
            level="error",
        )
        return _failed_push(
            job, "holdings", date_str, log, ValueError("ws_not_configured")
        )

    try:
        prev = {
            "FINCRM_USER": os.environ.get("FINCRM_USER"),
            "FINCRM_PASS": os.environ.get("FINCRM_PASS"),
        }
        os.environ["FINCRM_USER"] = ws_user
        os.environ["FINCRM_PASS"] = ws_pass

        from ws_downloader import run_all_downloads

        app_dir = Path(settings.workdir).resolve()
        app_dir.mkdir(parents=True, exist_ok=True)

        def progress(name: str, state: str, msg: str) -> None:
            lvl = "error" if state == "error" else "info"
            log(f"WS {name}: {state} — {msg}", level=lvl)

        reports_filter = None
        if isinstance(payload.get("reports"), list):
            reports_filter = [str(r) for r in payload["reports"]]

        # Build list of dates to download for
        dt_from = datetime.strptime(date_from, "%Y-%m-%d")
        dt_to = datetime.strptime(date_to, "%Y-%m-%d")
        dates: list[datetime] = []
        cursor = dt_from
        while cursor <= dt_to:
            dates.append(cursor)
            cursor += __import__("datetime").timedelta(days=1)

        total_success = 0
        total_total = 0
        total_errors = 0

        try:
            for date_obj in dates:
                ds = date_obj.strftime("%Y-%m-%d")
                log(f"WS download: {ds}")
                result = run_all_downloads(
                    date_obj=date_obj,
                    app_dir=app_dir,
                    progress_cb=progress,
                    reports_filter=reports_filter,
                )
                t = int(result.get("total", 0))
                s = int(result.get("success_count", 0))
                total_total += t
                total_success += s
                total_errors += t - s
                log(f"WS {ds}: ok={s}/{t}")
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        log(f"WS download complete — ok={total_success}/{total_total} errors={total_errors}")
        status: RunStatus = (
            "all_clear"
            if total_errors == 0 and total_total > 0
            else "breaks_found"
        )
        return RunPush(
            job_id=job.id,
            type="holdings",
            recon_date=date_str,
            status=status,
            counts={
                "downloaded": total_success,
                "total": total_total,
                "errors": total_errors,
                "days": len(dates),
            },
            attachments_meta={},
            reminder_count=0,
            log_lines=log.as_log_lines(),
        )
    except Exception as e:  # noqa: BLE001
        return _failed_push(job, "holdings", date_str, log, e)


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
