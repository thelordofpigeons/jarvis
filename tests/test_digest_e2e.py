"""The digest pipeline end to end, with the real client driving the fake claude binary.

Collectors, gates, sealing, the Claude call, rendering, the vault write, the notification and
the finish step all run for real. Only the notifier and the scheduled-task query are fakes.
Everything is synthetic (design D10). Real git repos are built in tmp_path.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import CANARY, FakeClock
from jarvisd import ROOT
from jarvisd import digest as digest_mod
from jarvisd.audit import AuditLog
from jarvisd.claude import ClaudeClient, default_runner
from jarvisd.collectors.brain import BrainCollector
from jarvisd.collectors.git import GitCollector
from jarvisd.collectors.system import SystemCollector
from jarvisd.collectors.task import TaskCollector
from jarvisd.common import iso, parse_iso
from jarvisd.config import Config, RepoCfg
from jarvisd.digest import Deps, Retry, run_digest_job
from jarvisd.dispatch import PayloadBlocked
from jarvisd.fsio import FileBusy
from jarvisd.jobstore import JobStore
from jarvisd.models import HistoryEntry, Job, JobWindow, TierHit
from jarvisd.notify import NotifyResult
from jarvisd.router import build_router
from jarvisd.state import StateStore
from jarvisd.vault import VaultWriter

FAKE = ROOT / "tests" / "fakes" / "fake_claude.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# --- fakes and rig -----------------------------------------------------------------------


class FakeRunner:
    """Starts the fake instead of `claude` and adds the FAKE_* variables the client never passes."""

    def __init__(self, tmp_path: Path, scenario: str = "ok") -> None:
        self.log = tmp_path / "fake-claude.jsonl"
        self.scenario = scenario

    def __call__(self, argv: Any, *, env: Any, cwd: str, creationflags: int) -> Any:
        full = [sys.executable, str(FAKE), *list(argv)[1:]]
        env2 = {**env, "FAKE_CLAUDE_LOG": str(self.log), "FAKE_CLAUDE_SCENARIO": self.scenario}
        return default_runner(full, env=env2, cwd=cwd, creationflags=creationflags)

    def records(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def paid(self) -> list[dict[str, Any]]:
        return [r for r in self.records() if r["argv"][:1] not in (["--version"], ["--help"])]


class RecordingNotifier:
    name = "recording"

    def __init__(self, ok: bool = True) -> None:
        self.messages: list[str] = []
        self.ok = ok

    def send(self, message: str) -> NotifyResult:
        self.messages.append(message)
        return NotifyResult(ok=self.ok, detail="sent" if self.ok else "exit_1")


@dataclass
class Rig:
    cfg: Config
    deps: Deps
    audit: AuditLog
    state: StateStore
    store: JobStore
    runner: FakeRunner
    notifier: RecordingNotifier
    clock: FakeClock
    vault: Path
    claude_home: Path
    # The job-key day, fixed when the rig is built. It must not follow the clock: tests advance
    # the clock by hours, and after UTC midnight a recomputed day names tomorrow's note.
    day: date

    def note(self, name: str | None = None) -> Path:
        return self.vault / "raw" / "jarvis" / (name or f"digest-{self.day.isoformat()}.md")

    def events(self, name: str) -> list[dict[str, Any]]:
        return self.audit.records(events=[name])


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
                   cwd=repo, env=env, capture_output=True, text=True, check=True)


def make_repo(path: Path, subjects: tuple[str, ...]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    for n, subject in enumerate(subjects):
        (path / f"f{n}.txt").write_text(f"{n}\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", subject)
    return path


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def set_age(path: Path, now: datetime, hours: float) -> None:
    stamp = (now - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))


SESSION = """---
type: session
date: {day}
{extra}---
## Next session entry point
Continue at fixture.py:10 - synthetic entry point

