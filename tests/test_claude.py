"""claude: the only module that spawns `claude`, driven against a real fake child process.

The fake (tests/fakes/fake_claude.py) is started for real through a runner that swaps argv[0],
so argv quoting, the env allowlist, stdin delivery, timeouts, the KILL file and the process
tree kill are all exercised, not mocked. Everything is synthetic (design D10).
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jarvisd import ROOT
from jarvisd.audit import AuditLog
from jarvisd.claude import (
    SYSTEM_PROMPT,
    ClaudeCallReply,
    ClaudeClient,
    ClaudeUnavailable,
    build_argv,
    child_env,
    default_runner,
    strip_fence,
)
from jarvisd.config import Config
from jarvisd.dispatch import GatedPayload, PayloadBlocked, clear_for_claude
from jarvisd.models import ClaudeReply, GateResult, Item
from jarvisd.state import StateStore

FAKE = ROOT / "tests" / "fakes" / "fake_claude.py"
FIXTURE = ROOT / "tests" / "fixtures" / "claude_result_ok.json"
IDS = ("item-a", "item-b")


class _Crash(BaseException):
    """Stands in for the machine dying between the intent record and the result."""


class FakeRunner:
    """Starts the fake instead of `claude`; adds the FAKE_* variables the client never passes."""

    def __init__(self, tmp_path: Path, scenario: str = "ok", **extra_env: str) -> None:
        self.log = tmp_path / "fake-claude.jsonl"
        self.scenario = scenario
        self.extra = extra_env
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: Any, *, env: Any, cwd: str, creationflags: int) -> Any:
        self.calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd})
        full = [sys.executable, str(FAKE), *list(argv)[1:]]
        env2 = {**env, "FAKE_CLAUDE_LOG": str(self.log), "FAKE_CLAUDE_SCENARIO": self.scenario, **self.extra}
        return default_runner(full, env=env2, cwd=cwd, creationflags=creationflags)

    def records(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def paid_calls(self) -> list[dict[str, Any]]:
        """Invocations that were not --version or --help."""
        return [r for r in self.records() if r["argv"][:1] not in (["--version"], ["--help"])]


class Rig:
    def __init__(self, cfg: Config, runner: FakeRunner, audit: AuditLog, state: StateStore,
                 client: ClaudeClient, sleeps: list[float]) -> None:
        self.cfg, self.runner, self.audit, self.state, self.client, self.sleeps = cfg, runner, audit, state, client, sleeps

    def events(self, name: str) -> list[dict[str, Any]]:
        return self.audit.records(events=[name])


def _rig(tmp_cfg: Config, tmp_path: Path, scenario: str = "ok", *, enabled: bool = True,
         timeout: int | None = None, probe: bool = True, runner: Any = None, **extra_env: str) -> Rig:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.claude.binary = str(FAKE)
    if timeout is not None:
        cfg.claude.timeout_seconds = timeout
    fake = FakeRunner(tmp_path, scenario, **extra_env)
    audit = AuditLog(cfg.paths.logs / "jarvisd-audit.jsonl", mirror_stdout=False)
    state = StateStore.from_config(cfg)
    sleeps: list[float] = []
    client = ClaudeClient(cfg, audit, state, runner=runner or fake, enabled=enabled,
                          network_probe=lambda: probe, sleep=sleeps.append, poll_seconds=0.1,
                          network_wait_seconds=20.0, network_step_seconds=10.0)
    return Rig(cfg, fake, audit, state, client, sleeps)


def _sub(tmp_path: Path, name: str) -> Path:
    """A second scratch directory, for tests that build two rigs (separate fake logs)."""
    path = tmp_path / name
    path.mkdir()
    return path


def _payload(cfg: Config, ids: tuple[str, ...] = IDS) -> GatedPayload:
    items = [Item(id=i, source="brain", kind="brain_thread", title=f"Synthetic thread {i}",
                  text="Synthetic body that waits on a reviewer.", ts="2026-10-05T20:00:00+00:00")
             for i in ids]
    results = [GateResult(item_id=i, route="claude", decided_by="confidence", local_tier="not_installed")
               for i in ids]
    return clear_for_claude(items, results, cfg)


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False)
    return str(pid) in out.stdout


needs_windows = pytest.mark.skipif(sys.platform != "win32", reason="taskkill tree kill is Windows only")


# --- the two named tests ---------------------------------------------------------------


def test_argv_golden_and_env_allowlist(tmp_cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-synthetic-not-real")
    monkeypatch.setenv("CLAUDE_CODE_SYNTHETIC", "1")
    monkeypatch.setenv("SOME_OTHER_SECRET", "nope")
    rig = _rig(tmp_cfg, tmp_path)
    payload = _payload(rig.cfg)
    rig.client.complete(payload, "digest")

    expected = [
        str(FAKE), "-p",
        "--output-format", "json",
        "--model", "sonnet",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--strict-mcp-config",
        "--tools", "",
        "--no-session-persistence",
        "--permission-prompts", "none",
        "--max-budget-usd", "0.5",
        "--system-prompt", SYSTEM_PROMPT,
    ]
    call = rig.runner.calls[-1]
    assert call["argv"] == expected  # includes the two empty-string values
    paid = rig.runner.paid_calls()[-1]
    assert paid["argv"] == expected[1:]  # and they survived a real process boundary

    # The prompt is on stdin, never in argv.
    prompt = payload.prompt("Summarize these items.", rig.cfg)
    assert paid["stdin"] == prompt
    assert all("Synthetic thread" not in a for a in call["argv"])

    # Environment: allowlist only, no ANTHROPIC_* or CLAUDE_*.
    keys = set(call["env"])
    assert "JARVISD_CHILD" in keys and call["env"]["JARVISD_CHILD"] == "1"
    assert not [k for k in keys if k.startswith(("ANTHROPIC_", "CLAUDE_"))]
    assert "SOME_OTHER_SECRET" not in keys
    assert not [k for k in paid["env_keys"] if k.startswith(("ANTHROPIC_", "CLAUDE_"))]
    assert "SOME_OTHER_SECRET" not in paid["env_keys"]
    # cwd is the empty directory under state/.
    assert Path(call["cwd"]) == rig.state.dir / "claude-cwd"
    assert Path(paid["cwd"]).resolve() == (rig.state.dir / "claude-cwd").resolve()
    assert list(Path(call["cwd"]).iterdir()) == []


def test_rejects_non_payload(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    for bad in ("<data>[]</data>", {"text": "x"}, b"raw", None):
        with pytest.raises(TypeError):
            rig.client.complete(bad, "digest")  # type: ignore[arg-type]
    # A forged GatedPayload cannot be built either.
    with pytest.raises(TypeError):
        GatedPayload(text="<data>\n[]\n</data>", sha256="0" * 64, byte_size=1, item_ids=[])
    with pytest.raises(TypeError):
        GatedPayload.model_construct(text="x", sha256="0", byte_size=1, item_ids=[])
    assert rig.runner.records() == []  # nothing was ever spawned, not even preflight
    assert rig.state.budget.snapshot()["calls"] == 0


def test_tampered_payload_is_refused_and_opens_breaker(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    payload = _payload(rig.cfg)
    object.__setattr__(payload, "text", payload.text.replace("Synthetic", "Forged"))
    with pytest.raises(TypeError):
        rig.client.complete(payload, "digest")
    assert rig.runner.paid_calls() == []
    peek = rig.state.breaker.peek()
    assert peek["state"] == "open" and peek["requires_human_reset"] is True
    assert rig.events("tier_violation")


# --- preflight and argv ----------------------------------------------------------------


def test_preflight_reads_version_and_flags(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    report = rig.client.preflight()
    assert report.ok and report.version.startswith("2.1.289")
    assert report.binary == str(FAKE) and not report.supports_max_turns and not report.shim


def test_max_turns_only_when_advertised(tmp_cfg: Config, tmp_path: Path) -> None:
    plain = _rig(tmp_cfg, _sub(tmp_path, "plain"))
    assert "--max-turns" not in plain.client.build_argv()
    on = _rig(tmp_cfg, tmp_path, FAKE_CLAUDE_MAX_TURNS="1")
    argv = on.client.build_argv()
    assert argv[-2:] == ["--max-turns", "1"]
    assert argv[:-2] == build_argv(on.cfg, binary=str(FAKE))


def test_missing_required_flag_blocks_calls(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    rig.cfg.claude.required_flags = [*rig.cfg.claude.required_flags, "--flag-the-fake-lacks"]
    report = rig.client.preflight()
    assert not report.ok and report.missing_flags == ("--flag-the-fake-lacks",)
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "preflight"
    assert rig.state.budget.snapshot()["calls"] == 0


def test_binary_not_found(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    rig.cfg.claude.binary = str(tmp_path / "no-such-claude.exe")
    report = rig.client.preflight()
    assert not report.ok and report.reason == "binary_not_found"


def test_child_env_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    env = child_env({"path": "p", "SystemRoot": "r", "ANTHROPIC_API_KEY": "k", "CLAUDE_X": "1", "HOME": "h"})
    assert env == {"PATH": "p", "SYSTEMROOT": "r", "JARVISD_CHILD": "1"}


def test_system_prompt_has_no_dashes() -> None:
    assert chr(0x2014) not in SYSTEM_PROMPT and chr(0x2013) not in SYSTEM_PROMPT


def test_system_prompt_asks_for_verb_first_whys_and_skips_resolved_items() -> None:
    # The Start here grammar (docs/hub-rework-contract.md, section 1.2) depends on these two sentences.
    assert "Each why starts with an imperative verb and states the deadline or what happens otherwise" in SYSTEM_PROMPT
    assert "skip items that read as done, delivered, shipped, cosmetic or optional, or already covered by another" in SYSTEM_PROMPT
    assert "Importance order: overdue or due-today task" in SYSTEM_PROMPT


# --- scenarios -------------------------------------------------------------------------


def test_ok_parses_tokens_cost_and_session(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    payload = _payload(rig.cfg)
    reply = rig.client.complete(payload, "digest", job_id="digest-2026-10-06")
    assert isinstance(reply, ClaudeReply) and isinstance(reply, ClaudeCallReply)
    assert reply.total_cost_usd == pytest.approx(0.0123)
    assert reply.session_id == "00000000-0000-4000-8000-000000000001"
    assert reply.usage.input_tokens == 1200 and reply.total_input_tokens == 1200
    assert reply.summary is not None
    assert reply.summary.attention[0].id == "item-a"
    assert set(reply.summary.summaries) == set(IDS)
    assert reply.hallucinated_ids == 0 and reply.payload_sha256 == payload.sha256
    assert reply.cli_version.startswith("2.1.289")
    snap = rig.state.budget.snapshot()
    assert snap["spent_usd"] == pytest.approx(0.0123) and snap["reserved_usd"] == 0 and snap["calls"] == 1
    assert rig.state.breaker.peek()["state"] == "closed"
    call = rig.events("claude_call")[-1]
    assert call["ok"] is True and call["kind"] == "ok" and call["job_id"] == "digest-2026-10-06"


def test_result_fixture_is_a_valid_reply() -> None:
    reply = ClaudeReply.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    assert reply.model_usage and reply.total_cost_usd == pytest.approx(0.0123)


def test_fenced_json_is_accepted(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "fenced")
    reply = rig.client.complete(_payload(rig.cfg), "digest")
    assert reply.summary is not None and reply.summary.headline.startswith("Quiet night")
    assert strip_fence("```json\n{\"a\": 1}\n```") == '{"a": 1}'
    assert strip_fence('{"a": 1}') == '{"a": 1}'


def test_unknown_ids_are_dropped_and_counted(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "hallucinated")
    reply = rig.client.complete(_payload(rig.cfg), "digest")
    assert reply.hallucinated_ids == 2
    assert reply.summary is not None
    assert [a.id for a in reply.summary.attention] == ["item-a"]
    assert "invented-2" not in reply.summary.summaries
    assert rig.events("claude_call")[-1]["hallucinated_ids"] == 2


@needs_windows
def test_timeout_kills_process_tree_near_deadline(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "timeout", timeout=1)
    started = time.monotonic()
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    elapsed = time.monotonic() - started
    assert err.value.kind == "timeout" and err.value.retryable is True
    assert elapsed < 1 + 2, f"took {elapsed:.2f}s for a 1s deadline"
    pid = rig.runner.paid_calls()[-1]["pid"]
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid), "the grandchild python survived the tree kill"
    assert rig.state.breaker.peek()["consecutive_failures"] == 1
    # Unknown cost is charged at the cap, so the ledger never under-counts a killed call.
    assert rig.state.budget.snapshot()["spent_usd"] == pytest.approx(rig.cfg.claude.max_budget_usd)


def test_429_is_not_retryable_and_opens_breaker(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "429")
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "rate_limit" and err.value.retryable is False
    peek = rig.state.breaker.peek()
    assert peek["state"] == "open" and peek["consecutive_failures"] == 1
    assert peek["requires_human_reset"] is False
    assert rig.events("breaker")
    # And the next call does not spawn at all.
    with pytest.raises(ClaudeUnavailable) as again:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert again.value.kind == "breaker"
    assert len(rig.runner.paid_calls()) == 1


def test_401_opens_breaker_requiring_reset(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "401")
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "auth" and err.value.retryable is False
    peek = rig.state.breaker.peek()
    assert peek["state"] == "open" and peek["requires_human_reset"] is True


def test_5xx_is_transient_and_retryable(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "500")
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "transient" and err.value.retryable is True


def test_retry_helper_makes_two_attempts_with_delay(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "500")
    with pytest.raises(ClaudeUnavailable):
        rig.client.complete_with_retry(_payload(rig.cfg), "digest")
    assert len(rig.runner.paid_calls()) == 2
    assert rig.sleeps == [20.0]
    attempts = [r["attempt"] for r in rig.events("claude_intent")]
    assert attempts == [1, 2]
    # A non-retryable kind gets one attempt only.
    rig2 = _rig(tmp_cfg, _sub(tmp_path, "second"), "invalid_json")
    with pytest.raises(ClaudeUnavailable) as err:
        rig2.client.complete_with_retry(_payload(rig2.cfg), "digest")
    assert err.value.kind == "bad_json" and len(rig2.runner.paid_calls()) == 1


def test_invalid_json_is_bad_json_and_raw_is_kept(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "invalid_json")
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest", job_id="digest-2026-10-06")
    assert err.value.kind == "bad_json" and err.value.retryable is False
    raw = list((rig.state.dir / "runs" / "digest-2026-10-06").glob("claude-*-raw.txt"))
    assert len(raw) == 1 and "not json" in raw[0].read_text(encoding="utf-8")


def test_valid_envelope_with_wrong_schema_is_bad_schema(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "bad_schema")
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "bad_schema" and err.value.retryable is False


def test_isolation_violation_discards_output_and_opens_breaker(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "isolation_violation")
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "isolation_anomaly" and err.value.retryable is False
    anomaly = rig.events("isolation_anomaly")
    assert len(anomaly) == 1 and anomaly[0]["input_tokens"] >= 20000
    peek = rig.state.breaker.peek()
    assert peek["state"] == "open" and peek["requires_human_reset"] is True
    call = rig.events("claude_call")[-1]
    assert call["ok"] is False and call["isolation_ok"] is False and "usage" not in call


def test_new_checkpoint_file_is_an_isolation_breach(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    stray = tmp_vault / "session-checkpoints" / "00000000-stray.json"
    rig = _rig(tmp_cfg, tmp_path, "breach", FAKE_CLAUDE_TOUCH=str(stray))
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "isolation_breach" and err.value.retryable is False
    assert stray.exists()
    breach = rig.events("isolation_breach")
    assert len(breach) == 1 and breach[0]["new_files"] == 1
    peek = rig.state.breaker.peek()
    assert peek["state"] == "open" and peek["requires_human_reset"] is True


def test_new_session_note_is_also_a_breach(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    stray = tmp_vault / "sessions" / "2026-10-06-07.md"
    rig = _rig(tmp_cfg, tmp_path, "breach", FAKE_CLAUDE_TOUCH=str(stray))
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "isolation_breach"


@needs_windows
def test_kill_file_during_hang_terminates_child(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "hang", timeout=60)
    kill = rig.state.dir / "KILL"
    threading.Timer(1.0, lambda: kill.write_text("stop", encoding="utf-8")).start()
    started = time.monotonic()
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "killed" and err.value.retryable is False
    assert time.monotonic() - started < 10
    pid = rig.runner.paid_calls()[-1]["pid"]
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)
    # A kill is not evidence about the service: the breaker stays closed.
    assert rig.state.breaker.peek()["state"] == "closed"


def test_kill_file_before_spawn_prevents_the_call(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    (rig.state.dir / "KILL").write_text("stop", encoding="utf-8")
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "killed"
    assert rig.runner.paid_calls() == [] and rig.state.budget.snapshot()["calls"] == 0


# --- ordering, budget and audit --------------------------------------------------------


def test_intent_and_reservation_exist_before_spawn(tmp_cfg: Config, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    holder: dict[str, Rig] = {}

    def crashing_runner(argv: Any, *, env: Any, cwd: str, creationflags: int) -> Any:
        if list(argv)[1:2] in (["--version"], ["--help"]):
            return holder["rig"].runner(argv, env=env, cwd=cwd, creationflags=creationflags)
        rig = holder["rig"]
        seen["intent"] = rig.events("claude_intent")
        seen["snapshot"] = rig.state.budget.snapshot()
        raise _Crash

    rig = _rig(tmp_cfg, tmp_path, runner=crashing_runner)
    holder["rig"] = rig
    with pytest.raises(_Crash):
        rig.client.complete(_payload(rig.cfg), "digest")
    assert len(seen["intent"]) == 1
    assert seen["snapshot"]["reserved_usd"] == pytest.approx(rig.cfg.claude.max_budget_usd)
    # After the "crash" nothing released it: a fresh store still sees the full cap reserved.
    fresh = StateStore.from_config(rig.cfg)
    snap = fresh.budget.snapshot()
    assert snap["reserved_usd"] == pytest.approx(rig.cfg.claude.max_budget_usd) and snap["calls"] == 1
    intent = rig.events("claude_intent")[0]
    for key in ("call_id", "profile", "model", "argv_sha256", "payload_sha256", "payload_bytes",
                "max_budget_usd", "reserved_usd", "attempt", "cli_version"):
        assert key in intent
    assert rig.audit.verify() == (True, None)


def test_spawn_failure_releases_the_reservation(tmp_cfg: Config, tmp_path: Path) -> None:
    holder: dict[str, Rig] = {}

    def failing_runner(argv: Any, *, env: Any, cwd: str, creationflags: int) -> Any:
        if list(argv)[1:2] in (["--version"], ["--help"]):
            return holder["rig"].runner(argv, env=env, cwd=cwd, creationflags=creationflags)
        raise FileNotFoundError("claude vanished")

    rig = _rig(tmp_cfg, tmp_path, runner=failing_runner)
    holder["rig"] = rig
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "spawn_failed"
    snap = rig.state.budget.snapshot()
    assert snap["reserved_usd"] == 0 and snap["calls"] == 0


def test_budget_refusal_emits_event_and_spawns_nothing(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    rig.state.budget.reserve("other", rig.cfg.claude.daily_budget_usd - 0.1)  # leaves 0.1 < 0.5 cap
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest", job_id="digest-2026-10-06")
    assert err.value.kind == "budget" and err.value.retryable is False
    assert rig.runner.paid_calls() == []
    refused = rig.events("budget_refused")
    assert len(refused) == 1 and refused[0]["reason"] == "usd" and refused[0]["job_id"] == "digest-2026-10-06"
    assert rig.events("claude_intent") == []


def test_claude_call_holds_no_payload_text(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    payload = _payload(rig.cfg)
    rig.client.complete(payload, "digest")
    raw = rig.audit.path.read_text(encoding="utf-8")
    assert "Synthetic thread" not in raw and "waits on a reviewer" not in raw
    assert "Quiet night" not in raw and SYSTEM_PROMPT[:40] not in raw
    call = rig.events("claude_call")[-1]
    intent = rig.events("claude_intent")[-1]
    assert call["payload_sha256"] == payload.sha256 == intent["payload_sha256"]
    assert call["call_id"] == intent["call_id"]
    for key in ("ok", "kind", "exit_code", "duration_ms", "num_turns", "total_cost_usd", "usage",
                "model_usage", "session_id", "stop_reason", "permission_denials",
                "api_error_status", "isolation_ok", "degraded", "cli_version"):
        assert key in call, key
    assert rig.audit.verify() == (True, None)


def test_disabled_never_spawns_or_reserves(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, enabled=False)
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "disabled" and err.value.retryable is False
    assert rig.runner.records() == [] and rig.state.budget.snapshot()["calls"] == 0


def test_network_down_waits_then_defers_without_consuming_an_attempt(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, probe=False)
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    exc = err.value
    assert exc.kind == "network" and exc.consume_attempt is False
    assert exc.retry_after == timedelta(minutes=10)
    assert rig.sleeps == [10.0, 10.0]
    assert rig.state.budget.snapshot()["calls"] == 0 and rig.runner.paid_calls() == []


def test_payload_blocked_on_final_scan_opens_breaker(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    payload = _payload(rig.cfg)
    # A header that carries a vault telos path is caught by the last scan, before any spend.
    telos = (rig.cfg.paths.brain_root / "telos").as_posix()
    header = f"Date: x. Window: y.\nSee {telos}/10-identity.md"
    with pytest.raises(PayloadBlocked):
        rig.client.complete(payload, "digest", header=header)
    assert rig.runner.paid_calls() == [] and rig.state.budget.snapshot()["calls"] == 0
    assert rig.state.breaker.peek()["requires_human_reset"] is True
    assert rig.events("tier_violation")


def test_archive_payload_is_opt_in(tmp_cfg: Config, tmp_path: Path) -> None:
    off = _rig(tmp_cfg, tmp_path)
    off.client.complete(_payload(off.cfg), "digest")
    assert not (off.cfg.paths.logs / "payloads").exists()
    on = _rig(tmp_cfg, _sub(tmp_path, "on"))
    on.cfg.claude.archive_payloads = True
    on.client.complete(_payload(on.cfg), "digest")
    files = list((on.cfg.paths.logs / "payloads").glob("*.txt"))
    assert len(files) == 1 and "<data>" in files[0].read_text(encoding="utf-8")


def test_three_counted_failures_open_the_breaker(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "invalid_json")
    for _ in range(3):
        with pytest.raises(ClaudeUnavailable):
            rig.client.complete(_payload(rig.cfg), "digest")
    assert rig.state.breaker.peek()["state"] == "open"
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "breaker"


# --- review fixes -------------------------------------------------------------------------


def _expire_breaker_cooldown(rig: Rig) -> None:
    path = rig.state.dir / "breaker.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["until"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(data), encoding="utf-8")


def test_failed_preflight_is_reprobed_on_the_next_call(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    rig.cfg.claude.binary = str(tmp_path / "no-such-claude.exe")  # logon: PATH or disk not ready yet
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "preflight"
    rig.cfg.claude.binary = str(FAKE)  # the CLI is there now
    reply = rig.client.complete(_payload(rig.cfg), "digest")
    assert reply.summary is not None
    assert rig.client.preflight_report is not None and rig.client.preflight_report.ok


def test_a_good_preflight_is_not_repeated(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    rig.client.complete(_payload(rig.cfg), "digest")
    rig.client.complete(_payload(rig.cfg), "digest")
    probes = [r for r in rig.runner.records() if r["argv"][:1] == ["--version"]]
    assert len(probes) == 1


def test_network_failure_does_not_burn_the_half_open_probe(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, probe=False)
    rig.state.breaker.trip("rate_limit")
    _expire_breaker_cooldown(rig)
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "network"
    rig.client._probe = lambda: True  # the machine is awake now; the retry ten minutes later
    reply = rig.client.complete(_payload(rig.cfg), "digest")
    assert reply.summary is not None
    assert rig.state.breaker.peek()["state"] == "closed"


def test_budget_refusal_does_not_burn_the_half_open_probe(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)
    rig.state.breaker.trip("rate_limit")
    _expire_breaker_cooldown(rig)
    held = rig.state.budget.reserve("other", rig.cfg.claude.daily_budget_usd - 0.1)
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "budget"
    rig.state.budget.release(held)
    assert rig.client.complete(_payload(rig.cfg), "digest").summary is not None


def test_spawn_failure_gives_the_probe_back(tmp_cfg: Config, tmp_path: Path) -> None:
    holder: dict[str, Rig] = {}

    def failing_runner(argv: Any, *, env: Any, cwd: str, creationflags: int) -> Any:
        if list(argv)[1:2] in (["--version"], ["--help"]):
            return holder["rig"].runner(argv, env=env, cwd=cwd, creationflags=creationflags)
        raise FileNotFoundError("claude vanished")

    rig = _rig(tmp_cfg, tmp_path, runner=failing_runner)
    holder["rig"] = rig
    rig.state.breaker.trip("rate_limit")
    _expire_breaker_cooldown(rig)
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "spawn_failed"
    assert rig.state.breaker.is_open() is False  # the probe is available again


def test_an_open_breaker_still_refuses_without_waiting_for_the_network(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, probe=False)
    rig.state.breaker.trip("auth", requires_reset=True)
    with pytest.raises(ClaudeUnavailable) as err:
        rig.client.complete(_payload(rig.cfg), "digest")
    assert err.value.kind == "breaker" and rig.sleeps == []


def test_timeout_is_charged_in_the_audit_cost_too(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "timeout", timeout=1)
    with pytest.raises(ClaudeUnavailable):
        rig.client.complete(_payload(rig.cfg), "digest")
    call = rig.events("claude_call")[-1]
    cap = rig.cfg.claude.max_budget_usd
    assert call["cost_usd"] == cap and call["total_cost_usd"] == cap
    assert rig.audit.cost_on(rig.state.local_date()) == pytest.approx(cap)


def test_isolation_discarded_call_still_records_its_cost(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "isolation_violation")
    with pytest.raises(ClaudeUnavailable):
        rig.client.complete(_payload(rig.cfg), "digest")
    call = rig.events("claude_call")[-1]
    assert call["total_cost_usd"] == call["cost_usd"] > 0
    assert "usage" not in call


def test_envelope_without_a_cost_is_charged_at_the_cap(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "no_cost")
    rig.client.complete(_payload(rig.cfg), "digest")
    assert rig.state.budget.snapshot()["spent_usd"] == pytest.approx(rig.cfg.claude.max_budget_usd)
    assert rig.events("claude_call")[-1]["total_cost_usd"] == rig.cfg.claude.max_budget_usd


def test_envelope_with_an_infinite_cost_is_charged_at_the_cap(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path, "inf_cost")
    rig.client.complete(_payload(rig.cfg), "digest")  # must not raise out of settle
    assert rig.state.budget.snapshot()["spent_usd"] == pytest.approx(rig.cfg.claude.max_budget_usd)
    assert len(rig.events("claude_call")) == 1


def test_a_reported_zero_cost_is_still_free(tmp_cfg: Config, tmp_path: Path) -> None:
    rig = _rig(tmp_cfg, tmp_path)  # the ok fixture reports 0.0123
    rig.client.complete(_payload(rig.cfg), "digest")
    assert rig.state.budget.snapshot()["spent_usd"] == pytest.approx(0.0123)


def test_a_file_that_syncthing_delivers_is_not_a_breach(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    # A remote session note keeps the sender's modification time, so it predates the call.
    delivered = tmp_vault / "sessions" / "2026-10-05-22.md"
    rig = _rig(tmp_cfg, tmp_path, "breach", FAKE_CLAUDE_TOUCH=str(delivered), FAKE_CLAUDE_TOUCH_AGE="3600")
    assert rig.client.complete(_payload(rig.cfg), "digest").summary is not None
    assert rig.state.breaker.peek()["state"] == "closed" and rig.events("isolation_breach") == []


@pytest.mark.parametrize("name", [".syncthing.2026-10-05-22.md.tmp", "note.md.tmp", "~syncthing~note.md.tmp"])
def test_syncthing_temp_files_are_not_a_breach(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, name: str) -> None:
    rig = _rig(tmp_cfg, tmp_path, "breach", FAKE_CLAUDE_TOUCH=str(tmp_vault / "sessions" / name))
    assert rig.client.complete(_payload(rig.cfg), "digest").summary is not None
    assert rig.events("isolation_breach") == []
