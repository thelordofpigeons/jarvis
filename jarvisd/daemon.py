"""Composition root and the resident loop (design sections 3 and 10).

`build_deps` is the one place that wires the real classes together; the daemon and the CLI
both call it. `serve` is the long-running process: single-instance lock, startup record,
recovery, a heartbeat thread, and either the APScheduler host (normal run) or a bounded
synchronous tick loop (`max_ticks`, used by tests and by `jarvis serve --max-ticks`).
`tick` is the unit of work: kill and pause checks, reconcile, claim, run one job.

Two modes. Task mode (`jarvis serve --task`) is what Task Scheduler runs under pythonw: the
Claude client is enabled, the task-disabled check is on, and nothing is printed. Dev mode is
everything else: the client is forced off whatever the caller built, nothing is spawned, and
`schtasks` is not queried, because a hand-started daemon is not covered by the kill switch.

Limits, stated plainly:
- Bounded runs (`max_ticks`) do not start the APScheduler host. The tick itself reconciles,
  so the digest still happens; the cron-only jobs (housekeeping, disk check) run once at
  startup instead of on their schedule.
- Ticks are serialised by a process-wide lock. A tick that finds it taken returns `busy`
  and does nothing; the next one picks up.
- A job that raises an unexpected exception is retried once per tick delay (10 minutes) until
  its attempts run out. The audit shows `job_failed` with the exception class only.
- This module is the one place that runs `schtasks`; it does so with a list argv and no shell.
"""
from __future__ import annotations

import csv
import getpass
import io
import os
import re
import shutil
import subprocess
import threading
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jarvisd import __version__
from jarvisd.audit import AuditLog
from jarvisd.claude import ClaudeClient
from jarvisd.collectors import Collector
from jarvisd.collectors.brain import BrainCollector
from jarvisd.collectors.clickup import ClickUpCollector
from jarvisd.collectors.git import GitCollector
from jarvisd.collectors.github import GitHubCollector
from jarvisd.collectors.system import SystemCollector
from jarvisd.collectors.task import TaskCollector
from jarvisd.common import local_now
from jarvisd.config import Config, ConfigError
from jarvisd.consolidate import CONSOLIDATE_KIND, reconcile_consolidation, run_consolidate_job
from jarvisd.digest import Deps, Retry, run_digest_job
from jarvisd.dispatch import local_state
from jarvisd.jobstore import JobStore
from jarvisd.notify import Notifier, build_notifier
from jarvisd.local import LlamaBackend
from jarvisd.local import build_runtime as build_local_runtime
from jarvisd.router import build_router
from jarvisd.scheduler import SchedulerHost, reconcile
from jarvisd.state import AlreadyRunning, StateStore
from jarvisd.vault import VaultWriter

TASK_NAME = "JarvisDaemon"
UNHANDLED_DELAY = timedelta(minutes=10)
MAX_JOBS_PER_TICK = 3
_BYTES_PER_MB = 1024 * 1024
_BYTES_PER_GB = 1024 * 1024 * 1024
# English "Disabled" and the French "Desactive" spellings; the accent may arrive as a
# replacement character because schtasks prints in the OEM code page.
_DISABLED = re.compile(r"disabled|d.sactiv", re.IGNORECASE)

CommandRunner = Callable[[Sequence[str], float], "tuple[int, str]"]
_TICK_LOCK = threading.Lock()


# --- running other programs ----------------------------------------------------------------


