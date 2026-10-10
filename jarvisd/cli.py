"""Command line for jarvisd (design section 11).

`main(argv) -> int` with the documented exit codes: 0 ok, 1 failure, 2 usage, 3 refused by a
safety control (kill, pause, breaker, budget) or killed. All output is plain sentences with
exact paths. Nothing here prints a secret; `held` is the one place a held item's source path
is shown, and only on the terminal.

Collaborators (config, the `claude` runner, the notifier, the command runner for `schtasks`
and `powershell`, the clock) are keyword arguments of `main` so tests drive the real code
against a throwaway machine. A real run passes none of them.

Limits, stated plainly:
- `run-digest` enqueues its job and claims it with `claim_next`. If the daemon is down and an
  older job is also due, that older job is handed back and the command stops, rather than
  running somebody else's job.
- `status` reads the audit log for the last known Claude CLI version; it never spawns it.
- This module may append to `state/corrections.jsonl`, nothing else.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvisd import ROOT, __version__, consolidate, daemon, selftest
from jarvisd.audit import AuditLog
from jarvisd.collectors import compute_window
from jarvisd.common import iso, local_now, parse_iso
from jarvisd.config import Config, ConfigError, load_config
from jarvisd.digest import Deps, Retry, run_digest_job
from jarvisd import local as local_tier
from jarvisd.fsio import FileLock, append_line_locked
from jarvisd.jobstore import JobStore
from jarvisd.models import HistoryEntry, Job, JobWindow
from jarvisd.scheduler import DEADLINE_AFTER, DIGEST_KIND, due_at
from jarvisd.state import StateStore

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3
SHOULD_CHOICES = ("escalate", "hold", "skip", "other")
# Claude refusals that make a manual `--claude` run exit 3 (the note is still written).
REFUSAL_STATUSES = frozenset({"budget", "breaker"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,100}$")
_DURATION = re.compile(r"^(\d+)([smhd])$")
_DIGEST_FILE = re.compile(r"^digest-(\d{4}-\d{2}-\d{2})(?:-r(\d+))?\.md$")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

RUNBOOK = """Incident runbook (design section 16):
1. Stop the daemon: run JarvisKillSwitch (Daemon scope) or create state/KILL.
2. Read the trail: jarvis audit tail -n 100, then jarvis audit verify.
3. Read the payload archive (logs/payloads/, only if archive_payloads is on) and state/runs/<job>/.
4. Nothing needs rotating in v1, because no outward token exists.
5. Add the missed term or glob to jarvis.local.toml, write a session note by hand, then
   run jarvis breaker reset --reason TEXT when you are satisfied. The resident daemon re-reads
   jarvis.toml and jarvis.local.toml at the start of every tick (two minutes), so no restart is
   needed; an invalid file stops all job work until it is fixed (audit event config_invalid)."""


class UsageError(Exception):
    """Bad arguments. Reported as one line and exit code 2, never a traceback."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse would print usage and call sys.exit(2)
        raise UsageError(message)


@dataclass
class Ctx:
    """The collaborators a command may use. All None in a real run."""

    cfg: Config
    claude_runner: Any = None
    notifier: Any = None
    command_runner: Callable[[Sequence[str], float], tuple[int, str]] | None = None
    task_base_dir: Path | None = None
    network_probe: Callable[[], bool] | None = None
    clock: Callable[[], datetime] | None = None
    # Set only when the config came from the files (a real run). The resident loop uses it to
    # pick up edits to jarvis.local.toml without a restart.
    config_loader: Callable[[], Config] | None = None

    def now(self) -> datetime:
        return (self.clock or local_now)()

    def audit(self) -> AuditLog:
        return AuditLog(daemon.audit_path(self.cfg), self.cfg.retention.audit_max_bytes,
                        self.cfg.retention.audit_keep_days, clock=self.clock, mirror_stdout=False)

    def state(self) -> StateStore:
        return StateStore.from_config(self.cfg, clock=self.clock)

    def store(self, audit: AuditLog | None = None) -> JobStore:
        return JobStore.from_config(self.cfg, clock=self.clock, audit=audit)

    def deps(self, *, claude_enabled: bool, mirror_stdout: bool = False) -> Deps:
        return daemon.build_deps(
            self.cfg, claude_enabled=claude_enabled, mirror_stdout=mirror_stdout, clock=self.clock,
            claude_runner=self.claude_runner, notifier=self.notifier, command_runner=self.command_runner,
            task_base_dir=self.task_base_dir, network_probe=self.network_probe,
            config_loader=self.config_loader,
        )


