"""System collector (design section 8): the digest's System section and `jarvis status` lines.

Never Claude-bound. Every item carries `meta["render"] == "deterministic"` and its title is a
finished one-line fact, so the digest pipeline must keep these items out of gating and out
of the payload. They hold counts from our own audit log, the watchdog and kill switch logs,
and the scheduled-task states. No item text, no titles from other sources. The digest's
System section is rendered from `facts` (one "All green" line, or one line per anomaly, see
docs/hub-rework-contract.md section 1.2); the item titles stay for the CLI and the tests.
`LastTaskResult` is decoded through `TASK_RESULTS`, so a reader never meets a raw Windows code.

Sources, each degrading on its own to an "unavailable" line instead of failing the source:
- the hash-chained audit log, through `AuditLog.records(since=window_start)`;
- `logs/killswitch.jsonl` and `logs/watchdog.jsonl`, read through `tier.safe_read_text`
  (the collectors' only read primitive). A log over 16 MB is reported unavailable rather
  than truncated. The primitive withholds a whole file on one text-gate hit and decodes only
  UTF-8 (BOM and CRLF are fine). BOM-less UTF-16 survives that decode as NUL-interleaved
  text, so it is re-decoded here and gated line by line; a log withheld by the primitive is
  reported unavailable with the reason code, never silently as "could not be read";
- `Get-ScheduledTaskInfo` for the two brain jobs through an injectable runner;
- the circuit breaker state and the queue counts.

`gpu_yield` watchdog records are counted as noise (chrome is on the yield list) and are
not shown.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from jarvisd.collectors import CollectContext
from jarvisd.common import parse_iso, short_id
from jarvisd.models import CollectResult, Item, WithheldItem
from jarvisd.tier import safe_read_text, text_hit

if TYPE_CHECKING:
    # Type-only: the collector must not pull the queue or state modules into its import graph.
    from jarvisd.audit import AuditLog
    from jarvisd.state import StateStore


class QueueCounts(Protocol):
    """The one thing the collector needs from the job store."""

    def counts(self) -> dict[str, int]: ...


_TASK_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # also enforced by [digest].watched_tasks
MAX_LOG_BYTES = 16 * 1024 * 1024
RUNNER_TIMEOUT_S = 20.0

# (argv, timeout) -> (returncode, stdout)
Runner = Callable[[Sequence[str], float], tuple[int, str]]

# Windows Task Scheduler `LastTaskResult` values a reader may meet, decoded to a fixed phrase. Any
# other code prints as `0x%08X (unknown)`. 0 and 267009 (still running) are the only healthy ones.
TASK_RESULTS: dict[int, str] = {
    0: "ok",
    1: "script error",
    267009: "still running",
    267011: "never ran",
    267014: "stopped by the user",
    2147750687: "an instance was already running",
    2147943623: "cancelled",
    2147946720: "refused by the operator or administrator",
}
TASK_OK_CODES = frozenset({0, 267009})


def task_result_text(code: object) -> str:
    """`LastTaskResult` as words plus the hex code, for example `ok (0x00000000)`."""
    if isinstance(code, bool) or not isinstance(code, int):
        try:
            code = int(str(code).strip())
        except (TypeError, ValueError):
            return "unknown result"
    if code < 0 or code > 0xFFFFFFFF:
        return "unknown result"
    phrase = TASK_RESULTS.get(code)
    return f"{phrase} (0x{code:08X})" if phrase else f"0x{code:08X} (unknown)"


def task_result_ok(code: object) -> bool:
    return isinstance(code, int) and not isinstance(code, bool) and code in TASK_OK_CODES


_MS_DATE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")
# A task that never ran reports a date near 1999-11-30 or year 1 depending on the host.
_NEVER_BEFORE = datetime(2000, 1, 1, tzinfo=timezone.utc)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _default_runner(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603  list argv, constant task names, no shell
        list(argv),
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


def task_info_argv(task: str, allowed: Sequence[str]) -> list[str]:
    """The argv that queries one scheduled task. `allowed` is `[digest].watched_tasks`."""
    if task not in allowed or not _TASK_NAME.match(task):
        raise ValueError("not a task this collector may query")
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        f"Get-ScheduledTaskInfo -TaskName {task} | ConvertTo-Json -Compress",
    ]


def _from_epoch_ms(ms: float) -> datetime | None:
    """Epoch milliseconds to UTC. timedelta arithmetic, because fromtimestamp() rejects the
    large negative values Windows uses for 'never' on some builds."""
    try:
        return _EPOCH + timedelta(milliseconds=ms)
    except OverflowError:
        return None


def parse_task_time(value: Any) -> datetime | None:
    """Windows PowerShell 5.1 prints /Date(ms)/, PowerShell 7 prints an ISO string."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_epoch_ms(float(value))
    if not isinstance(value, str):
        return None
    m = _MS_DATE.fullmatch(value.strip())
    if m:
        return _from_epoch_ms(float(m.group(1)))
    try:
        return parse_iso(value)
    except ValueError:
        return None


