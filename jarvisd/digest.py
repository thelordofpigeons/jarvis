"""The morning_digest job handler (design section 3, steps 1 to 8).

collect -> gate -> seal -> summarize (one Claude call) -> render -> write -> notify -> finish.
Layer L3: this module wires collectors, gates, the Claude client, the renderer, the vault
writer and the notifier together; it holds no policy of its own. Every decision about what
may leave the machine is made by `dispatch` and `tier`, and this module only passes their
results along. It never reads a held reference back and never builds Claude input by hand.

Outcomes of `run_digest_job`:
- a dict, and the job is `done` (note written, or a deterministic note after Claude failed);
- the same with status `noop`, when the date already has a note and the job is not forced;
- the same with status `failed`, when the vault writer refuses (the job is `failed`);
- the same with status `dry_run`, which touches no queue, vault, watermark or Claude state;
- `Retry`, for a failure that a later attempt may fix. The caller applies it with
  `JobStore.retry(job, retry.error, retry.delay, consume_attempt=retry.consume_attempt)`,
  and exits the daemon when `retry.killed` is set. A retry leaves no note and sends no toast.
  A busy or broken vault is such a failure until the last attempt; then the job fails with a
  toast and a copy of the rendered digest under `state/runs/<job>/digest-unwritten.md`,
  because a third silent failure would leave the reader with nothing.

Limits, stated plainly:
- A summary that Claude already produced is kept in `state/runs/<job>/summary.json` when the
  vault write fails, and reused by the next attempt if its payload hash is unchanged, so a
  busy Obsidian or Syncthing costs minutes, not three paid calls. A changed payload is paid for.
- One Claude call per job attempt. The design allows a second in-call attempt after 20 s for
  `timeout` and `transient`; here the job-level backoff (10 then 30 minutes, 3 attempts)
  does that work, so a bad night costs at most 3 calls, never 6.
- `payload_sealed` carries no `call_id`: the id is made inside `ClaudeClient.complete`. The
  `payload_sha256` in `claude_intent` ties the two records together.
- A watermark advance is not atomic with the vault write. A crash between them means the next
  window starts early, so items repeat; none are lost.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from jarvisd import render
from jarvisd.audit import AuditLog
from jarvisd.claude import ClaudeClient, ClaudeUnavailable, make_header
from jarvisd.collectors import CollectContext, Collector, compute_window, run_collectors
from jarvisd.collectors.brain import BrainCollector
from jarvisd.collectors.clickup import ClickUpCollector
from jarvisd.collectors.git import GitCollector
from jarvisd.collectors.github import GitHubCollector
from jarvisd.collectors.system import SystemCollector
from jarvisd.collectors.task import TaskCollector
from jarvisd.common import iso, local_now, parse_iso
from jarvisd.config import Config
from jarvisd.dispatch import GatedPayload, LocalBackend, PayloadBlocked, clear_for_claude, local_state, run_gates
from jarvisd.fsio import FileBusy, atomic_write_text
from jarvisd.jobstore import JobStore
from jarvisd.models import (
    CollectResult,
    DigestSummary,
    GateResult,
    Item,
    Job,
    LocalTier,
    RunManifest,
    WithheldItem,
)
from jarvisd.notify import Notifier, NotifyResult, digest_message
from jarvisd.router import Router
from jarvisd.state import StateStore
from jarvisd.vault import VaultWriteDenied, VaultWriter, WriteResult

PURPOSE = "digest"
RETRY_DELAYS = (timedelta(minutes=10), timedelta(minutes=30))  # after attempt 1, after attempt 2
NETWORK_DELAY = timedelta(minutes=10)
VAULT_BUSY_DELAY = timedelta(minutes=2)
VAULT_ERROR_DELAY = timedelta(minutes=5)
LATE_AFTER = time(12, 0)
# Claude statuses that are not a failure: the digest is complete without a Claude call.
QUIET_STATUSES = frozenset({"ok", "disabled", "no_items"})
# Failures that need a human, shown with the "breaker" toast.
BREAKER_STATUSES = frozenset({"breaker", "isolation_anomaly", "isolation_breach", "payload_blocked"})
_RUN_SUFFIX = re.compile(r"-r(\d+)$")
# A numbered Start here line (grammar 2: `N. Why [id]`), counted for the toast.
_ATTENTION_LINE = re.compile(r"^\d+\. ")


class Retry(Exception):
    """Ask the caller to put the job back in the queue. str() is the error code.

    `consume_attempt` False hands back the attempt taken at claim time (no network, killed).
    `killed` means the kill file is present: put the job back and stop the daemon.
    """

    def __init__(self, error: str, delay: timedelta = timedelta(0), *, consume_attempt: bool = True,
                 killed: bool = False) -> None:
        super().__init__(error)
        self.error = error
        self.delay = delay
        self.consume_attempt = consume_attempt
        self.killed = killed


@dataclass
class Deps:
    """Everything a digest run needs. Built once by the composition root (daemon.build_deps)."""

    cfg: Config
    audit: AuditLog
    state: StateStore
    store: JobStore
    vault: VaultWriter
    claude: ClaudeClient
    router: Router
    notifier: Notifier
    collectors: Sequence[Collector] | None = None  # None means default_collectors(...)
    clock: Callable[[], datetime] = local_now
    local: LocalTier | LocalBackend | None = None  # None reads [local] from the config
    # Re-reads the config files. The daemon calls it at the start of every tick so an edit to
    # jarvis.local.toml (a new sensitive term after `jarvis wrong --leak`) takes effect without
    # a restart. None for tests and one-shot commands, which load the config once anyway.
    config_loader: Callable[[], Config] | None = None


def default_collectors(audit: AuditLog, state: StateStore, store: JobStore,
                       task_base_dir: Path | None = None, claude: ClaudeClient | None = None) -> list[Collector]:
    """The v1 collectors in the order their sections appear. GitHub switches itself off through config.

    ClickUp is added only when `[digest].clickup_enabled` is on and a client is given.
    """
    found: list[Collector] = [BrainCollector(), TaskCollector(task_base_dir), GitCollector(), GitHubCollector(),
                              SystemCollector(audit, state, store=store)]
    if claude is not None and claude.cfg.digest.clickup_enabled:
        found.append(ClickUpCollector(claude, audit))
    return found


# --- run state -------------------------------------------------------------------------


@dataclass
class _Outcome:
    """What the summarize step produced. `summary` None means the deterministic fallback."""

    summary: DigestSummary | None = None
    claude_status: str = "ok"
    hallucinated_ids: int = 0
    tier_violation_seq: int | None = None
    failure: str = ""


@dataclass
class _Run:
    job: Job
    deps: Deps
    mode: str
    now: datetime
    day: date
    window: tuple[datetime, datetime]
    filename: str
    stages: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def audit(self) -> AuditLog:
        return self.deps.audit

    @property
    def dry(self) -> bool:
        return self.job.params.dry_run


def _job_day(job: Job, now: datetime) -> date:
    try:
        return date.fromisoformat(job.key)
    except ValueError:
        return now.date()


def _window(job: Job, deps: Deps, now: datetime) -> tuple[datetime, datetime]:
    if job.window is not None:
        start, end = parse_iso(job.window.start), parse_iso(job.window.end)
    else:
        start, end = compute_window(deps.state, deps.cfg, now)
    return start.astimezone(now.tzinfo), end.astimezone(now.tzinfo)


def _filename(job: Job, day: date) -> str:
    match = _RUN_SUFFIX.search(job.id)
    return render.digest_filename(day, int(match.group(1)) if match else 1)


def _past_deadline(job: Job, now: datetime) -> bool:
    return job.deadline is not None and now >= parse_iso(job.deadline)


def _final_attempt(job: Job, now: datetime) -> bool:
    return job.attempts >= job.max_attempts or _past_deadline(job, now)


# --- steps 1 and 2: collect and gate ----------------------------------------------------


def _collect(run: _Run) -> list[CollectResult]:
    deps = run.deps
    collectors = deps.collectors
    if collectors is None:
        collectors = default_collectors(deps.audit, deps.state, deps.store, claude=deps.claude)
    ctx = CollectContext(cfg=deps.cfg, window_start=run.window[0], window_end=run.window[1], now=run.now,
                         job_id=run.job.id)
    results = run_collectors(ctx, collectors)
    for res in results:
        run.audit.emit("collector_result", job_id=run.job.id, source=res.source, ok=res.ok, error=res.error,
                       items=len(res.items), withheld=len(res.withheld), duration_ms=res.duration_ms)
        if not res.ok:
            run.errors.append(f"{res.source}: {res.error or 'failed'}")
    run.stages["collect"] = "ok" if all(r.ok for r in results) else "partial"
    return results


def _gateable(results: Sequence[CollectResult]) -> list[Item]:
    """Distinct items that are candidates for Claude. Deterministic system lines never are."""
    seen: set[str] = set()
    items: list[Item] = []
    for res in results:
        for item in res.items:
            if render.is_deterministic(item) or item.id in seen:
                continue
            seen.add(item.id)
            items.append(item)
    return items


def _held_ref(item: Item, result: GateResult) -> WithheldItem:
    """A content-free reference for an item the gates did not send to Claude.

    `source_ref` is a local path or a short pointer, kept for `jarvis held` only; the renderer
    prints the id and the reason code, nothing else.
    """
    kind = result.hold_kind or ("sensitive" if result.decided_by == "tier" else "policy")
    ref = item.paths[0] if item.paths else f"{item.source}:{item.kind}:{item.id}"
    return WithheldItem(id=item.id, kind=item.kind, source_ref=ref,
                        reason=result.reasons[0] if result.reasons else "held", hold_kind=kind)


def _gate(run: _Run, results: Sequence[CollectResult]) -> tuple[list[Item], list[GateResult], list[WithheldItem], LocalTier]:
    deps = run.deps
    items = _gateable(results)
    gates = run_gates(items, deps.router, deps.cfg, deps.local, run.audit)
    if gates:
        state: LocalTier = gates[0].local_tier
    else:
        state = deps.local if isinstance(deps.local, str) else local_state(deps.cfg)  # type: ignore[assignment]
    held: dict[str, WithheldItem] = {}
    for res in results:
        for ref in res.withheld:
            held.setdefault(ref.id, ref)
    for item, gate in zip(items, gates):
        if gate.route != "claude":
            held.setdefault(item.id, _held_ref(item, gate))
    refs = list(held.values())
    reasons = Counter(r.reason for r in refs)
    run.audit.emit("items_held", job_id=run.job.id, count=len(refs), ids=[r.id for r in refs],
                   reasons=dict(reasons))
    if not run.dry:
        _record_holds(run, refs)
    run.stages["gate"] = "ok"
    return items, gates, refs, state


def _record_holds(run: _Run, refs: Sequence[WithheldItem]) -> None:
    for ref in refs:
        try:
            run.deps.store.hold(ref, run.job.id)
        except Exception as exc:  # noqa: BLE001  a held reference is bookkeeping, not the deliverable
            run.errors.append(f"hold: {type(exc).__name__}")


def _aggregate(run: _Run, gates: Sequence[GateResult], refs: Sequence[WithheldItem], state: LocalTier) -> None:
    """Fill the job's gate aggregates: router, tier, importance (max), confidence (min), sensitive (any)."""
    job = run.job
    decisions = [g.decision for g in gates if g.decision is not None]
    rank = {"low": 0, "med": 1, "high": 2}
    job.router = run.deps.cfg.router.adapter
    routes = {g.route for g in gates}
    job.tier = "claude" if "claude" in routes else ("local" if "local" in routes else "held")
    job.importance = max((d.importance for d in decisions), key=rank.__getitem__) if decisions else None
    job.confidence = min((d.confidence for d in decisions), default=None)
    job.sensitive = any(r.hold_kind == "sensitive" for r in refs) or any(d.sensitive for d in decisions)
    job.local_tier = state
    job.config_sha256 = run.deps.cfg.sha256 or job.config_sha256


