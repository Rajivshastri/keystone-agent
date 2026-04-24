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


def _default_recon_date() -> str:
    """Return the business date for reconciliation: T-1 before 15:30 IST, T after.

    Markets close at 15:30 IST; custodian files land after close.
    Before that cutoff the most recent complete data set is yesterday's.
    """
    from datetime import timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    cutoff = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    if now_ist < cutoff:
        return (now_ist - timedelta(days=1)).strftime("%Y-%m-%d")
    return now_ist.strftime("%Y-%m-%d")


def _job_date(job: PollJob, default_today: bool = True) -> str:
    """Extract the YYYY-MM-DD recon date from a job payload."""
    date = job.payload.get("date")
    if isinstance(date, str) and len(date) == 10:
        return date
    if default_today:
        return _default_recon_date()
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


def _recon_recipients(settings: AgentSettings) -> list[str]:
    """Return the configured recon email recipients, or an empty list.

    Sourced from ``settings.extras["recon_recipients"]``. A list of email
    addresses as strings. Empty list = no auto-email (handled gracefully
    upstream — the recon still completes, we just log a warning).
    """
    extras = settings.extras or {}
    raw = extras.get("recon_recipients") or []
    if isinstance(raw, str):
        # Tolerant: allow a single address or a comma-separated list
        return [a.strip() for a in raw.split(",") if a.strip()]
    if isinstance(raw, list):
        return [str(a).strip() for a in raw if str(a).strip()]
    return []


def _azure_config_for_ingestor(settings: AgentSettings) -> dict | None:
    """Build the azure_cfg dict the EmailIngestor expects.

    Returns None if any required credential is missing — callers log a
    warning and skip email sending. The secret is pulled from the DPAPI
    secret store; everything else lives in settings.extras.
    """
    from .secrets import KEY_M365_CLIENT_SECRET, get_store
    from .setup import EK_M365_CLIENT_ID, EK_M365_MAILBOX, EK_M365_TENANT_ID

    extras = settings.extras or {}
    cfg = {
        "tenant_id":     extras.get(EK_M365_TENANT_ID, ""),
        "client_id":     extras.get(EK_M365_CLIENT_ID, ""),
        "client_secret": get_store().get(KEY_M365_CLIENT_SECRET) or "",
        "mailbox":       extras.get(EK_M365_MAILBOX, ""),
    }
    if not all(cfg.values()):
        return None
    return cfg


def _send_recon_email(
    recon_type: str,
    date_str: str,
    summary: Any,
    results_or_dict: Any,
    attachment_path: str | None,
    log: _LogCollector,
    settings: AgentSettings,
) -> None:
    """Fire the post-recon summary email and queue a reminder if needed.

    Mirrors Flask's auto-email flow in app.py for holdings (line 1868,
    2142), bank (line 2327), and trade (line 3349) reconciliations.
    Best-effort: failures log a warning but never fail the run — the
    recon results still ship to the control plane via RunPush.

    recon_type:      "holdings" | "bank" | "trade"
    summary:         engine-native summary object (for bank and trade)
                     or None (holdings uses the results dict directly)
    results_or_dict: for holdings, the results dict {category: [rows]}
                     for bank/trade, the summary.to_dict() dict
    attachment_path: path to the Excel report to attach (optional)
    """
    recipients = _recon_recipients(settings)
    if not recipients:
        log(
            f"Recon auto-email skipped — extras.recon_recipients is empty. "
            f"Add recipient addresses to agent-config.json extras to enable.",
            level="warning",
        )
        return

    azure_cfg = _azure_config_for_ingestor(settings)
    if azure_cfg is None:
        log(
            "Recon auto-email skipped — M365 credentials not configured",
            level="warning",
        )
        return

    try:
        from core.email_ingestor import EmailIngestor

        ingestor = EmailIngestor(azure_cfg)
        log(f"Sending {recon_type} recon email to: {', '.join(recipients)}")

        if recon_type == "holdings":
            # send_recon_summary expects the results dict
            email_result = ingestor.send_recon_summary(
                results_or_dict,
                date_str,
                recipients,
                attachment_path=attachment_path,
            )
        elif recon_type == "bank":
            email_result = ingestor.send_bank_recon_summary(
                results_or_dict,
                date_str,
                recipients,
                attachment_path=attachment_path,
            )
        elif recon_type == "trade":
            email_result = ingestor.send_trade_recon_summary(
                results_or_dict,
                date_str,
                recipients,
                attachment_path=attachment_path,
            )
        else:
            log(f"Unknown recon_type {recon_type!r} — not sending email", level="warning")
            return

        if email_result.get("ok"):
            log(f"Recon email sent: {email_result.get('message', '')}")

            # Queue a reminder if there are breaks requiring a final
            # explained email. Matches Flask app.py:2334-2341. Trade
            # recon does NOT queue reminders — trade breaks must be
            # reconciled (not explained), so there's no "final email
            # with explanation" workflow to remind about.
            if recon_type in ("holdings", "bank"):
                breaks = _count_breaks_for_reminder(recon_type, summary, results_or_dict)
                if breaks > 0:
                    try:
                        from core.recon_reminders import ReconReminderStore
                        reminder_path = Path(settings.workdir) / "data" / "recon_reminders.json"
                        store = ReconReminderStore(str(reminder_path))
                        store.record_initial_sent(
                            recon_type,
                            date_str,
                            recipients,
                            attachment_path=attachment_path,
                        )
                        log(f"Reminder queued for {recon_type}/{date_str} ({breaks} break(s) pending)")
                    except Exception as rem_err:  # noqa: BLE001
                        log(f"Reminder queue failed: {rem_err}", level="warning")
        else:
            log(
                f"Recon email failed: {email_result.get('message', 'unknown error')}",
                level="error",
            )
    except Exception as e:  # noqa: BLE001
        log(f"Recon email failed: {e}", level="warning")


def _count_breaks_for_reminder(
    recon_type: str, summary: Any, results_or_dict: Any
) -> int:
    """Count breaks that would trigger a reminder queue entry.

    Holdings: len(results['unexplained']) + len(results['custody_only'])
        + len(results['ws_only']) — same rule the engine uses.
    Bank: summary.breaks (same as Flask).
    """
    try:
        if recon_type == "holdings":
            if isinstance(results_or_dict, dict):
                return (
                    len(results_or_dict.get("unexplained", []) or [])
                    + len(results_or_dict.get("custody_only", []) or [])
                    + len(results_or_dict.get("ws_only", []) or [])
                )
        if recon_type == "bank":
            # Prefer the engine summary's breaks attribute; fall back
            # to the dict in case the caller didn't pass the live object.
            if hasattr(summary, "breaks"):
                return int(summary.breaks or 0)
            if isinstance(results_or_dict, dict):
                return int(results_or_dict.get("breaks", 0) or 0)
    except Exception:
        return 0
    return 0


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


# ── Reverse-channel command dispatch ──────────────────────────────── #


