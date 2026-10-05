"""The resident loop: ticks, locks, kill and pause, crash recovery, the scheduler host.

Collectors, gates, the Claude client and the vault writer are the real ones; the clock, the
notifier, the scheduled-task query and the `claude` binary (tests/fakes/fake_claude.py,
started as a real child process) are fakes. Everything is synthetic (design D10).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from jarvisd import ROOT
from jarvisd import daemon as daemon_mod
from jarvisd import state as state_mod
from jarvisd.audit import AuditLog
from jarvisd.claude import ClaudeClient, default_runner
from jarvisd.collectors.brain import BrainCollector
from jarvisd.collectors.git import GitCollector
from jarvisd.collectors.system import SystemCollector
from jarvisd.collectors.task import TaskCollector
from jarvisd.common import iso
from jarvisd.config import Config
from jarvisd.daemon import serve, tick
from jarvisd.digest import Deps, Retry
from jarvisd.jobstore import JobStore
from jarvisd.models import HistoryEntry, Job, JobWindow
from jarvisd.notify import NotifyResult
from jarvisd.router import build_router
from jarvisd.scheduler import SchedulerHost
from jarvisd.state import StateStore
from jarvisd.vault import VaultWriter

FAKE = ROOT / "tests" / "fakes" / "fake_claude.py"
UTC = timezone.utc
# After 06:30 plus the 90 minute wait for the nightly rebuild, so a tick enqueues at once.
NOW = datetime(2026, 10, 6, 9, 0, 0, tzinfo=UTC)
READY = (0, '"\\JarvisDaemon","10/7/2026 6:00:00 AM","Ready"\n')
DISABLED = (0, '"\\JarvisDaemon","N/A","Disabled"\n')


class FakeRunner:
    """Starts the fake instead of `claude`; adds the FAKE_* variables the client never passes."""

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


class Notes:
    name = "recording"

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> NotifyResult:
        self.messages.append(message)
        return NotifyResult(ok=True, detail="sent")


class Schtasks:
    """Fake `schtasks /query`: returns a canned answer and remembers the argv it was given."""

    def __init__(self, answer: tuple[int, str] = READY) -> None:
        self.answer = answer
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any, timeout: float) -> tuple[int, str]:
        self.calls.append(list(argv))
        return self.answer


@dataclass
class Rig:
    cfg: Config
    deps: Deps
    audit: AuditLog
    state: StateStore
    store: JobStore
    runner: FakeRunner
    notes: Notes
    clock: FakeClock

    def events(self, name: str) -> list[dict[str, Any]]:
        return self.audit.records(events=[name])

    def note(self, day: str = "2026-10-06") -> Path:
        return self.deps.vault.raw_path(f"digest-{day}.md")


def make_rig(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, *, scenario: str = "ok", claude: bool = False,
             start: datetime = NOW, cfg: Config | None = None) -> Rig:
    cfg = (cfg or tmp_cfg).model_copy(deep=True)
    cfg.claude.binary = str(FAKE)
    cfg.digest.repos = []
    clock = FakeClock(start)
    claude_home = tmp_path / "claude-home"
    claude_home.mkdir(exist_ok=True)
    stamp = (start - timedelta(hours=2)).timestamp()
    os.utime(tmp_vault / "RECENT.md", (stamp, stamp))

    audit = AuditLog(cfg.paths.logs / "jarvisd-audit.jsonl", mirror_stdout=False, clock=clock)
    state = StateStore.from_config(cfg, clock=clock, tz=UTC)
    store = JobStore.from_config(cfg, clock=clock, audit=audit)
    runner = FakeRunner(tmp_path, scenario)
    notes = Notes()
    client = ClaudeClient(cfg, audit, state, runner=runner, enabled=claude, network_probe=lambda: True,
                          sleep=lambda s: None, poll_seconds=0.1)
    collectors = [
        BrainCollector(),
        TaskCollector(base_dir=claude_home),
        GitCollector(),
        SystemCollector(audit, state, store=store, runner=lambda argv, timeout: (1, "")),
    ]
    deps = Deps(cfg=cfg, audit=audit, state=state, store=store, vault=VaultWriter(cfg, audit), claude=client,
                router=build_router(cfg), notifier=notes, collectors=collectors, clock=clock)
    return Rig(cfg, deps, audit, state, store, runner, notes, clock)


@pytest.fixture
def rig(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> Rig:
    return make_rig(tmp_cfg, tmp_path, tmp_vault)


def no_wait(seconds: float) -> None:
    """Stands in for the sleep between ticks, so tests never wait in real time."""


def manual_job(rig: Rig, job_id: str = "digest-2026-10-06", *, state: str = "pending") -> Job:
    now = rig.clock.now
    created = iso(now)
    job = Job(
        id=job_id, kind="morning_digest", key="2026-10-06", job_class="observe_only",
        latency_class="background_batch", origin="manual", created_at=created, not_before=created,
        deadline=iso(now + timedelta(hours=3)), window=JobWindow(start=iso(now - timedelta(hours=36)), end=created),
        history=[HistoryEntry(ts=created, from_state=None, to="pending", note="test")],
    )
    assert rig.store.enqueue(job)
    if state == "running":
        claimed = rig.store.claim_next(now)
        assert claimed is not None
        return claimed
    return job


# --- the named test --------------------------------------------------------------------


def test_serve_three_ticks_runs_todays_job_once(rig: Rig) -> None:
    code = serve(rig.cfg, task_mode=False, max_ticks=3, deps=rig.deps, sleep=no_wait)

    assert code == 0
    assert rig.store.counts() == {"pending": 0, "running": 0, "done": 1, "failed": 0, "held": rig.store.counts()["held"]}
    assert len(rig.events("job_enqueued")) == 1
    assert len(rig.events("digest_start")) == 1
    assert len(rig.events("job_done")) == 1
    assert rig.note().exists()
    assert rig.events("daemon_start") and rig.events("daemon_stop")
    assert (rig.state.dir / "clean_shutdown").exists()
    ok, bad = rig.audit.verify()
    assert ok, bad


# --- single instance, kill, pause, task state ------------------------------------------


def test_second_instance_exits_without_running(rig: Rig, tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig.state.acquire_daemon_lock()
    try:
        second = make_rig(tmp_cfg, tmp_path, tmp_vault)
        code = serve(second.cfg, task_mode=False, max_ticks=3, deps=second.deps, sleep=no_wait)
        assert code == 0
        assert second.store.counts()["done"] == 0 and second.store.counts()["pending"] == 0
        assert not second.note().exists()
        assert not second.events("job_enqueued")
        assert second.events("daemon_refused")
        # The loser must not touch the winner's liveness markers.
        assert not (second.state.dir / "clean_shutdown").exists()
        assert second.state.read_heartbeat() is None
    finally:
        rig.state.release_daemon_lock()


def test_kill_file_returns_3_and_a_restart_with_kill_returns_3_at_once(rig: Rig) -> None:
    (rig.state.dir / "KILL").write_text("stop\n", encoding="utf-8")
    assert serve(rig.cfg, task_mode=False, max_ticks=3, deps=rig.deps, sleep=no_wait) == 3
    assert serve(rig.cfg, task_mode=False, max_ticks=3, deps=rig.deps, sleep=no_wait) == 3
    assert rig.events("kill_file_seen")
    assert rig.store.counts()["pending"] == 0 and rig.store.counts()["done"] == 0
    assert not rig.note().exists()
    # The lock was released on the way out: a later start can take it once KILL is removed.
    rig.state.acquire_daemon_lock()
    rig.state.release_daemon_lock()


def test_kill_appearing_between_ticks_returns_3(rig: Rig) -> None:
    kill = rig.state.dir / "KILL"
    calls: list[float] = []

    def sleeper(seconds: float) -> None:
        calls.append(seconds)
        kill.write_text("stop\n", encoding="utf-8")

    assert serve(rig.cfg, task_mode=False, max_ticks=5, deps=rig.deps, sleep=sleeper) == 3
    assert len(calls) == 1  # tick 1 ran, tick 2 saw KILL
    assert rig.note().exists()
    assert (rig.state.dir / "clean_shutdown").exists()


def test_pause_stops_enqueue_and_claim_but_the_heartbeat_updates(rig: Rig) -> None:
    rig.state.set_pause(None, "test")
    held_job = manual_job(rig)
    assert serve(rig.cfg, task_mode=False, max_ticks=2, deps=rig.deps, sleep=no_wait) == 0
    counts = rig.store.counts()
    assert counts["pending"] == 1 and counts["done"] == 0 and counts["running"] == 0
    assert rig.store.get(held_job.id) is not None and rig.store.get(held_job.id).attempts == 0  # type: ignore[union-attr]
    assert not rig.note().exists()
    beat = rig.state.read_heartbeat()
    assert beat is not None and beat["ts"] == iso(rig.clock.now)
    assert len(rig.events("pause_seen")) == 1  # once per pause, not once per tick


def test_resume_after_pause_lets_the_job_run(rig: Rig) -> None:
    rig.state.set_pause(None, "test")
    assert serve(rig.cfg, task_mode=False, max_ticks=1, deps=rig.deps, sleep=no_wait) == 0
    rig.state.clear_pause()
    assert serve(rig.cfg, task_mode=False, max_ticks=1, deps=rig.deps, sleep=no_wait) == 0
    assert rig.note().exists()


def test_a_disabled_task_causes_a_clean_exit_with_killswitch_seen(rig: Rig) -> None:
    schtasks = Schtasks(DISABLED)
    code = serve(rig.cfg, task_mode=True, max_ticks=3, deps=rig.deps, sleep=no_wait, command_runner=schtasks)
    assert code == 0
    assert rig.events("killswitch_seen")
    assert (rig.state.dir / "clean_shutdown").exists()
    assert not rig.note().exists()
    assert schtasks.calls and schtasks.calls[0][:3] == ["schtasks", "/query", "/tn"]
    assert schtasks.calls[0][3] == "JarvisDaemon"


def test_a_missing_task_does_not_stop_the_daemon(rig: Rig) -> None:
    schtasks = Schtasks((1, "ERROR: The system cannot find the file specified.\n"))
    assert serve(rig.cfg, task_mode=True, max_ticks=1, deps=rig.deps, sleep=no_wait, command_runner=schtasks) == 0
    assert not rig.events("killswitch_seen")
    assert rig.note().exists()


def test_a_localized_disabled_status_is_still_disabled(rig: Rig) -> None:
    schtasks = Schtasks((0, '"\\JarvisDaemon","N/A","D\u00e9sactiv\u00e9"\n'))
    assert serve(rig.cfg, task_mode=True, max_ticks=1, deps=rig.deps, sleep=no_wait, command_runner=schtasks) == 0
    assert rig.events("killswitch_seen")


def test_dev_mode_never_spawns_claude_and_never_queries_the_task(tmp_cfg: Config, tmp_path: Path,
                                                                tmp_vault: Path) -> None:
    rig = make_rig(tmp_cfg, tmp_path, tmp_vault, claude=True)  # a caller that wrongly enabled it
    schtasks = Schtasks(DISABLED)
    assert serve(rig.cfg, task_mode=False, max_ticks=2, deps=rig.deps, sleep=no_wait, command_runner=schtasks) == 0
    assert rig.runner.records() == []
    assert schtasks.calls == []
    assert rig.note().exists()
    assert "degraded_no_llm" in rig.note().read_text(encoding="utf-8")
    start = rig.events("daemon_start")[0]
    assert start["mode"] == "dev"
    assert start["claude_cli_version"] is None


# --- startup record, heartbeat, sockets ---------------------------------------------------


def test_daemon_start_records_mode_account_cli_version_tier_and_config_hash(tmp_cfg: Config, tmp_path: Path,
                                                                           tmp_vault: Path) -> None:
    rig = make_rig(tmp_cfg, tmp_path, tmp_vault, claude=True)
    code = serve(rig.cfg, task_mode=True, max_ticks=1, deps=rig.deps, sleep=no_wait, command_runner=Schtasks())
    assert code == 0
    start = rig.events("daemon_start")[0]
    assert start["mode"] == "task"
    assert start["account"]
    assert start["claude_cli_version"].startswith("2.1.289")
    assert start["local_tier"] == "not_installed"
    assert start["config_sha256"] == rig.cfg.sha256
    assert start["previous_exit"] == "first"
    # In task mode the one Claude call really happens, through the real client.
    assert len(rig.events("claude_call")) == 1
    paid = [r for r in rig.runner.records() if r["argv"][:1] not in (["--version"], ["--help"])]
    assert len(paid) == 1


def test_heartbeat_goes_through_the_atomic_writer(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[str] = []
    real = state_mod.atomic_write_text

    def spy(path: Any, text: str, **kw: Any) -> None:
        written.append(Path(path).name)
        real(path, text, **kw)

    monkeypatch.setattr(state_mod, "atomic_write_text", spy)
    serve(rig.cfg, task_mode=False, max_ticks=2, deps=rig.deps, sleep=no_wait)
    assert "heartbeat.json" in written
    beat = json.loads((rig.state.dir / "heartbeat.json").read_text(encoding="utf-8"))
    assert beat["pid"] == os.getpid() and beat["mode"] == "dev"
    assert not list(rig.state.dir.glob(".heartbeat.json.*.tmp"))


def test_the_daemon_opens_no_listener(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kw: Any) -> None:
        raise AssertionError("the daemon must not bind or listen")

    monkeypatch.setattr(socket.socket, "bind", refuse)
    monkeypatch.setattr(socket.socket, "listen", refuse)
    assert serve(rig.cfg, task_mode=False, max_ticks=3, deps=rig.deps, sleep=no_wait) == 0


# --- recovery and failure containment ------------------------------------------------------


def test_unclean_previous_exit_recovers_the_running_job_once(rig: Rig) -> None:
    job = manual_job(rig, state="running")
    assert job.attempts == 1
    rig.state.heartbeat(job.id)  # a heartbeat without the clean marker is an unclean exit
    assert serve(rig.cfg, task_mode=False, max_ticks=1, deps=rig.deps, sleep=no_wait) == 0
    names = [r["event"] for r in rig.audit.records()]
    assert names.index("unclean_previous_exit") < names.index("job_recover")
    assert rig.events("daemon_start")[0]["previous_exit"] == "unclean"
    done = rig.store.get(job.id)
    assert done is not None and done.state == "done" and done.attempts == 2  # attempts kept, then one more


def test_an_unhandled_error_is_audited_and_the_loop_continues(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def boom(job: Job, deps: Deps, *, mode: str = "daemon") -> dict[str, Any]:
        calls.append(job.id)
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(daemon_mod, "run_digest_job", boom)
    assert serve(rig.cfg, task_mode=False, max_ticks=3, deps=rig.deps, sleep=no_wait) == 0
    failed = rig.events("job_failed")
    assert len(failed) == 1 and failed[0]["error"] == "RuntimeError"
    assert calls == ["digest-2026-10-06"]  # the retry delay keeps ticks 2 and 3 from re-running it
    job = rig.store.get("digest-2026-10-06")
    assert job is not None and job.state == "pending" and job.attempts == 1
    assert job.last_error == "unhandled:RuntimeError"


def test_a_retry_from_the_digest_is_applied_without_consuming_the_attempt(rig: Rig,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    def later(job: Job, deps: Deps, *, mode: str = "daemon") -> dict[str, Any]:
        raise Retry("network", timedelta(minutes=10), consume_attempt=False)

    monkeypatch.setattr(daemon_mod, "run_digest_job", later)
    assert serve(rig.cfg, task_mode=False, max_ticks=1, deps=rig.deps, sleep=no_wait) == 0
    job = rig.store.get("digest-2026-10-06")
    assert job is not None and job.state == "pending" and job.attempts == 0
    assert job.not_before == iso(rig.clock.now + timedelta(minutes=10))


def test_a_killed_retry_puts_the_job_back_and_exits_3(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def killed(job: Job, deps: Deps, *, mode: str = "daemon") -> dict[str, Any]:
        raise Retry("killed", consume_attempt=False, killed=True)

    monkeypatch.setattr(daemon_mod, "run_digest_job", killed)
    assert serve(rig.cfg, task_mode=False, max_ticks=3, deps=rig.deps, sleep=no_wait) == 3
    job = rig.store.get("digest-2026-10-06")
    assert job is not None and job.state == "pending" and job.attempts == 0
    assert (rig.state.dir / "clean_shutdown").exists()


# --- housekeeping and the disk check ---------------------------------------------------------


def test_housekeeping_prunes_old_runs_and_held_references(rig: Rig) -> None:
    runs = rig.state.dir / "runs"
    old, fresh = runs / "digest-2026-08-01", runs / "digest-2026-10-05"
    for folder in (old, fresh):
        folder.mkdir(parents=True)
        (folder / "run.json").write_text("{}", encoding="utf-8")
    stamp = (rig.clock.now - timedelta(days=45)).timestamp()
    os.utime(old, (stamp, stamp))
    result = daemon_mod.housekeeping(rig.deps, rig.clock.now)
    assert not old.exists() and fresh.exists()
    assert result["runs_pruned"] == 1
    assert rig.events("housekeeping")


def test_disk_check_reports_sizes_and_free_space(rig: Rig) -> None:
    (rig.cfg.paths.logs / "x.log").write_text("x" * 2048, encoding="utf-8")
    result = daemon_mod.disk_check(rig.deps)
    assert result["logs_mb"] >= 0 and result["free_gb"] > 0
    assert result["logs_warn"] is False and result["disk_warn"] is False
    record = rig.events("disk_check")[0]
    assert set(record) >= {"logs_mb", "state_mb", "queue_mb", "free_gb"}


def test_tick_reports_busy_instead_of_running_two_jobs_at_once(rig: Rig) -> None:
    assert daemon_mod._TICK_LOCK.acquire(blocking=False)
    try:
        assert tick(rig.deps, rig.clock.now).action == "busy"
    finally:
        daemon_mod._TICK_LOCK.release()
    assert rig.store.counts()["pending"] == 0


# --- the scheduler host ------------------------------------------------------------------------


def test_scheduler_host_registers_the_four_jobs_with_the_documented_triggers(rig: Rig) -> None:
    host = SchedulerHost(rig.cfg, on_tick=lambda: None, on_housekeeping=lambda: None,
                         on_disk_check=lambda: None, tz=UTC)
    assert set(host.job_ids()) == {"tick", "digest_cron", "housekeeping", "disk_check"}
    desc = host.describe()
    assert "interval" in desc["tick"] and str(rig.cfg.daemon.tick_seconds) in desc["tick"]
    assert "hour='6'" in desc["digest_cron"] and "minute='30'" in desc["digest_cron"]
    assert "hour='4'" in desc["housekeeping"] and "minute='10'" in desc["housekeeping"]
    assert "day='1'" in desc["disk_check"] and "hour='9'" in desc["disk_check"]
    assert host.due_at(rig.cfg, NOW.date()) == datetime(2026, 10, 6, 6, 30, tzinfo=UTC)


def test_scheduler_host_fires_the_first_tick_at_once_and_stops_cleanly(rig: Rig) -> None:
    fired = threading.Event()
    errors: list[str] = []
    host = SchedulerHost(rig.cfg, on_tick=fired.set, on_housekeeping=lambda: None, on_disk_check=lambda: None,
                         on_error=lambda name, exc: errors.append(name), tz=UTC)
    host.start()
    try:
        assert fired.wait(10.0)
    finally:
        host.stop()
    assert errors == []
    assert not host.running


def test_scheduler_host_contains_a_job_that_raises(rig: Rig) -> None:
    seen: list[str] = []
    done = threading.Event()

    def bad() -> None:
        raise RuntimeError("synthetic")

    def note(name: str, exc: BaseException) -> None:
        seen.append(f"{name}:{type(exc).__name__}")
        done.set()

    host = SchedulerHost(rig.cfg, on_tick=bad, on_housekeeping=lambda: None, on_disk_check=lambda: None,
                         on_error=note, tz=UTC)
    host.start()
    try:
        assert done.wait(10.0)
    finally:
        host.stop()
    assert seen == ["tick:RuntimeError"]


# --- build_deps ------------------------------------------------------------------------------------


def test_build_deps_wires_the_default_composition(tmp_cfg: Config, tmp_path: Path) -> None:
    deps = daemon_mod.build_deps(tmp_cfg, claude_enabled=False, mirror_stdout=False, task_base_dir=tmp_path)
    assert deps.cfg is tmp_cfg
    assert deps.claude.enabled is False
    assert [type(c).__name__ for c in deps.collectors or []] == [
        "BrainCollector", "TaskCollector", "GitCollector", "GitHubCollector", "SystemCollector"]
    assert deps.audit.path == tmp_cfg.paths.logs / "jarvisd-audit.jsonl"
    assert deps.store.root == tmp_cfg.paths.queue
    enabled = daemon_mod.build_deps(tmp_cfg, claude_enabled=True, mirror_stdout=False)
    assert enabled.claude.enabled is True


# --- the unbounded path with the real APScheduler host -----------------------------------------------


def _listening_pids() -> set[int]:
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, check=False).stdout
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[4].isdigit():
            pids.add(int(parts[4]))
    return pids


@pytest.mark.skipif(sys.platform != "win32", reason="netstat output format is Windows specific")
def test_serve_with_the_real_scheduler_runs_the_job_opens_no_listener_and_stops_on_kill(rig: Rig) -> None:
    rig.cfg.daemon.tick_seconds = 1
    had_listener = os.getpid() in _listening_pids()
    code: list[int] = []
    thread = threading.Thread(
        target=lambda: code.append(serve(rig.cfg, task_mode=False, deps=rig.deps)), name="serve-under-test")
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not rig.note().exists():
            time.sleep(0.1)
        assert rig.note().exists()
        assert had_listener or os.getpid() not in _listening_pids()  # no new listener while it runs
    finally:
        (rig.state.dir / "KILL").write_text("stop\n", encoding="utf-8")
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert code == [3]
    assert len(rig.events("job_enqueued")) == 1 and len(rig.events("job_done")) == 1
    assert (rig.state.dir / "clean_shutdown").exists()



# --- config reload (review fix): the runbook says edit the local file, no restart ----------------------


def _loader_with(cfg: Config, **gates: Any) -> Any:
    fresh = cfg.model_copy(deep=True)
    for key, value in gates.items():
        setattr(fresh.gates, key, value)
    fresh.sha256 = "reloaded-" + "-".join(sorted(gates))
    return lambda: fresh


def test_tick_picks_up_a_new_sensitive_term_without_a_restart(rig: Rig) -> None:
    from jarvisd import tier

    assert tier.text_hit("project acme budget", rig.deps.cfg) is None
    rig.deps.config_loader = _loader_with(rig.cfg, sensitive_terms=["acme"])
    result = tick(rig.deps, rig.clock.now)
    assert result.action != "config_invalid"
    # The same object the collectors and gates already hold now carries the new term.
    assert tier.text_hit("project acme budget", rig.deps.cfg) is not None
    assert rig.deps.cfg.sha256 == "reloaded-sensitive_terms"
    reloaded = rig.events("config_reloaded")
    assert len(reloaded) == 1 and "acme" not in json.dumps(reloaded)


def test_an_unchanged_config_is_not_reapplied(rig: Rig) -> None:
    rig.deps.config_loader = lambda: rig.cfg.model_copy(deep=True)
    tick(rig.deps, rig.clock.now)
    tick(rig.deps, rig.clock.now)
    assert rig.events("config_reloaded") == []


def test_an_invalid_config_runs_no_job_and_keeps_the_old_rules(rig: Rig) -> None:
    from jarvisd.config import ConfigError

    def broken() -> Config:
        raise ConfigError("jarvis.local.toml has unknown key(s): gates.sensitive_term")

    rig.deps.config_loader = broken
    manual_job(rig)
    sha = rig.deps.cfg.sha256
    memory: dict[str, Any] = {}  # the serve loop keeps one between ticks
    first = tick(rig.deps, rig.clock.now, memory=memory)
    second = tick(rig.deps, rig.clock.now, memory=memory)
    assert first.action == "config_invalid" and second.action == "config_invalid"
    assert rig.store.counts()["pending"] == 1 and rig.events("job_done") == []
    assert rig.deps.cfg.sha256 == sha
    assert len(rig.events("config_invalid")) == 1  # audited once, not every two minutes


def test_a_fixed_config_resumes_the_queue(rig: Rig) -> None:
    from jarvisd.config import ConfigError

    state = {"broken": True}

    def loader() -> Config:
        if state["broken"]:
            raise ConfigError("bad")
        return rig.cfg.model_copy(deep=True)

    rig.deps.config_loader = loader
    manual_job(rig)
    assert tick(rig.deps, rig.clock.now).action == "config_invalid"
    state["broken"] = False
    assert tick(rig.deps, rig.clock.now).action == "ran"


def test_an_unknown_router_adapter_in_the_new_config_is_invalid(rig: Rig) -> None:
    fresh = rig.cfg.model_copy(deep=True)
    fresh.router.adapter = "nonexistent"
    fresh.sha256 = "other"
    rig.deps.config_loader = lambda: fresh
    assert tick(rig.deps, rig.clock.now).action == "config_invalid"
    assert rig.deps.cfg.router.adapter == "stub"