# --- parser ------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="jarvis", description="JARVIS v1 option 2: the morning digest daemon (observe only).")
    parser.add_argument("--version", action="version", version=f"jarvisd {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("serve", help="resident loop (dev mode without --task)")
    p.add_argument("--task", action="store_true", help="task mode: Claude on, task-disabled check on")
    p.add_argument("--max-ticks", type=int, default=None, metavar="N", help="stop after N ticks")

    p = sub.add_parser("run-digest", help="build today's digest inline (no Claude unless --claude)")
    p.add_argument("--claude", action="store_true", help="allow the one Claude call for this run")
    p.add_argument("--date", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--no-notify", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="print the payload and the held list, write nothing")
    p.add_argument("--force", action="store_true", help="on an existing date write digest-<date>-r2.md")

    p = sub.add_parser("status", help="daemon, budget, queue and last digest")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("digest", help="print the latest digest, or its path")
    p.add_argument("--path", action="store_true")
    p.add_argument("--date", default=None, metavar="YYYY-MM-DD")

    p = sub.add_parser("held", help="resolve held ids to source and reason (terminal only)")
    p.add_argument("id", nargs="?", default=None)
    p.add_argument("--date", default=None, metavar="YYYY-MM-DD")

    p = sub.add_parser("wrong", help="record a mistake in a gate decision")
    p.add_argument("id", nargs="?", default=None)
    p.add_argument("--should", choices=SHOULD_CHOICES, default=None)
    p.add_argument("--note", default="")
    p.add_argument("--leak", action="store_true", help="a held item reached Claude: open the breaker")
    p.add_argument("--list", action="store_true", help="show recorded corrections")

    p = sub.add_parser("pause", help="stop enqueue and claim")
    p.add_argument("--for", dest="duration", default=None, metavar="4h", help="s, m, h or d suffix")
    p.add_argument("--reason", default="")
    sub.add_parser("resume", help="clear the pause")

    p = sub.add_parser("audit", help="tail, verify or cost")
    audit_sub = p.add_subparsers(dest="audit_command", required=True, metavar="action")
    q = audit_sub.add_parser("tail")
    q.add_argument("-n", type=int, default=30)
    q = audit_sub.add_parser("verify")
    q.add_argument("--all", action="store_true", help="walk rotated files too")
    q = audit_sub.add_parser("cost")
    q.add_argument("--days", type=int, default=7)

    p = sub.add_parser("breaker", help="status or reset")
    breaker_sub = p.add_subparsers(dest="breaker_command", required=True, metavar="action")
    breaker_sub.add_parser("status")
    q = breaker_sub.add_parser("reset")
    q.add_argument("--reason", required=True)

    p = sub.add_parser("local", help="local model tier: status or check (no model is ever downloaded)")
    local_sub = p.add_subparsers(dest="local_command", required=True, metavar="action")
    q = local_sub.add_parser("status", help="tier state and configuration, one health probe when enabled")
    q.add_argument("--json", action="store_true")
    q = local_sub.add_parser("check", help="conformance probes against the configured loopback server")
    q.add_argument("--json", action="store_true")

    p = sub.add_parser("hub", help="read-only cockpit on 127.0.0.1 (docs/hub.md); --check renders every view")
    p.add_argument("--check", action="store_true", help="render every view against the real state, exit 1 on any FAIL")
    p.add_argument("--port", type=int, default=None, metavar="N", help="listen port (default [hub].port)")

    p = sub.add_parser("ask", help="answer one question from the latest digest, RECENT.md and recent session notes")
    p.add_argument("question", nargs="+", help="the question; quotes are optional")
    p.add_argument("--dry-run", action="store_true", help="print the payload and the held list, spawn nothing")

    p = sub.add_parser("consolidate", help="propose memory candidates from the window's session notes (spec 6a)")
    p.add_argument("--claude", action="store_true", help="allow the one Claude call for this run")
    p.add_argument("--dry-run", action="store_true", help="print the payload and the held list, spawn nothing")
    p.add_argument("--date", default=None, metavar="YYYY-MM-DD")
    p.add_argument("--force", action="store_true", help="on an existing date write candidates-<date>-r2.md")

    p = sub.add_parser("propose", help="turn the latest digest run's cleared items into task proposals (spec 1c)")
    p.add_argument("--dry-run", action="store_true", help="print the payload and the held list, spawn nothing")
    p.add_argument("--force", action="store_true",
                   help="run although [propose].enabled is false, and again on a date that already ran")

    p = sub.add_parser("proposals", help="list the task proposals under state/proposals; confirm or reject one")
    p.add_argument("--all", action="store_true", help="include confirmed, edited and rejected proposals")
    proposals_sub = p.add_subparsers(dest="proposals_command", required=False, metavar="action")
    q = proposals_sub.add_parser("confirm", help="create the task in the configured tracker and mark the proposal confirmed")
    q.add_argument("id", help="the proposal id, as `jarvis proposals` lists it")
    q.add_argument("--title", default=None, help="edit: replace the title before confirming")
    q.add_argument("--project", default=None, help="edit: replace the project before confirming")
    q.add_argument("--due", default=None, metavar="YYYY-MM-DD", help="edit: set the due date before confirming")
    q.add_argument("--confirm-anyway", action="store_true",
                   help="create the task although an earlier attempt ended with an unknown outcome (it may duplicate)")
    q = proposals_sub.add_parser("reject", help="reject a proposal; the reason teaches the next proposals run")
    q.add_argument("id", help="the proposal id")
    q.add_argument("--reason", required=True, help="why (required)")

    p = sub.add_parser("clickup", help="ClickUp section: verify the claude -p flag set on this machine")
    clickup_sub = p.add_subparsers(dest="clickup_command", required=True, metavar="action")
    q = clickup_sub.add_parser("check", help="free checks; --live adds one real call (up to [digest].clickup_max_budget_usd)")
    q.add_argument("--live", action="store_true", help="make the one paid call against the real connector and record the result")
    q.add_argument("--json", action="store_true")

    p = sub.add_parser("tracker", help="tracker adapters (markdown default, ClickUp): readiness check, sends nothing")
    tracker_sub = p.add_subparsers(dest="tracker_command", required=True, metavar="action")
    tracker_sub.add_parser("check", help="adapter, token present yes/no, list map size, markdown path writable")

    p = sub.add_parser("weekly", help="write the weekly review for the ISO week just ended, through the vault writer")
    p.add_argument("--week", default=None, metavar="YYYY-Www", help="another week (default: the one just ended)")
    p.add_argument("--dry-run", action="store_true", help="print the note, write nothing")

    p = sub.add_parser("self-test", help="PASS/FAIL checks, exit 1 on any FAIL")
    p.add_argument("--live", action="store_true", help="add the paid smoke (not in this build)")

    p = sub.add_parser("install-task", help="print or run the Task Scheduler registration")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--unregister", action="store_true")
    return parser


# --- helpers -------------------------------------------------------------------------------------


def _parse_date(text: str | None, option: str = "--date") -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise UsageError(f"{option} must look like 2026-10-06") from exc


def _parse_duration(text: str) -> timedelta:
    match = _DURATION.match(text.strip().lower())
    if not match or int(match.group(1)) <= 0:
        raise UsageError("--for must look like 30m, 4h or 2d")
    return timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})