class CommandResult:
    """Return value from an agent command handler.

    Success: CommandResult(ok=True, result={...})   (result optional)
    Failure: CommandResult(ok=False, error="...")
    """

    __slots__ = ("ok", "result", "error")

    def __init__(
        self,
        ok: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.ok = ok
        self.result = result
        self.error = error

    @classmethod
    def success(cls, result: dict[str, Any] | None = None) -> "CommandResult":
        return cls(ok=True, result=result)

    @classmethod
    def failure(cls, error: str) -> "CommandResult":
        return cls(ok=False, error=error)


def execute_command(cmd_kind: str, payload: dict[str, Any]) -> CommandResult:
    """Top-level dispatch for reverse-channel commands.

    Mirrors execute_job but for commands. Unknown kinds return a
    failure result so the control plane sees them in `failed` state
    with a clear error — the agent does NOT silently drop anything.

    New command kinds: add a handler function below and a case here.
    Handler contract: takes a payload dict, returns a CommandResult.
    Raise nothing — catch exceptions inside the handler and wrap them
    in CommandResult.failure(...).
    """
    try:
        if cmd_kind == "ping":
            return _cmd_ping(payload)
        # P3a: firing due reminders from the local queue
        if cmd_kind == "reminder_check":
            return _cmd_reminder_check(payload)
        # P3a: operator-initiated bulk clear of the local reminder queue
        if cmd_kind == "reminder_clear_all":
            return _cmd_reminder_clear_all(payload)
        # Power-user per-reminder cancel (by type+date)
        if cmd_kind == "reminder_clear_one":
            return _cmd_reminder_clear_one(payload)
        # P3b: operator-entered bank annotations -> final email
        if cmd_kind == "bank_finalize":
            return _cmd_bank_finalize(payload)
        # P3c: post-explanation follow-up email
        if cmd_kind == "send_final_email":
            return _cmd_send_final_email(payload)
        # P3b/c: dashboard explain page pulls break detail from the
        # agent's local results sidecar over the reverse channel.
        if cmd_kind == "push_break_detail":
            return _cmd_push_break_detail(payload)
        # P7: standing rules for agent autonomy
        if cmd_kind == "set_standing_rule":
            return _cmd_set_standing_rule(payload)
        # Masters: on-demand WS fetch + merge with local extras
        if cmd_kind == "master_client_list":
            return _cmd_master_client_list(payload)
        if cmd_kind == "master_pool_list":
            return _cmd_master_pool_list(payload)
        if cmd_kind == "master_broker_list":
            return _cmd_master_broker_list(payload)
        if cmd_kind == "master_custody_list":
            return _cmd_master_custody_list(payload)
        if cmd_kind == "master_pool_upsert":
            return _cmd_master_pool_upsert(payload)
        if cmd_kind == "master_broker_upsert":
            return _cmd_master_broker_upsert(payload)
        if cmd_kind == "master_custody_upsert":
            return _cmd_master_custody_upsert(payload)
        return CommandResult.failure(f"unknown command kind: {cmd_kind!r}")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"Command handler {cmd_kind!r} crashed")
        return CommandResult.failure(f"{type(e).__name__}: {e}")


# ---- Handlers ----


def _cmd_ping(payload: dict[str, Any]) -> CommandResult:
    """Smoke-test command. Returns immediately with a success result.

    Used by the control plane's /api/v1/admin/ping-agent (future) and
    by the test harness to verify the reverse channel is wired up
    end-to-end without needing any real side effects.
    """
    return CommandResult.success({
        "pong": True,
        "agent_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "echoed_payload": payload,
    })


# The following handlers are stubs filled in by later priorities.
# Each returns a failure so the control plane knows they were received
# but not yet executable. Remove the stub once the real handler lands.


def _cmd_reminder_check(payload: dict[str, Any]) -> CommandResult:
    """Fire any due reminder emails from the local reminder queue.

    Loads the reminder store from ``{workdir}/data/recon_reminders.json``,
    reads due entries, sends a reminder email for each, and marks them
    as sent so they won't re-fire until the next REMINDER_INTERVAL window.

    Payload is ignored — the control plane just signals "now is the
    time to check". The agent's clock and reminder queue are the
    source of truth for what's actually due.

    Returns a summary: ``{fired: N, skipped: M, errors: K,
    pruned_stale: S}``. No exception propagates out; crashes inside
    a single reminder are captured and counted as errors.
    """
    settings = load_settings()

    # Build the M365 config. Same helper the recon handlers use.
    azure_cfg = _azure_config_for_ingestor(settings)
    if azure_cfg is None:
        return CommandResult.failure(
            "M365 credentials not configured — cannot fire reminders",
        )

    reminder_path = Path(settings.workdir) / "data" / "recon_reminders.json"
    from core.recon_reminders import ReconReminderStore

    store = ReconReminderStore(reminder_path)
    # Housekeep: drop entries older than STALE_AFTER so the file doesn't
    # grow unbounded when operators stop caring about old dates.
    pruned = store.prune_stale()

    due = store.get_due()
    if not due:
        return CommandResult.success({
            "fired": 0,
            "skipped": 0,
            "errors": 0,
            "pruned_stale": pruned,
            "message": "no reminders due",
        })

    from core.email_ingestor import EmailIngestor

    ingestor = EmailIngestor(azure_cfg)

    fired = 0
    errors = 0
    error_details: list[str] = []
    for entry in due:
        rtype = entry.get("type", "")
        date_str = entry.get("date", "")
        recipients = entry.get("recipients", []) or []
        initial_sent_at = entry.get("initial_sent_at", "")
        reminder_count = int(entry.get("reminder_count", 0) or 0)
        attachment_path = entry.get("attachment_path") or None

        if not rtype or not date_str or not recipients:
            errors += 1
            error_details.append(f"{rtype}/{date_str}: incomplete entry")
            continue

        try:
            result = ingestor.send_reminder_email(
                recon_type=rtype,
                date_str=date_str,
                recipients=recipients,
                initial_sent_at=initial_sent_at,
                reminder_count=reminder_count,
                attachment_path=attachment_path,
            )
            if result.get("ok"):
                store.mark_reminder_sent(rtype, date_str)
                fired += 1
            else:
                errors += 1
                error_details.append(
                    f"{rtype}/{date_str}: {result.get('message', 'unknown error')}"
                )
        except Exception as rem_err:  # noqa: BLE001
            errors += 1
            error_details.append(f"{rtype}/{date_str}: {type(rem_err).__name__}: {rem_err}")

    summary: dict[str, Any] = {
        "fired": fired,
        "skipped": 0,
        "errors": errors,
        "pruned_stale": pruned,
    }
    if error_details:
        summary["error_details"] = error_details[:10]

    # Any fired reminder counts as a success even if others failed —
    # the control plane can see the error detail in the result payload.
    return CommandResult.success(summary)


def _cmd_reminder_clear_all(payload: dict[str, Any]) -> CommandResult:
    """Operator-initiated bulk wipe of the local reminder queue.

    Used when the CP operator clicks "Clear all reminders" on the agent
    detail page. Removes every pending entry from recon_reminders.json —
    no emails are sent, and the next reminder_check tick sees an empty
    queue. Idempotent: if the file doesn't exist yet this still reports
    success with cleared=0.

    Payload is ignored. Returns ``{cleared: N}`` where N is the number
    of entries removed.
    """
    settings = load_settings()
    reminder_path = Path(settings.workdir) / "data" / "recon_reminders.json"
    from core.recon_reminders import ReconReminderStore

    store = ReconReminderStore(reminder_path)
    try:
        cleared = store.clear_all()
    except Exception as rem_err:  # noqa: BLE001
        return CommandResult.failure(
            f"clear_all failed: {type(rem_err).__name__}: {rem_err}",
        )
    return CommandResult.success({"cleared": cleared})