## Open threads
- Synthetic follow up one
"""


def build(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, *, scenario: str = "ok", claude: bool = True,
          repos: list[RepoCfg] | None = None, mutate: Any = None, notifier_ok: bool = True) -> Rig:
    """A full set of dependencies on a throwaway machine, with a clean synthetic day."""
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.claude.binary = str(FAKE)
    cfg.digest.repos = repos if repos is not None else []
    if mutate is not None:
        mutate(cfg)
    # Real time, truncated, so git commit times fall inside the window.
    clock = FakeClock(datetime.now(timezone.utc).replace(microsecond=0))
    now = clock.now
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir()

    audit = AuditLog(cfg.paths.logs / "jarvisd-audit.jsonl", mirror_stdout=False)
    state = StateStore.from_config(cfg)
    store = JobStore.from_config(cfg, audit=audit)
    runner = FakeRunner(tmp_path, scenario)
    notifier = RecordingNotifier(ok=notifier_ok)
    client = ClaudeClient(cfg, audit, state, runner=runner, enabled=claude, network_probe=lambda: True,
                          sleep=lambda s: None, poll_seconds=0.1)
    collectors = [
        BrainCollector(),
        TaskCollector(base_dir=claude_home),
        GitCollector(),
        SystemCollector(audit, state, store=store, runner=lambda argv, timeout: (1, "")),
    ]
    deps = Deps(cfg=cfg, audit=audit, state=state, store=store, vault=VaultWriter(cfg, audit), claude=client,
                router=build_router(cfg), notifier=notifier, collectors=collectors, clock=clock)
    for name in ("RECENT.md",):
        set_age(tmp_vault / name, now, 2.0)
    return Rig(cfg, deps, audit, state, store, runner, notifier, clock, tmp_vault, claude_home, now.date())


def new_job(rig: Rig, job_id: str | None = None, *, attempts: int = 1, force: bool = False,
            dry_run: bool = False, no_claude: bool = False, notify: bool = True,
            deadline_hours: float = 3.0) -> Job:
    """Enqueue and claim, the way the daemon does, then set the attempt counter for the case."""
    now = rig.clock.now
    created = iso(now)
    job = Job(
        id=job_id or f"digest-{rig.day.isoformat()}", kind="morning_digest", key=rig.day.isoformat(),
        job_class="observe_only", latency_class="background_batch", origin="manual",
        created_at=created, not_before=created, deadline=iso(now + timedelta(hours=deadline_hours)),
        window=JobWindow(start=iso(now - timedelta(hours=36)), end=created),
        params={"force": force, "dry_run": dry_run, "no_claude": no_claude, "notify": notify},
        history=[HistoryEntry(ts=created, from_state=None, to="pending", note="test")],
    )
    assert rig.store.enqueue(job)
    claimed = rig.store.claim_next(now)
    assert claimed is not None and claimed.id == job.id
    claimed.attempts = attempts
    rig.store.update(claimed)
    return claimed


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def plant_clean_day(rig: Rig) -> None:
    """Two clean threads' worth of data: a clean session and a quiet synthetic task."""
    now = rig.clock.now
    prev = (rig.day - timedelta(days=1)).isoformat()
    clean = write(rig.vault / "sessions" / f"{prev}-11.md", SESSION.format(day=prev, extra=""))
    set_age(clean, now, 3.0)
    write(rig.claude_home / "current-task", "86synthetic")
    write(rig.claude_home / "current-task-name", "Synthetic task")
    write(rig.claude_home / "current-task-status", "IN REVIEW")


# --- the named test ----------------------------------------------------------------------