def _raw_dir(deps_or_cfg: Any) -> Path:
    """The digest folder, found through the vault writer so this module never names the vault root."""
    from jarvisd.vault import VaultWriter

    return VaultWriter(deps_or_cfg, _NullAudit()).raw_path("digest-0000-00-00.md").parent


class _NullAudit:
    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        return {}


def _digest_files(raw_dir: Path, day: date | None) -> list[Path]:
    found: list[tuple[str, int, Path]] = []
    if raw_dir.is_dir():
        for path in raw_dir.iterdir():
            match = _DIGEST_FILE.match(path.name)
            if match and (day is None or match.group(1) == day.isoformat()):
                found.append((match.group(1), int(match.group(2) or 1), path))
    return [p for _d, _r, p in sorted(found)]


# --- serve -----------------------------------------------------------------------------------------


def cmd_serve(ctx: Ctx, args: argparse.Namespace) -> int:
    if args.max_ticks is not None and args.max_ticks < 1:
        raise UsageError("--max-ticks must be at least 1")
    if not args.task:
        print("Dev mode: Claude is disabled and this process is not covered by the kill switch. "
              "Use serve --task for the real daemon.")
    deps = ctx.deps(claude_enabled=args.task, mirror_stdout=not args.task)
    return daemon.serve(ctx.cfg, task_mode=args.task, max_ticks=args.max_ticks, deps=deps,
                        command_runner=ctx.command_runner)


# --- run-digest -------------------------------------------------------------------------------------


def _manual_job_id(store: JobStore, day: date, force: bool, *, spare_base: bool = False) -> str | None:
    """The job id for a manual run. `spare_base` leaves digest-<date> free for the scheduled job."""
    base = f"digest-{day.isoformat()}"
    taken = store.exists(base) is not None
    if not taken and not spare_base:
        return base
    if taken and not force:
        return None
    number = 2
    while store.exists(f"{base}-r{number}") is not None:
        number += 1
    return f"{base}-r{number}"


def _manual_job(job_id: str, day: date, deps: Deps, args: argparse.Namespace) -> Job:
    now = deps.clock()
    start, end = compute_window(deps.state, deps.cfg, now)
    created = iso(now)
    return Job(
        id=job_id, kind=DIGEST_KIND, key=day.isoformat(), job_class="observe_only", latency_class="background_batch",
        origin="manual", created_at=created, not_before=created, deadline=iso(now + DEADLINE_AFTER),
        window=JobWindow(start=iso(start), end=iso(end)), config_sha256=deps.cfg.sha256 or None,
        params={"force": args.force, "dry_run": args.dry_run, "no_claude": not args.claude,
                "notify": not args.no_notify},
        history=[HistoryEntry(ts=created, from_state=None, to="pending", note="cli")],
    )


def _print_dry_run(day: date, result: dict[str, Any]) -> int:
    items = result["items"]
    print(f"Dry run for {day.isoformat()}. Nothing was written, no watermark moved, nothing was spawned.")
    print(f"Items gated: {items['gated']}. To Claude: {items['to_claude']}. Held: {items['held']}. "
          f"Over the size cap: {len(result['over_cap'])}.")
    print(f"Payload: {result['payload_bytes']} bytes, sha256 {result['payload_sha256'] or 'none'}.")
    print(result["payload"] or "(no payload: nothing is routed to Claude)")
    print("Held list:")
    for ref in result["held"]:
        print(f"  {ref['id']}  {ref['kind']}  {ref['hold_kind']}  {ref['reason']}")
    if not result["held"]:
        print("  none")
    for error in result["errors"]:
        print(f"Collector problem: {error}")
    if result["blocked"]:
        print(f"BLOCKED: the sealing step refused the payload ({result['blocked']}). Fix the cause before a real run.")
        return EXIT_REFUSED
    return EXIT_OK


def _print_result(result: dict[str, Any], claude_requested: bool) -> int:
    status = result["status"]
    if status == "noop":
        print(f"A note already exists at {result['note_path']}. Nothing was written. Use --force for a new one.")
        return EXIT_OK
    if status == "failed":
        print(f"The digest was not written: {result.get('reason', 'failed')}.")
        return EXIT_FAIL
    items = result["items"]
    print(f"Digest {status}: {result['note_path']}")
    print(f"Claude: {result['claude_status']}, {result['claude_calls']} call(s), cost {result['cost_usd']:.4f} USD.")
    print(f"Items collected {items['collected']}, to Claude {items['to_claude']}, held {items['held']}, "
          f"over the size cap {items['over_cap']}.")
    for error in result["errors"]:
        print(f"Problem: {error}")
    if claude_requested and result["claude_status"] in REFUSAL_STATUSES:
        print("Claude was refused by a safety control, so this note is deterministic only.")
        return EXIT_REFUSED
    return EXIT_OK


def cmd_run_digest(ctx: Ctx, args: argparse.Namespace) -> int:
    day = _parse_date(args.date) or ctx.now().date()
    # A dry run never enables the client: it builds the payload and stops.
    deps = ctx.deps(claude_enabled=args.claude and not args.dry_run)
    if args.dry_run:
        job = _manual_job(f"digest-{day.isoformat()}-dry", day, deps, args)
        try:
            result = run_digest_job(job, deps, mode="manual")
        except Retry as retry:
            print(f"Refused: {retry.error}.")
            return EXIT_REFUSED if retry.killed else EXIT_FAIL
        return _print_dry_run(day, result)

    # A run without --claude before the scheduled time must not take the scheduled job's id:
    # reconcile skips a date whose id exists, so the morning's Claude digest would never run.
    now = ctx.now()
    spare_base = not args.claude and day == now.date() and now < due_at(ctx.cfg, day, now.tzinfo)
    job_id = _manual_job_id(deps.store, day, args.force, spare_base=spare_base)
    if job_id is not None and spare_base and not job_id.endswith(day.isoformat()):
        print(f"Today's scheduled digest has not run yet, so this one is written as {job_id} "
              "and the scheduled job is left alone.")
    if job_id is None:
        print(f"A digest job for {day.isoformat()} already exists (state {deps.store.exists(f'digest-{day.isoformat()}')}). "
              "Use --force to write digest-<date>-r2.md.")
        return EXIT_FAIL
    job = _manual_job(job_id, day, deps, args)
    if not deps.store.enqueue(job):
        print(f"Job {job_id} was created by someone else a moment ago. Try again.")
        return EXIT_FAIL
    claimed = deps.store.claim_next(deps.clock())
    if claimed is None:
        print(f"Job {job_id} was claimed elsewhere (the daemon picked it up). Check jarvis status.")
        return EXIT_FAIL
    if claimed.id != job_id:
        deps.store.retry(claimed, "released_by_cli", 0, consume_attempt=False)
        print(f"Another job ({claimed.id}) is due and was handed back. Start the daemon or clear the queue, "
              f"then run this again. Job {job_id} stays pending.")
        return EXIT_FAIL
    return _run_claimed_manual(deps, claimed, args)