def _cmd_reminder_clear_one(payload: dict[str, Any]) -> CommandResult:
    """Operator-initiated cancel of a single pending reminder.

    Payload: ``{recon_type: str, date: str}``. Removes that one entry
    from the local queue; no emails are sent. Idempotent — calling with
    a key that's already gone returns success with found=False.
    """
    recon_type = str(payload.get("recon_type", "")).strip()
    date_str = str(payload.get("date", "")).strip()
    if not recon_type or not date_str:
        return CommandResult.failure("reminder_clear_one needs recon_type and date")
    settings = load_settings()
    reminder_path = Path(settings.workdir) / "data" / "recon_reminders.json"
    from core.recon_reminders import ReconReminderStore

    store = ReconReminderStore(reminder_path)
    try:
        found = store.clear_one(recon_type, date_str)
    except Exception as rem_err:  # noqa: BLE001
        return CommandResult.failure(
            f"clear_one failed: {type(rem_err).__name__}: {rem_err}",
        )
    return CommandResult.success({"found": found, "recon_type": recon_type, "date": date_str})


def _cmd_push_break_detail(payload: dict[str, Any]) -> CommandResult:
    """Read the break-detail sidecar for a run and POST it to the CP.

    Payload (from lib/break-detail.ts requestBreakDetail):
        run_id:     control-plane run id (used in the POST URL)
        job_id:     agent-local job id (used to look up the output
                    path in local_runs)
        recon_type: "bank" | "holdings" | "trade"
        recon_date: "YYYY-MM-DD" (informational)

    On success, POSTs the breaks array to /api/v1/agent/break-detail/
    {run_id} and returns the row count in the command result. On
    failure (missing local_runs row, missing sidecar, read error) we
    POST an ``{error: ...}`` body so the cache flips to `failed` and
    the dashboard explain page stops spinning.
    """
    run_id = str(payload.get("run_id") or "").strip()
    job_id = str(payload.get("job_id") or "").strip()
    if not run_id or not job_id:
        return CommandResult.failure("missing run_id or job_id in payload")

    from .transport import ControlPlaneClient, TransportError
    from core.break_sidecar import read_sidecar

    settings = load_settings()
    client = ControlPlaneClient(settings)
    try:
        local = _get_local_runs().get(job_id)
        if local is None:
            try:
                client.push_break_detail(
                    run_id, error=f"no local_runs row for job {job_id}"
                )
            except TransportError:
                pass
            return CommandResult.failure(
                f"no local_runs row for job {job_id}"
            )

        sidecar = read_sidecar(local.output_path, job_id)
        if sidecar is None:
            try:
                client.push_break_detail(
                    run_id, error="break sidecar missing on disk"
                )
            except TransportError:
                pass
            return CommandResult.failure("break sidecar missing on disk")

        breaks = sidecar.get("breaks") or []
        if not isinstance(breaks, list):
            breaks = []
        try:
            client.push_break_detail(run_id, breaks=breaks)
        except TransportError as te:
            return CommandResult.failure(f"POST failed: {te}")
        return CommandResult.success({
            "run_id": run_id,
            "break_count": len(breaks),
        })
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _cmd_bank_finalize(payload: dict[str, Any]) -> CommandResult:
    # P3b — the legacy Flask "bank finalize" flow is being replaced by
    # the new break-explanation workflow (see KEYSTONE_HANDOVER.md).
    # The submit handler on the control plane explain page validates
    # the ₹100 rule server-side and dispatches send_final_email
    # directly, so this command kind is unused in the new flow.
    # Left here for completeness; future-proof for if we add a direct
    # agent-side trigger.
    return CommandResult.failure("bank_finalize superseded by explain workflow")


def _cmd_send_final_email(payload: dict[str, Any]) -> CommandResult:
    """Send the post-explanation follow-up email for a run.

    Dispatched by the control plane's ``submitPerBreakExplanations``
    after an operator submits per-break explanations (or accept-all)
    on the explain page. The payload carries everything needed to
    compose the email without any further server round-trips:

        run_id:           control plane run id (informational)
        job_id:           agent-local job id (to find the Excel path)
        recon_type:       "holdings" | "bank" | "trade"
        recon_date:       YYYY-MM-DD
        explanations:     [{break_id, explanation, explained_amount}]
        accepted_all:     bool
        explainer_email:  operator who submitted

    Reads the recipient list from ``settings.extras.recon_recipients``
    (same convention as every other recon email), attaches the
    existing Excel report from disk, and fires via
    EmailIngestor.send_explanation_followup.

    On success, also marks the reminder queue entry as final so
    hourly reminders stop for this (recon_type, date) pair.
    """
    job_id = str(payload.get("job_id") or "").strip()
    recon_type = str(payload.get("recon_type") or "").strip()
    date_str = str(payload.get("recon_date") or "").strip()
    explanations = payload.get("explanations") or []
    accepted_all = bool(payload.get("accepted_all") or False)
    explainer_email = str(payload.get("explainer_email") or "")

    if not job_id or not recon_type or not date_str:
        return CommandResult.failure("missing job_id / recon_type / recon_date in payload")
    if recon_type == "trade":
        return CommandResult.failure(
            "trade runs do not use the explain workflow — final email skipped",
        )

    settings = load_settings()
    recipients = _recon_recipients(settings)
    if not recipients:
        return CommandResult.failure(
            "extras.recon_recipients is empty — no one to send to",
        )
    azure_cfg = _azure_config_for_ingestor(settings)
    if azure_cfg is None:
        return CommandResult.failure("M365 credentials not configured")

    # Find the Excel attachment via local_runs.
    local = _get_local_runs().get(job_id)
    attachment_path = local.output_path if local else None

    from core.email_ingestor import EmailIngestor

    ingestor = EmailIngestor(azure_cfg)
    result = ingestor.send_explanation_followup(
        recon_type=recon_type,
        date_str=date_str,
        recipients=recipients,
        explanations=explanations if isinstance(explanations, list) else [],
        explainer_email=explainer_email,
        accepted_all=accepted_all,
        attachment_path=attachment_path,
    )
    if not result.get("ok"):
        return CommandResult.failure(
            f"send failed: {result.get('message', 'unknown error')}",
        )

    # Clear the reminder queue entry so hourly reminders stop.
    try:
        from core.recon_reminders import ReconReminderStore
        reminder_path = Path(settings.workdir) / "data" / "recon_reminders.json"
        store = ReconReminderStore(reminder_path)
        store.mark_final_sent(recon_type, date_str)
    except Exception as rem_err:  # noqa: BLE001
        # Non-fatal — the email was sent, just note that reminder
        # clearing failed.
        return CommandResult.success({
            "sent_to": recipients,
            "attachment_used": bool(attachment_path),
            "reminder_clear_warning": str(rem_err),
        })

    return CommandResult.success({
        "sent_to": recipients,
        "attachment_used": bool(attachment_path),
        "reminder_cleared": True,
    })


def _cmd_set_standing_rule(payload: dict[str, Any]) -> CommandResult:
    # P7 — filled in later
    return CommandResult.failure("set_standing_rule handler not yet implemented")


