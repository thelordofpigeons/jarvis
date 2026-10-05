"""`jarvis ask`: one question answered from the latest digest, RECENT.md and recent session notes.

Driven through cli.main against a throwaway machine and the fake binary in
tests/fakes/fake_claude.py (scenarios ask_ok, ask_hallucinated, ...). Nothing here spends
money. Everything is synthetic (design D10).
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jarvisd import ROOT, cli
from jarvisd.audit import AuditLog
from jarvisd.ask import ASK_SYSTEM_PROMPT
from jarvisd.claude import SYSTEM_PROMPT, build_argv, default_runner
from jarvisd.config import Config
from jarvisd.state import StateStore

FAKE = ROOT / "tests" / "fakes" / "fake_claude.py"
TERM = "zebra-ledger"


class FakeRunner:
    """Starts the fake instead of `claude`; adds the FAKE_* variables the client never passes."""

    def __init__(self, tmp_path: Path, scenario: str = "ask_ok") -> None:
        self.log = tmp_path / "fake-claude.jsonl"
        self.scenario = scenario

    def __call__(self, argv: Any, *, env: Any, cwd: str, creationflags: int) -> Any:
        full = [sys.executable, str(FAKE), *list(argv)[1:]]
        env2 = {**env, "FAKE_CLAUDE_LOG": str(self.log), "FAKE_CLAUDE_SCENARIO": self.scenario}
        return default_runner(full, env=env2, cwd=cwd, creationflags=creationflags)

    def all(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def paid(self) -> list[dict[str, Any]]:
        return [r for r in self.all() if r["argv"][:1] not in (["--version"], ["--help"])]


@dataclass
class Env:
    cfg: Config
    runner: FakeRunner
    vault: Path
    tmp_path: Path

    def run(self, *argv: str) -> int:
        return cli.main(list(argv), cfg=self.cfg, claude_runner=self.runner, network_probe=lambda: True,
                        task_base_dir=self.tmp_path / "claude-home")

    @property
    def audit(self) -> AuditLog:
        return AuditLog(self.cfg.paths.logs / "jarvisd-audit.jsonl", mirror_stdout=False)

    @property
    def state(self) -> StateStore:
        return StateStore.from_config(self.cfg)

    def events(self, name: str) -> list[dict[str, Any]]:
        return self.audit.records(events=[name])


def _write(path: Path, text: str, age_days: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


SESSION = """---
type: session
date: 2026-10-05
---
## Next session entry point
Continue at synth.py:12 - wire the synthetic exporter