LOG_FIELDS = ("ts", "event", "dry_run", "reason")
REASON_MAX = 80


@dataclass
class LogData:
    records: list[dict[str, Any]] = field(default_factory=list)
    malformed: int = 0
    withheld_lines: int = 0


def _decode_log(data: bytes) -> str | None:
    """UTF-8 (with or without BOM) or UTF-16 (BOM, or BOM-less LE/BE spotted by the NUL bytes
    that ASCII leaves in every second byte). None if the bytes are neither."""
    try:
        if data.startswith(b"\xef\xbb\xbf"):
            return data.decode("utf-8-sig")
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        if len(data) >= 2 and data[0] != 0 and data[1] == 0:
            return data.decode("utf-16-le")
        if len(data) >= 2 and data[0] == 0 and data[1] != 0:
            return data.decode("utf-16-be")
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _parse_log(text: str, cfg: Any, start: datetime, *, gate_lines: bool) -> LogData:
    """Records inside the window, projected to LOG_FIELDS. Unreadable lines are counted."""
    out = LogData()
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff").strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict):
                raise ValueError("not an object")
            ts = parse_iso(str(rec.get("ts", "")))
        except (ValueError, TypeError, RecursionError):
            out.malformed += 1
            continue
        hit = gate_lines and text_hit(line, cfg) is not None
        if hit:
            out.withheld_lines += 1
        if ts < start:
            continue
        kept = {k: rec[k] for k in LOG_FIELDS if k in rec}
        kept["ts"] = ts
        # The reason is free text on disk, so it is always gated on its own. The event name is
        # only re-checked when its line hit.
        reason = kept.get("reason")
        if isinstance(reason, str):
            reason = " ".join(reason.split())
            if not reason or text_hit(reason, cfg) is not None:
                kept.pop("reason")
            else:
                kept["reason"] = reason if len(reason) <= REASON_MAX else reason[:REASON_MAX] + "..."
        else:
            kept.pop("reason", None)
        if hit and (not isinstance(kept.get("event"), str) or text_hit(kept["event"], cfg) is not None):
            kept["event"] = "withheld"
        out.records.append(kept)
    return out


