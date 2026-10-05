"""bin/kill-switch.ps1 and tests/hostile-sim.ps1 for the v1 daemon (docs/killswitch-v1-patch.md).

Static checks run everywhere. The behavioural checks need Windows PowerShell and run the real
script against a temp state folder, a task name that is not registered and markers that no real
process carries, so they cannot touch the live JarvisDaemon task, its state or its audit log.
The full hostile simulation (real processes, a real scheduled task) is opt-in: set
JARVIS_RUN_HOSTILE_SIM=1.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from jarvisd import ROOT

KILL = ROOT / "bin" / "kill-switch.ps1"
SIM = ROOT / "tests" / "hostile-sim.ps1"
POWERSHELL = shutil.which("powershell") if sys.platform == "win32" else None
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required")


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- static ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [KILL, SIM])
def test_scripts_are_ascii_lf_without_bom(path: Path) -> None:
    # The elevated task runs the script under Windows PowerShell 5.1, which reads a BOM-less
    # file as ANSI, so anything outside ASCII would be misread.
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    raw.decode("ascii")


def test_kill_switch_declares_the_v1_parameters_with_safe_defaults() -> None:
    src = text(KILL)
    for name, default in (
        ("TaskName", "'JarvisDaemon'"),
        ("V1Marker", "'jarvisd serve --task'"),
        ("ClaudeMarker", "'--no-session-persistence --permission-prompts none'"),
    ):
        assert re.search(rf"\[string\]\s*\${name}\s*=\s*{re.escape(default)}", src), name
    assert re.search(r"\[string\]\s*\$StateDir\b", src)
    assert re.search(r"\[string\]\s*\$AuditPath\b", src)
    # the jarvis-account branch is kept for the later phases
    assert re.search(r"\[string\]\s*\$TargetAccount\s*=\s*'jarvis'", src)


def test_kill_switch_has_every_v1_step_and_audits_it() -> None:
    src = text(KILL)
    for step in ("create-kill-file", "daemon-lock", "daemon-targets", "stop-task", "kill-daemon-tree",
                 "kill-processes", "revoke-egress"):
        assert f"'{step}'" in src, step
    # the egress rule is keyed to the jarvis SID, so it is not applicable to any other target
    assert "not-applicable" in src
    # the script excludes its own process chain (a process-killing tool must not kill itself)
    assert "$selfChain" in src
    # killing by image name alone would hit every python the owner has open
    assert "-Name python" not in src and "Get-Process python" not in src


def test_kill_switch_never_uses_a_shell_or_invoke_expression() -> None:
    src = text(KILL)
    assert "Invoke-Expression" not in src and not re.search(r"\biex\b", src)
    assert "cmd /c" not in src.lower()


def test_hostile_sim_never_aims_a_kill_switch_run_at_the_live_daemon() -> None:
    src = text(SIM)
    assert src.count("'-File'") == 1, "the kill switch must be started from one helper only"
    block = src[src.index("function Invoke-KillSwitch"):]
    block = block[:block.index("\n}\n")]
    for flag in ("'-TaskName'", "'-StateDir'", "'-AuditPath'", "'-V1Marker'", "'-ClaudeMarker'", "'-Scope', 'Daemon'"):
        assert flag in block, flag
    assert "Full" not in block
    assert "$liveTask" in block and "throw" in block, "the helper must refuse the live task and state"
    calls = re.findall(r"^\s+Invoke-KillSwitch .*$", src, re.MULTILINE)
    assert len(calls) >= 6, "dry and real runs for the account scenario and the three v1 scenarios"
    for call in calls:
        assert "$simTask" in call, call
        assert "-TargetAccount jarvis" not in call and "'jarvis'" not in call, call
    assert "-Scope Full" not in src


def test_hostile_sim_simulates_the_v1_daemon_shape() -> None:
    src = text(SIM)
    for needle in ("pythonw", "heartbeat.json", "Register-ScheduledTask", "Unregister-ScheduledTask",
                   "KILL", "bystander", "live-daemon-untouched", "claude"):
        assert needle in src, needle


def test_operations_doc_and_patch_doc_say_the_patch_is_applied() -> None:
    ops = text(ROOT / "docs" / "v1-operations.md")
    patch = text(KILL.parent.parent / "docs" / "killswitch-v1-patch.md")
    assert "Status: applied" in patch
    assert "do not match an\n  `<owner>` daemon" not in ops
    assert "hostile-sim" in ops.lower() or "hostile sim" in ops.lower()
    readme = text(ROOT / "README.md")
    assert "hostile" in readme.lower() and "v1 daemon" in readme.lower()


# --- behavioural, isolated ------------------------------------------------------------------------------


def run_kill(tmp: Path, *extra: str, dry: bool) -> tuple[subprocess.CompletedProcess[str], list[dict], Path]:
    state = tmp / "state"
    audit = tmp / "ks.jsonl"
    marker = "KS-TEST-" + uuid.uuid4().hex[:8]
    cmd = [
        POWERSHELL or "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(KILL),
        "-Scope", "Daemon", "-Reason", "pytest", "-TargetAccount", "ks-test-nobody",
        "-TaskName", "JarvisDaemon-pytest-" + uuid.uuid4().hex[:6], "-StateDir", str(state),
        "-AuditPath", str(audit), "-V1Marker", marker, "-ClaudeMarker", marker + "-claude",
        "-CommandLineMarker", marker, *extra,
    ]
    if dry:
        cmd.append("-DryRun")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=str(tmp))
    lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()] if audit.exists() else []
    return proc, lines, state


@needs_powershell
def test_dry_run_reports_each_step_and_creates_nothing(tmp_path: Path) -> None:
    proc, lines, state = run_kill(tmp_path, dry=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(lines) == 1 and lines[0]["dry_run"] is True and lines[0]["event"] == "killswitch"
    steps = {a["step"]: a["result"] for a in lines[0]["actions"]}
    assert steps["create-kill-file"] == "would-create"
    assert steps["daemon-lock"] in ("free", "absent")
    assert steps["stop-task"] == "absent"
    assert steps["revoke-egress"] in ("not-applicable", "skipped")
    assert not (state / "KILL").exists()
    assert not state.exists()


@needs_powershell
def test_real_run_creates_the_kill_file_and_survives_having_nothing_to_kill(tmp_path: Path) -> None:
    proc, lines, state = run_kill(tmp_path, dry=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (state / "KILL").is_file()
    body = (state / "KILL").read_text(encoding="utf-8")
    assert "pytest" in body and not body.startswith("﻿")
    steps = {a["step"]: a["result"] for a in lines[-1]["actions"]}
    assert steps["create-kill-file"] == "ok"
    assert steps["daemon-targets"] == "none"
    assert lines[-1]["dry_run"] is False


@needs_powershell
def test_a_stale_heartbeat_pid_that_is_not_a_daemon_is_never_killed(tmp_path: Path) -> None:
    # PID reuse guard: the heartbeat names this very pytest process, which is not a jarvisd.
    state = tmp_path / "state"
    state.mkdir()
    (state / "heartbeat.json").write_text(json.dumps({"pid": os.getpid(), "mode": "task"}), encoding="utf-8")
    proc, lines, _ = run_kill(tmp_path, dry=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    targets = [a for a in lines[-1]["actions"] if a["step"] == "daemon-targets"]
    assert targets and targets[0]["result"] == "none"
    assert "not a jarvisd" in targets[0]["detail"]
    assert not any(a["step"] == "kill-daemon-tree" and a["result"] == "ok" for a in lines[-1]["actions"])


@needs_powershell
@pytest.mark.skipif(os.environ.get("JARVIS_RUN_HOSTILE_SIM") != "1", reason="set JARVIS_RUN_HOSTILE_SIM=1")
def test_full_hostile_simulation_passes() -> None:
    proc = subprocess.run(
        [POWERSHELL or "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SIM)],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
    assert "RESULT: PASS" in proc.stdout


# --- the self-test alignment check ----------------------------------------------------------------------


def test_alignment_check_compares_the_script_with_the_daemon() -> None:
    from jarvisd.selftest import check_alignment

    ok, detail = check_alignment()
    assert ok is True, detail
    assert "kill switch finds the v1 daemon" in detail
    assert "process-level kill does not cover" not in detail


def test_alignment_check_fails_when_the_script_drifts(tmp_path: Path) -> None:
    from jarvisd.selftest import check_alignment

    drifted = tmp_path / "kill-switch.ps1"
    drifted.write_text(
        text(KILL).replace("[string] $V1Marker = 'jarvisd serve --task'", "[string] $V1Marker = 'somethingelse'")
        .replace("[string] $TaskName = 'JarvisDaemon'", "[string] $TaskName = 'OtherTask'"),
        encoding="utf-8", newline="\n")
    ok, detail = check_alignment(drifted)
    assert ok is False
    assert "marker" in detail and "task name" in detail


def test_alignment_check_fails_when_the_script_is_missing(tmp_path: Path) -> None:
    from jarvisd.selftest import check_alignment

    ok, detail = check_alignment(tmp_path / "nope.ps1")
    assert ok is False and "not found" in detail