# --- steps 3 and 4: seal and summarize ---------------------------------------------------


def _claude_wanted(run: _Run) -> bool:
    return not run.job.params.no_claude and bool(getattr(run.deps.claude, "enabled", False))


def _violation_seq(run: _Run) -> int | None:
    """Sequence number of this job's newest tier_violation record, for the loud line."""
    mine = [r for r in run.audit.records(events=["tier_violation"]) if r.get("job_id") == run.job.id]
    return int(mine[-1]["seq"]) if mine else None


def _block(run: _Run, exc: PayloadBlocked) -> _Outcome:
    """Gate 1 fired at sealing: audit it, open the breaker for a human, render the loud line."""
    reason = f"payload_blocked:{exc.hit.code}"
    rec = run.audit.emit("tier_violation", job_id=run.job.id, code=exc.hit.code, item_id=exc.item_id, stage="digest")
    run.deps.state.breaker.trip(reason, requires_reset=True)
    run.audit.emit("breaker", job_id=run.job.id, action="trip", reason=reason, requires_reset=True)
    return _Outcome(claude_status="payload_blocked", tier_violation_seq=int(rec.get("seq", 0)) or None,
                    failure="payload_blocked")


def _seal(run: _Run, items: Sequence[Item], gates: Sequence[GateResult]) -> tuple[GatedPayload | None, _Outcome]:
    """Seal the claude-routed items. Returns (payload, outcome); outcome is final when payload is None."""
    if not any(g.route == "claude" for g in gates):
        run.stages["seal"] = "skipped"
        return None, _Outcome(claude_status="no_items")
    if not run.dry and not _claude_wanted(run):
        run.stages["seal"] = "skipped"
        return None, _Outcome(claude_status="disabled")
    try:
        payload = clear_for_claude(items, gates, run.deps.cfg)
    except PayloadBlocked as exc:
        run.stages["seal"] = "blocked"
        if run.dry:
            return None, _Outcome(claude_status="payload_blocked", failure=exc.hit.code)
        return None, _block(run, exc)
    run.stages["seal"] = "ok"
    if not run.dry:
        run.audit.emit("payload_sealed", job_id=run.job.id, sha256=payload.sha256, bytes=payload.byte_size,
                       item_count=len(payload.item_ids), truncated=payload.truncated, over_cap=len(payload.over_cap))
    if not payload.item_ids:
        return payload, _Outcome(claude_status="no_items")
    return payload, _Outcome()


