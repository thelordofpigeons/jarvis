"""The sleep-time consolidation pass (spec 6a, design section 15): jarvisd/consolidate.py.

The collector side, the output parser, the job handler, the scheduling, the command and the
digest line are tested against a throwaway machine. The paid call goes through the real
ClaudeClient into tests/fakes/fake_claude.py, started as a real child process. Everything is
synthetic (design D10).
"""
from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import CANARY, FakeClock
from jarvisd import ROOT, cli, consolidate
from jarvisd import daemon as daemon_mod
from jarvisd.audit import AuditLog
from jarvisd.claude import ClaudeClient, ClaudeUnavailable, default_runner
from jarvisd.collectors import CollectContext
from jarvisd.collectors.system import SystemCollector
from jarvisd.common import iso
from jarvisd.config import Config, ConfigError, build_config
from jarvisd.consolidate import (
    CONSOLIDATE_KIND,
    gather,
    parse_candidates,
    reconcile_consolidation,
    run_consolidate_job,
)
from jarvisd.digest import Deps, Retry
from jarvisd.fsio import FileBusy
from jarvisd.jobstore import JobStore
from jarvisd.models import HistoryEntry, Job, JobWindow
from jarvisd.notify import NotifyResult
from jarvisd.router import build_router
from jarvisd.state import StateStore
from jarvisd.vault import VaultWriter

FAKE = ROOT / "tests" / "fakes" / "fake_claude.py"
READY = (0, '"\\JarvisDaemon","10/7/2026 6:00:00 AM","Ready"\n')

SESSION = """---
type: session
date: {day}
{extra}---
## What we built
- Built the synthetic widget in widget.py
- Added retry logic to the synthetic queue

## Decisions made (with rationale)
- Decision: keep one queue because ordering matters
{more}
## Files changed
- src/widget.py

## Next session entry point
Continue at widget.py:10 - synthetic entry point

## Open threads
- Unclear flaky test
"""


# --- rig ------------------------------------------------------------------------------


class FakeRunner:
    """Starts the fake instead of `claude`; adds the FAKE_* variables the client never passes."""

    def __init__(self, tmp_path: Path, scenario: str = "ok", **extra: str) -> None:
        self.log = tmp_path / "fake-claude.jsonl"
        self.scenario = scenario
        self.extra = extra

    def __call__(self, argv: Any, *, env: Any, cwd: str, creationflags: int) -> Any:
        full = [sys.executable, str(FAKE), *list(argv)[1:]]
        env2 = {**env, "FAKE_CLAUDE_LOG": str(self.log), "FAKE_CLAUDE_SCENARIO": self.scenario, **self.extra}
        return default_runner(full, env=env2, cwd=cwd, creationflags=creationflags)

    def records(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]

    def paid(self) -> list[dict[str, Any]]:
        return [r for r in self.records() if r["argv"][:1] not in (["--version"], ["--help"])]


class Notes:
    name = "recording"

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> NotifyResult:
        self.messages.append(message)
        return NotifyResult(ok=True, detail="sent")