# ── Pre-reconciliation pipeline ────────────────────────────────────── #
#
# Every reconciliation job (holdings, bank, trade) starts by ensuring
# the raw data is present: emails fetched + WS masters downloaded.
# This mirrors the Flask app's daily workflow where the operator would
# click "Fetch" then "Download" then "Reconcile" — Keystone collapses
# all three into one job. If either pre-step fails, the reconciliation
# still proceeds with whatever data is already on disk (same resilience
# as the Flask app's manual flow).


def _ws_reports_for(recon_type: str) -> list[str]:
    """Return the list of WS report display names needed for a recon type.

    Derives the list from the REPORTS definitions in ws_downloader.py by
    filtering rows whose tag list contains `recon_type`. Masters-tagged
    rows are intentionally excluded from every recon — they're reference
    data that belongs in manual master-refresh flows, not pre-recon.
    """
    try:
        from ws_downloader import REPORTS
    except Exception:
        return []
    names: list[str] = []
    for row in REPORTS:
        # row = (display, stem, ext, ..., tags)
        tags = row[-1] if isinstance(row[-1], list) else []
        if "masters" in tags:
            continue
        if recon_type in tags:
            names.append(row[0])
    return names


def _pre_reconciliation(
    date_str: str, settings: AgentSettings, log: _LogCollector,
    recon_type: str = "",
) -> None:
    """Fetch emails + download WS masters for date_str.

    Runs before every reconciliation. Failures are logged as warnings
    but do NOT abort the reconciliation — the operator may have already
    fetched manually, or some sources may be down while others worked.
    """
    # ── Step 1: Fetch emails (incremental from last_fetch_at) ──────
    from .secrets import KEY_M365_CLIENT_SECRET, get_store
    from .setup import EK_M365_CLIENT_ID, EK_M365_MAILBOX, EK_M365_TENANT_ID

    extras = settings.extras or {}
    azure_cfg = {
        "tenant_id": extras.get(EK_M365_TENANT_ID, ""),
        "client_id": extras.get(EK_M365_CLIENT_ID, ""),
        "client_secret": get_store().get(KEY_M365_CLIENT_SECRET) or "",
        "mailbox": extras.get(EK_M365_MAILBOX, ""),
    }

    if all(azure_cfg.values()):
        try:
            from core.email_ingestor import EmailIngestor

            fm = _file_manager(settings.workdir)
            sources = _load_config_json("sources.json").get("sources", [])
            trade_sources = _build_trade_fetch_sources()
            all_sources = sources + trade_sources

            since = extras.get(EK_LAST_FETCH_AT)
            if since:
                log(f"Pre-fetch: incremental email fetch for {date_str} (since {since[:16]})")
            else:
                log(f"Pre-fetch: full email fetch for {date_str} (no prior fetch)")

            ingestor = EmailIngestor(azure_cfg)
            results = ingestor.fetch_for_range(
                date_from=date_str,
                date_to=date_str,
                sources=all_sources,
                file_manager=fm,
                log_callback=lambda msg, **kw: log(f"Pre-fetch: {msg}", **kw),
                since=since,
            )

            ok = sum(1 for r in results if r.get("status") == "ok")
            err = sum(1 for r in results if r.get("status") == "error")
            log(f"Pre-fetch complete: {ok} fetched, {err} errors")

            # Update last_fetch_at
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            settings = load_settings()
            new_extras = dict(settings.extras or {})
            new_extras[EK_LAST_FETCH_AT] = now_iso
            settings.extras = new_extras
            from .config import save_settings
            save_settings(settings)
        except Exception as e:  # noqa: BLE001
            log(f"Pre-fetch failed (continuing with existing data): {e}", level="warning")
    else:
        log("Pre-fetch skipped — M365 credentials not configured", level="warning")

    # ── Step 2: Download WS masters ────────────────────────────────
    from .secrets import KEY_WS_PORTAL_PASSWORD
    from .setup import EK_WS_USERNAME

    ws_user = extras.get(EK_WS_USERNAME, "").strip()
    ws_pass = (get_store().get(KEY_WS_PORTAL_PASSWORD) or "").strip()

    if ws_user and ws_pass:
        prev = {
            "FINCRM_USER": os.environ.get("FINCRM_USER"),
            "FINCRM_PASS": os.environ.get("FINCRM_PASS"),
        }
        os.environ["FINCRM_USER"] = ws_user
        os.environ["FINCRM_PASS"] = ws_pass
        try:
            from ws_downloader import run_all_downloads

            app_dir = Path(settings.workdir).resolve()
            app_dir.mkdir(parents=True, exist_ok=True)
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")

            reports_filter = _ws_reports_for(recon_type) if recon_type else None
            if reports_filter:
                log(f"Pre-download: WS reports for {date_str} "
                    f"({recon_type}): {', '.join(reports_filter)}")
            else:
                log(f"Pre-download: WS reports for {date_str}")
            result = run_all_downloads(
                date_obj=date_obj,
                app_dir=app_dir,
                progress_cb=lambda name, state, msg: log(
                    f"Pre-download {name}: {msg}",
                    level="error" if state == "error" else "info",
                ),
                reports_filter=reports_filter,
            )
            s = int(result.get("success_count", 0))
            t = int(result.get("total", 0))
            log(f"Pre-download complete: {s}/{t} reports")
        except Exception as e:  # noqa: BLE001
            log(f"Pre-download failed (continuing with existing data): {e}", level="warning")
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    else:
        log("Pre-download skipped — WS credentials not configured", level="warning")


# ── Holdings ───────────────────────────────────────────────────────── #