def run_command(argv: Sequence[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run a short command as a list argv (never a shell). Returns (exit code, stdout).

    A command that cannot start or times out returns (1, "") so callers treat it as unknown.
    """
    try:
        proc = subprocess.run(  # noqa: S603  list argv built from constants and our own paths
            list(argv), capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


def query_task_state(runner: CommandRunner | None = None) -> str | None:
    """Status text of the JarvisDaemon scheduled task ("Ready", "Running", "Disabled"), or None.

    None means the query failed or the task is not registered; callers must not treat that
    as a kill signal.
    """
    run = runner or run_command
    code, out = run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "CSV", "/nh"], 15.0)
    if code != 0:
        return None
    for row in csv.reader(io.StringIO(out)):
        if row:
            return row[-1].strip()
    return None


def status_is_disabled(status: str | None) -> bool:
    return bool(status and _DISABLED.search(status))


def task_is_disabled(runner: CommandRunner | None = None) -> bool:
    return status_is_disabled(query_task_state(runner))


# --- composition -----------------------------------------------------------------------------


def audit_path(cfg: Config) -> Path:
    return cfg.paths.logs / "jarvisd-audit.jsonl"


def build_deps(cfg: Config, *, claude_enabled: bool = False, mirror_stdout: bool = True,
               clock: Callable[[], datetime] | None = None, claude_runner: Any = None,
               notifier: Notifier | None = None, command_runner: CommandRunner | None = None,
               task_base_dir: Path | None = None, network_probe: Callable[[], bool] | None = None,
               run_id: str | None = None, config_loader: Callable[[], Config] | None = None) -> Deps:
    """The real composition, with seams for tests. Used by the daemon and the CLI alike.

    `clock` (aware) reaches the audit log, the state store, the queue and the digest. Left
    out, each uses the real time. `mirror_stdout` must be False under pythonw.
    """
    audit = AuditLog(audit_path(cfg), cfg.retention.audit_max_bytes, cfg.retention.audit_keep_days,
                     run_id=run_id or uuid.uuid4().hex[:8], clock=clock, mirror_stdout=mirror_stdout)
    state = StateStore.from_config(cfg, clock=clock)
    store = JobStore.from_config(cfg, clock=clock, audit=audit)
    client = ClaudeClient(cfg, audit, state, runner=claude_runner, enabled=claude_enabled,
                          network_probe=network_probe)
    collectors: list[Collector] = [
        BrainCollector(),
        TaskCollector(task_base_dir),
        GitCollector(),
        GitHubCollector(),
        SystemCollector(audit, state, store=store, runner=command_runner),
    ]
    if cfg.digest.clickup_enabled:  # read at build time: turning the flag on needs a daemon restart
        collectors.append(ClickUpCollector(client, audit))
    # The local tier is off by default: then this is the configured router (the stub) and
    # local is None, exactly as before. Enabled, the router asks the local model and `local`
    # is the backend whose status() waits for a server that is down (spec 4b).
    local_runtime = build_local_runtime(cfg, audit)
    return Deps(cfg=cfg, audit=audit, state=state, store=store, vault=VaultWriter(cfg, audit), claude=client,
                router=local_runtime.router, local=local_runtime.gate_backend,
                notifier=notifier if notifier is not None else build_notifier(cfg, audit=audit),
                collectors=collectors, clock=clock or local_now, config_loader=config_loader)


# --- config reload -------------------------------------------------------------------------


def refresh_config(deps: Deps, mem: dict[str, Any] | None = None) -> bool:
    """Re-read the config files and apply a change in place. False means: do not run jobs.

    The runbook tells the owner to add a missed term to jarvis.local.toml and reset the breaker
    while the daemon keeps running, so the gates must see the edit. Every component holds the
    same Config object, so the new values are copied into it field by field; the router, which
    compiles its rules at construction, is rebuilt. An invalid file keeps the old config in
    memory but stops all job work (the audit says so once), because running on stale gates is
    exactly the failure this exists to prevent. Values other objects copied at construction
    (the budget and breaker settings in StateStore, queue and retention paths) still need a
    restart; the gates, the repo list, the work flag and the router rules do not.
    """
    loader = deps.config_loader
    if loader is None:
        return True
    memory = mem if mem is not None else {}
    try:
        fresh = loader()
        build_router(fresh)  # an unknown adapter name is a ConfigError, checked on every tick
    except ConfigError as exc:
        reason = str(exc)[:300]  # names of keys and tables only, never values
        if memory.get("config_invalid") != reason:
            deps.audit.emit("config_invalid", error=reason)
            memory["config_invalid"] = reason
        return False
    if memory.pop("config_invalid", None) is not None:
        deps.audit.emit("config_valid_again")
    cfg = deps.cfg
    if fresh.sha256 == cfg.sha256:
        return True
    old = cfg.sha256
    for name in type(cfg).model_fields:
        setattr(cfg, name, getattr(fresh, name))
    # Built only when something changed: the runtime owns the tier state and the registry entry,
    # and rebuilding it every tick would throw both away.
    local_runtime = build_local_runtime(deps.cfg, deps.audit)
    deps.router = local_runtime.router
    if deps.local is None or isinstance(deps.local, LlamaBackend):  # a test's own backend stays
        deps.local = local_runtime.gate_backend
    deps.audit.emit("config_reloaded", old_sha256=old, new_sha256=cfg.sha256)
    return True


# --- one tick ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class TickResult:
    """What a tick did. `exit_code` not None means the daemon must stop with that code."""

    action: str  # idle, ran, retry, failed, paused, busy, killed, task_disabled, config_invalid
    exit_code: int | None = None
    job_id: str | None = None


def _where(exc: BaseException) -> str:
    """Innermost frame as file:line, no message (a message can carry paths or content)."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return ""
    last = frames[-1]
    return f"{Path(last.filename).name}:{last.lineno}"


def _run_claimed(deps: Deps, job: Any, mode: str) -> TickResult:
    """Run one claimed job and apply the outcome. Never lets a job error escape."""
    audit, store = deps.audit, deps.store
    try:
        if job.kind == CONSOLIDATE_KIND:
            run_consolidate_job(job, deps, mode=mode)
        else:
            run_digest_job(job, deps, mode=mode)
    except Retry as retry:
        store.retry(job, retry.error, retry.delay, consume_attempt=retry.consume_attempt)
        if retry.killed:
            audit.emit("kill_file_seen", stage="job", job_id=job.id)
            return TickResult("killed", exit_code=3, job_id=job.id)
        return TickResult("retry", job_id=job.id)
    except Exception as exc:  # noqa: BLE001  the loop must survive any bug in a job
        audit.emit("job_failed", job_id=job.id, error=type(exc).__name__, where=_where(exc), unhandled=True)
        try:
            store.retry(job, f"unhandled:{type(exc).__name__}", UNHANDLED_DELAY)
        except Exception as inner:  # noqa: BLE001  recover_running() picks the job up at next start
            audit.emit("tick_error", stage="retry", error=type(inner).__name__, job_id=job.id)
        return TickResult("failed", job_id=job.id)
    return TickResult("ran", job_id=job.id)


def tick(deps: Deps, now: datetime, *, task_mode: bool = False, command_runner: CommandRunner | None = None,
         memory: dict[str, Any] | None = None) -> TickResult:
    """Kill and pause checks, task state, reconcile, then claim and run due jobs.

    `memory` is a small dict the caller keeps between ticks so a state that persists (a
    pause) is audited once, not every two minutes.
    """
    if not _TICK_LOCK.acquire(blocking=False):
        return TickResult("busy")
    try:
        return _tick_locked(deps, now, task_mode, command_runner, memory if memory is not None else {})
    finally:
        _TICK_LOCK.release()


def _tick_locked(deps: Deps, now: datetime, task_mode: bool, command_runner: CommandRunner | None,
                 mem: dict[str, Any]) -> TickResult:
    state, audit, store = deps.state, deps.audit, deps.store
    if state.killed():
        audit.emit("kill_file_seen", stage="tick")
        return TickResult("killed", exit_code=3)
    if task_mode and task_is_disabled(command_runner):
        audit.emit("killswitch_seen", task=TASK_NAME)
        return TickResult("task_disabled", exit_code=0)

    hb_mode = "task" if task_mode else "dev"
    state.heartbeat(None, mode=hb_mode)
    if not refresh_config(deps, mem):
        # Fail closed: stale or broken rules must not gate a job. The queue waits for a fix.
        return TickResult("config_invalid")
    if state.paused():
        if not mem.get("paused"):
            audit.emit("pause_seen")
            mem["paused"] = True
        return TickResult("paused")
    mem["paused"] = False

    try:
        reconcile(now, deps.cfg, state, store, audit)
    except Exception as exc:  # noqa: BLE001  a bad reconcile must not stop the claim of existing jobs
        audit.emit("tick_error", stage="reconcile", error=type(exc).__name__, where=_where(exc))
    try:
        reconcile_consolidation(now, deps.cfg, state, store, audit)
    except Exception as exc:  # noqa: BLE001  the nightly pass is optional; it must not stop the digest
        audit.emit("tick_error", stage="reconcile_consolidation", error=type(exc).__name__, where=_where(exc))

    last = TickResult("idle")
    for _ in range(MAX_JOBS_PER_TICK):
        job = store.claim_next(now)
        if job is None:
            break
        state.heartbeat(job.id, mode=hb_mode)
        last = _run_claimed(deps, job, "daemon")
        state.heartbeat(None, mode=hb_mode)
        if last.exit_code is not None:
            return last
    return last


# --- housekeeping -------------------------------------------------------------------------------


def _prune_runs(runs_dir: Path, now: datetime, keep_days: int) -> int:
    """Delete state/runs/<job> folders not touched for `keep_days` days."""
    if not runs_dir.is_dir():
        return 0
    cutoff = now.timestamp() - keep_days * 86400
    removed = 0
    for child in runs_dir.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 0 if child.exists() else 1
        except OSError:
            continue
    return removed


def housekeeping(deps: Deps, now: datetime) -> dict[str, int]:
    """Audit rotation and pruning, finished jobs, expired held references, old run folders."""
    cfg = deps.cfg
    rotated = 1 if deps.audit.rotate_if_due() else 0
    audit_pruned = len(deps.audit.prune(now))
    jobs_pruned = len(deps.store.prune(now))
    held_expired = len(deps.store.expire_held(now))
    runs_pruned = _prune_runs(deps.state.dir / "runs", now, cfg.retention.runs_days)
    result = {"audit_rotated": rotated, "audit_pruned": audit_pruned, "jobs_pruned": jobs_pruned,
              "held_expired": held_expired, "runs_pruned": runs_pruned}
    deps.audit.emit("housekeeping", **result)
    return result


def _tree_bytes(root: Path) -> int:
    total = 0
    for folder, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(folder) / name).stat().st_size
            except OSError:
                continue
    return total


def disk_check(deps: Deps) -> dict[str, Any]:
    """Size of logs, state and queue plus free space; flags what the digest should warn about."""
    cfg = deps.cfg
    logs_mb = _tree_bytes(cfg.paths.logs) / _BYTES_PER_MB
    state_mb = _tree_bytes(deps.state.dir) / _BYTES_PER_MB
    queue_mb = _tree_bytes(cfg.paths.queue) / _BYTES_PER_MB
    try:
        free_gb = shutil.disk_usage(cfg.paths.logs).free / _BYTES_PER_GB
    except OSError:
        free_gb = 0.0
    result: dict[str, Any] = {
        "logs_mb": round(logs_mb, 2), "state_mb": round(state_mb, 2), "queue_mb": round(queue_mb, 2),
        "free_gb": round(free_gb, 2),
        "logs_warn": logs_mb > cfg.retention.logs_warn_mb, "disk_warn": free_gb < cfg.retention.disk_warn_gb,
    }
    deps.audit.emit("disk_check", **result)
    return result


# --- heartbeat thread ------------------------------------------------------------------------------


class _Heartbeat:
    """Beats every `[daemon].heartbeat_seconds` from its own thread, so a long digest run
    does not make the daemon look dead."""

    def __init__(self, state: StateStore, interval: float, mode: str) -> None:
        self._state, self._interval, self._mode = state, interval, mode
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jarvisd-heartbeat", daemon=True)

    def _beat(self) -> None:
        try:
            self._state.heartbeat(None, mode=self._mode)
        except Exception:  # noqa: BLE001  a missed beat is not worth a crash
            pass

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._beat()

    def start(self) -> None:
        self._beat()
        self._thread.start()

    def stop(self) -> None:
        """Stop and join first, or a late beat would delete the clean-shutdown marker."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10.0)


# --- serve ---------------------------------------------------------------------------------------------


def _account() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001  getuser can fail with no environment
        return os.environ.get("USERNAME", "unknown")


def _startup_record(deps: Deps, task_mode: bool, previous: str, killed: bool, max_ticks: int | None) -> dict[str, Any]:
    report = None
    if task_mode and not killed:
        report = deps.claude.preflight()  # --version and --help only; never a paid call
    return {
        "mode": "task" if task_mode else "dev",
        "account": _account(),
        "claude_cli_version": report.version if report is not None else None,
        "claude_ok": report.ok if report is not None else None,
        "preflight": report.as_dict() if report is not None else None,
        "local_tier": local_state(deps.cfg),
        "config_sha256": deps.cfg.sha256,
        "previous_exit": previous,
        "daemon_version": __version__,
        "killed": killed,
        "max_ticks": max_ticks,
    }


def serve(cfg: Config, *, task_mode: bool, max_ticks: int | None = None, deps: Deps | None = None,
          sleep: Callable[[float], None] | None = None, command_runner: CommandRunner | None = None) -> int:
    """Run the daemon. Returns the exit code: 0 clean or already running, 3 killed.

    A crash propagates (the entry point's excepthook records it) and leaves no clean-shutdown
    marker, so the next start reports `unclean_previous_exit` and recovers any running job.
    """
    if deps is None:
        deps = build_deps(cfg, claude_enabled=task_mode, mirror_stdout=not task_mode)
    if not task_mode:
        deps.claude.enabled = False  # dev mode contract: no paid call, whatever the caller built
    state, audit = deps.state, deps.audit

    try:
        state.acquire_daemon_lock()
    except AlreadyRunning:
        audit.emit("daemon_refused", reason="already_running", mode="task" if task_mode else "dev")
        return 0

    stop = threading.Event()
    box = {"code": 0}
    heartbeat: _Heartbeat | None = None
    host: SchedulerHost | None = None
    clean = False
    try:
        previous = state.previous_exit()  # before the first heartbeat, which clears the marker
        killed = state.killed()
        audit.emit("daemon_start", **_startup_record(deps, task_mode, previous, killed, max_ticks))
        if previous == "unclean":
            audit.emit("unclean_previous_exit")
        if killed:
            audit.emit("kill_file_seen", stage="startup")
            clean = False  # leave the markers as they were; nothing ran
            return 3

        deps.store.recover_running()
        housekeeping(deps, deps.clock())
        heartbeat = _Heartbeat(state, cfg.daemon.heartbeat_seconds, "task" if task_mode else "dev")
        heartbeat.start()
        memory: dict[str, Any] = {}

        def run_tick() -> TickResult:
            result = tick(deps, deps.clock(), task_mode=task_mode, command_runner=command_runner, memory=memory)
            if result.exit_code is not None:
                box["code"] = result.exit_code
                stop.set()
            return result

        try:
            if max_ticks is not None:
                _bounded_loop(run_tick, stop, max_ticks, cfg.daemon.tick_seconds, sleep)
            else:
                host = SchedulerHost(
                    cfg, on_tick=run_tick, on_housekeeping=lambda: housekeeping(deps, deps.clock()),
                    on_disk_check=lambda: disk_check(deps),
                    on_error=lambda name, exc: audit.emit("tick_error", stage=name, error=type(exc).__name__,
                                                          where=_where(exc)),
                )
                host.start()
                while not stop.wait(1.0):  # short waits keep Ctrl-C deliverable on Windows
                    pass
            clean = True
        except KeyboardInterrupt:
            clean = True
        return box["code"]
    finally:
        if host is not None:
            host.stop(wait=clean)
        if heartbeat is not None:
            heartbeat.stop()
        if clean:
            state.mark_clean_shutdown()
            audit.emit("daemon_stop", exit_code=box["code"])
        state.release_daemon_lock()


def _bounded_loop(run_tick: Callable[[], TickResult], stop: threading.Event, max_ticks: int, seconds: float,
                  sleep: Callable[[float], None] | None) -> None:
    for number in range(max_ticks):
        run_tick()
        if stop.is_set() or number == max_ticks - 1:
            return
        (sleep or stop.wait)(seconds)
        if stop.is_set():
            return