@dataclass
class Rig:
    cfg: Config
    deps: Deps
    audit: AuditLog
    state: StateStore
    store: JobStore
    runner: FakeRunner
    notifier: Notes
    clock: FakeClock
    vault: Path

    @property
    def day(self) -> date:
        return self.clock.now.date()

    def note(self, name: str | None = None) -> Path:
        return self.vault / "raw" / "jarvis" / (name or f"candidates-{self.day.isoformat()}.md")

    def events(self, name: str) -> list[dict[str, Any]]:
        return self.audit.records(events=[name])


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def set_age(path: Path, now: datetime, hours: float) -> None:
    stamp = (now - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def build(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, *, scenario: str = "ok", claude: bool = True,
          mutate: Any = None, **extra: str) -> Rig:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.claude.binary = str(FAKE)
    if mutate is not None:
        mutate(cfg)
    clock = FakeClock(datetime.now(timezone.utc).replace(microsecond=0))
    audit = AuditLog(cfg.paths.logs / "jarvisd-audit.jsonl", mirror_stdout=False)
    state = StateStore.from_config(cfg)
    store = JobStore.from_config(cfg, audit=audit)
    runner = FakeRunner(tmp_path, scenario, **extra)
    notifier = Notes()
    client = ClaudeClient(cfg, audit, state, runner=runner, enabled=claude, network_probe=lambda: True,
                          sleep=lambda s: None, poll_seconds=0.1)
    deps = Deps(cfg=cfg, audit=audit, state=state, store=store, vault=VaultWriter(cfg, audit), claude=client,
                router=build_router(cfg), notifier=notifier, collectors=[], clock=clock)
    return Rig(cfg, deps, audit, state, store, runner, notifier, clock, tmp_vault)


def new_job(rig: Rig, *, attempts: int = 1, force: bool = False, dry_run: bool = False,
            no_claude: bool = False, window_hours: float = 36.0, suffix: str = "") -> Job:
    """Enqueue and claim, the way the daemon does."""
    now = rig.clock.now
    created = iso(now)
    job = Job(
        id=f"consolidate-{rig.day.isoformat()}{suffix}", kind=CONSOLIDATE_KIND, key=rig.day.isoformat(),
        job_class="observe_only", latency_class="background_batch", origin="manual",
        created_at=created, not_before=created, deadline=iso(now + timedelta(hours=3)),
        window=JobWindow(start=iso(now - timedelta(hours=window_hours)), end=created),
        params={"force": force, "dry_run": dry_run, "no_claude": no_claude, "notify": False},
        history=[HistoryEntry(ts=created, from_state=None, to="pending", note="test")],
    )
    assert rig.store.enqueue(job)
    claimed = rig.store.claim_next(now)
    assert claimed is not None and claimed.id == job.id
    claimed.attempts = attempts
    rig.store.update(claimed)
    return claimed


def plant(rig: Rig, name: str, text: str, hours_ago: float = 3.0) -> Path:
    path = write(rig.vault / "sessions" / name, text)
    set_age(path, rig.clock.now, hours_ago)
    return path


def plant_clean(rig: Rig, name: str = "2026-10-05-11.md", hours_ago: float = 3.0) -> Path:
    return plant(rig, name, SESSION.format(day="2026-10-05", extra="", more=""), hours_ago)


def ctx_for(rig: Rig, hours: float = 36.0) -> CollectContext:
    now = rig.clock.now
    return CollectContext(cfg=rig.cfg, window_start=now - timedelta(hours=hours), window_end=now, now=now)


# --- gather: the collector side --------------------------------------------------------


def test_gather_keeps_content_lines_with_physical_line_numbers(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    by_id = {i.id: i for i in got.items}
    # Line 1 to 4 are the front matter, line 5 is a heading, line 6 the first bullet.
    assert by_id["2026-10-05-11#L6"].text == "Built the synthetic widget in widget.py"
    assert by_id["2026-10-05-11#L7"].text == "Added retry logic to the synthetic queue"
    assert by_id["2026-10-05-11#L10"].text.startswith("Decision: keep one queue")
    assert got.notes_read == 1


def test_gather_skips_front_matter_headings_blank_lines_and_files_changed(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    texts = [i.text for i in got.items]
    assert not any(t.startswith(("##", "---", "type:", "date:")) for t in texts)
    assert not any("src/widget.py" in t for t in texts), "the Files changed section is not memory material"
    assert all(t.strip() for t in texts)
    assert any("Unclear flaky test" in t for t in texts)


def test_gather_window_and_jarvis_notes(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig, "2026-10-05-11.md", hours_ago=3.0)
    plant_clean(rig, "2026-09-01-09.md", hours_ago=24 * 40)
    plant(rig, "jarvis-digest-2026-10-05.md", SESSION.format(day="2026-10-05", extra="", more=""))
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    assert {i.id.split("#")[0] for i in got.items} == {"2026-10-05-11"}
    assert got.notes_read == 1


def test_gather_holds_only_the_line_with_a_term(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.gates.sensitive_terms = ["zebra-token"]

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    plant(rig, "2026-10-05-11.md", SESSION.format(
        day="2026-10-05", extra="", more="- Decision: rotate the zebra-token before friday\n"))
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    assert not any("zebra" in i.text or "zebra" in i.title for i in got.items)
    assert any("Built the synthetic widget" in i.text for i in got.items), "the other lines flow on"
    assert got.lines_held == 1 and len(got.withheld) == 1
    ref = got.withheld[0]
    assert ref.reason.startswith("term:") and ref.source_ref.endswith("2026-10-05-11.md#L11")
    assert "zebra" not in ref.model_dump_json()


NOTE_WITH_HEADINGS = """---
type: session
date: 2026-10-05
---
# {h1}

## What we built
- Built the synthetic widget

## {h2}
- Decision: first line under the second heading
- Decision: second line under the second heading

### Detail
- A detail under the second heading

## Open threads
- Unclear flaky test
"""


def test_gather_holds_a_whole_section_when_its_heading_has_a_term(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.gates.sensitive_terms = ["zebra-token"]

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    plant(rig, "2026-10-05-11.md", NOTE_WITH_HEADINGS.format(h1="Synthetic note", h2="Decisions about the zebra-token"))
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    texts = [i.text for i in got.items]
    assert "Built the synthetic widget" in texts and "Unclear flaky test" in texts, "other sections flow on"
    assert not any("second heading" in t or "A detail" in t for t in texts), "the heading and what it covers are held"
    assert got.lines_held == 4, "the heading and its three lines, the nested heading's line included"
    assert all("zebra" not in w.model_dump_json() for w in got.withheld)
    assert {w.reason for w in got.withheld} == {"term:0", "section:term:0"}


def test_gather_holds_the_whole_note_when_its_title_has_a_term(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.gates.sensitive_terms = ["zebra-token"]

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    plant(rig, "2026-10-05-11.md", NOTE_WITH_HEADINGS.format(h1="About the zebra-token", h2="Decisions"))
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    assert got.items == [] and got.lines_held >= 5


def test_gather_leaves_the_note_title_out_of_item_titles(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant(rig, "2026-10-05-11.md", NOTE_WITH_HEADINGS.format(h1="Synthetic note title", h2="Decisions"))
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    assert got.items and all("Synthetic note title" not in i.title for i in got.items)
    by_id = {i.id: i for i in got.items}
    assert by_id["2026-10-05-11#L8"].title == "2026-10-05-11 what we built"
    assert len(by_id["2026-10-05-11#L11"].title) <= 80


def test_gather_withholds_a_tagged_note_whole(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant(rig, "2026-10-05-09.md", SESSION.format(day="2026-10-05", extra="tags: [sensitive]\n", more=f"\n{CANARY}\n"))
    plant_clean(rig, "2026-10-05-11.md")
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    assert {i.id.split("#")[0] for i in got.items} == {"2026-10-05-11"}
    assert got.notes_withheld == 1 and got.notes_read == 1
    assert CANARY not in json.dumps([i.model_dump() for i in got.items])
    assert any(w.reason.startswith("tag_") or "sensitive" in w.reason for w in got.withheld)


def test_gather_limits_notes_and_lines(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.consolidate.max_notes = 2
        cfg.consolidate.max_lines_per_note = 3

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    for n in range(4):
        plant_clean(rig, f"2026-10-0{n + 1}-10.md", hours_ago=2.0 + n)
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    assert got.notes_read == 2
    assert {i.id.split("#")[0] for i in got.items} == {"2026-10-04-10", "2026-10-03-10"}, "newest names first"
    assert len(got.items) == 6


def test_gather_without_a_sessions_folder_is_empty(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    (tmp_vault / "sessions").rmdir()
    got = gather(rig.cfg, ctx_for(rig).window_start, rig.clock.now)
    assert got.items == [] and got.notes_read == 0


# --- parse_candidates: the output side --------------------------------------------------

ALLOWED = ["2026-10-05-11#L6", "2026-10-05-11#L10", "2026-10-04-09#L3"]


def reply(*candidates: dict[str, Any]) -> str:
    return json.dumps({"candidates": list(candidates)})


def cand(title: str = "Queue ordering", evidence: Any = None, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": title, "pattern": "A single queue keeps ordering.", "why_it_matters": "Reordering broke a run.",
        "applies_to": "synthetic widget", "evidence": evidence if evidence is not None
        else [{"note": "2026-10-05-11", "line": 10}],
    }
    body.update(over)
    return body


def test_parse_valid_candidate(tmp_cfg: Config) -> None:
    out = parse_candidates(reply(cand()), ALLOWED, tmp_cfg, 8)
    assert len(out.candidates) == 1
    c = out.candidates[0]
    assert c.title == "Queue ordering" and c.evidence == [("2026-10-05-11", 10)]
    assert out.dropped_ungrounded == 0 and out.dropped_flagged == 0


def test_parse_drops_evidence_that_was_never_sent(tmp_cfg: Config) -> None:
    mixed = cand(evidence=[{"note": "2026-10-05-11", "line": 99}, {"note": "2026-10-05-11", "line": 6}])
    invented = cand("Invented", evidence=[{"note": "made-up-note", "line": 1}])
    empty = cand("No evidence", evidence=[])
    out = parse_candidates(reply(mixed, invented, empty), ALLOWED, tmp_cfg, 8)
    assert [c.title for c in out.candidates] == ["Queue ordering"]
    assert out.candidates[0].evidence == [("2026-10-05-11", 6)]
    assert out.dropped_ungrounded == 2


def test_parse_accepts_note_names_with_md_or_wikilink_and_string_lines(tmp_cfg: Config) -> None:
    c = cand(evidence=[{"note": "[[2026-10-05-11.md]]", "line": "10"}, {"note": "2026-10-05-11", "line": 10}])
    out = parse_candidates(reply(c), ALLOWED, tmp_cfg, 8)
    assert out.candidates[0].evidence == [("2026-10-05-11", 10)], "normalized and de-duplicated"


def test_parse_caps_the_number_of_candidates(tmp_cfg: Config) -> None:
    many = [cand(f"Idea {n}") for n in range(12)]
    out = parse_candidates(reply(*many), ALLOWED, tmp_cfg, 8)
    assert len(out.candidates) == 8 and out.dropped_over_limit == 4
    assert parse_candidates(reply(*many), ALLOWED, tmp_cfg, 3).candidates[2].title == "Idea 2"


def test_parse_strips_a_fence_dashes_wikilinks_and_headings(tmp_cfg: Config) -> None:
    em = chr(0x2014)
    c = cand(f"# Title {em} with [[link]]", pattern=f"Line one\n## Open threads\n- item {em} two")
    out = parse_candidates("```json\n" + reply(c) + "\n```", ALLOWED, tmp_cfg, 8)
    got = out.candidates[0]
    for text in (got.title, got.pattern, got.why_it_matters, got.applies_to):
        assert "\n" not in text and em not in text and "[[" not in text and not text.startswith("#")
    assert "Open threads" in got.pattern and "\n## " not in got.pattern


def test_parse_clips_long_fields(tmp_cfg: Config) -> None:
    c = cand("T" * 500, pattern="p" * 5000, why_it_matters="w" * 5000, applies_to="a" * 5000)
    got = parse_candidates(reply(c), ALLOWED, tmp_cfg, 8).candidates[0]
    assert len(got.title) <= 120 and len(got.pattern) <= 500 and len(got.why_it_matters) <= 400
    assert len(got.applies_to) <= 160


def test_parse_drops_a_candidate_that_echoes_a_sensitive_term(tmp_cfg: Config) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.gates.sensitive_terms = ["zebra-token"]
    leaky = cand("Leaky", pattern="Rotate the zebra-token weekly")
    out = parse_candidates(reply(leaky, cand()), ALLOWED, cfg, 8)
    assert [c.title for c in out.candidates] == ["Queue ordering"]
    assert out.dropped_flagged == 1


def test_parse_rejects_bad_json_and_bad_schema(tmp_cfg: Config) -> None:
    with pytest.raises(ValueError):
        parse_candidates("not json {", ALLOWED, tmp_cfg, 8)
    with pytest.raises(ValueError):
        parse_candidates(json.dumps({"nothing": []}), ALLOWED, tmp_cfg, 8)
    with pytest.raises(ValueError):
        parse_candidates(json.dumps({"candidates": [{"title": "only a title"}]}), ALLOWED, tmp_cfg, 8)


def test_parse_an_empty_list_is_valid(tmp_cfg: Config) -> None:
    out = parse_candidates(reply(), ALLOWED, tmp_cfg, 8)
    assert out.candidates == []


# --- the job ------------------------------------------------------------------------------


def test_dry_run_prints_a_payload_and_touches_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    before = tree(tmp_vault)
    result = run_consolidate_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    assert result["status"] == "dry_run"
    assert "2026-10-05-11#L6" in result["payload"] and "Built the synthetic widget" in result["payload"]
    assert result["items"]["to_claude"] == 5 and result["payload_bytes"] > 0
    assert tree(tmp_vault) == before
    assert rig.runner.records() == [], "nothing was spawned, not even --version"
    assert rig.state.budget.snapshot()["calls"] == 0
    assert rig.events("vault_intent") == []


def test_dry_run_payload_carries_no_held_content(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.gates.sensitive_terms = ["zebra-token"]

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    plant(rig, "2026-10-05-11.md", SESSION.format(
        day="2026-10-05", extra="", more="- Decision: rotate the zebra-token before friday\n"))
    plant(rig, "2026-10-05-09.md", SESSION.format(day="2026-10-05", extra="tags: [sensitive]\n", more=f"\n{CANARY}\n"))
    (tmp_vault / "telos" / "notes-for-me.md").write_text(f"telos {CANARY}\n", encoding="utf-8")
    result = run_consolidate_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    blob = json.dumps(result)
    assert "zebra" not in blob and CANARY not in blob and "telos" not in result["payload"]
    assert result["items"]["held"] == 2
    assert {h["reason"].split(":")[0] for h in result["held"]} >= {"term"}


def test_a_run_writes_one_candidates_note_and_nothing_else(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    before = tree(tmp_vault)
    job = new_job(rig)
    result = run_consolidate_job(job, rig.deps, mode="daemon")

    assert tree(tmp_vault) - before == {f"raw/jarvis/candidates-{rig.day.isoformat()}.md"}
    assert not (tree(tmp_vault) - before) & {p for p in tree(tmp_vault) if p.startswith(("insights/", "telos/"))}
    text = rig.note().read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "---" and "generator: jarvisd" in lines[:12] and "type: jarvis-candidates" in lines
    assert "status: proposed" in text and f"date: {rig.day.isoformat()}" in text
    assert "## 1. " in text and "[[2026-10-05-11]]" in text and "line 6" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text
    assert not any(line.lower().startswith("## open threads") for line in lines)
    assert result["status"] == "written" and result["candidates"] == 2 and result["claude_calls"] == 1
    assert result["rel"] == f"raw/jarvis/candidates-{rig.day.isoformat()}.md"
    assert rig.store.exists(job.id) == "done"
    done = rig.events("consolidate_done")
    assert len(done) == 1 and done[0]["candidates"] == 2 and done[0]["rel"] == result["rel"]
    assert rig.notifier.messages == [], "the digest announces candidates, this job does not"


def test_the_call_uses_the_isolated_argv_budget_and_breaker(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    paid = rig.runner.paid()
    assert len(paid) == 1
    argv = paid[0]["argv"]
    for flag, value in (("--tools", ""), ("--setting-sources", ""), ("--permission-prompts", "none")):
        assert argv[argv.index(flag) + 1] == value
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv and "--disable-slash-commands" in argv
    assert "--max-budget-usd" in argv
    prompt = argv[argv.index("--system-prompt") + 1]
    assert prompt == consolidate.SYSTEM_PROMPT and "memory candidates" in prompt
    assert chr(0x2014) not in prompt and chr(0x2013) not in prompt
    assert "at most 8" in paid[0]["stdin"], "the limit travels in the header"
    snap = rig.state.budget.snapshot()
    assert snap["calls"] == 1 and snap["by_purpose"].get("consolidate", 0) > 0
    intent = rig.events("claude_intent")
    assert len(intent) == 1 and intent[0]["purpose"] == "consolidate"
    assert len(rig.events("claude_call")) == 1


def test_canary_never_leaves_the_machine(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.gates.sensitive_terms = ["zebra-token"]

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    plant_clean(rig)
    plant(rig, "2026-10-05-09.md", SESSION.format(day="2026-10-05", extra="tags: [sensitive]\n", more=f"\n{CANARY}\n"))
    plant(rig, "2026-10-04-09.md", SESSION.format(
        day="2026-10-04", extra="", more=f"- Decision: zebra-token {CANARY}\n"))
    run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    stdin = rig.runner.paid()[0]["stdin"]
    assert CANARY not in json.dumps(rig.runner.records()) and "zebra" not in stdin
    assert "2026-10-05-09" not in stdin, "the tagged note's name never leaves either"
    assert CANARY not in rig.audit.path.read_text(encoding="utf-8")
    assert CANARY not in rig.note().read_text(encoding="utf-8")
    for path in (tmp_path / "jarvis").rglob("*"):
        if path.is_file():
            assert CANARY not in path.read_text(encoding="utf-8", errors="ignore"), path
    held = rig.store.held()
    assert len(held) >= 2 and all("zebra" not in json.dumps(h) for h in held)


def test_no_items_means_no_call_and_no_note(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    before = tree(tmp_vault)
    job = new_job(rig)
    result = run_consolidate_job(job, rig.deps, mode="daemon")
    assert result["status"] == "no_items" and result["candidates"] == 0
    assert tree(tmp_vault) == before and rig.runner.paid() == []
    assert rig.store.exists(job.id) == "done"
    assert rig.events("consolidate_done")[0]["candidates"] == 0


def test_a_disabled_client_writes_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, claude=False)
    plant_clean(rig)
    before = tree(tmp_vault)
    result = run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    assert result["status"] == "disabled" and result["claude_status"] == "disabled"
    assert tree(tmp_vault) == before and rig.runner.records() == []


def test_no_claude_param_writes_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    before = tree(tmp_vault)
    result = run_consolidate_job(new_job(rig, no_claude=True), rig.deps, mode="manual")
    assert result["status"] == "disabled" and rig.runner.records() == [] and tree(tmp_vault) == before


def test_an_existing_note_is_a_noop_unless_forced(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    assert len(rig.runner.paid()) == 1
    rig.store.prune(rig.clock.now + timedelta(days=400), done_days=1)
    second = new_job(rig)
    result = run_consolidate_job(second, rig.deps, mode="daemon")
    assert result["status"] == "noop" and len(rig.runner.paid()) == 1
    rig.store.prune(rig.clock.now + timedelta(days=400), done_days=1)
    forced = run_consolidate_job(new_job(rig, force=True, suffix="-r2"), rig.deps, mode="manual")
    assert forced["status"] == "written" and forced["rel"].endswith("-r2.md")
    assert rig.note(f"candidates-{rig.day.isoformat()}-r2.md").is_file()


@pytest.mark.parametrize("scenario,kind", [("401", "auth"), ("429", "rate_limit"), ("invalid_json", "bad_json")])
def test_claude_failures_fail_the_job_and_write_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                                        scenario: str, kind: str) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario=scenario)
    plant_clean(rig)
    before = tree(tmp_vault)
    job = new_job(rig, attempts=3)
    result = run_consolidate_job(job, rig.deps, mode="daemon")
    assert result["status"] == "failed" and result["claude_status"] == kind
    assert tree(tmp_vault) == before
    assert rig.store.exists(job.id) == "failed"
    failed = rig.events("consolidate_failed")
    assert len(failed) == 1 and failed[0]["error"] == kind


def test_a_transient_failure_asks_for_a_retry_until_the_last_attempt(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario="500")
    plant_clean(rig)
    with pytest.raises(Retry) as caught:
        run_consolidate_job(new_job(rig, attempts=1), rig.deps, mode="daemon")
    assert caught.value.error == "transient" and caught.value.delay > timedelta(0)


def test_a_bad_schema_reply_fails_without_a_note(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario="bad_schema")
    plant_clean(rig)
    before = tree(tmp_vault)
    result = run_consolidate_job(new_job(rig, attempts=3), rig.deps, mode="daemon")
    assert result["status"] == "failed" and result["claude_status"] == "bad_schema"
    assert tree(tmp_vault) == before


def test_the_budget_refuses_a_second_call_and_the_breaker_counts(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.claude.daily_calls = 0

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    plant_clean(rig)
    result = run_consolidate_job(new_job(rig, attempts=3), rig.deps, mode="daemon")
    assert result["status"] == "failed" and result["claude_status"] == "budget"
    assert rig.runner.paid() == [] and rig.events("budget_refused")


def test_a_kill_file_puts_the_job_back(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    (rig.state.dir / "KILL").write_text("x", encoding="utf-8")
    with pytest.raises(Retry) as caught:
        run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    assert caught.value.killed and rig.runner.records() == []


def test_hallucinated_evidence_is_dropped_from_the_note(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_CANDIDATES="ungrounded")
    plant_clean(rig)
    result = run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    text = rig.note().read_text(encoding="utf-8")
    assert result["candidates"] == 1 and result["dropped"]["ungrounded"] == 1
    assert "made-up-note" not in text and "Invented idea" not in text


def test_more_than_eight_candidates_are_cut_to_eight(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_CANDIDATES="many")
    plant_clean(rig)
    result = run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    assert result["candidates"] == 8 and rig.note().read_text(encoding="utf-8").count("\n## ") == 8


def test_a_zero_candidate_reply_writes_a_note_that_says_so(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_CANDIDATES="none")
    plant_clean(rig)
    result = run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    assert result["status"] == "written" and result["candidates"] == 0
    assert "No candidates" in rig.note().read_text(encoding="utf-8")


def test_a_busy_vault_retries_and_reuses_the_paid_answer(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    real = rig.deps.vault.write_raw
    calls = {"n": 0}

    def busy(name: str, text: str, job_id: str | None) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileBusy("Obsidian has the file open")
        return real(name, text, job_id)

    monkeypatch.setattr(rig.deps.vault, "write_raw", busy)
    job = new_job(rig, attempts=1)
    with pytest.raises(Retry) as caught:
        run_consolidate_job(job, rig.deps, mode="daemon")
    assert caught.value.error == "vault_busy"
    assert not rig.note().exists() and len(rig.runner.paid()) == 1
    result = run_consolidate_job(job, rig.deps, mode="daemon")
    assert result["status"] == "written" and len(rig.runner.paid()) == 1, "no second paid call"
    assert not (rig.state.dir / "runs" / job.id / "candidates.json").exists(), "the stash is dropped"


def test_the_watermark_does_not_move_on_a_failed_run(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario="401")
    plant_clean(rig)
    run_consolidate_job(new_job(rig, attempts=3), rig.deps, mode="daemon")
    assert consolidate.read_watermark(rig.state) is None


def test_the_watermark_moves_after_a_written_note(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    job = new_job(rig)
    run_consolidate_job(job, rig.deps, mode="daemon")
    mark = consolidate.read_watermark(rig.state)
    assert mark is not None and job.window is not None and iso(mark) == job.window.end


def test_the_next_window_starts_at_the_watermark(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=enabled)
    day = at(rig.clock.now.astimezone(timezone.utc), "05:00")
    consolidate.write_watermark(rig.state, day - timedelta(hours=10))
    job_id = reconcile_consolidation(day, rig.cfg, rig.state, rig.store, rig.audit)
    job = rig.store.get(job_id or "")
    assert job is not None and job.window is not None
    assert job.window.start == iso(day - timedelta(hours=10))


def test_a_long_absence_is_capped_like_the_digest_window(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    day = at(rig.clock.now.astimezone(timezone.utc), "05:00")
    consolidate.write_watermark(rig.state, day - timedelta(days=30))
    assert consolidate.window_for(rig.cfg, rig.state, day)[0] == day - timedelta(hours=rig.cfg.consolidate.window_hours_max)
    assert consolidate.write_watermark(rig.state, day - timedelta(days=31)) is False, "the mark only moves forward"


def test_the_job_never_names_a_write_path_other_than_write_raw() -> None:
    source = (ROOT / "jarvisd" / "consolidate.py").read_text(encoding="utf-8")
    assert "write_session" not in source and source.count(".write_raw(") == 1
    assert "vault_write_raw" not in source and "vault_write_sessions" not in source
    assert "subprocess" not in source and "shell=True" not in source


# --- scheduling ------------------------------------------------------------------------------


def enabled(cfg: Config) -> None:
    cfg.consolidate.enabled = True


def at(day: datetime, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def test_reconcile_does_nothing_while_disabled(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    now = at(rig.clock.now, "05:00")
    assert reconcile_consolidation(now, rig.cfg, rig.state, rig.store, rig.audit) is None
    assert rig.store.counts()["pending"] == 0


def test_reconcile_enqueues_once_after_run_at(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=enabled)
    day = rig.clock.now.astimezone(timezone.utc)
    assert reconcile_consolidation(at(day, "01:59"), rig.cfg, rig.state, rig.store, rig.audit) is None
    job_id = reconcile_consolidation(at(day, "02:00"), rig.cfg, rig.state, rig.store, rig.audit)
    assert job_id == f"consolidate-{day.date().isoformat()}"
    assert reconcile_consolidation(at(day, "02:02"), rig.cfg, rig.state, rig.store, rig.audit) is None
    job = rig.store.get(job_id)
    assert job is not None and job.kind == CONSOLIDATE_KIND and job.latency_class == "background_batch"
    assert job.window is not None and job.params.dry_run is False and job.params.no_claude is False
    enq = rig.events("job_enqueued")
    assert len(enq) == 1 and enq[0]["kind"] == CONSOLIDATE_KIND


def test_reconcile_respects_kill_and_pause_and_the_run_at_setting(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def later(cfg: Config) -> None:
        cfg.consolidate.enabled = True
        cfg.consolidate.run_at = "04:30"

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=later)
    day = rig.clock.now.astimezone(timezone.utc)
    assert reconcile_consolidation(at(day, "03:00"), rig.cfg, rig.state, rig.store, rig.audit) is None
    rig.state.set_pause(None, "test")
    assert reconcile_consolidation(at(day, "05:00"), rig.cfg, rig.state, rig.store, rig.audit) is None
    rig.state.clear_pause()
    (rig.state.dir / "KILL").write_text("x", encoding="utf-8")
    assert reconcile_consolidation(at(day, "05:00"), rig.cfg, rig.state, rig.store, rig.audit) is None


def test_reconcile_needs_an_aware_time(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=enabled)
    with pytest.raises(ValueError):
        reconcile_consolidation(datetime(2026, 10, 6, 5, 0), rig.cfg, rig.state, rig.store, rig.audit)


def test_a_tick_enqueues_and_runs_the_consolidation(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=enabled)
    plant_clean(rig)
    now = at(rig.clock.now.astimezone(timezone.utc), "05:00")
    rig.clock.set(now)
    # A fresh RECENT.md so the digest's own wait for the nightly rebuild does not hold the digest back.
    set_age(tmp_vault / "RECENT.md", now, 0.5)
    for path in (tmp_vault / "sessions").iterdir():
        set_age(path, now, 3.0)
    result = daemon_mod.tick(rig.deps, now, task_mode=False, command_runner=lambda argv, timeout: READY)
    assert result.action == "ran"
    assert rig.note(f"candidates-{now.date().isoformat()}.md").is_file()
    assert rig.store.exists(f"consolidate-{now.date().isoformat()}") == "done"
    assert not (tmp_vault / "raw" / "jarvis" / f"digest-{now.date().isoformat()}.md").exists(), "06:30 has not come"


# --- the digest line -----------------------------------------------------------------------------


def system_lines(rig: Rig) -> list[str]:
    now = rig.clock.now
    collector = SystemCollector(rig.audit, rig.state, store=rig.store, runner=lambda argv, timeout: (1, ""))
    ctx = CollectContext(cfg=rig.cfg, window_start=now - timedelta(hours=30), window_end=now, now=now)
    return [i.title for i in collector.collect(ctx).items]


def test_the_digest_has_no_candidates_line_without_a_run(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    assert not any("candidate" in line.lower() for line in system_lines(rig))


def test_the_digest_gains_a_candidates_line_after_a_run(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    lines = [line for line in system_lines(rig) if "candidate" in line.lower()]
    assert len(lines) == 1
    assert "2 memory candidates" in lines[0] and f"raw/jarvis/candidates-{rig.day.isoformat()}.md" in lines[0]
    assert "not promoted" in lines[0] or "review" in lines[0].lower()


def test_the_digest_candidates_line_for_a_failed_and_an_empty_run(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, scenario="401")
    plant_clean(rig)
    run_consolidate_job(new_job(rig, attempts=3), rig.deps, mode="daemon")
    failed = [line for line in system_lines(rig) if "candidate" in line.lower()]
    assert failed and "failed" in failed[0].lower() and "auth" in failed[0]


def test_the_candidates_line_renders_in_the_note_under_the_system_heading(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    from jarvisd import render
    from jarvisd.models import CollectResult

    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    run_consolidate_job(new_job(rig), rig.deps, mode="daemon")
    now = rig.clock.now
    collector = SystemCollector(rig.audit, rig.state, store=rig.store, runner=lambda argv, timeout: (1, ""))
    res = collector.collect(CollectContext(cfg=rig.cfg, window_start=now - timedelta(hours=30), window_end=now, now=now))
    assert isinstance(res, CollectResult)
    ctx = render.DigestContext(job_id="digest-x", day=now.date(), generated_at=now, window_start=now - timedelta(hours=30),
                               window_end=now, results={"system": res})
    body = "\n".join(render.SECTIONS["system"].render(ctx))
    assert body.startswith("## What JARVIS did while you slept") and "memory candidates" in body


# --- configuration -------------------------------------------------------------------------------


def test_the_tracked_config_ships_the_table_switched_off() -> None:
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    assert raw["consolidate"] == {"enabled": False, "run_at": "02:00", "max_candidates": 8}


def test_defaults_and_validation(tmp_cfg: Config) -> None:
    assert tmp_cfg.consolidate.enabled is False and tmp_cfg.consolidate.run_at == "02:00"
    assert tmp_cfg.consolidate.max_candidates == 8 and tmp_cfg.consolidate.run_at_hm() == (2, 0)
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    for bad in ({"run_at": "2am"}, {"max_candidates": 9}, {"max_candidates": 0}, {"enabled": "yes"}, {"typo": 1},
                {"window_hours_default": 100, "window_hours_max": 72}):
        broken = {**raw, "consolidate": {**raw["consolidate"], **bad}}
        with pytest.raises(ConfigError):
            build_config(broken)


# --- the command ---------------------------------------------------------------------------------


def cli_cfg(rig: Rig) -> Config:
    return rig.cfg


def test_cli_dry_run_prints_the_payload_and_spawns_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    code = cli.main(["consolidate", "--dry-run"], cfg=rig.cfg, claude_runner=rig.runner,
                    command_runner=lambda argv, timeout: READY, clock=lambda: datetime.now(timezone.utc))
    out = capsys.readouterr().out
    assert code == 0
    assert "Dry run" in out and "2026-10-05-11#L6" in out and "Nothing was written" in out
    assert rig.runner.records() == [] and tree(tmp_vault / "raw") == set()


def test_cli_without_claude_writes_nothing_and_spends_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    code = cli.main(["consolidate"], cfg=rig.cfg, claude_runner=rig.runner, clock=lambda: datetime.now(timezone.utc))
    out = capsys.readouterr().out
    assert code == 0 and "disabled" in out.lower() and "--claude" in out
    assert rig.runner.records() == [] and tree(tmp_vault / "raw") == set()


def test_cli_with_claude_writes_the_note(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    code = cli.main(["consolidate", "--claude"], cfg=rig.cfg, claude_runner=rig.runner,
                    network_probe=lambda: True, clock=lambda: datetime.now(timezone.utc))
    out = capsys.readouterr().out
    assert code == 0 and "candidates-" in out and "2 candidate" in out
    assert len(rig.runner.paid()) == 1
    assert len([p for p in (tmp_vault / "raw" / "jarvis").iterdir() if p.name.startswith("candidates-")]) == 1


def test_cli_rejects_a_bad_date(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    assert cli.main(["consolidate", "--date", "tomorrow"], cfg=rig.cfg) == 2
    assert "--date" in capsys.readouterr().err


def test_cli_refuses_under_a_kill_file(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean(rig)
    (rig.state.dir / "KILL").write_text("x", encoding="utf-8")
    code = cli.main(["consolidate", "--claude"], cfg=rig.cfg, claude_runner=rig.runner,
                    network_probe=lambda: True, clock=lambda: datetime.now(timezone.utc))
    assert code == 3 and rig.runner.records() == []
    capsys.readouterr()


# --- the client seam ------------------------------------------------------------------------------


def test_the_client_default_call_is_unchanged(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    """complete() with no new argument still returns a DigestSummary and uses the digest prompt."""
    from jarvisd.claude import SYSTEM_PROMPT
    from jarvisd.dispatch import clear_for_claude, run_gates
    from jarvisd.models import Item

    rig = build(tmp_cfg, tmp_path, tmp_vault)
    item = Item(id="a1", source="brain", kind="brain_thread", title="Synthetic", text="Synthetic thread", priority=1)
    gates = run_gates([item], rig.deps.router, rig.cfg, None, rig.audit)
    payload = clear_for_claude([item], gates, rig.cfg)
    reply_ = rig.deps.claude.complete(payload, "digest", 1)
    assert reply_.summary is not None and reply_.parsed is None
    argv = rig.runner.paid()[0]["argv"]
    assert argv[argv.index("--system-prompt") + 1] == SYSTEM_PROMPT


def test_a_custom_parser_failure_maps_to_the_usual_kinds(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    from jarvisd.dispatch import clear_for_claude, run_gates
    from jarvisd.models import Item

    rig = build(tmp_cfg, tmp_path, tmp_vault)
    item = Item(id="a1", source="brain", kind="brain_thread", title="Synthetic", text="Synthetic thread", priority=1)
    gates = run_gates([item], rig.deps.router, rig.cfg, None, rig.audit)
    payload = clear_for_claude([item], gates, rig.cfg)

    def parser(result: str, ids: Any) -> Any:
        raise ValueError("nope")

    with pytest.raises(ClaudeUnavailable) as caught:
        rig.deps.claude.complete(payload, "consolidate", 1, system_prompt="You propose memory candidates.", parser=parser)
    assert caught.value.kind == "bad_json"