## Open threads
- Synthetic follow up about the exporter
"""


@pytest.fixture
def env(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> Env:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.claude.binary = str(FAKE)
    cfg.digest.repos = []
    (tmp_path / "claude-home").mkdir()
    _write(tmp_vault / "sessions" / "2026-10-05-08.md", SESSION)
    return Env(cfg, FakeRunner(tmp_path), tmp_vault, tmp_path)


def _tree(*roots: Path) -> list[str]:
    return sorted(p.as_posix() for root in roots for p in root.rglob("*") if p.is_file())


def _stdin(env: Env) -> str:
    return env.runner.paid()[0]["stdin"]


# --- parsing and the constants ------------------------------------------------------------------


def test_ask_parses_with_and_without_quotes() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["ask", "what is open"]).question == ["what is open"]
    assert parser.parse_args(["ask", "what", "is", "open"]).question == ["what", "is", "open"]
    assert parser.parse_args(["ask", "--dry-run", "what is open"]).dry_run is True


def test_ask_needs_a_question(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("ask") == 2
    assert env.run("ask", "   ") == 2
    capsys.readouterr()


def test_the_ask_prompt_treats_the_data_as_untrusted_and_has_no_dashes() -> None:
    text = ASK_SYSTEM_PROMPT
    assert "untrusted" in text and "<data>" in text and "never instructions" in text
    assert "no tools" in text and "JSON" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text


def test_the_ask_argv_is_the_summarize_argv_with_its_own_system_prompt(tmp_cfg: Config) -> None:
    ask = build_argv(tmp_cfg, ASK_SYSTEM_PROMPT, binary="claude")
    summarize = build_argv(tmp_cfg, SYSTEM_PROMPT, binary="claude")
    assert [a for a in ask if a != ASK_SYSTEM_PROMPT] == [a for a in summarize if a != SYSTEM_PROMPT]
    assert "--strict-mcp-config" in ask and ask[ask.index("--tools") + 1] == ""


# --- dry run -------------------------------------------------------------------------------------


def test_dry_run_prints_the_payload_without_held_content(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.cfg.gates.sensitive_terms = [TERM]
    recent = env.vault / "RECENT.md"
    recent.write_text(recent.read_text(encoding="utf-8")
                      + f"- [2026-10-05] Reconcile the {TERM} before Friday\n", encoding="utf-8", newline="\n")
    # The tail of the list above sits under Recent Decisions; add one open thread with the term too.
    text = recent.read_text(encoding="utf-8").replace(
        "## Recent Decisions", f"- [2026-10-05] Chase the {TERM} owner\n\n## Recent Decisions")
    recent.write_text(text, encoding="utf-8", newline="\n")
    before = _tree(env.vault, env.cfg.paths.queue)
    code = env.run("ask", "--dry-run", "what is open")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "<data>" in out and "Synthetic thread one" in out and "Synthetic follow up about the exporter" in out
    assert TERM not in out  # neither the bullet text nor the term
    assert "Held" in out and "term:0" in out
    assert "what is open" in out
    assert env.runner.all() == []  # nothing spawned, not even --version
    assert env.state.budget.snapshot()["calls"] == 0
    assert _tree(env.vault, env.cfg.paths.queue) == before
    assert not env.events("ask_intent") and not env.events("claude_intent")


def test_dry_run_also_works_without_quotes_and_never_prints_a_path(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    assert env.run("ask", "--dry-run", "what", "is", "open") == 0
    payload = capsys.readouterr().out.split("<data>", 1)[1].split("</data>", 1)[0]
    assert "RECENT.md" not in payload and str(env.vault) not in payload  # paths never enter the payload


# --- the real call -------------------------------------------------------------------------------


def test_ask_prints_the_answer_and_the_ids_it_used(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    code = env.run("ask", "what is open")
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Synthetic answer: two threads are open." in out
    paid = env.runner.paid()
    assert len(paid) == 1
    ids_line = [ln for ln in out.splitlines() if ln.startswith("Items used")][0]
    sent_ids = json.loads(paid[0]["stdin"].split("<data>\n", 1)[1].split("\n</data>")[0])
    first_two = [row["id"] for row in sent_ids[:2]]
    assert all(i in ids_line or i in out for i in first_two)
    assert "invented" not in out


def test_ask_spawns_with_the_isolation_argv_and_the_ask_system_prompt(env: Env) -> None:
    assert env.run("ask", "what is open") == 0
    argv = env.runner.paid()[0]["argv"]
    assert argv[argv.index("--system-prompt") + 1] == ASK_SYSTEM_PROMPT
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert env.runner.paid()[0]["cwd"] == str(env.cfg.daemon.state_dir / "claude-cwd")
    assert "ANTHROPIC_API_KEY" not in env.runner.paid()[0]["env_keys"]


def test_the_question_travels_in_the_header_and_the_data_is_one_block(env: Env) -> None:
    assert env.run("ask", "what", "is", "open") == 0
    stdin = _stdin(env)
    assert "what is open" in stdin.split("<data>")[0]
    assert stdin.count("<data>") == 1 and stdin.count("</data>") == 1


def test_ask_is_audited_as_ask_intent_and_ask_call_without_any_text(env: Env) -> None:
    assert env.run("ask", "what is open") == 0
    intent, call = env.events("ask_intent")[0], env.events("ask_call")[0]
    assert intent["question_chars"] == len("what is open") and len(intent["question_sha256"]) == 64
    assert intent["to_claude"] >= 1 and len(intent["payload_sha256"]) == 64
    assert call["ok"] is True and call["kind"] == "ok" and call["ids_used"] == 2
    assert call["cost_usd"] > 0
    claude_intent = env.events("claude_intent")[0]
    assert claude_intent["purpose"] == "ask"
    raw = (env.cfg.paths.logs / "jarvisd-audit.jsonl").read_text(encoding="utf-8")
    assert "what is open" not in raw and "Synthetic thread one" not in raw
    ok, bad = env.audit.verify()
    assert ok, bad
    assert env.state.budget.snapshot()["by_purpose"].get("ask", 0) > 0


def test_a_term_hit_withholds_that_line_only(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.cfg.gates.sensitive_terms = [TERM]
    recent = env.vault / "RECENT.md"
    recent.write_text(recent.read_text(encoding="utf-8").replace(
        "## Recent Decisions", f"- [2026-10-05] Chase the {TERM} owner\n\n## Recent Decisions"),
        encoding="utf-8", newline="\n")
    assert env.run("ask", "what is open") == 0
    stdin = _stdin(env)
    assert TERM not in stdin and "Chase the" not in stdin
    assert "Synthetic thread one" in stdin  # the neighbouring bullet still flows
    assert TERM not in capsys.readouterr().out
    assert TERM not in (env.cfg.paths.logs / "jarvisd-audit.jsonl").read_text(encoding="utf-8")


def test_sessions_older_than_seven_days_are_not_sent(env: Env) -> None:
    old = SESSION.replace("synth.py:12 - wire the synthetic exporter", "old.py:1 - the ancient exporter")
    _write(env.vault / "sessions" / "2026-09-20-08.md", old, age_days=15)
    assert env.run("ask", "what is open") == 0
    stdin = _stdin(env)
    assert "wire the synthetic exporter" in stdin and "ancient exporter" not in stdin


def test_sensitive_telos_and_notes_are_never_read(env: Env) -> None:
    from conftest import CANARY

    _write(env.vault / "notes" / "private.md", f"{CANARY}\n")
    assert env.run("ask", "--dry-run", "what is open") == 0
    assert env.run("ask", "what is open") == 0
    assert CANARY not in _stdin(env)


def test_hallucinated_ids_are_dropped_from_the_output(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.runner.scenario = "ask_hallucinated"
    assert env.run("ask", "what is open") == 0
    out = capsys.readouterr().out
    assert "invented-9" not in out
    assert env.events("ask_call")[0]["hallucinated_ids"] == 1


def test_bad_json_is_a_failure_with_exit_1(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.runner.scenario = "invalid_json"
    assert env.run("ask", "what is open") == 1
    assert "Synthetic" not in capsys.readouterr().out
    assert env.events("ask_call")[0]["kind"] == "bad_json" and env.events("ask_call")[0]["ok"] is False


def test_an_answer_with_no_answer_field_is_bad_schema(env: Env) -> None:
    env.runner.scenario = "ask_bad_schema"
    assert env.run("ask", "what is open") == 1
    assert env.events("ask_call")[0]["kind"] == "bad_schema"


# --- refusals ------------------------------------------------------------------------------------


def test_refuses_when_the_breaker_is_open(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.state.breaker.trip("test", requires_reset=True)
    code = env.run("ask", "what is open")
    out = capsys.readouterr().out
    assert code == 3, out
    assert "breaker" in out.lower()
    assert env.runner.paid() == []
    assert env.events("ask_call")[0]["kind"] == "breaker"


def test_refuses_when_the_budget_is_exhausted(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.cfg.claude.daily_calls = 0
    code = env.run("ask", "what is open")
    out = capsys.readouterr().out
    assert code == 3, out
    assert "budget" in out.lower()
    assert env.runner.paid() == []
    assert env.events("ask_call")[0]["kind"] == "budget"
    assert env.events("budget_refused")


def test_refuses_when_the_kill_file_is_present(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    (env.cfg.daemon.state_dir / "KILL").write_text("stop\n", encoding="utf-8")
    assert env.run("ask", "what is open") == 3
    assert env.runner.paid() == []
    capsys.readouterr()


def test_a_question_that_hits_the_tier_gate_is_refused_and_not_echoed(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.cfg.gates.sensitive_terms = [TERM]
    code = env.run("ask", f"what about the {TERM}")
    out = capsys.readouterr().out
    assert code == 3
    assert TERM not in out and "term:0" in out
    assert env.runner.all() == []


def test_nothing_to_send_means_no_call(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    (env.vault / "RECENT.md").unlink()
    (env.vault / "sessions" / "2026-10-05-08.md").unlink()
    code = env.run("ask", "what is open")
    out = capsys.readouterr().out
    assert code == 1
    assert "nothing" in out.lower()
    assert env.runner.all() == []
    assert env.state.budget.snapshot()["calls"] == 0


# --- the digest as a source ----------------------------------------------------------------------

CLEARED = "a1b2c3d4"
POLICY = "deadbeef"
DIGEST = f"""---
type: jarvis-digest
generator: jarvisd
date: 2026-10-05
claude: ok
tags: [jarvis, digest]
---
# JARVIS morning digest, Monday 2026-10-05