def test_canary_never_leaves(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    """Canary in telos/sensitive, a tagged session, a tagged commit subject and a checkpoint cwd.

    Nothing planted may reach the fake claude (stdin, argv, env), the audit, the toast, the
    digest or any other file the pipeline writes, and every planted item must be counted held.
    """
    alpha = make_repo(tmp_path / "repos" / "alpha", ("feat: synthetic ok commit", f"wip #private {CANARY} notes"))
    hidden = make_repo(tmp_vault / "telos" / "sensitive" / "proj", (f"feat: {CANARY}",))
    repos = [RepoCfg(name="alpha", path=alpha), RepoCfg(name="hiddenrepo", path=hidden)]
    rig = build(tmp_cfg, tmp_path, tmp_vault, repos=repos)
    now = rig.clock.now
    prev = (rig.day - timedelta(days=1)).isoformat()

    tagged = write(tmp_vault / "sessions" / f"{prev}-09.md",
                   SESSION.format(day=prev, extra="tags: [sensitive]\n") + f"\n{CANARY}\n")
    set_age(tagged, now, 5.0)
    plant_clean_day(rig)
    write(tmp_vault / "session-checkpoints" / "a.json", json.dumps({"cwd": f"C:/{CANARY}/work"}))
    # A RECENT bullet that shares the tagged session's date is derived-sensitive.
    recent = tmp_vault / "RECENT.md"
    recent.write_text(recent.read_text(encoding="utf-8").replace(
        "## Recent Decisions", f"- [{prev}] Synthetic bullet from the tagged day\n\n## Recent Decisions", 1),
        encoding="utf-8", newline="\n")
    set_age(recent, now, 2.0)

    before = tree(tmp_vault)
    job = new_job(rig)
    result = run_digest_job(job, rig.deps, mode="daemon")

    # Five planted items hold: the tagged session, the tagged commit, the repo under telos, and
    # the two RECENT bullets that share the tagged session's date (the fixture one and mine).
    note = rig.note().read_text(encoding="utf-8")
    assert "held_sensitive: 5" in note and "held_policy: 1" in note
    assert result["items"]["held"] == 6
    assert len(rig.store.held()) == 6, "five sensitive items plus the work task held by policy"
    assert "checkpoints waiting for /promote-sessions: 1" in note.lower()

    # Nothing outside the one allowed note appeared in the vault, least of all a checkpoint.
    assert tree(tmp_vault) - before == {f"raw/jarvis/digest-{rig.day.isoformat()}.md"}

    # Fake claude: every recorded stdin, argv and env key.
    records = rig.runner.records()
    assert len(rig.runner.paid()) == 1
    assert CANARY not in json.dumps(records)
    stdin = rig.runner.paid()[0]["stdin"]
    assert "telos" not in stdin and "private" not in stdin.lower() and "sensitive" not in stdin.lower()

    # Audit, toast, digest, and every file the pipeline wrote under the jarvis tree.
    assert CANARY not in (rig.cfg.paths.logs / "jarvisd-audit.jsonl").read_text(encoding="utf-8")
    assert CANARY not in "\n".join(rig.notifier.messages)
    assert CANARY not in note and "telos" not in note.lower()
    for path in (tmp_path / "jarvis").rglob("*"):
        if path.is_file():
            assert CANARY not in path.read_text(encoding="utf-8", errors="ignore"), path


# --- success path ------------------------------------------------------------------------


def test_note_is_written_with_the_marker_and_the_watermark_advances(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    job = new_job(rig)
    assert rig.state.watermark.get() is None

    result = run_digest_job(job, rig.deps, mode="daemon")

    note = rig.note().read_text(encoding="utf-8")
    lines = note.splitlines()
    assert lines[0] == "---" and lines[2] == "generator: jarvisd"
    assert "status: complete" in note and "claude: ok" in note
    assert result["status"] == "complete" and result["claude_calls"] == 1
    assert result["note_path"].endswith(f"raw/jarvis/digest-{rig.day.isoformat()}.md")
    assert job.window is not None
    assert rig.state.watermark.get() == parse_iso(job.window.end)
    assert rig.store.exists(job.id) == "done"
    done = rig.store.get(job.id)
    assert done is not None and done.result is not None and done.result["status"] == "complete"
    assert done.tier == "claude" and done.importance is not None and done.confidence == 0.0
    assert done.cost_usd > 0.0


def test_job_aggregates_are_filled(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    job = new_job(rig)
    run_digest_job(job, rig.deps, mode="daemon")
    done = rig.store.get(job.id)
    assert done is not None
    assert done.router == "stub" and done.tier == "claude"
    assert done.importance in {"low", "med", "high"}
    assert done.confidence == 0.0 and done.sensitive is False
    assert done.local_tier == "not_installed" and done.degraded.flag is False
    assert done.cost_usd > 0.0


def test_exactly_one_notification_with_no_item_text(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    run_digest_job(new_job(rig), rig.deps, mode="daemon")
    assert len(rig.notifier.messages) == 1
    msg = rig.notifier.messages[0]
    assert msg.startswith("Digest ready: ") and msg.endswith(f"brain/raw/jarvis/digest-{rig.day.isoformat()}.md")
    assert "Synthetic" not in msg and "86synthetic" not in msg
    sent = rig.events("notify")
    assert len(sent) == 1 and sent[0]["ok"] is True and sent[0]["variant"] == "ok"


def test_a_failing_toast_never_fails_the_job(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, notifier_ok=False)
    plant_clean_day(rig)
    job = new_job(rig)
    run_digest_job(job, rig.deps, mode="daemon")
    assert rig.store.exists(job.id) == "done"
    assert rig.events("notify")[0]["ok"] is False


def test_audit_trail_counts_and_redaction(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    job = new_job(rig)
    result = run_digest_job(job, rig.deps, mode="daemon")

    gated = result["items"]["gated"]
    assert len(rig.events("gate_decision")) == gated > 0
    assert len(rig.events("claude_intent")) == 1 and len(rig.events("claude_call")) == 1
    assert len(rig.events("payload_sealed")) == 1
    assert len(rig.events("collector_result")) == 4
    assert rig.events("digest_start")[0]["mode"] == "daemon"
    assert rig.events("job_done")[0]["job_id"] == job.id
    ok, bad = rig.audit.verify()
    assert ok and bad is None

    raw = (rig.cfg.paths.logs / "jarvisd-audit.jsonl").read_text(encoding="utf-8")
    for needle in ("Synthetic task", "Synthetic thread", "Synthetic entry point", "86synthetic"):
        assert needle not in raw
    for rec in rig.audit.records():
        assert not {"title", "text", "body", "content", "prompt"} & set(rec)


def test_run_manifest_has_stages_counts_cost_paths_and_hashes(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    job = new_job(rig)
    result = run_digest_job(job, rig.deps, mode="daemon")
    manifest = json.loads((rig.state.dir / "runs" / job.id / "run.json").read_text(encoding="utf-8"))
    assert manifest["job_id"] == job.id and manifest["status"] == "complete"
    assert set(manifest["stages"]) >= {"collect", "gate", "seal", "summarize", "render", "write", "notify", "finish"}
    assert all(v for v in manifest["stages"].values())
    assert manifest["counts"]["collected"] == result["items"]["collected"]
    assert manifest["cost_usd"] == result["cost_usd"] > 0
    assert manifest["paths"]["note"] == f"raw/jarvis/digest-{rig.day.isoformat()}.md"
    assert manifest["hashes"]["note_sha256"] == result["sha256"]
    assert len(manifest["hashes"]["payload_sha256"]) == 64
    assert manifest["audit_seq"] > 0
    text = json.dumps(manifest)
    assert "Synthetic" not in text and "86synthetic" not in text


def test_directory_snapshot_shows_only_the_note(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    before = tree(tmp_vault)
    run_digest_job(new_job(rig), rig.deps, mode="daemon")
    added = tree(tmp_vault) - before
    assert added == {f"raw/jarvis/digest-{rig.day.isoformat()}.md"}
    assert not [p for p in tree(tmp_vault) if p.startswith("session-checkpoints/") and p not in before]
    assert not list((tmp_vault / "raw" / "jarvis").glob(".*.tmp"))


# --- idempotence -------------------------------------------------------------------------


def test_second_run_for_the_same_date_is_a_noop_without_force(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    run_digest_job(new_job(rig), rig.deps, mode="daemon")
    note_before = rig.note().read_bytes()
    calls, toasts = len(rig.runner.paid()), len(rig.notifier.messages)
    mark = rig.state.watermark.get()

    again = new_job(rig, f"digest-{rig.day.isoformat()}-again")
    result = run_digest_job(again, rig.deps, mode="manual")

    assert result["status"] == "noop" and result["reason"] == "already_written"
    assert rig.note().read_bytes() == note_before
    assert len(rig.runner.paid()) == calls and len(rig.notifier.messages) == toasts
    assert rig.state.watermark.get() == mark
    assert rig.store.exists(again.id) == "done"


def test_force_writes_the_r2_file(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    run_digest_job(new_job(rig), rig.deps, mode="daemon")
    forced = new_job(rig, f"digest-{rig.day.isoformat()}-r2", force=True)
    result = run_digest_job(forced, rig.deps, mode="manual")
    assert result["status"] == "complete"
    assert rig.note(f"digest-{rig.day.isoformat()}-r2.md").is_file()
    assert rig.note().is_file()


# --- failure handling --------------------------------------------------------------------


def test_first_claude_failure_raises_retry_and_leaves_no_note(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario="500")
    plant_clean_day(rig)
    job = new_job(rig, attempts=1)
    before = tree(tmp_vault)

    with pytest.raises(Retry) as caught:
        run_digest_job(job, rig.deps, mode="daemon")

    assert caught.value.delay == timedelta(minutes=10) and caught.value.consume_attempt is True
    assert tree(tmp_vault) == before, "a retry leaves no note"
    assert rig.state.watermark.get() is None
    assert rig.notifier.messages == [], "one toast per job, never per retry"
    assert rig.store.exists(job.id) == "running"
    rig.store.retry(job, caught.value.error, caught.value.delay)
    assert rig.store.exists(job.id) == "pending"


def test_second_failure_backs_off_thirty_minutes(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario="500")
    plant_clean_day(rig)
    with pytest.raises(Retry) as caught:
        run_digest_job(new_job(rig, attempts=2), rig.deps, mode="daemon")
    assert caught.value.delay == timedelta(minutes=30)


def test_third_failure_writes_the_deterministic_note_and_completes(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario="500")
    plant_clean_day(rig)
    job = new_job(rig, attempts=3)

    result = run_digest_job(job, rig.deps, mode="daemon")

    note = rig.note().read_text(encoding="utf-8")
    assert "status: degraded_no_llm" in note and "claude: unavailable" in note
    assert result["status"] == "degraded_no_llm" and result["claude_status"] == "unavailable"
    assert rig.store.exists(job.id) == "done"
    assert rig.state.watermark.get() is not None
    assert len(rig.notifier.messages) == 1
    assert rig.notifier.messages[0].startswith("Digest ready, Claude was unavailable (")
    assert "Deterministic sections only" in rig.notifier.messages[0]


def test_a_past_deadline_is_final_even_on_attempt_one(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario="500")
    plant_clean_day(rig)
    job = new_job(rig, attempts=1, deadline_hours=3.0)
    rig.clock.advance(hours=4)
    result = run_digest_job(job, rig.deps, mode="daemon")
    assert result["status"] == "degraded_no_llm" and rig.note().is_file()


@pytest.mark.parametrize(("scenario", "claude_status", "variant_text"), [
    ("401", "auth", "Claude login expired. Run claude /login"),
    ("429", "rate_limit", "Digest ready, Claude was unavailable (quota)"),
    ("invalid_json", "bad_json", "Digest ready, Claude was unavailable (bad reply)"),
    ("isolation_violation", "isolation_anomaly", "JARVIS paused Claude calls: isolation check"),
])
def test_non_retryable_failures_fall_back_at_once_on_attempt_one(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, scenario: str, claude_status: str, variant_text: str
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario=scenario)
    plant_clean_day(rig)
    job = new_job(rig, attempts=1)

    result = run_digest_job(job, rig.deps, mode="daemon")

    assert result["status"] == "degraded_no_llm" and result["claude_status"] == claude_status
    assert f"claude: {claude_status}" in rig.note().read_text(encoding="utf-8")
    assert rig.store.exists(job.id) == "done"
    assert len(rig.runner.paid()) == 1, "no paid retry"
    assert rig.notifier.messages[0].startswith(variant_text)


def test_budget_refusal_is_an_immediate_deterministic_fallback(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def no_calls(cfg: Config) -> None:
        cfg.claude.daily_calls = 0

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=no_calls)
    plant_clean_day(rig)
    result = run_digest_job(new_job(rig, attempts=1), rig.deps, mode="daemon")
    assert result["status"] == "degraded_no_llm" and result["claude_status"] == "budget"
    assert rig.runner.paid() == []
    assert "Claude summary unavailable (budget)" in rig.note().read_text(encoding="utf-8")
    assert rig.notifier.messages[0].startswith("Digest ready, Claude was unavailable (budget)")


def test_payload_blocked_renders_the_loud_line_and_opens_the_breaker(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise PayloadBlocked(TierHit(kind="sensitive", code="tag_inline", where="text"), "some-item")

    monkeypatch.setattr(digest_mod, "clear_for_claude", blocked)
    result = run_digest_job(new_job(rig, attempts=1), rig.deps, mode="daemon")

    note = rig.note().read_text(encoding="utf-8")
    violations = rig.events("tier_violation")
    assert len(violations) == 1
    assert f"TIER VIOLATION, Claude call aborted, see audit seq {violations[0]['seq']}" in note
    assert result["status"] == "degraded_no_llm" and result["claude_status"] == "payload_blocked"
    assert rig.runner.paid() == []
    assert rig.state.breaker.peek()["requires_human_reset"] is True
    assert rig.notifier.messages[0].startswith("JARVIS paused Claude calls: tier violation")


def test_a_kill_file_before_the_call_sends_the_job_back_without_a_note(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    (rig.state.dir / "KILL").write_text("stop", encoding="utf-8")
    with pytest.raises(Retry) as caught:
        run_digest_job(new_job(rig), rig.deps, mode="daemon")
    assert caught.value.killed is True and caught.value.consume_attempt is False
    assert not rig.note().exists() and rig.notifier.messages == []


def test_no_network_hands_the_attempt_back(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    rig.deps.claude._probe = lambda: False  # noqa: SLF001  the client has no public switch for this
    with pytest.raises(Retry) as caught:
        run_digest_job(new_job(rig), rig.deps, mode="daemon")
    assert caught.value.consume_attempt is False and caught.value.delay == timedelta(minutes=10)
    assert not rig.note().exists()


def test_a_denied_write_fails_the_job_with_a_toast_a_copy_and_no_watermark(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    # A foreign file at the target: the writer refuses to replace what it did not make.
    write(rig.note(), "someone else's note")
    job = new_job(rig, force=True)
    result = run_digest_job(job, rig.deps, mode="manual")
    assert result["status"] == "failed" and result["reason"] == "vault_denied:existing_file_not_ours"
    assert rig.store.exists(job.id) == "failed"
    assert rig.state.watermark.get() is None
    assert rig.events("job_failed")[0]["job_id"] == job.id
    # The reader is told, and the rendered digest is not lost with the refused write.
    assert len(rig.notifier.messages) == 1 and "could not be written" in rig.notifier.messages[0]
    assert "vault refused" in rig.notifier.messages[0]
    copy = rig.state.dir / "runs" / job.id / "digest-unwritten.md"
    assert copy.read_text(encoding="utf-8").startswith("---") and result["unwritten_copy"] == copy.as_posix()


def test_a_busy_or_broken_vault_raises_retry_with_no_watermark(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)

    def busy(*args: Any, **kwargs: Any) -> Any:
        raise FileBusy("target busy")

    monkeypatch.setattr(rig.deps.vault, "write_raw", busy)
    with pytest.raises(Retry) as caught:
        run_digest_job(new_job(rig), rig.deps, mode="daemon")
    assert caught.value.error == "vault_busy" and caught.value.consume_attempt is True

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(rig.deps.vault, "write_raw", broken)
    with pytest.raises(Retry) as again:
        run_digest_job(new_job(rig, f"digest-{rig.day.isoformat()}-r2", force=True), rig.deps, mode="daemon")
    assert again.value.error == "vault_error:PermissionError"
    assert rig.state.watermark.get() is None and rig.notifier.messages == []


def test_no_network_past_the_deadline_falls_back_instead_of_waiting(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    rig.deps.claude._probe = lambda: False  # noqa: SLF001
    job = new_job(rig)
    rig.clock.advance(hours=4)
    result = run_digest_job(job, rig.deps, mode="daemon")
    assert result["claude_status"] == "network" and result["status"] == "degraded_no_llm"


def test_digest_module_never_reads_the_held_directory() -> None:
    """Only `jarvis held` reads queue/held; the pipeline only writes references (design section 5)."""
    source = (ROOT / "jarvisd" / "digest.py").read_text(encoding="utf-8")
    assert ".held(" not in source and "expire_held" not in source


# --- modes -------------------------------------------------------------------------------


def test_no_claude_param_never_spawns_and_says_so(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    result = run_digest_job(new_job(rig, no_claude=True), rig.deps, mode="manual")
    assert rig.runner.records() == [] and result["claude_calls"] == 0
    assert result["claude_status"] == "disabled" and result["status"] == "degraded_no_llm"
    assert "claude: disabled" in rig.note().read_text(encoding="utf-8")


def test_a_disabled_client_never_spawns(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, claude=False)
    plant_clean_day(rig)
    result = run_digest_job(new_job(rig), rig.deps, mode="daemon")
    assert rig.runner.records() == []
    assert result["claude_status"] == "disabled" and rig.note().is_file()


def test_dry_run_prints_the_payload_and_writes_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    before = tree(tmp_vault)
    job = new_job(rig, dry_run=True)

    result = run_digest_job(job, rig.deps, mode="manual")

    assert result["status"] == "dry_run"
    assert result["payload"].startswith("<data>") and len(result["payload_sha256"]) == 64
    assert any(h["hold_kind"] == "policy" for h in result["held"])
    assert all(set(h) == {"id", "kind", "reason", "hold_kind"} for h in result["held"])
    assert tree(tmp_vault) == before
    assert rig.runner.records() == []
    assert rig.state.watermark.get() is None and rig.notifier.messages == []
    assert rig.store.held() == [], "a dry run records nothing"


def test_a_failed_collector_makes_the_note_partial(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)

    class Boom:
        name = "git"

        def collect(self, ctx: Any) -> Any:
            raise RuntimeError(f"leaky detail {CANARY}")

    rig.deps.collectors = [c if c.name != "git" else Boom() for c in rig.deps.collectors]
    result = run_digest_job(new_job(rig), rig.deps, mode="daemon")
    note = rig.note().read_text(encoding="utf-8")
    assert result["status"] == "partial" and "status: partial" in note
    assert CANARY not in note and any("git" in e for e in result["errors"])
    assert CANARY not in json.dumps(result)


def test_the_sealed_payload_carries_only_cleared_items(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    run_digest_job(new_job(rig), rig.deps, mode="daemon")
    stdin = rig.runner.paid()[0]["stdin"]
    assert stdin.startswith(f"Date: {rig.day.isoformat()}. Window: ")
    assert "Synthetic task" not in stdin, "the work task is held by policy and never sent"
    assert "Summarize these items." in stdin and "<data>" in stdin


def _paid_calls(rig: Rig) -> int:
    return len([r for r in rig.runner.records() if r["argv"][:1] not in (["--version"], ["--help"])])


def test_a_vault_retry_reuses_the_summary_and_pays_once(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    job = new_job(rig)
    real_write = rig.deps.vault.write_raw
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise FileBusy("Obsidian has the file open")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(rig.deps.vault, "write_raw", flaky)
    for _ in range(2):
        with pytest.raises(Retry) as caught:
            run_digest_job(job, rig.deps, mode="daemon")
        assert caught.value.error == "vault_busy"
    result = run_digest_job(job, rig.deps, mode="daemon")

    assert result["status"] == "complete" and result["claude_status"] == "ok"
    assert _paid_calls(rig) == 1, "three attempts at the write must cost one Claude call"
    assert result["claude_calls"] == 1 and len(rig.events("claude_intent")) == 1
    assert len(rig.events("claude_summary_reused")) == 2
    assert "Quiet night" in rig.note().read_text(encoding="utf-8")  # the reused summary is in the note
    assert not (rig.state.dir / "runs" / job.id / "summary.json").exists(), "the stash goes once it is used"


def test_a_stashed_summary_for_a_different_payload_is_not_reused(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    job = new_job(rig)

    def busy(*args: Any, **kwargs: Any) -> Any:
        raise FileBusy("busy")

    real_write = rig.deps.vault.write_raw
    monkeypatch.setattr(rig.deps.vault, "write_raw", busy)
    with pytest.raises(Retry):
        run_digest_job(job, rig.deps, mode="daemon")
    stash = rig.state.dir / "runs" / job.id / "summary.json"
    data = json.loads(stash.read_text(encoding="utf-8"))
    data["payload_sha256"] = "0" * 64  # the items changed between attempts
    stash.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(rig.deps.vault, "write_raw", real_write)
    run_digest_job(job, rig.deps, mode="daemon")
    assert _paid_calls(rig) == 2 and rig.events("claude_summary_reused") == []


def test_the_last_vault_failure_degrades_with_a_toast_and_a_local_copy(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    job = new_job(rig, attempts=3)

    def busy(*args: Any, **kwargs: Any) -> Any:
        raise FileBusy("Syncthing has the file")

    monkeypatch.setattr(rig.deps.vault, "write_raw", busy)
    result = run_digest_job(job, rig.deps, mode="daemon")  # no Retry: there is no attempt left

    assert result["status"] == "failed" and result["reason"] == "vault_busy"
    assert rig.store.exists(job.id) == "failed"
    assert len(rig.notifier.messages) == 1
    assert "could not be written" in rig.notifier.messages[0] and "vault busy" in rig.notifier.messages[0]
    assert "Syncthing" not in rig.notifier.messages[0]
    copy = Path(result["unwritten_copy"])
    assert copy.is_file() and copy.read_text(encoding="utf-8").startswith("---")
    assert rig.state.watermark.get() is None and not rig.note().exists()
    assert rig.events("job_failed")[-1]["error"] == "vault_busy"


def test_the_last_vault_error_names_its_class_in_the_reason(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr(rig.deps.vault, "write_raw", broken)
    result = run_digest_job(new_job(rig, attempts=3), rig.deps, mode="daemon")
    assert result["reason"] == "vault_error:PermissionError"
    assert len(rig.notifier.messages) == 1 and "vault error" in rig.notifier.messages[0]


def test_an_unwritten_digest_toast_respects_no_notify(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)

    def busy(*args: Any, **kwargs: Any) -> Any:
        raise FileBusy("busy")

    monkeypatch.setattr(rig.deps.vault, "write_raw", busy)
    run_digest_job(new_job(rig, attempts=3, notify=False), rig.deps, mode="manual")
    assert rig.notifier.messages == []