def _run_holdings(job: PollJob) -> RunPush:
    log = _LogCollector()
    try:
        date_str = _job_date(job)
    except ValueError as e:
        return _failed_push(job, "holdings", _now_iso()[:10], log, e)

    log(f"Starting holdings reconciliation for {date_str}")
    settings = load_settings()

    # Auto-fetch emails + download WS masters before reconciliation
    _pre_reconciliation(date_str, settings, log, recon_type="holdings")
    settings = load_settings()  # reload in case pre-step updated extras
    try:
        fm = _file_manager(settings.workdir)

        from core.holdings_recon_workflow import run_holdings_recon
        from core.pools_hub import PoolsHub

        hub = PoolsHub.load()
        mappings_cfg = {
            "strategy_mappings": hub.mappings_dict().get("strategy_mappings", [])
        }
        sources = _load_config_json("sources.json").get("sources", [])

        # Pool-level MAPIN/custodian codes to skip in custodian files.
        # Auto-detect: whenever a pool's `mapin` equals its `custodian_code`
        # the custodian (Axis-style) is known to emit a pool-level summary
        # row under that code, which would otherwise surface as a phantom
        # Custody-Only break. Sub-accounts are excluded because their mapin
        # IS a valid investor code (e.g. GWPJ0016). The legacy
        # `skip_pool_row_in_custodian: true` opt-in is still honoured as a
        # supplement for any pool that doesn't fit the auto rule, so the
        # behaviour is robust even if master-edit strips the flag.
        pool_mapin_codes: set[str] = set()
        for pool in getattr(hub, "_pools", []) or []:
            if pool.get("is_sub_account"):
                continue
            mapin_v = str(pool.get("mapin") or "").strip().upper()
            cust_v  = str(pool.get("custodian_code") or "").strip().upper()
            if mapin_v and cust_v and mapin_v == cust_v:
                pool_mapin_codes.add(mapin_v)
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
            log("No custodian records — cannot run holdings reconciliation", level="error")
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
        log(f"Holdings reconciliation complete — report at {renamed}")
        if renamed:
            _get_local_runs().record(job.id, "holdings", date_str, renamed)
            # Write the break-detail sidecar next to the Excel so the
            # push_break_detail reverse-channel command can serve it
            # to the dashboard explain page later.
            try:
                from core.break_sidecar import write_holdings_sidecar
                write_holdings_sidecar(
                    renamed, job.id, date_str, result.results,
                )
            except Exception as side_err:  # noqa: BLE001
                log(f"Holdings break sidecar write failed: {side_err}", level="warning")

        # Auto-email the holdings recon summary to stakeholders. Mirrors
        # Flask app.py:1868. Best-effort — recon still ships to the
        # control plane even if the email send fails.
        _send_recon_email(
            recon_type="holdings",
            date_str=date_str,
            summary=None,
            results_or_dict=result.results,
            attachment_path=renamed,
            log=log,
            settings=settings,
        )

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

    log(f"Starting bank reconciliation for {date_str}")
    settings = load_settings()

    _pre_reconciliation(date_str, settings, log, recon_type="bank")
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

        # Tier-2 fallback opening balances. The bank engine reads prev
        # working day raw files via the calendar-expanded bank_dates list
        # below (Tier 1, primary). This dict is used only when those files
        # are missing — fresh installs, long holiday windows, or aged-out
        # workdirs. Mirrors Flask's _load_bank_balance_history fallback.
        from core.bank_balance_history import (
            load_bank_balance_history as _load_bank_history,
            append_bank_balance_history as _append_bank_history,
        )
        bank_history: dict = _load_bank_history(date_str, settings.workdir)
        if bank_history.get("cust"):
            log(
                f"Bank balance history fallback loaded: "
                f"{len(bank_history['cust'])} account(s)"
            )

        # Build the calendar-aware bank_dates list, mirroring Flask's
        # logic at app.py:2265-2289. Bank statements arrive daily including
        # weekends/holidays, so we need everything from the previous
        # working day (for opening balances) through the recon day
        # (for closing balances). If the operator supplied an explicit
        # date_from / date_to range in the job payload, honour it instead.
        from core.calendar import (
            load_calendar,
            bank_dates_range,
            bank_dates_for_range,
        )
        cal = load_calendar()
        payload_from = str(job.payload.get("date_from") or "").strip()
        payload_to = str(job.payload.get("date_to") or "").strip()
        if payload_from and payload_to:
            bank_dates_list = bank_dates_for_range(payload_from, payload_to, cal)
            log(
                f"Bank dates (operator range): {bank_dates_list[0]} → "
                f"{bank_dates_list[-1]} ({len(bank_dates_list)} days)"
            )
        else:
            bank_dates_list = bank_dates_range(date_str, cal)
            log(
                f"Bank dates (calendar-expanded): {bank_dates_list[0]} → "
                f"{bank_dates_list[-1]} ({len(bank_dates_list)} days)"
            )

        # Load admin-configured bank tolerance (₹). Missing file or bad
        # value → 100 (legacy default). Stored locally on the agent so
        # it can be edited without a CP round-trip; CP-side push follows
        # in a later change.
        _bank_tol = 100.0
        try:
            import json as _json_tol
            _tol_path = fm.base_dir / 'config' / 'recon_settings.json'
            if _tol_path.exists():
                with _tol_path.open(encoding='utf-8') as _tf:
                    _tol_data = _json_tol.load(_tf) or {}
                _bank_tol = float(_tol_data.get('bank_tolerance_rs', 100))
        except Exception as _tol_err:
            log(f'Bank tolerance load skipped ({_tol_err}); using ₹{_bank_tol}')

        try:
            summary, balance_summary, parse_log = run_bank_recon(
                date_str,
                fm,
                sources,
                password,
                bank_history,
                log,  # log_fn — our collector is callable
                bank_dates=bank_dates_list,
                bank_tolerance_rs=_bank_tol,
            )
        except BankReconError as be:
            for line in getattr(be, "parse_log", []) or []:
                log(str(line))
            return _failed_push(job, "bank", date_str, log, be)

        for line in parse_log or []:
            log(str(line))

        summary_dict = summary.to_dict()
        log(
            f"Bank reconciliation complete — {summary_dict.get('total_pools', 0)} pools, "
            f"{summary_dict.get('clean', 0)} clean, {summary_dict.get('breaks', 0)} breaks"
        )

        # Persist closing balances to the cumulative history file. Best
        # effort — never blocks the run if the write fails. Mirrors
        # Flask app.py:2349.
        try:
            _append_bank_history(date_str, summary, settings.workdir)
        except Exception as hist_err:  # noqa: BLE001
            log(f"Bank balance history append failed: {hist_err}", level="warning")

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

            # Load prior-day closings so Pool Detail / Ledger / Cust
            # Balance Check can render a "Prior-Day Closing" row above
            # each pool. Strictly best-effort — if history is unreadable,
            # we still export (just without the tie-out rows).
            prior_closings: dict[str, tuple[str, float]] = {}
            try:
                import json as _json_pc
                from core.bank_balance_history import history_path as _hp
                _hist_path = _hp(str(fm.base_dir))
                if _hist_path.exists():
                    with _hist_path.open(encoding="utf-8") as _hf:
                        _hist = _json_pc.load(_hf) or []
                    _best: dict[str, tuple[str, float]] = {}
                    for _entry in _hist:
                        _acct = str(_entry.get("cust_account") or "")
                        _ed   = str(_entry.get("date") or "")
                        _cl   = _entry.get("cust_closing", 0)
                        if not _acct or not _ed or _ed >= date_str:
                            continue
                        _ex = _best.get(_acct)
                        if _ex is None or _ed > _ex[0]:
                            _best[_acct] = (_ed, _cl)
                    prior_closings = _best
            except Exception as _pc_err:
                log(f"Prior-day closing lookup skipped: {_pc_err}")

            export_bank_recon(
                summary_dict,
                raw_path,
                balance_check=balance_dict,
                recon_date=date_str,
                prior_closings=prior_closings,
            )
            bank_output_path = _keystone_rename(raw_path, "bank", date_str)
            if bank_output_path:
                log(f"Bank reconciliation report: {bank_output_path}")
                _get_local_runs().record(job.id, "bank", date_str, bank_output_path)
                # Write the break-detail sidecar next to the Excel so
                # the push_break_detail reverse-channel command can
                # serve it to the dashboard explain page later.
                try:
                    from core.break_sidecar import write_bank_sidecar
                    write_bank_sidecar(
                        bank_output_path, job.id, date_str, summary_dict,
                    )
                except Exception as side_err:  # noqa: BLE001
                    log(f"Bank break sidecar write failed: {side_err}", level="warning")
        except Exception as exp_err:  # noqa: BLE001
            log(f"Bank export failed: {exp_err}", level="warning")
            bank_output_path = None

        # Auto-email the bank recon summary to stakeholders. Mirrors
        # Flask app.py:2304-2346. Best-effort — recon still ships to
        # the control plane even if the email send fails.
        _send_recon_email(
            recon_type="bank",
            date_str=date_str,
            summary=summary,
            results_or_dict=summary_dict,
            attachment_path=bank_output_path,
            log=log,
            settings=settings,
        )

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

    log(f"Starting trade reconciliation for {date_str}")
    settings = load_settings()

    _pre_reconciliation(date_str, settings, log, recon_type="trade")
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
            f"Trade reconciliation complete — orders: {summary_dict.get('total_orders', 0)}, "
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
            log(f"Trade reconciliation report: {renamed_recon}")
            _get_local_runs().record(job.id, "trade", date_str, renamed_recon)
            # Write the break-detail sidecar next to the Excel. Trade
            # breaks are not currently part of the explain workflow
            # (trade breaks must be reconciled, not explained) but we
            # still write the sidecar so push_break_detail has
            # something to return for audit / investigation.
            try:
                from core.break_sidecar import write_trade_sidecar
                write_trade_sidecar(
                    renamed_recon, job.id, date_str, summary_dict,
                )
            except Exception as side_err:  # noqa: BLE001
                log(f"Trade break sidecar write failed: {side_err}", level="warning")
        if renamed_0096:
            log(f"Trade 0096 upload file: {renamed_0096}")
        att = attachment_metadata(renamed_recon)

        # Auto-email the trade recon summary to stakeholders. Mirrors
        # Flask app.py:3349. Best-effort — the auto-dispatch below still
        # runs even if the email send fails. Trade recon does NOT queue
        # reminders (breaks must be reconciled, not explained).
        _send_recon_email(
            recon_type="trade",
            date_str=date_str,
            summary=getattr(result, "summary", None),
            results_or_dict=summary_dict,
            attachment_path=renamed_recon,
            log=log,
            settings=settings,
        )

        # Auto-dispatch: when trade recon is all_clear and a 0096 file
        # exists, upload it to WS and email custody interface files to
        # custodians. This is the "straight-through processing" path —
        # if any break exists, dispatch is skipped and the operator must
        # resolve breaks first.
        dispatch_counts: dict[str, int | str] = {}
        if status == "all_clear" and (renamed_0096 or xlsx_0096):
            # Extract which MAPINs are in the 0096 output and map each
            # to its custodian using pools_hub — this is authoritative
            # because the trade recon engine already resolved UCC aliases
            # to canonical MAPINs when building the 0096 rows.
            raw_summary = getattr(result, "summary", None)
            output_rows = getattr(raw_summary, "output_0096", []) if raw_summary else []
            involved_mapins = set()
            for row in output_rows:
                m = getattr(row, "mapin_id", "").strip()
                if m:
                    involved_mapins.add(m)

            # Build mapin → custodian from pools_hub + pool_map aliases
            mapin_cust: dict[str, str] = {}
            mapin_name: dict[str, str] = {}
            for pool in getattr(hub, "_pools", []) or []:
                mp = (pool.get("mapin") or "").strip()
                cb = (pool.get("custodian_bank") or "").strip().upper()
                nm = pool.get("display_name") or pool.get("pool_id") or mp
                if mp and cb:
                    mapin_cust[mp] = cb
                    mapin_name[mp] = nm
            # Also build alias → canonical MAPIN resolution so dispatch
            # uses the canonical code (GWPJ0004) not the broker's back-
            # office UCC (24725) in email subjects and file names.
            alias_to_canonical: dict[str, str] = {}
            pool_map_entries = pool_map_raw.get("pools", [])
            for entry in pool_map_entries:
                mp = (entry.get("mapin") or "").strip()
                canon = (entry.get("canonical_mapin") or "").strip()
                cust_long = (entry.get("custodian") or "").strip().upper()
                if mp and canon:
                    alias_to_canonical[mp] = canon
                # Map the alias to the canonical's custodian
                if mp and canon and canon in mapin_cust:
                    mapin_cust[mp] = mapin_cust[canon]
                    mapin_name[mp] = mapin_name.get(canon, mp)
                elif mp and cust_long:
                    for short in ("ICICI", "HDFC", "KOTAK", "AXIS"):
                        if short in cust_long:
                            mapin_cust[mp] = short
                            mapin_name[mp] = entry.get("pool_name", mp)
                            break

            from collections import defaultdict
            by_custodian: dict[str, list[dict]] = defaultdict(list)
            for m in involved_mapins:
                c = mapin_cust.get(m)
                # Resolve to canonical MAPIN for display in emails/filenames
                resolved = alias_to_canonical.get(m, m)
                if c:
                    by_custodian[c].append({
                        "mapin": resolved,
                        "strategy": mapin_name.get(m, mapin_name.get(resolved, resolved)),
                    })
                else:
                    log(f"Dispatch: MAPIN {m} from 0096 not found in pools config", level="warning")

            dispatch_counts = _auto_dispatch_trades(
                file_0096=renamed_0096 or xlsx_0096 or "",
                date_str=date_str,
                workdir=settings.workdir,
                log=log,
                by_custodian=dict(by_custodian),
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
    file_0096: str, date_str: str, workdir: str, log: _LogCollector,
    by_custodian: dict[str, list[dict]] | None = None,
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
        from ws_uploader import dispatch_trades, dispatch_nsdl
        from .secrets import KEY_M365_CLIENT_SECRET
        from .setup import (EK_M365_CLIENT_ID, EK_M365_MAILBOX,
                            EK_M365_TENANT_ID, EK_TRADE_DISPATCH_TYPE)
        from .paths import config_dir

        m365_extras = settings.extras or {}
        azure_config = {
            "tenant_id": m365_extras.get(EK_M365_TENANT_ID, ""),
            "client_id": m365_extras.get(EK_M365_CLIENT_ID, ""),
            "client_secret": get_store().get(KEY_M365_CLIENT_SECRET) or "",
            "mailbox": m365_extras.get(EK_M365_MAILBOX, ""),
        }

        # Operator choice: 0096 block-deals or NSDL Steady contract notes.
        # Configured via the trade_dispatch_type extras key (mapped to
        # EK_TRADE_DISPATCH_TYPE). Defaults to 0096 to preserve current
        # behaviour for agents that haven't migrated.
        dispatch_type = (
            (settings.extras or {}).get(EK_TRADE_DISPATCH_TYPE) or "0096"
        ).strip().lower()
        if dispatch_type not in ("0096", "nsdl"):
            log(f"Dispatch type {dispatch_type!r} not recognised — defaulting to 0096",
                level="warning")
            dispatch_type = "0096"

        def progress(stage: str, detail: str) -> None:
            log(f"Dispatch {stage}: {detail}")

        if dispatch_type == "nsdl":
            # Find today's NSDL CNSTAT file on disk. File manager's
            # raw folder for the "nsdl" source (broker_map entry with
            # file_contains='CNSTAT') is where the ingestor dropped it.
            nsdl_file = None
            try:
                nsdl_raw = (
                    Path(settings.workdir).resolve()
                    / "data" / date_str / "raw" / "nsdl"
                )
                if nsdl_raw.exists():
                    cands = [p for p in nsdl_raw.iterdir()
                             if p.is_file() and "CNSTAT" in p.name.upper()
                             and p.suffix.lower() in (".xls", ".xlsx")]
                    if cands:
                        nsdl_file = str(max(cands, key=lambda p: p.stat().st_mtime))
            except Exception as _nf_err:
                log(f"NSDL file discovery failed: {_nf_err}", level="warning")

            if not nsdl_file:
                log(f"Dispatch failed — no NSDL CNSTAT file for {date_str} "
                    f"under data/{date_str}/raw/nsdl/", level="warning")
                return {"dispatch": "failed: no_nsdl_file"}

            log(f"Auto-dispatch (NSDL): uploading {Path(nsdl_file).name} + "
                f"sending custody emails for {date_str}")
            result = dispatch_nsdl(
                nsdl_file=nsdl_file,
                date_str=date_str,
                progress_cb=progress,
                config_dir=config_dir(),
                workdir=Path(settings.workdir).resolve(),
                azure_config=azure_config,
                by_custodian=by_custodian,
            )
        else:
            log(f"Auto-dispatch (0096): uploading 0096 + sending custody emails for {date_str}")
            result = dispatch_trades(
                file_0096=file_0096,
                date_str=date_str,
                progress_cb=progress,
                config_dir=config_dir(),
                workdir=Path(settings.workdir).resolve(),
                azure_config=azure_config,
                by_custodian=by_custodian,
            )

        # Treat duplicate upload as success — the 0096 was already in WS
        # from a prior run or manual upload. The custody dispatch should
        # still proceed (and does inside dispatch_trades).
        upload_detail = getattr(result.upload_result, "detail", "") if result.upload_result else ""
        is_dup = upload_detail == "duplicate"

        if result.ok or is_dup:
            n_cust = len(result.custodian_results)
            n_ok = sum(1 for c in result.custodian_results if c.get("email_ok"))
            dup_note = " (0096 was already uploaded)" if is_dup else ""
            log(f"Dispatch complete{dup_note} — {n_ok}/{n_cust} custodian emails sent")
            return {
                "dispatch": "ok" if not is_dup else "ok_duplicate",
                "dispatch_custodians": n_cust,
                "dispatch_emails_ok": n_ok,
            }
        else:
            upload_msg = getattr(result.upload_result, "message", "") if result.upload_result else ""
            msg = upload_detail or upload_msg or "unknown"
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
            "file_contains": ts.get("file_contains", ""),
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


EK_LAST_FETCH_AT = "last_fetch_at"


def _run_fetch_emails(job: PollJob) -> RunPush:
    """Pull custodian/bank/trade emails via Microsoft Graph.

    Two modes:

    1. **Manual / multi-day** — operator dispatches with date_from + date_to.
       Uses per-date pivot stepping so every day in the range is covered.
       No `since` logic — operator explicitly asked for a range.

    2. **Scheduled / single-date** — scheduler creates a job with just {date}.
       Uses the incremental `since` path: a single Graph query from
       "30 minutes before last successful fetch" to "target + 2 days".
       Fast, narrow, no redundant pages. Falls back to a per-date lookback
       on first-ever fetch (no `since` saved yet).

    After a successful fetch, saves the current UTC timestamp as
    last_fetch_at in agent settings so the next scheduled fetch can
    use the incremental path.
    """
    log = _LogCollector()
    payload = job.payload or {}

    date_from = str(payload.get("date_from", "")).strip()
    date_to = str(payload.get("date_to", "")).strip()
    is_range = bool(date_from and date_to)

    if is_range:
        date_str = date_to
    else:
        try:
            date_str = _job_date(job)
        except ValueError as e:
            return _failed_push(job, "holdings", _now_iso()[:10], log, e)
        date_from = date_str
        date_to = date_str

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

    # Determine since for incremental fetch (single-date / scheduled path)
    since: str | None = None
    if not is_range:
        since = extras.get(EK_LAST_FETCH_AT)
        if since:
            log(f"Starting incremental email fetch for {date_str} (since {since[:16]})")
        else:
            log(f"Starting full email fetch for {date_str} (no prior fetch recorded)")
    else:
        log(f"Starting email fetch for range {date_from} to {date_to}")

    try:
        from core.email_ingestor import EmailIngestor

        fm = _file_manager(settings.workdir)

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
            since=since,
        )

        ok_count = sum(1 for r in results if r.get("status") == "ok")
        err_count = sum(1 for r in results if r.get("status") == "error")
        skip_count = sum(1 for r in results if r.get("status") == "skipped")
        log(
            f"Fetch complete — ok={ok_count} skipped={skip_count} errors={err_count}"
        )

        # Archive pass: catch-all email archiver mirroring Flask
        # app.py:585-590. Best-effort — never blocks the run if it fails.
        try:
            arch_dir = fm.archive_base_dir()
            ingestor.archive_for_date(
                date_to, all_sources, arch_dir, log_callback=log
            )
        except Exception as arch_err:  # noqa: BLE001
            log(f"Archive pass failed: {arch_err}", level="warning")

        # Persist last_fetch_at so the next scheduled fetch can use
        # the incremental path. Only update on success (not on errors
        # that might indicate a partial fetch).
        if err_count == 0 or ok_count > 0:
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            settings = load_settings()
            new_extras = dict(settings.extras or {})
            new_extras[EK_LAST_FETCH_AT] = now_iso
            settings.extras = new_extras
            from .config import save_settings
            save_settings(settings)
            log(f"Saved last_fetch_at = {now_iso}")

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