def _run_claimed_manual(deps: Deps, job: Job, args: argparse.Namespace) -> int:
    try:
        result = run_digest_job(job, deps, mode="manual")
    except Retry as retry:
        deps.store.retry(job, retry.error, retry.delay, consume_attempt=retry.consume_attempt)
        if retry.killed:
            print("Refused: state/KILL is present. The job is back in the queue; remove KILL first.")
            return EXIT_REFUSED
        print(f"The run was postponed ({retry.error}). The job {job.id} stays pending and the daemon retries it.")
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001  report, mark the job, never leave it running
        deps.audit.emit("job_failed", job_id=job.id, error=type(exc).__name__, unhandled=True)
        deps.store.fail(job, f"unhandled:{type(exc).__name__}")
        print(f"The digest failed with {type(exc).__name__}. See jarvis audit tail.")
        return EXIT_FAIL
    return _print_result(result, args.claude)


# --- status ------------------------------------------------------------------------------------------


def daemon_running(state: StateStore) -> bool:
    """Probe the single-instance lock: if we can take it, nobody holds it."""
    lock = FileLock(state.dir / "daemon.lock", timeout=0.0)
    try:
        won = lock.acquire(0.0)
    except OSError:
        return True
    if won:
        lock.release()
    return not won


def _last_cli_version(audit: AuditLog, now: datetime) -> str | None:
    for record in reversed(audit.records(since=now - timedelta(days=60), events=["daemon_start"])):
        if record.get("claude_cli_version"):
            return str(record["claude_cli_version"])
    return None


def _last_digest(store: JobStore, now: datetime) -> dict[str, Any] | None:
    best: tuple[str, Job] | None = None
    for job in store.jobs("done"):
        if job.kind != DIGEST_KIND or not job.result or job.result.get("status") == "noop":
            continue
        stamp = job.history[-1].ts if job.history else job.created_at
        if best is None or stamp > best[0]:
            best = (stamp, job)
    if best is None:
        return None
    stamp, job = best
    result = job.result or {}
    return {"job_id": job.id, "path": result.get("note_path"), "status": result.get("status"),
            "cost_usd": result.get("cost_usd", 0.0),
            "age_hours": round((now - parse_iso(stamp)).total_seconds() / 3600, 1)}


def collect_status(ctx: Ctx) -> dict[str, Any]:
    cfg, now = ctx.cfg, ctx.now()
    audit = ctx.audit()
    state, store = ctx.state(), ctx.store(audit)
    running = daemon_running(state)
    beat = state.read_heartbeat()
    age = None
    if beat is not None:
        try:
            age = round((now - parse_iso(str(beat["ts"]))).total_seconds(), 1)
        except (KeyError, ValueError):
            age = None
    today = now.date()
    next_day = today + timedelta(days=1) if store.exists(f"digest-{today.isoformat()}") else today
    watermark = state.watermark.get()
    seq, head = audit.head()
    return {
        "running": running,
        "pid": beat.get("pid") if (beat and running) else None,
        "heartbeat_age_s": age,
        "version": beat.get("version") if beat else __version__,
        "mode": beat.get("mode") if beat else None,
        "claude_cli_version": _last_cli_version(audit, now),
        "local_tier": local_tier.gate_state(cfg),
        "breaker": state.breaker.peek(),
        "budget": state.budget.snapshot(),
        "queue": store.counts(),
        "last_digest": _last_digest(store, now),
        "watermark": iso(watermark) if watermark else None,
        "next_due": iso(due_at(cfg, next_day, now.tzinfo)),
        "kill": state.killed(),
        "pause": state.pause_info() if state.paused() else None,
        "held_count": len(store.held()),
        "audit": {"seq": seq, "head": head},
    }


def _status_lines(data: dict[str, Any]) -> list[str]:
    budget, queue = data["budget"], data["queue"]
    lines = [
        "JARVIS daemon: " + ("running" if data["running"] else "stopped (nothing holds state/daemon.lock)"),
        ("Heartbeat: none yet" if data["heartbeat_age_s"] is None else
         f"Heartbeat: {data['heartbeat_age_s']} s ago, mode {data['mode']}, version {data['version']}"
         + (f", pid {data['pid']}" if data["pid"] else "")),
        f"Claude CLI: {data['claude_cli_version'] or 'not probed yet'}",
        f"Local tier: {data['local_tier']}",
        f"Breaker: {data['breaker']['state']}"
        + (f" ({data['breaker']['reason']})" if data["breaker"].get("reason") else ""),
        f"Budget today ({budget['date']}): {budget['spent_usd']:.4f} of {budget['daily_budget_usd']:.2f} USD spent, "
        f"{budget['calls']} of {budget['daily_calls']} calls",
        "Queue: " + ", ".join(f"{k} {v}" for k, v in queue.items()),
    ]
    last = data["last_digest"]
    lines.append("Last digest: none yet" if last is None else
                 f"Last digest: {last['path']} ({last['status']}, {last['age_hours']} h ago, {last['cost_usd']} USD)")
    lines.append(f"Watermark: {data['watermark'] or 'none yet'}")
    lines.append(f"Next digest due: {data['next_due']}")
    lines.append(f"Kill file: {'PRESENT' if data['kill'] else 'absent'}. "
                 f"Pause: {'on' if data['pause'] else 'off'}.")
    lines.append(f"Held references: {data['held_count']}")
    lines.append(f"Audit head: seq {data['audit']['seq']}, hash {str(data['audit']['head'])[:12]}")
    return lines