def _why(reason: str) -> str:
    if reason.startswith("term:") or reason in {
        "flag_sensitive_true", "tag_frontmatter", "tag_inline", "literal_telos_sensitive"
    }:
        return "was withheld by the sensitive text gate"
    return {"too_large": "is over the 16 MB cap", "undecodable": "is not UTF-8 text"}.get(
        reason, "could not be read")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _tokens(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def _candidates_line(done: list[dict[str, Any]], failed: list[dict[str, Any]]) -> str:
    """The consolidation line for the newest run in the window, or "" when none ran.

    Counts, a vault-relative path and an error code from our own audit; no candidate text.
    Quiet by design: a pass that never ran (switched off, or the client disabled) leaves no
    audit event and so no line, and a reader who never turned it on never sees it.
    """
    newest = max([*done, *failed], key=lambda r: int(r.get("seq", 0)), default=None)
    if newest is None:
        return ""
    if newest in failed:
        return f"Consolidation: failed ({str(newest.get('error', 'unknown'))[:40]}), no memory candidates tonight."
    count = int(newest.get("candidates") or 0)
    if count == 0:
        return f"Consolidation: no memory candidates ({int(newest.get('notes_read') or 0)} session notes read)."
    rel = str(newest.get("rel") or "")
    return (f"Consolidation: {_plural(count, 'memory candidate')} proposed in brain/{rel}, "
            "for your review, none promoted.")


class SystemCollector:
    """Collector named `system`."""

    name = "system"

    def __init__(
        self,
        audit: AuditLog,
        state: StateStore,
        *,
        store: QueueCounts | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.audit = audit
        self.state = state
        self.store = store
        self.runner = runner or _default_runner

    def collect(self, ctx: CollectContext) -> CollectResult:
        facts: dict[str, Any] = {}
        lines: list[tuple[str, str]] = []
        for key, part in (
            ("jobs", self._audit_lines),
            ("tasks", self._task_lines),
            ("logs", self._log_lines),
            ("queue", self._queue_lines),
        ):
            try:
                lines.extend(part(ctx, facts))
            except Exception as exc:  # noqa: BLE001  one broken part must not hide the others
                facts[f"{key}_error"] = type(exc).__name__
                lines.append((key, f"{key.capitalize()}: unavailable ({type(exc).__name__})."))
        items = [
            Item(
                id=short_id("system", key, text),
                source="system",
                kind="system_line",
                title=text,
                priority=5,
                meta={"render": "deterministic", "line": key},
            )
            for key, text in lines
        ]
        return CollectResult(source=self.name, ok=True, items=items, facts=facts)

    # --- audit -------------------------------------------------------------------------

    def _audit_lines(self, ctx: CollectContext, facts: dict[str, Any]) -> list[tuple[str, str]]:
        records = self.audit.records(since=ctx.window_start)
        by_event: dict[str, list[dict[str, Any]]] = {}
        for rec in records:
            by_event.setdefault(str(rec.get("event", "")), []).append(rec)

        def count(event: str) -> int:
            return len(by_event.get(event, []))

        calls = by_event.get("claude_call", [])
        cost = sum(float(r.get("total_cost_usd") or 0.0) for r in calls)
        tokens = 0
        for rec in calls:
            usage = rec.get("usage")
            if isinstance(usage, dict):
                tokens += sum(int(v) for v in usage.values() if isinstance(v, (int, float)) and not isinstance(v, bool))
        vault_writes = sum(1 for r in by_event.get("vault_write", []) if r.get("ok", True))
        breaker = str(self.state.breaker.peek().get("state", "unknown"))
        facts.update(
            jobs_done=count("job_done"),
            jobs_failed=count("job_failed"),
            claude_calls=len(calls),
            claude_cost_usd=round(cost, 4),
            claude_tokens=tokens,
            vault_writes=vault_writes,
            breaker_state=breaker,
            breaker_events=count("breaker"),
            daemon_starts=count("daemon_start"),
            daemon_crashes=count("daemon_crash"),
            unclean_exits=count("unclean_previous_exit"),
            config_invalid=count("config_invalid"),
            corrections=count("correction"),
        )
        out = [
            (
                "jobs",
                f"Jobs: {count('job_done')} done, {count('job_failed')} failed. "
                f"Claude calls: {len(calls)} (${cost:.2f}, {_tokens(tokens)} tokens). "
                f"Vault writes: {vault_writes}. Breaker: {breaker}.",
            ),
            (
                "daemon",
                f"Daemon starts since last digest: {count('daemon_start')}. "
                f"Unclean exits: {count('unclean_previous_exit')}. Breaker events: {count('breaker')}.",
            ),
        ]
        disk = by_event.get("disk_check", [])
        facts["disk_checked"] = bool(disk)
        if disk:
            last = disk[-1]
            disk_ok = bool(last.get("ok", True))
            verdict = "ok" if disk_ok else "WARNING"
            free = last.get("free_gb")
            facts["disk_ok"] = disk_ok
            facts["disk_free_gb"] = free if isinstance(free, (int, float)) and not isinstance(free, bool) else None
            out.append(("disk", f"Disk check: {verdict}" + (f", {free} GB free." if free is not None else ".")))
        else:
            out.append(("disk", "Disk check: no record in this window."))
        if count("correction"):
            out.append(("corrections", f"Corrections logged: {count('correction')}."))
        line = _candidates_line(by_event.get("consolidate_done", []), by_event.get("consolidate_failed", []))
        if line:
            out.append(("candidates", line))
            facts["consolidation"] = line
        return out

    # --- scheduled tasks ---------------------------------------------------------------

    def _task_lines(self, ctx: CollectContext, facts: dict[str, Any]) -> list[tuple[str, str]]:
        out = []
        tasks: dict[str, Any] = {}
        watched = ctx.cfg.digest.watched_tasks
        for task in watched:
            tasks[task] = {"available": False}
            try:
                code, stdout = self.runner(task_info_argv(task, watched), RUNNER_TIMEOUT_S)
                info = json.loads(stdout) if code == 0 and stdout.strip() else None
            except (OSError, ValueError, subprocess.SubprocessError):
                info = None
            if not isinstance(info, dict):
                out.append((task, f"{task}: unavailable (could not query the scheduled task)."))
                continue
            last = parse_task_time(info.get("LastRunTime"))
            result = info.get("LastTaskResult")
            decoded = task_result_text(result)
            tasks[task] = {"available": True, "last_result": result, "last_result_text": decoded,
                           "ok": task_result_ok(result)}
            if last is None or last < _NEVER_BEFORE:
                detail = decoded if result == 267011 else f"never ran, last result {decoded}"
                out.append((task, f"{task}: {detail}."))
                continue
            tasks[task]["last_run"] = last.astimezone(ctx.now.tzinfo).isoformat(timespec="minutes")
            stamp = last.astimezone(ctx.now.tzinfo).strftime("%Y-%m-%d %H:%M")
            tasks[task]["last_run_text"] = stamp
            out.append((task, f"{task}: last run {stamp}, {decoded}."))
        facts["scheduled_tasks"] = tasks
        return out

    # --- watchdog and kill switch logs -------------------------------------------------

    def _read_log(self, ctx: CollectContext, name: str) -> LogData | str:
        """Records inside the window (empty if the log does not exist), or the reason it is unreadable."""
        logs = Path(ctx.cfg.paths.logs)
        path = logs / name
        if not path.is_file():
            return LogData()
        text = safe_read_text(path, ctx.cfg, [logs], max_bytes=MAX_LOG_BYTES)
        if isinstance(text, WithheldItem):
            return text.reason
        if "\x00" not in text:
            return _parse_log(text, ctx.cfg, ctx.window_start, gate_lines=False)
        # BOM-less UTF-16 decoded as UTF-8: the primitive scanned NUL-interleaved text, which
        # proves nothing. Recover the original bytes, decode properly and gate every line.
        decoded = _decode_log(text.encode("utf-8"))
        if decoded is None:
            return "undecodable"
        return _parse_log(decoded, ctx.cfg, ctx.window_start, gate_lines=True)

    def _log_lines(self, ctx: CollectContext, facts: dict[str, Any]) -> list[tuple[str, str]]:
        names = ("killswitch.jsonl", "watchdog.jsonl")
        kill, dog = (self._read_log(ctx, n) for n in names)
        failed = {n: d for n, d in zip(names, (kill, dog)) if isinstance(d, str)}
        if failed:
            facts["logs_available"] = False
            facts["logs_unavailable"] = failed
            why = "; ".join(f"{n} {_why(r)}" for n, r in failed.items())
            return [("watch", f"Kill switch and watchdog logs: unavailable ({why}).")]
        assert isinstance(kill, LogData) and isinstance(dog, LogData)

        def events(data: LogData, event: str) -> list[dict[str, Any]]:
            return [r for r in data.records if r.get("event") == event]

        real_trips = [r for r in events(kill, "killswitch") if r.get("dry_run") is not True]
        dry_runs = sum(1 for r in events(kill, "killswitch") if r.get("dry_run") is True)
        last = max(real_trips, key=lambda r: r["ts"], default=None)
        last_reason = last.get("reason") if last else None
        # Watchdog trip requests are separate from kill switch records: the script a request
        # starts writes its own killswitch.jsonl record, so summing would count one trip twice.
        requests = len(events(dog, "killswitch_trip"))
        loops = len(events(dog, "health_crashloop"))
        restarts = len(events(dog, "restart_attempt"))
        malformed = kill.malformed + dog.malformed
        facts.update(
            logs_available=True,
            killswitch_trips=len(real_trips),
            killswitch_dry_runs=dry_runs,
            killswitch_last_reason=last_reason,
            watchdog_trip_requests=requests,
            watchdog_crashloops=loops,
            restart_attempts=restarts,
            restarts_skipped=len(events(dog, "restart_skipped")),
            gpu_yield_noise=len(events(dog, "gpu_yield")),
            log_malformed_lines=malformed,
            log_withheld_lines=kill.withheld_lines + dog.withheld_lines,
        )
        text = (
            f"Kill switch: {_plural(len(real_trips), 'trip')}"
            + (f" (last: {last_reason})" if last_reason else "")
            + f", {_plural(dry_runs, 'dry run')}. "
            f"Watchdog: {_plural(loops, 'crashloop')}, {_plural(restarts, 'restart attempt')}, "
            f"{_plural(requests, 'kill switch request')}."
        )
        if malformed:
            text += f" Skipped {_plural(malformed, 'unreadable log line')}."
        return [("watch", text)]

    # --- queue -------------------------------------------------------------------------

    def _queue_lines(self, ctx: CollectContext, facts: dict[str, Any]) -> list[tuple[str, str]]:
        if self.store is None:
            return []
        counts = self.store.counts()
        facts["queue"] = dict(counts)
        order = ("pending", "running", "done", "failed", "held")
        return [("queue", "Queue: " + ", ".join(f"{k} {counts.get(k, 0)}" for k in order) + ".")]

