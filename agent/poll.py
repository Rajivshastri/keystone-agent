"""Background poll loop.

Phase 1: drains the job queue on every tick, executes each job via
`agent.runner.execute_job`, and pushes the resulting `RunPush` to the
control plane. If the push fails (transport error, 5xx), the payload
is persisted to the local SQLite outbox and retried on the next tick.

Heartbeats go out every 60 seconds independent of the poll cadence.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from .config import AgentSettings, load_settings
from .local_runs import get_store as get_local_runs
from .outbox import get_outbox
from .protocol import (
    AgentCommand, FileRequestJob, HealthPing, PollJob, RunPush, TaskPush,
)
from .runner import execute_command, execute_job
from .transport import ControlPlaneClient, TransportError

logger = logging.getLogger(__name__)


class PollLoop:
    """Encapsulates the agent's outbound polling behaviour."""

    # Module-level singleton reference so PairingCoordinator (and anyone
    # else in-process) can wake us without having to pass a handle around.
    _instance: "PollLoop | None" = None

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings
        self._client = ControlPlaneClient(settings)
        self._stop_event = threading.Event()
        # Separate event used purely as a cancellable sleep target. We
        # reach for _wake_event.wait(interval) between ticks; anything
        # that wants to jumpstart the next tick sets it. The loop re-
        # creates it each iteration so a single wake doesn't flow
        # through every subsequent wait.
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_heartbeat_at: float = 0.0
        PollLoop._instance = self

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="keystone-poll", daemon=True
        )
        self._thread.start()
        logger.info("Poll loop started")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()  # break any current wait
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._client.close()
        logger.info("Poll loop stopped")
        if PollLoop._instance is self:
            PollLoop._instance = None

    def wake(self) -> None:
        """Interrupt the current inter-tick sleep so the next tick runs now.

        Safe to call from any thread. Used by PairingCoordinator so the
        first poll after a successful pair happens within ~1s rather
        than up to the full poll_interval.
        """
        self._wake_event.set()

    @classmethod
    def wake_current(cls) -> None:
        """Convenience for callers that don't hold a PollLoop reference."""
        inst = cls._instance
        if inst is not None:
            inst.wake()

    # ---- internals ---- #

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except TransportError as e:
                logger.warning(f"Poll transport error: {e}")
            except Exception as e:  # noqa: BLE001
                logger.exception(f"Poll loop unexpected error: {e}")
            if self._stop_event.is_set():
                break
            # Re-read the interval on each loop so a pairing handshake
            # that changes poll_interval_seconds is picked up immediately.
            interval = max(1, int(load_settings().poll_interval_seconds))
            # Sleep on wake_event — wake() sets it, which returns from
            # wait() immediately. We then clear it so the next tick
            # waits again from zero.
            self._wake_event.wait(interval)
            self._wake_event.clear()

    def _tick(self) -> None:
        settings = load_settings()

        # If the control plane URL changed (e.g. operator filled in the
        # wizard while the poll loop was already running with the old
        # default), recreate the httpx client so it hits the new host.
        if settings.control_plane_url != self._settings.control_plane_url:
            logger.info(
                f"Control plane URL changed: {self._settings.control_plane_url} "
                f"→ {settings.control_plane_url} — reconnecting"
            )
            self._settings = settings
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = ControlPlaneClient(settings)

        # Heartbeat every 60s regardless of poll cadence
        now = time.monotonic()
        if now - self._last_heartbeat_at >= 60.0:
            self._send_heartbeat(settings)
            self._last_heartbeat_at = now

        # Only poll / push if the agent is actually paired
        if not settings.agent_id:
            return

        # 1) Drain the outbox first — anything queued while we were offline
        self._drain_outbox()

        # 2) Pull fresh jobs
        try:
            resp = self._client.poll(max_jobs=8)
        except TransportError as e:
            logger.warning(f"Poll call failed: {e}")
            return

        if resp.update is not None:
            logger.info(
                f"Update available: {resp.update.version} "
                f"(required={resp.update.required})"
            )
            # Phase 4: auto-updater flow

        # 3) Fulfil any file_requests handed down in the poll envelope.
        #    These are independent of normal jobs — the operator asked
        #    for a specific local artifact and we upload it back up.
        if resp.file_requests:
            logger.info(
                f"Received {len(resp.file_requests)} file request(s) from control plane"
            )
            for fr in resp.file_requests:
                try:
                    self._fulfil_file_request(fr)
                except Exception as e:  # noqa: BLE001
                    logger.exception(f"File request {fr.id} fulfil crashed: {e}")

        # 4) Drain any reverse-channel commands handed down in the poll
        #    envelope. Each command runs through runner.execute_command
        #    and is ACKed back via the transport client. Command failures
        #    never propagate — they're ACKed with ok=false so the control
        #    plane sees them in `failed` state.
        if resp.commands:
            logger.info(
                f"Received {len(resp.commands)} command(s) from control plane"
            )
            for cmd in resp.commands:
                try:
                    self._execute_and_ack_command(cmd)
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        f"Command {cmd.id} ({cmd.kind}) dispatcher crashed: {e}"
                    )

        if not resp.jobs:
            return
        logger.info(f"Received {len(resp.jobs)} job(s) from control plane")
        for job in resp.jobs:
            self._execute_and_push(job)

    def _fulfil_file_request(self, fr: FileRequestJob) -> None:
        """Read a local artifact and upload it to the control plane.

        The control plane gave us a job_id that we use to look up the
        local_runs row; that row carries the absolute on-disk path of
        the Excel artifact the agent wrote for that run. Anything that
        isn't resolvable is logged and left pending — the control
        plane operator can retry or cancel.
        """
        if not fr.job_id:
            logger.warning(f"File request {fr.id} has no job_id — skipping")
            return
        row = get_local_runs().get(fr.job_id)
        if row is None:
            logger.warning(
                f"File request {fr.id}: no local_runs row for job {fr.job_id}"
            )
            return
        from pathlib import Path

        p = Path(row.output_path)
        if not p.is_file():
            logger.warning(
                f"File request {fr.id}: artifact missing on disk: {row.output_path}"
            )
            return
        try:
            data = p.read_bytes()
        except OSError as e:
            logger.warning(f"File request {fr.id}: read failed: {e}")
            return
        logger.info(
            f"File request {fr.id}: uploading {p.name} ({len(data)} bytes) "
            f"to {fr.upload_url}"
        )
        try:
            result = self._client.upload_file_request(
                fr.upload_url, p.name, data
            )
        except TransportError as e:
            logger.warning(f"File request {fr.id}: upload failed: {e}")
            return
        logger.info(f"File request {fr.id}: upload ok — {result}")

    def _execute_and_ack_command(self, cmd: AgentCommand) -> None:
        """Run one reverse-channel command end-to-end.

        Runner returns a CommandResult. We ACK to the control plane
        regardless of outcome — a failed command should end up in
        `failed` state on the server, not silently retried.
        """
        logger.info(f"Executing command {cmd.id} kind={cmd.kind}")
        result = execute_command(cmd.kind, cmd.payload)
        try:
            self._client.ack_command(
                cmd.id,
                result.ok,
                result=result.result,
                error=result.error,
            )
            if result.ok:
                logger.info(f"Command {cmd.id} ACKed: ok")
            else:
                logger.info(f"Command {cmd.id} ACKed: failed — {result.error}")
        except TransportError as e:
            # ACK failed. The command row on the server stays in
            # `delivered` state. The reaper sweep will eventually
            # flip it to `expired` once expires_at passes — no auto-
            # retry here to avoid double-executing external side
            # effects like emails.
            logger.warning(
                f"Command {cmd.id} ACK failed ({e}) — server row stays delivered"
            )

    # ---- dispatch ---- #

    def _execute_and_push(self, job: PollJob) -> None:
        """Run one job end-to-end: execute, push, fall back to outbox.

        execute_job returns RunPush for recon-style jobs and TaskPush
        for task-style jobs (client_onboard, ws_upload, etc.). We
        dispatch the push to the right endpoint based on the result
        type.
        """
        logger.info(f"Executing job {job.id} type={job.type}")
        try:
            result = execute_job(job)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Runner crashed on job {job.id}: {e}")
            return

        if isinstance(result, TaskPush):
            self._push_task(result)
        else:
            self._push_run(result)

    def _push_run(self, run: RunPush) -> None:
        """Attempt immediate push; fall back to outbox on failure."""
        try:
            resp = self._client.push_run(run)
            if resp.ok:
                logger.info(
                    f"Run pushed to control plane: {run.type}/{run.recon_date} "
                    f"status={run.status} run_id={resp.run_id}"
                )
                return
            logger.warning(
                f"Push returned ok=false — queueing to outbox: {run.type}/{run.recon_date}"
            )
        except TransportError as e:
            logger.warning(
                f"Push failed ({e}) — queueing to outbox: {run.type}/{run.recon_date}"
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                f"Push unexpected error — queueing to outbox: {e}"
            )

        # Fall-through: enqueue
        try:
            get_outbox().enqueue(run.model_dump())
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Outbox enqueue failed: {e}")

    def _push_task(self, task: TaskPush) -> None:
        """Push a task result (client_onboard, ws_upload, etc.) to the
        control plane. Mirrors _push_run but hits /agent/tasks. Falls
        back to the outbox on transport failure — when the outbox
        drains we attempt RunPush first then TaskPush based on payload
        shape (the outbox is shared)."""
        try:
            resp = self._client.push_task(task)
            if resp.ok:
                logger.info(
                    f"Task pushed to control plane: {task.type} "
                    f"status={task.status} task_id={resp.task_id}"
                )
                return
            logger.warning(
                f"Task push returned ok=false — queueing to outbox: {task.type}"
            )
        except TransportError as e:
            logger.warning(
                f"Task push failed ({e}) — queueing to outbox: {task.type}"
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(
                f"Task push unexpected error — queueing to outbox: {e}"
            )
        # Fall-through: enqueue. Mark the payload so the outbox drainer
        # can distinguish task from run when it retries.
        try:
            payload = task.model_dump()
            payload["__kind__"] = "task"
            get_outbox().enqueue(payload)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Outbox enqueue failed: {e}")

    def _drain_outbox(self) -> None:
        """Retry everything in the outbox, FIFO.

        Any success deletes the row; any failure bumps the attempt
        counter and leaves the row in place for the next tick. We cap
        per-tick work at 16 rows so a huge backlog doesn't stall the
        live job loop.
        """
        outbox = get_outbox()
        items = outbox.peek(limit=16)
        if not items:
            return
        logger.info(f"Draining {len(items)} outbox item(s)")
        for item in items:
            # Distinguish task from run payloads. Task items have the
            # __kind__ sentinel added by _push_task before enqueue.
            is_task = (isinstance(item.payload, dict)
                        and item.payload.get("__kind__") == "task")
            try:
                if is_task:
                    raw = {k: v for k, v in item.payload.items() if k != "__kind__"}
                    msg = TaskPush.model_validate(raw)
                else:
                    msg = RunPush.model_validate(item.payload)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Outbox item {item.id} has invalid payload, dropping: {e}")
                outbox.delete(item.id)
                continue
            try:
                if isinstance(msg, TaskPush):
                    resp = self._client.push_task(msg)
                    if resp.ok:
                        outbox.delete(item.id)
                        logger.info(
                            f"Outbox item {item.id} flushed (task): {msg.type}"
                        )
                    else:
                        outbox.bump_attempt(item.id, "task push returned ok=false")
                    continue
                run = msg
                resp = self._client.push_run(run)
                if resp.ok:
                    outbox.delete(item.id)
                    logger.info(
                        f"Outbox item {item.id} flushed: {run.type}/{run.recon_date}"
                    )
                else:
                    outbox.bump_attempt(item.id, "push returned ok=false")
                    return  # stop draining so live jobs get a turn
            except TransportError as e:
                outbox.bump_attempt(item.id, str(e))
                return  # control plane down — stop draining
            except Exception as e:  # noqa: BLE001
                outbox.bump_attempt(item.id, f"{type(e).__name__}: {e}")
                return

    def _send_heartbeat(self, settings: AgentSettings) -> None:
        if not settings.agent_id:
            return
        from . import __version__

        try:
            disk_free = _disk_free_mb(settings.workdir or ".")
        except Exception:
            disk_free = None
        reminders_snapshot = _reminders_snapshot(settings)
        try:
            self._client.health(
                HealthPing(
                    version=__version__,
                    disk_free_mb=disk_free,
                    pending_jobs=get_outbox().count(),
                    metrics={
                        "local_time": datetime.now(timezone.utc).isoformat(),
                        "reminders": reminders_snapshot,
                    },
                )
            )
        except TransportError as e:
            logger.warning(f"Heartbeat failed: {e}")


def _disk_free_mb(path: str) -> int | None:
    import shutil

    try:
        total, used, free = shutil.disk_usage(path)
    except OSError:
        return None
    return int(free // (1024 * 1024))


def _reminders_snapshot(settings: AgentSettings) -> list[dict]:
    """Return a compact summary of pending reminders for the heartbeat.

    Empty list when the store file is missing or unreadable — keeps the
    ping payload resilient.
    """
    try:
        from pathlib import Path as _Path
        from core.recon_reminders import ReconReminderStore

        reminder_path = _Path(settings.workdir or ".") / "data" / "recon_reminders.json"
        store = ReconReminderStore(reminder_path)
        return [
            {
                "recon_type":      e.get("type", ""),
                "date":            e.get("date", ""),
                "reminder_count":  int(e.get("reminder_count", 0) or 0),
                "last_reminder_at": e.get("last_reminder_at"),
                "next_due_at":     e.get("next_due_at"),
                "initial_sent_at": e.get("initial_sent_at"),
            }
            for e in store.list_pending()
        ]
    except Exception:  # noqa: BLE001
        return []