def _status_of(kind: str) -> str:
    # The design table calls both of these "unavailable"; the other kinds are already clear.
    return "unavailable" if kind in ("timeout", "transient") else kind


def _summarize(run: _Run, payload: GatedPayload) -> _Outcome:
    """The one Claude call. Raises Retry for a failure a later attempt may fix."""
    job, deps = run.job, run.deps
    reused = _load_stash(run, payload)
    if reused is not None:
        run.audit.emit("claude_summary_reused", job_id=job.id, payload_sha256=payload.sha256)
        return reused
    header = make_header(run.day.isoformat(), iso(run.window[0]), iso(run.window[1]))
    try:
        reply = deps.claude.complete(payload, PURPOSE, 1, header=header, job_id=job.id)
    except ClaudeUnavailable as exc:
        return _handle_unavailable(run, exc)
    except (PayloadBlocked, TypeError):
        # The client already audited the violation and opened the breaker (it sees the final prompt).
        return _Outcome(claude_status="payload_blocked", tier_violation_seq=_violation_seq(run),
                        failure="payload_blocked")
    if reply.summary is None:
        return _Outcome(claude_status="bad_schema", failure="bad_schema")
    return _Outcome(summary=reply.summary, claude_status="ok", hallucinated_ids=reply.hallucinated_ids)