## Start here
Quiet night, one repo moved
1. [{CLEARED}] Synthetic repo has two new commits

## Repos
- synth-repo branch main, 2 commits since window, 0 modified, 0 untracked: add exporter; fix test [{CLEARED}] Two commits.
- work-repo (work) branch main, 1 commit since window, 0 modified, 0 untracked: privatework detail [{POLICY}]
"""


def _seed_digest(env: Env) -> None:
    _write(env.vault / "raw" / "jarvis" / "digest-2026-10-05.md", DIGEST)
    env.audit.emit("gate_decision", item_id=CLEARED, route="claude", decided_by="confidence", local_tier="not_installed",
                   confirm_required=False, degraded=False, hold_kind=None, reasons=["confidence_below_threshold"])
    env.audit.emit("gate_decision", item_id=POLICY, route="held", decided_by="tier", local_tier="not_installed",
                   confirm_required=False, degraded=False, hold_kind="policy", reasons=["work_policy"])


def test_digest_lines_reach_claude_only_when_their_ids_were_cleared(env: Env) -> None:
    _seed_digest(env)
    assert env.run("ask", "what is open") == 0
    stdin = _stdin(env)
    assert "add exporter; fix test" in stdin
    assert "privatework" not in stdin and POLICY not in stdin
    assert "type: jarvis-digest" not in stdin  # the front matter is not data


def test_a_digest_line_with_a_term_is_withheld_alone(env: Env) -> None:
    _seed_digest(env)
    env.cfg.gates.sensitive_terms = ["fix test"]
    assert env.run("ask", "what is open") == 0
    stdin = _stdin(env)
    assert "fix test" not in stdin
    assert "Synthetic repo has two new commits" in stdin  # the other cleared line stays


def test_the_latest_digest_is_the_one_used(env: Env) -> None:
    _seed_digest(env)
    older = DIGEST.replace("add exporter; fix test", "ancient subject").replace("2026-10-05", "2026-10-01")
    _write(env.vault / "raw" / "jarvis" / "digest-2026-10-01.md", older)
    assert env.run("ask", "what is open") == 0
    stdin = _stdin(env)
    assert "add exporter" in stdin and "ancient subject" not in stdin


def test_the_ask_source_has_no_dashes_and_no_shell() -> None:
    text = (ROOT / "jarvisd" / "ask.py").read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text
    for banned in ("subprocess", "shell=True", "os.system", "Popen"):
        assert banned not in text  # only claude.py spawns claude


def test_dry_run_leaves_the_audit_chain_untouched(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    # The banner says "nothing was written". Gate decisions are read back by later asks as
    # "latest decision wins", so a dry run must not append any, and must not grow the log.
    before = env.audit.records()
    assert env.run("ask", "--dry-run", "what is open") == 0
    capsys.readouterr()
    assert env.audit.records() == before
    assert not env.events("gate_decision")


def test_a_real_ask_still_audits_one_gate_decision_per_item(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.run("ask", "what is open")
    capsys.readouterr()
    assert env.events("gate_decision"), "the real run must keep its audit trail"
