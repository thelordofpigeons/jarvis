"""Crash trail: the daemon in a real subprocess, killed hard while a paid call is in flight.

The audit must show `claude_intent` without `claude_call`, then on the next start
`unclean_previous_exit` and `job_recover`; the budget reservation must still be counted;
the hash chain must verify across the crash. A second subprocess proves the excepthook writes
`daemon_crash`. The `claude` binary is tests/fakes/fake_claude.py in scenario `hang`, and
the toast is a null notifier, so nothing is paid and nothing is shown (design D10).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jarvisd import ROOT
from jarvisd.audit import AuditLog
from jarvisd.config import Config, build_config
from jarvisd.common import iso
from jarvisd.jobstore import JobStore
from jarvisd.models import HistoryEntry, Job, JobWindow
from jarvisd.state import StateStore

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="taskkill tree kill is Windows only")

FAKE = ROOT / "tests" / "fakes" / "fake_claude.py"

DRIVER = r'''
"""Test driver: runs the real daemon with the fake claude, or crashes on purpose."""
import json
import sys
from pathlib import Path

root, cfg_json, mode, scenario, log, max_ticks = sys.argv[1:7]
sys.path.insert(0, root)

from jarvisd.__main__ import install_crash_handlers
from jarvisd.claude import default_runner
from jarvisd.config import build_config
from jarvisd.daemon import build_deps, serve
from jarvisd.notify import NullNotifier

cfg = build_config(json.loads(Path(cfg_json).read_text(encoding="utf-8")))
install_crash_handlers(cfg.paths.logs)

if mode == "crash":
    raise RuntimeError("synthetic crash with a private-looking message")

fake = str(Path(root) / "tests" / "fakes" / "fake_claude.py")


def runner(argv, *, env, cwd, creationflags):
    env2 = {**env, "FAKE_CLAUDE_LOG": log, "FAKE_CLAUDE_SCENARIO": scenario}
    return default_runner([sys.executable, fake, *list(argv)[1:]], env=env2, cwd=cwd, creationflags=creationflags)


def commands(argv, timeout):
    return 0, '"\\JarvisDaemon","N/A","Ready"\n'


deps = build_deps(cfg, claude_enabled=True, mirror_stdout=False, claude_runner=runner,
                  notifier=NullNotifier(), command_runner=commands,
                  task_base_dir=Path(cfg_json).parent / "claude-home", network_probe=lambda: True)
ticks = int(max_ticks) or None
sys.exit(serve(cfg, task_mode=True, max_ticks=ticks, deps=deps, command_runner=commands))
'''


def raw_config(tmp_path: Path, tmp_vault: Path) -> dict[str, Any]:
    """The real jarvis.toml with every path moved under tmp_path, as plain JSON-able data."""
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    root = tmp_path / "jarvis"
    for name in ("queue", "logs", "models", "state", "deploy"):
        (root / name).mkdir(parents=True, exist_ok=True)
    fwd = lambda p: p.as_posix()  # noqa: E731
    raw["paths"].update({
        "root": fwd(root), "queue": fwd(root / "queue"), "logs": fwd(root / "logs"), "models": fwd(root / "models"),
        "vault_write_raw": fwd(tmp_vault / "raw" / "jarvis"), "vault_write_sessions": fwd(tmp_vault / "sessions"),
        "vault_forbidden": [fwd(tmp_vault / "telos"), fwd(tmp_vault / "notes"), fwd(tmp_path / "Documents" / "Work")],
    })
    raw.setdefault("daemon", {})["state_dir"] = fwd(root / "state")
    raw.setdefault("notify", {})["toast_script"] = fwd(root / "deploy" / "notify-jarvis.ps1")
    raw["claude"]["binary"] = fwd(FAKE)
    raw.setdefault("digest", {})["repos"] = []
    return raw


class Machine:
    def __init__(self, tmp_path: Path, tmp_vault: Path) -> None:
        self.tmp_path = tmp_path
        raw = raw_config(tmp_path, tmp_vault)
        self.cfg: Config = build_config(raw)
        self.cfg_json = tmp_path / "config.json"
        self.cfg_json.write_text(json.dumps(raw), encoding="utf-8")
        (tmp_path / "claude-home").mkdir()
        self.driver = tmp_path / "driver.py"
        self.driver.write_text(DRIVER, encoding="utf-8", newline="\n")
        self.fake_log = tmp_path / "fake-claude.jsonl"
        self.audit = AuditLog(self.cfg.paths.logs / "jarvisd-audit.jsonl", mirror_stdout=False)

    def start(self, mode: str, scenario: str, max_ticks: int = 0) -> subprocess.Popen[bytes]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
        return subprocess.Popen(
            [sys.executable, str(self.driver), str(ROOT), str(self.cfg_json), mode, scenario,
             str(self.fake_log), str(max_ticks)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=str(self.tmp_path),
        )

    def events(self, name: str) -> list[dict[str, Any]]:
        return self.audit.records(events=[name])

    def seed_job(self) -> str:
        today = datetime.now().astimezone().date()
        now = datetime.now().astimezone() - timedelta(minutes=1)
        created = iso(now)
        job = Job(
            id=f"digest-{today.isoformat()}", kind="morning_digest", key=today.isoformat(), job_class="observe_only",
            latency_class="background_batch", origin="manual", created_at=created, not_before=created,
            deadline=iso(now + timedelta(hours=3)), window=JobWindow(start=iso(now - timedelta(hours=36)), end=created),
            history=[HistoryEntry(ts=created, from_state=None, to="pending", note="test")],
        )
        assert JobStore.from_config(self.cfg).enqueue(job)
        return job.id


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_for(predicate: Any, proc: subprocess.Popen[bytes], seconds: float = 60.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        if proc.poll() is not None:
            return bool(predicate())
        time.sleep(0.2)
    return False


def test_hard_kill_during_a_call_leaves_a_complete_trail(tmp_path: Path, tmp_vault: Path) -> None:
    machine = Machine(tmp_path, tmp_vault)
    job_id = machine.seed_job()

    first = machine.start("serve", "hang")
    try:
        assert wait_for(lambda: machine.events("claude_intent"), first), (first.stdout.read() if first.stdout else b"")
    finally:
        kill_tree(first)

    # Run 1 left an intent and no result, and the reservation is already on the ledger.
    assert len(machine.events("claude_intent")) == 1
    assert machine.events("claude_call") == []
    snapshot = StateStore.from_config(machine.cfg).budget.snapshot()
    assert snapshot["reserved_usd"] == pytest.approx(machine.cfg.claude.max_budget_usd)
    assert (machine.cfg.daemon.state_dir / "heartbeat.json").exists()
    assert not (machine.cfg.daemon.state_dir / "clean_shutdown").exists()
    assert JobStore.from_config(machine.cfg).exists(job_id) == "running"

    second = machine.start("serve", "ok", max_ticks=1)
    out, _ = second.communicate(timeout=120)
    assert second.returncode == 0, out.decode("utf-8", errors="replace")

    names = [r["event"] for r in machine.audit.records()]
    assert names.count("claude_intent") == 2 and names.count("claude_call") == 1
    first_intent = names.index("claude_intent")
    unclean = names.index("unclean_previous_exit")
    recover = names.index("job_recover")
    assert first_intent < unclean < recover
    assert "claude_call" not in names[:unclean]
    starts = machine.events("daemon_start")
    assert [s["previous_exit"] for s in starts] == ["first", "unclean"]

    job = JobStore.from_config(machine.cfg).get(job_id)
    assert job is not None and job.state == "done" and job.attempts == 2  # attempts kept across the crash
    ok, bad = machine.audit.verify()
    assert ok, bad  # the chain verifies across the crash
    final = StateStore.from_config(machine.cfg).budget.snapshot()
    assert final["calls"] == 2  # the crashed call is counted, not forgotten


def test_the_excepthook_writes_daemon_crash_without_the_message(tmp_path: Path, tmp_vault: Path) -> None:
    machine = Machine(tmp_path, tmp_vault)
    proc = machine.start("crash", "ok")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode != 0, out
    crash_log = (machine.cfg.paths.logs / "jarvisd-crash.log").read_text(encoding="utf-8")
    assert "RuntimeError" in crash_log and "synthetic crash" in crash_log
    records = machine.events("daemon_crash")
    assert len(records) == 1
    assert records[0]["error_type"] == "RuntimeError"
    assert "private-looking" not in json.dumps(records[0])  # the message may carry paths or content
    assert machine.audit.verify() == (True, None)