def _cmd_master_client_list(payload: dict[str, Any]) -> CommandResult:
    """Return the merged client master (WS Z30 + local extras).

    Payload:
        mode: "load" | "fetch" | "cache" (default "load")
              load  = parse latest Z30 on disk
              fetch = download fresh from WS then parse
              cache = in-memory cache within TTL, else load
    """
    try:
        from core.masters_service import get_client_master
    except Exception as e:
        return CommandResult.failure(f"masters_service import failed: {e}")

    mode = str(payload.get("mode") or "load").lower()
    if mode not in ("load", "fetch", "cache"):
        mode = "load"

    try:
        data = get_client_master(mode=mode)
    except Exception as e:
        logger.exception("master_client_list failed")
        return CommandResult.failure(f"{type(e).__name__}: {e}")

    if data.get("error"):
        return CommandResult.failure(data["error"])

    return CommandResult.success({
        "rows": data.get("rows", []),
        "row_count": len(data.get("rows", [])),
        "fetched_at": data.get("fetched_at"),
        "source_file": data.get("source_file"),
        "mode": data.get("mode", mode),
    })


def _read_config_json(filename: str) -> dict[str, Any] | None:
    """Read a JSON file from the agent's writable config dir.

    Uses agent.paths.config_dir() so we always read from the seeded
    writable snapshot (not the read-only bundle).
    """
    try:
        from agent.paths import config_dir
        path = config_dir() / filename
        if not path.exists():
            return None
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to read {filename}: {e}")
        return None


