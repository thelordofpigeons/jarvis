"""The command line: every subcommand, its exit codes and its output.

`cli.main` takes injectable collaborators (config, the `claude` runner, the notifier, the
scheduled-task runner), so these tests drive the real code paths against a throwaway machine
and the fake binary in tests/fakes/fake_claude.py. Nothing here spends money (design D10).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jarvisd import ROOT, cli
from jarvisd.audit import AuditLog
from jarvisd.claude import default_runner
from jarvisd.config import Config
from jarvisd.jobstore import JobStore
from jarvisd.models import WithheldItem
from jarvisd.notify import NotifyResult
from jarvisd.state import StateStore

FAKE = ROOT / "tests" / "fakes" / "fake_claude.py"
READY = (0, '"\\JarvisDaemon","10/7/2026 6:00:00 AM","Ready"\n')


class FakeRunner:
    """Starts the fake instead of `claude`; adds the FAKE_* variables the client never passes."""

    def __init__(self, tmp_path: Path, scenario: str = "ok") -> None:
        self.log = tmp_path / "fake-claude.jsonl"
        self.scenario = scenario

    def __call__(self, argv: Any, *, env: Any, cwd: str, creationflags: int) -> Any:
        full = [sys.executable, str(FAKE), *list(argv)[1:]]
        env2 = {**env, "FAKE_CLAUDE_LOG": str(self.log), "FAKE_CLAUDE_SCENARIO": self.scenario}
        return default_runner(full, env=env2, cwd=cwd, creationflags=creationflags)

    def paid(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]
        return [r for r in rows if r["argv"][:1] not in (["--version"], ["--help"])]

    def all(self) -> list[dict[str, Any]]:
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


class Commands:
    """Fake command runner for `schtasks` and `powershell`: canned answer, remembered argv."""

    def __init__(self, answer: tuple[int, str] = READY) -> None:
        self.answer = answer
        self.calls: list[list[str]] = []

    def __call__(self, argv: Any, timeout: float) -> tuple[int, str]:
        self.calls.append(list(argv))
        return self.answer


@dataclass
class Env:
    cfg: Config
    runner: FakeRunner
    notes: Notes
    commands: Commands
    vault: Path
    tmp_path: Path

    def run(self, *argv: str) -> int:
        return cli.main(
            list(argv), cfg=self.cfg, claude_runner=self.runner, notifier=self.notes,
            command_runner=self.commands, task_base_dir=self.tmp_path / "claude-home",
            network_probe=lambda: True,
        )

    @property
    def audit(self) -> AuditLog:
        return AuditLog(self.cfg.paths.logs / "jarvisd-audit.jsonl", mirror_stdout=False)

    @property
    def state(self) -> StateStore:
        return StateStore.from_config(self.cfg)

    @property
    def store(self) -> JobStore:
        return JobStore.from_config(self.cfg)

    def events(self, name: str) -> list[dict[str, Any]]:
        return self.audit.records(events=[name])

    def raw(self, name: str) -> Path:
        return self.vault / "raw" / "jarvis" / name


@pytest.fixture
def env(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> Env:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.claude.binary = str(FAKE)
    cfg.digest.repos = []
    (tmp_path / "claude-home").mkdir()
    return Env(cfg, FakeRunner(tmp_path), Notes(), Commands(), tmp_vault, tmp_path)


def tree(*roots: Path) -> list[str]:
    return sorted(p.as_posix() for root in roots for p in root.rglob("*") if p.is_file())


# --- parsing -----------------------------------------------------------------------------------


def test_every_subcommand_parses() -> None:
    parser = cli.build_parser()
    forms = [
        ["serve"], ["serve", "--task", "--max-ticks", "3"],
        ["run-digest"], ["run-digest", "--claude", "--date", "2026-10-06", "--no-notify", "--dry-run", "--force"],
        ["status"], ["status", "--json"],
        ["digest"], ["digest", "--path", "--date", "2026-10-06"],
        ["held"], ["held", "w-3a9f1c"], ["held", "--date", "2026-10-06"],
        ["wrong", "w-3a9f1c"], ["wrong", "w-3a9f1c", "--should", "escalate", "--note", "text", "--leak"],
        ["wrong", "--list"],
        ["pause"], ["pause", "--for", "4h", "--reason", "x"], ["resume"],
        ["audit", "tail"], ["audit", "tail", "-n", "5"], ["audit", "verify"], ["audit", "verify", "--all"],
        ["audit", "cost"], ["audit", "cost", "--days", "3"],
        ["breaker", "status"], ["breaker", "reset", "--reason", "checked"],
        ["self-test"], ["self-test", "--live"],
        ["install-task"], ["install-task", "--apply"], ["install-task", "--unregister"],
    ]
    for form in forms:
        parser.parse_args(form)


def test_usage_errors_exit_2_without_a_traceback(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("no-such-command") == 2
    assert env.run("run-digest", "--date", "not-a-date") == 2
    assert env.run("wrong") == 2  # an id is required unless --list
    assert env.run("pause", "--for", "soon") == 2
    assert env.run("breaker", "reset") == 2  # --reason is required
    assert env.run("audit") == 2
    assert env.run("--help") == 0
    capsys.readouterr()


def test_the_cli_source_has_no_em_or_en_dashes() -> None:
    text = (ROOT / "jarvisd" / "cli.py").read_text(encoding="utf-8")
    assert "\u2014" not in text and "\u2013" not in text


# --- run-digest --------------------------------------------------------------------------------


def test_run_digest_default_spends_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code = env.run("run-digest", "--date", "2026-10-03")
    out = capsys.readouterr().out
    assert code == 0, out
    assert env.runner.paid() == []  # the fake claude is never even started
    assert env.runner.all() == []
    assert env.state.budget.snapshot()["calls"] == 0
    assert env.raw("digest-2026-10-03.md").exists()
    assert "degraded_no_llm" in env.raw("digest-2026-10-03.md").read_text(encoding="utf-8")
    start = env.events("digest_start")[0]
    assert start["mode"] == "manual" and start["no_claude"] is True
    assert not env.events("claude_call") and not env.events("claude_intent")
    assert len(env.notes.messages) == 1  # a toast is sent unless --no-notify
    assert "digest-2026-10-03" in out


def test_run_digest_dry_run_prints_the_payload_and_writes_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    before = tree(env.vault, env.cfg.paths.queue)
    code = env.run("run-digest", "--dry-run", "--claude", "--date", "2026-10-03")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "<data>" in out  # the exact payload that would leave the machine
    assert "Held" in out or "held" in out
    assert tree(env.vault, env.cfg.paths.queue) == before
    assert env.runner.all() == []  # spawns nothing, not even --version
    assert not (env.cfg.daemon.state_dir / "watermark.json").exists()
    assert env.state.budget.snapshot()["calls"] == 0
    assert env.notes.messages == []


def test_run_digest_claude_writes_a_note_and_audits_mode_manual(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code = env.run("run-digest", "--claude", "--no-notify", "--date", "2026-10-03")
    assert code == 0, capsys.readouterr().out
    note = env.raw("digest-2026-10-03.md")
    assert note.exists() and "generator: jarvisd" in note.read_text(encoding="utf-8")
    assert env.events("digest_start")[0]["mode"] == "manual"
    assert len(env.events("claude_call")) == 1
    assert len(env.runner.paid()) == 1
    assert env.notes.messages == []  # --no-notify
    job = env.store.get("digest-2026-10-03")
    assert job is not None and job.state == "done" and job.origin == "manual"
    ok, bad = env.audit.verify()
    assert ok, bad


def test_run_digest_refuses_an_existing_date_unless_forced(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("run-digest", "--no-notify", "--date", "2026-10-03") == 0
    capsys.readouterr()
    assert env.run("run-digest", "--no-notify", "--date", "2026-10-03") == 1
    assert "--force" in capsys.readouterr().out
    assert env.run("run-digest", "--no-notify", "--force", "--date", "2026-10-03") == 0
    assert env.raw("digest-2026-10-03-r2.md").exists()
    assert env.run("run-digest", "--no-notify", "--force", "--date", "2026-10-03") == 0
    assert env.raw("digest-2026-10-03-r3.md").exists()


def test_run_digest_exits_3_when_the_kill_file_is_present(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    (env.cfg.daemon.state_dir / "KILL").write_text("stop\n", encoding="utf-8")
    assert env.run("run-digest", "--claude", "--date", "2026-10-03") == 3
    assert env.runner.paid() == []
    assert not env.raw("digest-2026-10-03.md").exists()
    capsys.readouterr()


# --- status ------------------------------------------------------------------------------------


STATUS_KEYS = {
    "running", "pid", "heartbeat_age_s", "version", "mode", "claude_cli_version", "local_tier", "breaker",
    "budget", "queue", "last_digest", "watermark", "next_due", "kill", "pause", "held_count", "audit",
}


def test_status_exits_1_when_stopped_and_json_has_the_documented_keys(env: Env,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    code = env.run("status", "--json")
    data = json.loads(capsys.readouterr().out)
    assert code == 1
    assert STATUS_KEYS <= set(data)
    assert data["running"] is False and data["local_tier"] == "not_installed"
    assert data["kill"] is False and data["pause"] is None
    assert data["budget"]["calls"] == 0 and data["queue"]["pending"] == 0
    assert data["audit"]["seq"] >= 0


def test_status_exits_0_when_the_daemon_holds_the_lock(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    daemon_state = env.state
    daemon_state.acquire_daemon_lock()
    try:
        daemon_state.heartbeat(None, mode="task")
        code = env.run("status", "--json")
        data = json.loads(capsys.readouterr().out)
        assert code == 0
        assert data["running"] is True and data["mode"] == "task" and data["pid"] > 0
        assert data["heartbeat_age_s"] is not None
    finally:
        daemon_state.release_daemon_lock()


def test_status_text_is_plain_sentences(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("status") == 1
    out = capsys.readouterr().out
    assert "stopped" in out.lower() and "local tier: not_installed" in out.lower()
    assert "\u2014" not in out and "\u2013" not in out


# --- digest, held ------------------------------------------------------------------------------


def test_digest_prints_the_latest_note_or_its_path(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("digest") == 1  # nothing yet
    capsys.readouterr()
    for day in ("2026-10-02", "2026-10-04"):
        env.raw(f"digest-{day}.md").write_text(f"---\ngenerator: jarvisd\n---\n# Digest {day}\n", encoding="utf-8")
    assert env.run("digest", "--path") == 0
    assert capsys.readouterr().out.strip().endswith("digest-2026-10-04.md")
    assert env.run("digest") == 0
    assert "# Digest 2026-10-04" in capsys.readouterr().out
    assert env.run("digest", "--date", "2026-10-02") == 0
    assert "# Digest 2026-10-02" in capsys.readouterr().out
    assert env.run("digest", "--date", "2026-09-01") == 1


def test_held_resolves_an_id_to_its_source_and_reason(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    ref = WithheldItem(id="w-3a9f1c", kind="brain_session", source_ref="C:/synthetic/brain/sessions/x.md",
                       reason="glob_floor")
    env.store.hold(ref, "digest-2026-10-06")
    assert env.run("held", "w-3a9f1c") == 0
    out = capsys.readouterr().out
    assert "C:/synthetic/brain/sessions/x.md" in out and "glob_floor" in out and "brain_session" in out
    assert env.run("held", "--date", "2026-10-06") == 0
    assert "w-3a9f1c" in capsys.readouterr().out
    assert env.run("held", "--date", "2026-10-07") == 0
    assert "w-3a9f1c" not in capsys.readouterr().out
    assert env.run("held", "w-nothing") == 1


# --- wrong -------------------------------------------------------------------------------------


def test_wrong_writes_a_correction_and_audits_it(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.audit.emit("gate_decision", item_id="brain-abc123", route="claude", decided_by="confidence",
                   local_tier="not_installed", reasons=["confidence_below_threshold"], importance="low",
                   confidence=0.0, category="brain_thread")
    code = env.run("wrong", "brain-abc123", "--should", "hold", "--note", "this was private")
    assert code == 0, capsys.readouterr().out
    lines = (env.cfg.daemon.state_dir / "corrections.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["id"] == "brain-abc123" and row["should"] == "hold" and row["note"] == "this was private"
    assert row["leak"] is False and row["snapshot"]["route"] == "claude"
    record = env.events("correction")[0]
    assert record["item_id"] == "brain-abc123" and record["should"] == "hold" and record["leak"] is False
    assert "this was private" not in json.dumps(record)  # free text stays out of the audit
    assert env.state.breaker.peek()["state"] == "closed"
    assert env.run("wrong", "--list") == 0
    assert "brain-abc123" in capsys.readouterr().out


def test_wrong_with_an_unknown_id_exits_2_and_writes_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("wrong", "nothing-like-this") == 2
    assert not (env.cfg.daemon.state_dir / "corrections.jsonl").exists()
    assert not env.events("correction")
    capsys.readouterr()


def test_wrong_leak_opens_the_breaker_and_prints_the_runbook(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.store.hold(WithheldItem(id="w-aaaaaa", kind="git_commit", source_ref="repo:abc", reason="tag_inline"),
                   "digest-2026-10-06")
    assert env.run("wrong", "w-aaaaaa", "--leak") == 0
    out = capsys.readouterr().out
    peek = env.state.breaker.peek()
    assert peek["state"] == "open" and peek["requires_human_reset"] is True
    assert "audit verify" in out and "state/KILL" in out
    assert env.events("correction")[0]["leak"] is True
    assert any(r.get("action") == "trip" for r in env.events("breaker"))


# --- pause, resume, breaker --------------------------------------------------------------------


def test_pause_and_resume_toggle_the_pause_file(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("pause", "--for", "4h", "--reason", "travelling") == 0
    info = env.state.pause_info()
    assert env.state.paused() and info is not None and info["reason"] == "travelling" and info["until"]
    assert env.run("pause") == 0  # no duration: until resumed
    info = env.state.pause_info()
    assert info is not None and info["until"] is None
    assert env.run("resume") == 0
    assert not env.state.paused()
    assert env.run("resume") == 0  # idempotent
    capsys.readouterr()


def test_breaker_status_and_reset(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.state.breaker.trip("isolation_breach", requires_reset=True)
    assert env.run("breaker", "status") == 0
    out = capsys.readouterr().out
    assert "open" in out and "isolation_breach" in out
    assert env.run("breaker", "reset", "--reason", "checked by hand") == 0
    assert env.state.breaker.peek()["state"] == "closed"
    assert any(r.get("action") == "reset" for r in env.events("breaker"))
    capsys.readouterr()


# --- audit -------------------------------------------------------------------------------------


def _fill_audit(env: Env, n: int = 5) -> Path:
    log = env.audit
    for i in range(n):
        log.emit("cli_test", n=i, cost_usd=0.0)
    return log.path


def test_audit_verify_returns_1_on_a_tampered_chain(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    path = _fill_audit(env)
    assert env.run("audit", "verify") == 0
    assert "ok" in capsys.readouterr().out.lower()
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace('"n":2', '"n":9')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    assert env.run("audit", "verify") == 1
    assert "3" in capsys.readouterr().out  # the first broken seq
    assert env.run("audit", "verify", "--all") == 1
    capsys.readouterr()


def test_audit_tail_prints_the_last_records(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    _fill_audit(env, 6)
    assert env.run("audit", "tail", "-n", "2") == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert [r["n"] for r in rows] == [4, 5]


def test_audit_cost_lists_days(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("audit", "cost", "--days", "3") == 0
    out = capsys.readouterr().out
    assert out.count("USD") >= 3


# --- self-test ---------------------------------------------------------------------------------


def test_self_test_prints_pass_lines_and_exits_0_when_everything_holds(env: Env,
                                                                      capsys: pytest.CaptureFixture[str]) -> None:
    env.cfg.notify.toast_script.write_text("# synthetic toast script\n", encoding="utf-8")
    code = env.run("self-test")
    out = capsys.readouterr().out
    assert code == 0, out
    lines = [line for line in out.splitlines() if line.strip()]
    assert any(line.startswith("[PASS]") for line in lines)
    assert not any(line.startswith("[FAIL]") for line in lines)
    assert lines[-1].startswith("self-test:")
    for name in ("config-loads", "floor-present", "vault-writer-denies", "tier-fixtures-hit",
                 "gate-order-truth-table", "build-argv-golden", "claude-resolves", "killswitch-alignment"):
        assert any(f"] {name}" in line for line in lines), name
    assert "kill switch finds the v1 daemon" in out and "process-level kill does not cover" not in out


def test_self_test_exits_1_on_any_fail(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.cfg.claude.binary = str(env.tmp_path / "no-such-claude.exe")
    assert env.run("self-test") == 1
    out = capsys.readouterr().out
    assert "[FAIL] claude-resolves" in out
    assert out.strip().splitlines()[-1].startswith("self-test:")


def test_self_test_reports_a_disabled_daemon_task_as_a_failure(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.commands.answer = (0, '"\\JarvisDaemon","N/A","Disabled"\n')
    assert env.run("self-test") == 1
    assert "[FAIL] daemon-task-state" in capsys.readouterr().out


def test_self_test_live_does_not_spend_in_this_build(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.run("self-test", "--live")
    out = capsys.readouterr().out
    assert "live-smoke" in out and env.runner.paid() == []


# --- install-task ------------------------------------------------------------------------------


def test_install_task_prints_the_registration_block(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("install-task") == 0
    out = capsys.readouterr().out
    assert "Register-ScheduledTask" in out and "-m jarvisd serve --task" in out
    assert "pythonw.exe" in out and "-MultipleInstances IgnoreNew" in out and "-RunLevel Limited" in out
    assert env.commands.calls == []  # printing registers nothing
    assert env.run("install-task", "--unregister") == 0
    assert "Unregister-ScheduledTask" in capsys.readouterr().out
    assert env.commands.calls == []


def test_install_task_apply_runs_powershell_with_a_list_argv(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("install-task", "--apply") == 0
    assert len(env.commands.calls) == 1
    argv = env.commands.calls[0]
    assert argv[0].lower().startswith("powershell") and "-NoProfile" in argv
    assert any("Register-ScheduledTask" in part for part in argv)
    capsys.readouterr()


# --- serve and the self-test folders -----------------------------------------------------------


def test_serve_in_dev_mode_warns_and_stops_after_max_ticks(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.state.set_pause(None, "keep the test from running a digest")
    assert env.run("serve", "--max-ticks", "1") == 0
    out = capsys.readouterr().out
    assert "Dev mode" in out and "not covered by the kill switch" in out
    assert env.runner.all() == []  # dev mode never starts claude, not even for --version
    assert env.events("daemon_start")[0]["mode"] == "dev"
    assert env.run("serve", "--max-ticks", "0") == 2
    capsys.readouterr()


def test_self_test_creates_the_runtime_folders_it_needs(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    import shutil

    shutil.rmtree(env.cfg.daemon.state_dir)
    env.run("self-test")
    assert env.cfg.daemon.state_dir.is_dir()
    assert "[PASS] required-dirs" in capsys.readouterr().out


def test_a_real_run_gives_the_daemon_a_config_loader(tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    # The runbook says to edit jarvis.local.toml and reset the breaker without a restart, so a
    # real run (config read from the files) must hand the resident loop a way to re-read them.
    seen: dict[str, Any] = {}

    def capture(ctx: cli.Ctx, args: Any) -> int:
        seen["ctx"] = ctx
        return 0

    monkeypatch.setitem(cli.HANDLERS, "serve", capture)
    monkeypatch.setattr(cli, "load_config", lambda: tmp_cfg)
    assert cli.main(["serve", "--max-ticks", "1"]) == 0
    ctx = seen["ctx"]
    assert ctx.config_loader is cli.load_config or ctx.config_loader() is tmp_cfg
    assert ctx.deps(claude_enabled=False).config_loader is ctx.config_loader


def test_an_injected_config_is_never_reloaded_from_disk(tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setitem(cli.HANDLERS, "serve", lambda ctx, args: seen.setdefault("ctx", ctx) and 0)
    cli.main(["serve", "--max-ticks", "1"], cfg=tmp_cfg)
    assert seen["ctx"].config_loader is None


def test_the_runbook_says_no_restart_is_needed() -> None:
    assert "no restart" in " ".join(cli.RUNBOOK.split())


def _main_at(env: Env, when: datetime, *argv: str) -> int:
    return cli.main(
        list(argv), cfg=env.cfg, claude_runner=env.runner, notifier=env.notes, command_runner=env.commands,
        task_base_dir=env.tmp_path / "claude-home", network_probe=lambda: True, clock=lambda: when,
    )


def test_a_plain_run_before_the_scheduled_time_leaves_the_scheduled_job_id_free(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    early = datetime(2026, 10, 6, 5, 40, tzinfo=timezone.utc)  # before the 06:30 digest is due
    assert _main_at(env, early, "run-digest", "--no-notify") == 0
    out = capsys.readouterr().out
    assert "digest-2026-10-06-r2" in out and "scheduled job is left alone" in out
    assert env.raw("digest-2026-10-06-r2.md").exists() and not env.raw("digest-2026-10-06.md").exists()
    assert env.store.exists("digest-2026-10-06") is None
    # So the daemon's reconcile still enqueues the Claude digest once the time comes.
    from jarvisd.scheduler import reconcile

    later = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    assert reconcile(later, env.cfg, env.state, env.store, None) == "digest-2026-10-06"


def test_a_claude_run_before_the_scheduled_time_takes_the_scheduled_id(env: Env) -> None:
    early = datetime(2026, 10, 6, 5, 40, tzinfo=timezone.utc)
    assert _main_at(env, early, "run-digest", "--claude", "--no-notify") == 0
    assert env.store.exists("digest-2026-10-06") == "done"


def test_a_plain_run_after_the_scheduled_time_keeps_the_ordinary_id(env: Env) -> None:
    late = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    assert _main_at(env, late, "run-digest", "--no-notify") == 0
    assert env.store.exists("digest-2026-10-06") == "done"


# --- output encoding -------------------------------------------------------------------------
# A redirected or piped stdout on Windows uses the locale code page (cp1252). A note with an
# arrow or an emoji in it then crashed `ask --dry-run`, `run-digest --dry-run` and
# `consolidate --dry-run` with UnicodeEncodeError, after the paid call in the `ask` case.


def _cp1252_streams() -> tuple[Any, Any, Any, Any]:
    import io

    out_raw, err_raw = io.BytesIO(), io.BytesIO()
    out = io.TextIOWrapper(out_raw, encoding="cp1252", errors="strict", newline="\n")
    err = io.TextIOWrapper(err_raw, encoding="cp1252", errors="strict", newline="\n")
    return out, err, out_raw, err_raw


def test_main_survives_a_cp1252_pipe_with_a_character_outside_the_code_page(
    tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    arrow = chr(0x2192) + chr(0x1F600)  # not representable in cp1252

    def handler(ctx: Any, args: Any) -> int:
        print("note says " + arrow)
        print("to stderr " + arrow, file=sys.stderr)
        return 0

    out, err, out_raw, err_raw = _cp1252_streams()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setitem(cli.HANDLERS, "status", handler)
    assert cli.main(["status"], cfg=tmp_cfg) == 0
    out.flush()
    err.flush()
    assert out_raw.getvalue().decode("utf-8").strip() == "note says " + arrow
    assert err_raw.getvalue().decode("utf-8").strip() == "to stderr " + arrow


def test_a_stream_without_reconfigure_is_left_alone(tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr(sys, "stdout", io.StringIO())  # no reconfigure(), like some capture objects
    monkeypatch.setattr(sys, "stderr", None)  # pythonw has no streams at all
    monkeypatch.setitem(cli.HANDLERS, "status", lambda ctx, args: 0)
    assert cli.main(["status"], cfg=tmp_cfg) == 0


def test_dry_runs_do_not_crash_on_a_cp1252_pipe(tmp_cfg: Config, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real commands, end to end, with the arrow in a note and in the question the dry run echoes."""
    arrow = chr(0x2192)
    recent = tmp_vault / "RECENT.md"
    recent.write_text(recent.read_text(encoding="utf-8") + f"\n## Open Threads\n- Synthetic thread {arrow} next step\n",
                      encoding="utf-8")
    out, err, out_raw, _ = _cp1252_streams()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    for argv in (["run-digest", "--dry-run"], ["ask", "--dry-run", "what is open " + arrow + " next?"]):
        assert cli.main(argv, cfg=tmp_cfg) in (0, 1, 3), argv
    out.flush()
    assert arrow in out_raw.getvalue().decode("utf-8")