def cmd_status(ctx: Ctx, args: argparse.Namespace) -> int:
    data = collect_status(ctx)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        print("\n".join(_status_lines(data)))
    return EXIT_OK if data["running"] else EXIT_FAIL


# --- digest, held -----------------------------------------------------------------------------------------


def cmd_digest(ctx: Ctx, args: argparse.Namespace) -> int:
    day = _parse_date(args.date)
    files = _digest_files(_raw_dir(ctx.cfg), day)
    if not files:
        print("No digest found" + (f" for {day.isoformat()}." if day else " yet."))
        return EXIT_FAIL
    latest = files[-1]
    print(latest.as_posix() if args.path else latest.read_text(encoding="utf-8"))
    return EXIT_OK


def _print_held(ref: dict[str, Any]) -> None:
    print(f"{ref['id']}  {ref['kind']}  reason {ref['reason']}")
    print(f"  source: {ref['source_ref']}")
    print(f"  first seen {ref['first_seen']}, last seen {ref['last_seen']}, expires {ref['expires_at']}")
    print(f"  digests: {', '.join(ref.get('digest_ids', []))}")


def cmd_held(ctx: Ctx, args: argparse.Namespace) -> int:
    day = _parse_date(args.date)
    refs = ctx.store().held(day)
    if args.id:
        refs = [r for r in refs if r.get("id") == args.id]
        if not refs:
            print(f"No held reference with id {args.id}.")
            return EXIT_FAIL
    elif not refs:
        print("No held references.")
        return EXIT_OK
    for ref in refs:
        _print_held(ref)
    return EXIT_OK


# --- wrong ----------------------------------------------------------------------------------------------------


def _snapshot(audit: AuditLog, store: JobStore, item_id: str) -> dict[str, Any] | None:
    """What the gates decided about `item_id`, or None if the id is unknown everywhere."""
    snapshot: dict[str, Any] | None = None
    for record in audit.records(events=["gate_decision", "items_held"]):
        if record.get("item_id") == item_id:
            snapshot = {k: record.get(k) for k in (
                "route", "decided_by", "hold_kind", "reasons", "importance", "confidence", "category",
                "local_tier", "degraded", "confirm_required", "ts") if k in record}
        elif item_id in (record.get("ids") or []) and snapshot is None:
            snapshot = {"held": True, "ts": record.get("ts")}
    for ref in store.held():
        if ref.get("id") == item_id:
            snapshot = {**(snapshot or {}), "held": True, "kind": ref.get("kind"), "held_reason": ref.get("reason")}
    return snapshot


def _corrections_path(state: StateStore) -> Path:
    return state.dir / "corrections.jsonl"


def _list_corrections(state: StateStore) -> int:
    path = _corrections_path(state)
    rows = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if not rows:
        print("No corrections recorded.")
        return EXIT_OK
    for line in rows:
        row = json.loads(line)
        print(f"{row['ts']}  {row['id']}  should {row['should'] or 'unspecified'}"
              f"{'  LEAK' if row['leak'] else ''}  {row['note']}")
    return EXIT_OK


def cmd_wrong(ctx: Ctx, args: argparse.Namespace) -> int:
    state = ctx.state()
    if args.list:
        return _list_corrections(state)
    if not args.id or not _ID.match(args.id):
        raise UsageError("wrong needs the id of a held or gated item (or --list)")
    audit = ctx.audit()
    snapshot = _snapshot(audit, ctx.store(), args.id)
    if snapshot is None:
        print(f"Unknown id {args.id}: no gate decision or held reference mentions it.")
        return EXIT_USAGE
    row = {"ts": iso(ctx.now()), "id": args.id, "should": args.should, "note": args.note,
           "leak": bool(args.leak), "snapshot": snapshot}
    append_line_locked(_corrections_path(state), json.dumps(row, ensure_ascii=False))
    # The audit gets ids and flags only; the free-text note stays in the local file.
    audit.emit("correction", item_id=args.id, should=args.should, leak=bool(args.leak), note_chars=len(args.note))
    print(f"Recorded a correction for {args.id} in {_corrections_path(state).as_posix()}.")
    if args.leak:
        state.breaker.trip("leak_reported", requires_reset=True)
        audit.emit("breaker", action="trip", reason="leak_reported", requires_reset=True)
        print("The Claude breaker is now open and needs a human reset.")
        print(RUNBOOK)
    return EXIT_OK


# --- pause, resume, breaker, audit ----------------------------------------------------------------------------


def cmd_pause(ctx: Ctx, args: argparse.Namespace) -> int:
    state = ctx.state()
    until = state.now() + _parse_duration(args.duration) if args.duration else None
    state.set_pause(until, args.reason)
    ctx.audit().emit("pause_set", until=iso(until) if until else None, reason_chars=len(args.reason))
    print("Paused " + (f"until {iso(until)}." if until else "until jarvis resume.") + " Heartbeats continue.")
    return EXIT_OK


def cmd_resume(ctx: Ctx, args: argparse.Namespace) -> int:
    state = ctx.state()
    was = state.pause_info() is not None
    state.clear_pause()
    if was:
        ctx.audit().emit("pause_cleared")
    print("Resumed." if was else "Was not paused.")
    return EXIT_OK


def cmd_breaker(ctx: Ctx, args: argparse.Namespace) -> int:
    state = ctx.state()
    if args.breaker_command == "reset":
        state.breaker.reset(args.reason)
        ctx.audit().emit("breaker", action="reset", reason=args.reason[:200])
        print("Breaker reset.")
        return EXIT_OK
    peek = state.breaker.peek()
    print(f"Breaker: {peek['state']}")
    for key in ("reason", "opened_at", "until", "consecutive_failures", "probe_at"):
        if peek.get(key) not in (None, ""):
            print(f"  {key.replace('_', ' ')}: {peek[key]}")
    print(f"  requires human reset: {'yes' if peek.get('requires_human_reset') else 'no'}")
    return EXIT_OK