def _cmd_master_pool_list(payload: dict[str, Any]) -> CommandResult:
    """Return the pool master (pools_hub.json, live from agent's config dir).

    No WS call — this is agent-local reference data. Returns rows as-is
    plus a synthetic row_count / source_file for consistency with the
    other master commands.
    """
    hub = _read_config_json("pools_hub.json")
    if hub is None:
        return CommandResult.failure(
            "pools_hub.json not found — check agent config directory"
        )
    pools = hub.get("pools", [])
    return CommandResult.success({
        "rows": pools,
        "row_count": len(pools),
        "fetched_at": int(datetime.now(timezone.utc).timestamp()),
        "source_file": "pools_hub.json",
        "mode": "local",
    })


def _cmd_master_broker_list(payload: dict[str, Any]) -> CommandResult:
    """Return the broker master (broker_map.json, live from agent's config dir).

    No WS call — this is agent-local reference data.
    """
    bm = _read_config_json("broker_map.json")
    if bm is None:
        return CommandResult.failure(
            "broker_map.json not found — check agent config directory"
        )
    brokers = bm.get("brokers", [])
    return CommandResult.success({
        "rows": brokers,
        "row_count": len(brokers),
        "fetched_at": int(datetime.now(timezone.utc).timestamp()),
        "source_file": "broker_map.json",
        "mode": "local",
    })