def _stash_path(run: _Run) -> Path:
    return run.deps.state.dir / "runs" / run.job.id / "summary.json"


def _stash_summary(run: _Run, payload: GatedPayload | None, outcome: _Outcome) -> None:
    """Keep a paid-for summary across a failed vault write. Best effort; never fails the job."""
    if payload is None or outcome.summary is None:
        return
    record = {"payload_sha256": payload.sha256, "hallucinated_ids": outcome.hallucinated_ids,
              "summary": outcome.summary.model_dump(mode="json")}
    try:
        atomic_write_text(_stash_path(run), json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        run.errors.append(f"stash: {type(exc).__name__}")


def _load_stash(run: _Run, payload: GatedPayload) -> _Outcome | None:
    """The summary kept by an earlier attempt, only if it was made from exactly this payload."""
    path = _stash_path(run)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["payload_sha256"] != payload.sha256:
            return None
        return _Outcome(summary=DigestSummary.model_validate(record["summary"]), claude_status="ok",
                        hallucinated_ids=int(record.get("hallucinated_ids", 0)))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _drop_stash(run: _Run) -> None:
    try:
        _stash_path(run).unlink(missing_ok=True)
    except OSError:
        pass


def _handle_unavailable(run: _Run, exc: ClaudeUnavailable) -> _Outcome:
    job, now = run.job, run.now
    if exc.kind == "killed":
        raise Retry("killed", consume_attempt=False, killed=True) from exc
    if exc.kind == "network" and not _past_deadline(job, now):
        raise Retry("network", exc.retry_after or NETWORK_DELAY, consume_attempt=False) from exc
    if exc.retryable and not _final_attempt(job, now):
        raise Retry(exc.kind, RETRY_DELAYS[min(max(job.attempts, 1), len(RETRY_DELAYS)) - 1]) from exc
    return _Outcome(claude_status=_status_of(exc.kind), failure=exc.kind)


def _claude_usage(run: _Run) -> tuple[int, float]:
    """(calls, settled cost) over every attempt of this job, read from the audit."""
    calls = [r for r in run.audit.records(events=["claude_call"]) if r.get("job_id") == run.job.id]
    return len(calls), round(sum(float(r.get("cost_usd") or 0.0) for r in calls), 6)


# --- steps 5 to 7: render, write, notify --------------------------------------------------


def _context(run: _Run, results: Sequence[CollectResult], refs: Sequence[WithheldItem], state: LocalTier,
             outcome: _Outcome, payload: GatedPayload | None, cost: float, degraded: list[str]) -> render.DigestContext:
    seq, head = run.audit.head()
    if outcome.summary is None and outcome.claude_status != "no_items":
        status = "degraded_no_llm"
    else:
        status = "complete" if all(r.ok for r in results) else "partial"
    late = run.now >= datetime.combine(run.day, LATE_AFTER, tzinfo=run.now.tzinfo)
    return render.DigestContext(
        job_id=run.job.id, day=run.day, generated_at=run.now, window_start=run.window[0], window_end=run.window[1],
        results={r.source: r for r in results}, status=status, late=late, claude_status=outcome.claude_status,
        local_tier=state, degraded=bool(degraded), cost_usd=cost, summary=outcome.summary, held=list(refs),
        over_cap=list(payload.over_cap) if payload is not None else [],
        tier_violation_seq=outcome.tier_violation_seq, audit_seq=seq, audit_head=head,
    )


def _attention_count(ctx: render.DigestContext) -> int:
    """How many numbered 'Start here' lines the reader will see (Claude's or the fallback's)."""
    return sum(1 for line in render.SECTIONS["start_here"].render(ctx) if _ATTENTION_LINE.match(line))


def _variant(outcome: _Outcome, written: WriteResult) -> tuple[str, str]:
    status = outcome.claude_status
    if status == "auth":
        return "auth", ""
    if status in BREAKER_STATUSES:
        return "breaker", status
    if written.fallback_used:
        return "fallback", ""
    if outcome.summary is None and status != "no_items":
        return "degraded", status
    return "ok", ""


def _notify(run: _Run, ctx: render.DigestContext, outcome: _Outcome, written: WriteResult) -> str:
    """One toast per job. A failure is audited and never fails the job."""
    if run.dry or not run.job.params.notify:
        return "skipped"
    counts = render.View(ctx).counts()
    variant, reason = _variant(outcome, written)
    message = digest_message(variant, {"attention": _attention_count(ctx),
                                       "held": counts["held_sensitive"] + counts["held_policy"]},
                             written.rel, reason)
    notifier = run.deps.notifier
    try:
        sent = notifier.send(message)
    except Exception:  # noqa: BLE001  the notifier contract says never raise; do not trust it
        sent = NotifyResult(ok=False, detail="error")
    run.audit.emit("notify", job_id=run.job.id, ok=sent.ok, variant=variant, adapter=getattr(notifier, "name", ""),
                   detail=sent.detail, chars=len(message))
    return "ok" if sent.ok else "failed"



# --- step 8: finish ----------------------------------------------------------------------


def _counts(ctx: render.DigestContext, payload: GatedPayload | None, calls: int) -> dict[str, int]:
    counts = render.View(ctx).counts()
    return {**counts, "to_claude": len(payload.item_ids) if payload is not None else 0, "claude_calls": calls}


def _write_manifest(run: _Run, ctx: render.DigestContext, counts: dict[str, int], cost: float,
                    written: WriteResult | None, payload: GatedPayload | None, status: str) -> None:
    hashes = {"payload_sha256": payload.sha256} if payload is not None else {}
    if written is not None:
        hashes["note_sha256"] = written.sha256
    manifest = RunManifest(
        job_id=run.job.id, status=status, started_at=iso(run.now), finished_at=iso(run.deps.clock()),
        stages=dict(run.stages), counts=counts, cost_usd=cost,
        paths={"note": written.rel} if written is not None else {}, hashes=hashes,
        config_sha256=run.deps.cfg.sha256 or None, audit_seq=run.audit.head()[0],
    )
    target = run.deps.state.dir / "runs" / run.job.id / "run.json"
    try:
        atomic_write_text(target, manifest.model_dump_json(indent=2) + "\n")
    except OSError as exc:
        run.errors.append(f"manifest: {type(exc).__name__}")


def _result(run: _Run, status: str, counts: dict[str, int], cost: float, outcome: _Outcome,
            written: WriteResult | None, gated: int) -> dict[str, Any]:
    refs_only = counts["collected"] - gated
    return {
        "status": status,
        "claude_status": outcome.claude_status,
        "note_path": written.path.as_posix() if written is not None else None,
        "rel": written.rel if written is not None else None,
        "sha256": written.sha256 if written is not None else None,
        "fallback_used": bool(written and written.fallback_used),
        "claude_calls": counts["claude_calls"],
        "cost_usd": cost,
        "hallucinated_ids": outcome.hallucinated_ids,
        "items": {"collected": counts["collected"], "held": counts["held_sensitive"] + counts["held_policy"],
                  "to_claude": counts["to_claude"], "over_cap": counts["over_cap"], "gated": gated,
                  "withheld_refs": max(0, refs_only)},
        "errors": list(run.errors) + ([f"claude: {outcome.failure}"] if outcome.failure else []),
    }


def _advance_watermark(run: _Run) -> None:
    advanced = run.deps.state.watermark.advance(run.window[1], run.job.id)
    run.audit.emit("watermark", job_id=run.job.id, end=iso(run.window[1]), advanced=advanced)


def _noop(run: _Run) -> dict[str, Any]:
    """The date already has a note and the job is not forced: do nothing, once."""
    result = {"status": "noop", "reason": "already_written", "note_path": run.deps.vault.raw_path(run.filename).as_posix()}
    run.audit.emit("job_done", job_id=run.job.id, status="noop", reason="already_written")
    run.deps.store.complete(run.job, result)
    return result


def _keep_unwritten(run: _Run, text: str) -> str | None:
    """Save the rendered digest next to the run record when the vault would not take it."""
    target = run.deps.state.dir / "runs" / run.job.id / "digest-unwritten.md"
    try:
        atomic_write_text(target, text)
    except OSError as exc:
        run.errors.append(f"unwritten_copy: {type(exc).__name__}")
        return None
    return target.as_posix()


def _notify_unwritten(run: _Run, reason: str) -> str:
    """The one toast for a digest that never reached the vault. Fixed phrases only."""
    if run.job.params.notify:
        message = digest_message("unwritten", {}, "", reason.split(":", 1)[0])
        try:
            sent = run.deps.notifier.send(message)
        except Exception:  # noqa: BLE001  the notifier contract says never raise; do not trust it
            sent = NotifyResult(ok=False, detail="error")
        run.audit.emit("notify", job_id=run.job.id, ok=sent.ok, variant="unwritten",
                       adapter=getattr(run.deps.notifier, "name", ""), detail=sent.detail, chars=len(message))
        return "ok" if sent.ok else "failed"
    return "skipped"


def _fail(run: _Run, reason: str, ctx: render.DigestContext, counts: dict[str, int], cost: float,
          payload: GatedPayload | None, outcome: _Outcome, gated: int, text: str) -> dict[str, Any]:
    """The vault will not take the note and no later attempt is coming: tell the reader."""
    run.stages["write"] = "denied"
    copy = _keep_unwritten(run, text)
    run.stages["notify"] = _notify_unwritten(run, reason)
    run.audit.emit("job_failed", job_id=run.job.id, error=reason)
    run.deps.store.fail(run.job, reason)
    result = _result(run, "failed", counts, cost, outcome, None, gated)
    result["reason"] = reason
    result["unwritten_copy"] = copy
    _write_manifest(run, ctx, counts, cost, None, payload, "failed")
    return result


def _dry_result(run: _Run, refs: Sequence[WithheldItem], payload: GatedPayload | None, outcome: _Outcome,
                gated: int, results: Sequence[CollectResult] = (), state: LocalTier = "not_installed") -> dict[str, Any]:
    # The note a real run would write with these items and no Claude summary, rendered in memory
    # so a dry run can be read against the digest grammar. Nothing is written. Claude was never
    # called, so the headline says "dry run" rather than inheriting the default "ok".
    shown = replace(outcome, claude_status="dry_run") if outcome.summary is None and outcome.claude_status == "ok" else outcome
    note = render.render_digest(_context(run, results, refs, state, shown, payload, 0.0, [])) if results else ""
    return {
        "status": "dry_run",
        "note": note,
        "payload": payload.text if payload is not None else "",
        "payload_sha256": payload.sha256 if payload is not None else "",
        "payload_bytes": payload.byte_size if payload is not None else 0,
        "over_cap": list(payload.over_cap) if payload is not None else [],
        "blocked": outcome.failure if outcome.claude_status == "payload_blocked" else None,
        "held": [{"id": r.id, "kind": r.kind, "reason": r.reason, "hold_kind": r.hold_kind} for r in refs],
        "items": {"gated": gated, "held": len(refs), "to_claude": len(payload.item_ids) if payload else 0},
        "errors": list(run.errors),
    }


def run_digest_job(job: Job, deps: Deps, *, mode: str = "daemon") -> dict[str, Any]:
    """Run one morning_digest job to completion, or raise Retry. See the module docstring."""
    now = deps.clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("the digest clock must return an aware time")
    if deps.state.killed():
        raise Retry("killed", consume_attempt=False, killed=True)
    day = _job_day(job, now)
    run = _Run(job=job, deps=deps, mode=mode, now=now, day=day, window=_window(job, deps, now),
               filename=_filename(job, day))
    deps.audit.emit("digest_start", job_id=job.id, mode=mode, attempt=job.attempts, dry_run=run.dry,
                    no_claude=job.params.no_claude, force=job.params.force)
    if not run.dry and not job.params.force and deps.vault.raw_path(run.filename).exists():
        return _noop(run)

    results = _collect(run)
    items, gates, refs, state = _gate(run, results)
    _aggregate(run, gates, refs, state)
    payload, outcome = _seal(run, items, gates)
    if run.dry:
        return _dry_result(run, refs, payload, outcome, len(items), results, state)

    if payload is not None and payload.item_ids:
        outcome = _summarize(run, payload)
        run.stages["summarize"] = "ok" if outcome.summary is not None else f"failed:{outcome.claude_status}"
    else:
        run.stages["summarize"] = "skipped"

    calls, cost = _claude_usage(run)
    degraded = (["local_unavailable"] if any(g.degraded for g in gates) else []) + (
        [] if outcome.claude_status in QUIET_STATUSES else [f"claude_{outcome.claude_status}"])
    job.degraded.flag, job.degraded.reasons = bool(degraded), degraded
    job.cost_usd = cost
    ctx = _context(run, results, refs, state, outcome, payload, cost, degraded)
    counts = _counts(ctx, payload, calls)
    run.stages["render"] = "ok"
    text = render.render_digest(ctx)

    try:
        written = deps.vault.write_raw(run.filename, text, job.id)
    except VaultWriteDenied as exc:
        return _fail(run, f"vault_denied:{exc.reason}", ctx, counts, cost, payload, outcome, len(items), text)
    except (FileBusy, OSError) as exc:
        busy = isinstance(exc, FileBusy)
        reason = "vault_busy" if busy else f"vault_error:{type(exc).__name__}"
        if _final_attempt(job, now):
            return _fail(run, reason, ctx, counts, cost, payload, outcome, len(items), text)
        # Claude has been paid for already: keep its answer for the next attempt.
        _stash_summary(run, payload, outcome)
        raise Retry(reason, VAULT_BUSY_DELAY if busy else VAULT_ERROR_DELAY) from exc
    run.stages["write"] = "ok"
    _drop_stash(run)

    _advance_watermark(run)
    run.stages["notify"] = _notify(run, ctx, outcome, written)
    run.stages["finish"] = "ok"
    result = _result(run, ctx.status, counts, cost, outcome, written, len(items))
    _write_manifest(run, ctx, counts, cost, written, payload, ctx.status)
    run.audit.emit("job_done", job_id=job.id, status=ctx.status, claude_status=outcome.claude_status,
                   rel=written.rel, claude_calls=calls, cost_usd=cost, collected=counts["collected"],
                   held=result["items"]["held"], to_claude=counts["to_claude"])
    deps.store.complete(job, result)
    return result