def cmd_audit(ctx: Ctx, args: argparse.Namespace) -> int:
    audit = ctx.audit()
    if args.audit_command == "tail":
        for record in audit.records()[-max(args.n, 0):]:
            print(json.dumps(record, ensure_ascii=False))
        return EXIT_OK
    if args.audit_command == "verify":
        paths = None if args.all else [audit.path]
        if not audit.path.exists() and not audit.rotated_files():
            print("Audit chain ok: no records yet.")
            return EXIT_OK
        ok, bad = audit.verify(paths)
        if not ok:
            print(f"Audit chain BROKEN at seq {bad}. Treat records from that seq on as untrusted.")
            return EXIT_FAIL
        seq, _head = audit.head()
        print(f"Audit chain ok: {len(paths) if paths else len(audit.all_files())} file(s), head seq {seq}.")
        return EXIT_OK
    today, total = ctx.now().date(), 0.0
    for offset in range(max(args.days, 1)):
        day = today - timedelta(days=offset)
        spent = audit.cost_on(day, ctx.now().tzinfo)
        total += spent
        print(f"{day.isoformat()}: {spent:.4f} USD")
    print(f"Total over {max(args.days, 1)} day(s): {total:.4f} USD")
    return EXIT_OK


# --- self-test and install-task --------------------------------------------------------------------------------------


def cmd_local(ctx: Ctx, args: argparse.Namespace) -> int:
    if args.local_command == "status":
        report = local_tier.status_report(ctx.cfg)
        if args.json:
            print(json.dumps(report, indent=2))
            return EXIT_OK
        print(f"Local tier: {report['state']}")
        print(f"Why: {_local_reason(report['reason'])}")
        probe = "not probed, the tier is off" if not report["probed"] else f"probed, {report['latency_ms']} ms"
        print(f"Server: {report['server']} ({probe})")
        print(f"Backend: {report['backend'] or 'none set'}. Router in use: {report['router']}.")
        print(f"API key variable: {report['api_key_env'] or 'none set'}. "
              f"Summaries of sensitive text: {'on' if report['summarize_sensitive'] else 'off'}.")
        if not report["enabled"]:
            print("Nothing is installed or contacted. docs/local-tier.md has the steps.")
        return EXIT_OK
    # check: runs whether or not the tier is enabled, so a server can be verified before it is switched on.
    audit = ctx.audit()
    results = local_tier.run_conformance(ctx.cfg, audit)
    failed = [r for r in results if not r.ok]
    if args.json:
        print(json.dumps([{"name": r.name, "ok": r.ok, "detail": r.detail, "ms": r.ms} for r in results], indent=2))
        return EXIT_FAIL if failed else EXIT_OK
    if not ctx.cfg.local.enabled:
        print("[local].enabled is false: this is a dry check. Dispatch will not use the server until you enable it.")
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.name}: {r.detail}" + (f" ({r.ms} ms)" if r.ms else ""))
    print("These probes check the shape of the answers, not their quality or speed. "
          "Run bin/bench.ps1 for throughput on this card.")
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed.")
    return EXIT_FAIL if failed else EXIT_OK


_LOCAL_REASONS = {
    "disabled": "[local].enabled is false, so the stub router runs and no local server is contacted.",
    "ok": "the server answers its health check.",
    "host_refused": "the configured host is not 127.0.0.1; the local tier only talks to loopback.",
    "api_key_missing": "[local].api_key_env names a variable that is not set in this environment.",
    "requests_failing": "the server is reachable but recent replies failed or broke the contract.",
}


def _local_reason(code: str) -> str:
    if code in _LOCAL_REASONS:
        return _LOCAL_REASONS[code]
    if code.startswith("health_"):
        return f"the server did not pass its health check ({code[7:]}); it may still be loading a model."
    if code.startswith("backend_not_"):
        return f"[local].backend must be \"{code[12:]}\"; nothing else is provided."
    return code


def cmd_hub(ctx: Ctx, args: argparse.Namespace) -> int:
    port = ctx.cfg.hub.port if args.port is None else args.port
    if not 1024 <= port <= 65535:
        raise UsageError("--port must be between 1024 and 65535")
    try:  # imported here so every other command works without the hub's two dependencies
        from jarvisd.hub import app as hub_app, check as hub_check
    except ImportError as exc:
        print(f"jarvis: the hub needs fastapi, uvicorn and (for --check) httpx2: {exc}. "
              "Install them with: pip install -r requirements.lock (pinned), or pip install \".[hub]\", "
              "or run deploy/setup-venv.ps1 on Windows.", file=sys.stderr)
        return EXIT_FAIL
    if args.check:
        results = hub_check.run_check(ctx.cfg, clock=ctx.clock)
        for line in hub_check.format_results(results):
            print(line)
        return EXIT_OK if all(r.ok for r in results) else EXIT_FAIL
    return hub_app.serve(ctx.cfg, port, clock=ctx.clock)


def cmd_ask(ctx: Ctx, args: argparse.Namespace) -> int:
    from jarvisd import ask  # imported here: ask pulls in the collectors and the digest wiring

    files = _digest_files(_raw_dir(ctx.cfg), None)
    return ask.cmd(ctx, args, files[-1] if files else None)


# --- consolidate -----------------------------------------------------------------------------------


def _manual_consolidate_id(store: JobStore, day: date, force: bool) -> str | None:
    base = f"consolidate-{day.isoformat()}"
    if store.exists(base) is None:
        return base
    if not force:
        return None
    number = 2
    while store.exists(f"{base}-r{number}") is not None:
        number += 1
    return f"{base}-r{number}"