def _cmd_master_custody_list(payload: dict[str, Any]) -> CommandResult:
    """Return the custody/dispatch master (custodian_dispatch.json).

    This is the operational dispatch config — one row per custodian
    (AXIS, HDFC, ICICI, KOTAK) with interface_type, report_format, and
    email routing. Each row is flattened to include the custodian code
    as a `code` field so the UI can sort/filter on it. Full detail
    fields (email_subject, email_body) are included so the detail page
    has everything it needs without a second round-trip.
    """
    cd = _read_config_json("custodian_dispatch.json")
    if cd is None:
        return CommandResult.failure(
            "custodian_dispatch.json not found — check agent config directory"
        )
    custodians = cd.get("custodians", {}) or {}
    rows = []
    for code, info in custodians.items():
        if not isinstance(info, dict):
            continue
        rows.append({
            "code": code,
            "interface_type": info.get("interface_type", ""),
            "report_format": info.get("report_format", ""),
            "email_to": info.get("email_to", []),
            "email_to_count": len(info.get("email_to", []) or []),
            "send_from": info.get("send_from", ""),
            "email_subject": info.get("email_subject", ""),
            "email_body": info.get("email_body", ""),
            "active": info.get("active", True),
        })
    return CommandResult.success({
        "rows": rows,
        "row_count": len(rows),
        "fetched_at": int(datetime.now(timezone.utc).timestamp()),
        "source_file": "custodian_dispatch.json",
        "mode": "local",
    })


def _write_config_json(filename: str, data: Any) -> Path:
    """Atomically write JSON to the agent's writable config dir.

    Writes to a sibling tempfile and os.replace()s into place so a
    crash mid-write can't leave a half-written config that would
    fail to parse on next startup.
    """
    from agent.paths import config_dir
    import json
    import os
    target = config_dir() / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


def _cmd_master_pool_upsert(payload: dict[str, Any]) -> CommandResult:
    """Insert or update one pool in pools_hub.json, keyed by pool_id.

    Payload: {"pool": {pool_id, display_name, ..., broker_cn_aliases, ws_overrides}}

    We match on pool_id (case-insensitive). If found, shallow-merge the
    payload over the existing record so fields the CP form doesn't know
    about (e.g. skip_pool_row_in_custodian, parent_pool_id, is_sub_account,
    ws_overrides from an older schema) survive the round-trip. If not
    found, append the record as-is.
    """
    pool = payload.get("pool")
    if not isinstance(pool, dict):
        return CommandResult.failure("missing 'pool' object in payload")
    pool_id = str(pool.get("pool_id") or "").strip()
    if not pool_id:
        return CommandResult.failure("pool.pool_id is required")

    hub = _read_config_json("pools_hub.json") or {}
    pools = hub.get("pools", [])
    if not isinstance(pools, list):
        return CommandResult.failure("pools_hub.json malformed: 'pools' is not a list")

    replaced = False
    for i, existing in enumerate(pools):
        if isinstance(existing, dict) and str(existing.get("pool_id", "")).lower() == pool_id.lower():
            merged = {**existing, **pool}
            pools[i] = merged
            pool = merged   # reflect the full record in the response
            replaced = True
            break
    if not replaced:
        pools.append(pool)

    hub["pools"] = pools
    try:
        path = _write_config_json("pools_hub.json", hub)
    except Exception as e:
        logger.exception("pool upsert write failed")
        return CommandResult.failure(f"write failed: {type(e).__name__}: {e}")

    return CommandResult.success({
        "pool": pool,
        "mode": "update" if replaced else "create",
        "source_file": str(path.name),
    })


def _cmd_master_broker_upsert(payload: dict[str, Any]) -> CommandResult:
    """Insert or update one broker in broker_map.json, keyed by dealer_code.

    Same merge-on-update semantics as the pool upsert — only the fields
    the CP form sends are overwritten; every other existing field is
    preserved. Prevents the master-edit round-trip from silently
    stripping fields the form doesn't know about (sebi_reg_no fallbacks,
    historical alias rows, etc.).
    """
    broker = payload.get("broker")
    if not isinstance(broker, dict):
        return CommandResult.failure("missing 'broker' object in payload")
    dealer_code = str(broker.get("dealer_code") or "").strip()
    if not dealer_code:
        return CommandResult.failure("broker.dealer_code is required")

    bm = _read_config_json("broker_map.json") or {}
    brokers = bm.get("brokers", [])
    if not isinstance(brokers, list):
        return CommandResult.failure("broker_map.json malformed: 'brokers' is not a list")

    replaced = False
    for i, existing in enumerate(brokers):
        if isinstance(existing, dict) and str(existing.get("dealer_code", "")).lower() == dealer_code.lower():
            merged = {**existing, **broker}
            brokers[i] = merged
            broker = merged
            replaced = True
            break
    if not replaced:
        brokers.append(broker)

    bm["brokers"] = brokers
    try:
        path = _write_config_json("broker_map.json", bm)
    except Exception as e:
        logger.exception("broker upsert write failed")
        return CommandResult.failure(f"write failed: {type(e).__name__}: {e}")

    return CommandResult.success({
        "broker": broker,
        "mode": "update" if replaced else "create",
        "source_file": str(path.name),
    })


def _cmd_master_custody_upsert(payload: dict[str, Any]) -> CommandResult:
    """Insert or update one custodian in custodian_dispatch.json.

    Keyed by `code` in the input row. The on-disk format is a dict
    keyed by code, so we assign the row (minus the code/count fields)
    into custodians[code].
    """
    custody = payload.get("custody")
    if not isinstance(custody, dict):
        return CommandResult.failure("missing 'custody' object in payload")
    code = str(custody.get("code") or "").strip()
    if not code:
        return CommandResult.failure("custody.code is required")

    cd = _read_config_json("custodian_dispatch.json") or {}
    custodians = cd.get("custodians", {})
    if not isinstance(custodians, dict):
        return CommandResult.failure("custodian_dispatch.json malformed: 'custodians' is not a dict")

    existed = code in custodians
    # Merge over existing row so fields the CP form doesn't know about
    # (schedule flags, custom notes, etc.) are preserved on update.
    prev = custodians.get(code) if existed else {}
    if not isinstance(prev, dict):
        prev = {}
    incoming = {k: v for k, v in custody.items() if k not in ("code", "email_to_count")}
    row = {**prev, **incoming}
    custodians[code] = row
    cd["custodians"] = custodians

    try:
        path = _write_config_json("custodian_dispatch.json", cd)
    except Exception as e:
        logger.exception("custody upsert write failed")
        return CommandResult.failure(f"write failed: {type(e).__name__}: {e}")

    return CommandResult.success({
        "custody": {**row, "code": code, "email_to_count": len(row.get("email_to", []) or [])},
        "mode": "update" if existed else "create",
        "source_file": str(path.name),
    })