def _consolidate_job(job_id: str, day: date, deps: Deps, args: argparse.Namespace) -> Job:
    now = deps.clock()
    start, end = consolidate.window_for(deps.cfg, deps.state, now)
    created = iso(now)
    return Job(
        id=job_id, kind=consolidate.CONSOLIDATE_KIND, key=day.isoformat(), job_class="observe_only",
        latency_class="background_batch", origin="manual", created_at=created, not_before=created,
        deadline=iso(now + DEADLINE_AFTER), window=JobWindow(start=iso(start), end=iso(end)),
        config_sha256=deps.cfg.sha256 or None,
        params={"force": args.force, "dry_run": args.dry_run, "no_claude": not args.claude, "notify": False},
        history=[HistoryEntry(ts=created, from_state=None, to="pending", note="cli")],
    )


def _print_consolidate_dry_run(day: date, result: dict[str, Any]) -> int:
    items = result["items"]
    print(f"Dry run for {day.isoformat()}. Nothing was written, no watermark moved, nothing was spawned.")
    print(f"Session notes read: {result['notes_read']}, withheld whole: {result['notes_withheld']}. "
          f"Lines gated: {items['gated']}. To Claude: {items['to_claude']}. Held: {items['held']}. "
          f"Over the size cap: {len(result['over_cap'])}.")
    print(f"Payload: {result['payload_bytes']} bytes, sha256 {result['payload_sha256'] or 'none'}.")
    print(result["payload"] or "(no payload: nothing is routed to Claude)")
    print("Held list:")
    for ref in result["held"]:
        print(f"  {ref['id']}  {ref['kind']}  {ref['hold_kind']}  {ref['reason']}")
    if not result["held"]:
        print("  none")
    for error in result["errors"]:
        print(f"Problem: {error}")
    if result["blocked"]:
        print(f"BLOCKED: the sealing step refused the payload ({result['blocked']}). Fix the cause before a real run.")
        return EXIT_REFUSED
    return EXIT_OK


def _print_consolidate_result(result: dict[str, Any]) -> int:
    status = result["status"]
    if status == "noop":
        print(f"A candidates note already exists at {result['note_path']}. Nothing was written. Use --force for a new one.")
        return EXIT_OK
    if status == "failed":
        print(f"The consolidation failed: {result.get('reason', 'failed')}. Nothing was written.")
        return EXIT_REFUSED if result["claude_status"] in REFUSAL_STATUSES else EXIT_FAIL
    if status == "no_items":
        print(f"Nothing in the window was cleared for Claude ({result['notes_read']} session note(s) read, "
              f"{result['items']['held']} held). No call was made and nothing was written.")
        return EXIT_OK
    print(f"Candidates written: {result['note_path']}")
    print(f"{result['candidates']} candidate(s) proposed from {result['notes_read']} session note(s); "
          f"{result['items']['held']} held, {sum(result['dropped'].values())} dropped by the checks.")
    print(f"Claude: {result['claude_status']}, {result['claude_calls']} call(s), cost {result['cost_usd']:.4f} USD.")
    for error in result["errors"]:
        print(f"Problem: {error}")
    return EXIT_OK


def cmd_consolidate(ctx: Ctx, args: argparse.Namespace) -> int:
    day = _parse_date(args.date) or ctx.now().date()
    if not args.dry_run and not args.claude:
        # No job is queued: a Claude-less run must not take the id the nightly job needs.
        print("Consolidation is one paid Claude call, and Claude is disabled for this run, so nothing was "
              "written. Use --dry-run to see the payload, or --claude to run it.")
        return EXIT_OK
    deps = ctx.deps(claude_enabled=args.claude and not args.dry_run)
    if args.dry_run:
        job = _consolidate_job(f"consolidate-{day.isoformat()}-dry", day, deps, args)
        try:
            result = consolidate.run_consolidate_job(job, deps, mode="manual")
        except Retry as retry:
            print(f"Refused: {retry.error}.")
            return EXIT_REFUSED if retry.killed else EXIT_FAIL
        return _print_consolidate_dry_run(day, result)
    job_id = _manual_consolidate_id(deps.store, day, args.force)
    if job_id is None:
        print(f"A consolidation job for {day.isoformat()} already exists "
              f"(state {deps.store.exists(f'consolidate-{day.isoformat()}')}). "
              "Use --force to write candidates-<date>-r2.md.")
        return EXIT_FAIL
    job = _consolidate_job(job_id, day, deps, args)
    if not deps.store.enqueue(job):
        print(f"Job {job_id} was created by someone else a moment ago. Try again.")
        return EXIT_FAIL
    claimed = deps.store.claim_next(deps.clock())
    if claimed is None:
        print(f"Job {job_id} was claimed elsewhere (the daemon picked it up). Check jarvis status.")
        return EXIT_FAIL
    if claimed.id != job_id:
        deps.store.retry(claimed, "released_by_cli", 0, consume_attempt=False)
        print(f"Another job ({claimed.id}) is due and was handed back. Start the daemon or clear the queue, "
              f"then run this again. Job {job_id} stays pending.")
        return EXIT_FAIL
    try:
        result = consolidate.run_consolidate_job(claimed, deps, mode="manual")
    except Retry as retry:
        deps.store.retry(claimed, retry.error, retry.delay, consume_attempt=retry.consume_attempt)
        if retry.killed:
            print("Refused: state/KILL is present. The job is back in the queue; remove KILL first.")
            return EXIT_REFUSED
        print(f"The run was postponed ({retry.error}). The job {claimed.id} stays pending and the daemon retries it.")
        return EXIT_FAIL
    except Exception as exc:  # noqa: BLE001  report, mark the job, never leave it running
        deps.audit.emit("job_failed", job_id=claimed.id, error=type(exc).__name__, unhandled=True)
        deps.store.fail(claimed, f"unhandled:{type(exc).__name__}")
        print(f"The consolidation failed with {type(exc).__name__}. See jarvis audit tail.")
        return EXIT_FAIL
    return _print_consolidate_result(result)


def cmd_propose(ctx: Ctx, args: argparse.Namespace) -> int:
    from jarvisd import propose  # imported here: it pulls in the collectors and the digest wiring

    return propose.cmd_propose(ctx, args)


def cmd_weekly(ctx: Ctx, args: argparse.Namespace) -> int:
    from jarvisd import weekly  # the same renderer and vault path the digest uses on the first run of a week

    return weekly.cmd_weekly(ctx, args)


def cmd_proposals(ctx: Ctx, args: argparse.Namespace) -> int:
    from jarvisd import inbox, propose

    if getattr(args, "proposals_command", None) in ("confirm", "reject"):
        return inbox.cmd_decide(ctx, args)  # the same functions the hub's Inbox forms call
    return propose.cmd_proposals(ctx, args)


def cmd_clickup(ctx: Ctx, args: argparse.Namespace) -> int:
    from jarvisd.claude import ClaudeClient
    from jarvisd.collectors import clickup

    audit = ctx.audit()
    client = ClaudeClient(ctx.cfg, audit, ctx.state(), runner=ctx.claude_runner, enabled=args.live,
                          network_probe=ctx.network_probe)
    return clickup.cmd_check(ctx.cfg, client, audit, ctx.now(), live=args.live, as_json=args.json)


def cmd_tracker(ctx: Ctx, args: argparse.Namespace) -> int:
    from jarvisd import tracker

    ready, lines = tracker.readiness(ctx.cfg, ctx.audit())
    print("\n".join(lines))
    return EXIT_OK if ready else EXIT_FAIL


def cmd_self_test(ctx: Ctx, args: argparse.Namespace) -> int:
    return selftest.run(ctx.cfg, live=args.live, runner=ctx.claude_runner, command_runner=ctx.command_runner,
                        out=print)


def registration_block() -> str:
    """The Task Scheduler registration of design section 10, with this checkout's paths."""
    py = str(ROOT / ".venv" / "Scripts" / "pythonw.exe")
    return "\n".join([
        '$user = "$env:USERDOMAIN\\$env:USERNAME"',
        f"$py   = '{py}'",
        f"$act  = New-ScheduledTaskAction -Execute $py -Argument '-m jarvisd serve --task' -WorkingDirectory '{ROOT}'",
        "$t1   = New-ScheduledTaskTrigger -AtLogOn -User $user",
        "$t2   = New-ScheduledTaskTrigger -Daily -At 06:00",
        "$t2.StartBoundary = (Get-Date -Hour 6 -Minute 0 -Second 0).ToString('yyyy-MM-dd\\THH:mm:ss')",
        "$set  = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 2)",
        "$prin = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited",
        f"Register-ScheduledTask -TaskName '{daemon.TASK_NAME}' -Action $act -Trigger @($t1,$t2) -Settings $set "
        "-Principal $prin -Description 'JARVIS v1 option 2 resident daemon (morning digest). Observe-only. "
        "Stopped and disabled by JarvisKillSwitch.' -Force",
    ])


def unregistration_block() -> str:
    return f"Unregister-ScheduledTask -TaskName '{daemon.TASK_NAME}' -Confirm:$false"


def cmd_install_task(ctx: Ctx, args: argparse.Namespace) -> int:
    block = unregistration_block() if args.unregister else registration_block()
    if not args.apply:
        print(block)
        print("\nThis was only printed. Add --apply to run it.")
        return EXIT_OK
    run = ctx.command_runner or daemon.run_command
    code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", block], 60.0)
    if out.strip():
        print(out.strip())
    print("Done." if code == 0 else f"PowerShell exited with {code}.")
    return EXIT_OK if code == 0 else EXIT_FAIL


HANDLERS: dict[str, Callable[[Ctx, argparse.Namespace], int]] = {
    "serve": cmd_serve, "run-digest": cmd_run_digest, "status": cmd_status, "digest": cmd_digest,
    "held": cmd_held, "wrong": cmd_wrong, "pause": cmd_pause, "resume": cmd_resume, "audit": cmd_audit,
    "breaker": cmd_breaker, "local": cmd_local, "ask": cmd_ask, "clickup": cmd_clickup, "tracker": cmd_tracker, "hub": cmd_hub, "self-test": cmd_self_test,
    "install-task": cmd_install_task, "consolidate": cmd_consolidate, "propose": cmd_propose,
    "proposals": cmd_proposals, "weekly": cmd_weekly,
}


def force_utf8_streams() -> None:
    """Make stdout and stderr UTF-8, whatever the console or the pipe would have chosen.

    A redirected stdout on Windows takes the locale code page (cp1252), and one arrow or emoji
    in a note then raised UnicodeEncodeError, for `ask` after the paid call was already made.
    `errors="replace"` keeps the command alive if a character still cannot be written. Streams
    that cannot be reconfigured (pythonw has none, test captures may be plain StringIO) stay as is.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main(argv: Sequence[str] | None = None, *, cfg: Config | None = None, claude_runner: Any = None,
         notifier: Any = None, command_runner: Callable[[Sequence[str], float], tuple[int, str]] | None = None,
         task_base_dir: Path | None = None, network_probe: Callable[[], bool] | None = None,
         clock: Callable[[], datetime] | None = None) -> int:
    force_utf8_streams()
    args_in = list(sys.argv[1:] if argv is None else argv)
    try:
        args = build_parser().parse_args(args_in)
    except UsageError as exc:
        print(f"jarvis: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as exc:  # --help and --version exit through argparse
        return int(exc.code or 0)
    loader: Callable[[], Config] | None = None
    if cfg is None:
        try:
            cfg = load_config()
        except ConfigError as exc:
            print(f"jarvis: the configuration is invalid and no job will run: {exc}", file=sys.stderr)
            return EXIT_FAIL
        loader = load_config
    ctx = Ctx(cfg, claude_runner, notifier, command_runner, task_base_dir, network_probe, clock, loader)
    try:
        return HANDLERS[args.command](ctx, args)
    except UsageError as exc:
        print(f"jarvis: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
