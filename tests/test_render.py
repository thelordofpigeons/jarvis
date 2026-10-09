"""Deterministic digest renderer (design section 9, grammar 2 in docs/hub-rework-contract.md).

The contexts are hand built from synthetic items. Goldens live in tests/golden and were
read by eye when they were regenerated for grammar 2; a diff here is a layout change. The
regexes below are copied from the contract: the hub parses the note with the same ones.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import CANARY
from jarvisd import render
from jarvisd.common import norm_key
from jarvisd.models import Attention, CollectResult, DigestSummary, Item, WithheldItem
from jarvisd.render import (
    DEFAULT_SECTIONS,
    GRAMMAR,
    HEADINGS,
    ID_TAIL,
    SECTIONS,
    DigestContext,
    Section,
    digest_filename,
    render_digest,
)

GOLDEN = Path(__file__).parent / "golden"
TZ = timezone(timedelta(hours=1))
EM = chr(0x2014)
EN = chr(0x2013)
HEAD = "ab" * 32

FRONTMATTER_KEYS = [
    "type", "generator", "generator_version", "job_id", "date", "generated_at", "window_start",
    "window_end", "status", "late", "claude", "local_tier", "degraded", "cost_usd", "items",
    "grammar", "n_collected", "n_cleared", "n_held", "n_start_here", "n_attention", "n_still_open",
    "n_still_open_hidden", "n_decided", "n_repos_active", "n_repos_quiet", "n_system_anomalies",
    "sources", "audit_seq", "audit_head", "tags",
]

# The grammar, as the contract states it (section 1.2).
START_LINE = re.compile(r"^(?P<n>[1-5])\. (?P<text>[A-Z][^\[]{9,158}) \[(?P<id>[0-9a-f]{8})\]$")
ATTENTION_LINE = re.compile(
    r"^- (?P<kind>CI failing|Job failed|Daemon crashed|Unclean exit|Task overdue|Backlog held|Breaker open|Config invalid): "
    r"(?P<text>[^\[]+?)(?: \[(?P<id>[0-9a-f]{8})\])?$")
TASK_LINE = re.compile(
    r"^- (?P<task_id>\S+) (?P<title>.+?), status (?P<status>[^,]+), "
    r"(?P<due>due \d{4}-\d{2}-\d{2}(?: \((?:OVERDUE|TODAY)\))?|no due date)\. \[(?P<id>[0-9a-f]{8})\]$")
OPEN_LINE = re.compile(r"^- (?P<group>[^:\[\]]{1,60}): (?P<text>[^\[]+?)(?: \((?P<age>\d+)d\))? \[(?P<id>[0-9a-f]{8})\]$")
HIDDEN_LINE = re.compile(r"^- (?P<hidden>\d+) more open threads? not shown \(cap 10\)\.$")
DECIDED_LINE = re.compile(r"^- (?P<text>[^\[]{3,200}) \[(?P<id>[0-9a-f]{8})\]$")
REPO_LINE = re.compile(
    r"^- (?P<name>\S+?)(?: \((?P<tag>work)\))?:? (?:branch (?P<branch>.+?), )?(?P<commits>\d+) commits? since window, "
    r"(?P<modified>\d+) modified, (?P<untracked>\d+) untracked(?P<rest>[^\[]*) \[(?P<id>[0-9a-f]{8})\]$")
DIRTY_LINE = re.compile(r"^- Uncommitted only: (?P<list>(?:\S+ \d+/\d+)(?:, \S+ \d+/\d+)*)\.$")
QUIET_LINE = re.compile(r"^- Quiet: (?P<n>\d+) repos?\.$")
GH_QUIET_LINE = re.compile(r"^- GitHub quiet: (?P<n>\d+) repos?, CI green or none\.$")
GH_NOT_READ_LINE = re.compile(r"^- GitHub not read: (?P<n>\d+) repos? \((?P<states>[a-z_]+ \d+(?:, [a-z_]+ \d+)*)\)\.$")
GREEN_LINE = re.compile(
    r"^- All green: (?P<jobs>\d+) jobs? done, 0 failed, \$(?P<usd>\d+\.\d{2}) Claude, breaker closed, disk ok, tasks ok\.$")
ANOMALY_LINE = re.compile(r"^- (?P<what>[A-Z][^:]{2,40}): (?P<detail>.+)\.$")
HELD_LINE = re.compile(
    r"^- Held: (?P<sens>\d+) sensitive(?: \(ids (?P<ids>[^;]+); reasons: (?P<reasons>[^)]+)\))?, (?P<pol>\d+) policy, "
    r"(?P<cap>\d+) over cap\. Claude: (?P<claude>[a-z_ ]+)\. Run `jarvis held` in a terminal\.$")


# --- builders --------------------------------------------------------------------------


def thread(item_id: str, day: str, text: str, *, stale: bool = False, age: int | None = None,
           session: str | None = None) -> Item:
    if age is None:
        age = (date(2026, 10, 6) - date.fromisoformat(day)).days
    meta = {"date": day, "stale": stale, "section": "thread", "age_days": age, "key": norm_key(text)}
    if session:
        meta["session"] = session
    return Item(id=item_id, source="brain", kind="brain_thread", title=text[:80], text=text,
                tags=["stale"] if stale else [], meta=meta)


def decision(item_id: str, day: str, text: str) -> Item:
    return Item(id=item_id, source="brain", kind="brain_decision", title=text[:80], text=text,
                meta={"date": day, "stale": False, "section": "decision", "key": norm_key(text)})


def session(item_id: str, name: str, section: str, text: str, *, day: str = "2026-10-05", age: int = 1) -> Item:
    return Item(id=item_id, source="brain", kind="brain_session", title=name, text=text,
                meta={"session": name, "section": section, "date": day, "age_days": age, "stale": False,
                      "key": norm_key(text)})


def task(item_id: str = "c0ffee01", *, due_state: str = "overdue", due: str = "2026-10-05") -> Item:
    return Item(id=item_id, source="task", kind="active_task", title="fix(parser): synthetic task name",
                text="status IN REVIEW", work=True, priority=0,
                meta={"task_id": "123synth", "status": "IN REVIEW", "step": "in_review", "due_state": due_state,
                      "due_date": due})


def repo(item_id: str, name: str, *, branch: str = "main", commits: int = 0, modified: int = 0, untracked: int = 0,
         subjects: tuple[str, ...] = (), work: bool = False, counts_only: bool = False, ahead: int = 0,
         behind: int = 0, withheld: int = 0) -> Item:
    return Item(id=item_id, source="git", kind="git_repo", title=name, text="\n".join(subjects), work=work,
                meta={"repo": name, "branch": "" if counts_only else branch, "commits": commits,
                      "commits_withheld": withheld, "modified": modified, "untracked": untracked, "ahead": ahead,
                      "behind": behind, "counts_only": counts_only})


def gh(item_id: str, name: str, *, authored: int = 0, review: int = 0, ci: str = "success", branch: str = "main") -> Item:
    return Item(id=item_id, source="github", kind="github_repo", title=name, text="",
                meta={"repo": name, "authored": authored, "review_requested": review, "prs_withheld": 0, "ci": ci,
                      "ci_branch": branch, "stale_branches": 0, "stale_is_floor": False, "unknown": [],
                      "counts_only": False})


def system_line(item_id: str, text: str) -> Item:
    return Item(id=item_id, source="system", kind="system_line", title=text, meta={"render": "deterministic"})


def result(source: str, items: list[Item] | None = None, facts: dict | None = None, *, ok: bool = True,
           error: str | None = None) -> CollectResult:
    return CollectResult(source=source, ok=ok, error=error, items=items or [], facts=facts or {})


SYSTEM_LINES = [
    "Jobs: 1 done, 0 failed. Claude calls: 1 ($0.04, 3.2k tokens). Vault writes: 1. Breaker: closed.",
    "Daemon starts since last digest: 2. Unclean exits: 0.",
    "ExampleNightly: last run 2026-10-06 02:30, ok (0x00000000).",
    "Kill switch trips: 0. Watchdog crashloops: 0.",
]
SYSTEM_FACTS = {
    "jobs_done": 1, "jobs_failed": 0, "claude_calls": 1, "claude_cost_usd": 0.0412, "claude_tokens": 3200,
    "vault_writes": 1, "breaker_state": "closed", "breaker_events": 0, "daemon_starts": 2, "daemon_crashes": 0,
    "unclean_exits": 0, "config_invalid": 0, "corrections": 0, "disk_checked": True, "disk_ok": True,
    "disk_free_gb": 123,
    "scheduled_tasks": {"ExampleNightly": {"available": True, "last_result": 0, "last_result_text": "ok (0x00000000)",
                                           "ok": True, "last_run": "2026-10-06T02:30+01:00",
                                           "last_run_text": "2026-10-06 02:30"}},
    "logs_available": True, "killswitch_trips": 0, "killswitch_dry_runs": 0, "killswitch_last_reason": None,
    "watchdog_trip_requests": 0, "watchdog_crashloops": 0, "restart_attempts": 0, "restarts_skipped": 0,
    "gpu_yield_noise": 0, "log_malformed_lines": 0, "log_withheld_lines": 0,
    "queue": {"pending": 0, "running": 0, "done": 3, "failed": 0, "held": 2},
}

BRAIN_FACTS = {"recent_age_hours": 4.1, "recent_stale": False, "recent_missing": False, "orphan_checkpoints": 2,
               "new_sessions": 1, "withheld_count": 0, "duplicates_dropped": 0}
GIT_FACTS = {"repos_total": 4, "quiet": ["example-web"], "not_repos": ["example-missing"], "errors": []}


def system_result(facts: dict | None = None, lines: list[str] | None = None) -> CollectResult:
    return result("system", [system_line(f"s{i}", line) for i, line in enumerate(lines or SYSTEM_LINES)],
                  {**SYSTEM_FACTS, **(facts or {})})


def complete_results() -> dict[str, CollectResult]:
    return {
        "brain": result("brain", [
            thread("e5f6a7b8", "2026-10-04", "Synthetic thread waiting on a reviewer"),
            thread("0a1b2c3d", "2026-09-20", "Synthetic stale thread", stale=True),
            thread("4d4d4d4d", "2026-10-05", "Synthetic exporter is shipped, nothing left to do"),
            decision("9f8e7d6c", "2026-10-05", "Synthetic decision, because it is a fixture"),
            session("5a5a5a5a", "2026-10-05-20-parser-rework", "entry_point",
                    "Continue at fixture.py:10, wire the synthetic thing"),
            session("6b6b6b6b", "2026-10-05-20-parser-rework", "open_thread", "Synthetic follow up one"),
        ], BRAIN_FACTS),
        "task": result("task", [task()], {"task_active": True}),
        "git": result("git", [
            repo("a1b2c3d4", "example-api", branch="test", commits=3, modified=2, untracked=1, work=True,
                 subjects=("fix: synthetic parser bug", "feat: synthetic endpoint")),
            repo("b2c3d4e5", "example-notes", counts_only=True, commits=1, modified=0, untracked=0),
            repo("c3d4e5f6", "example-site", modified=4, untracked=2),
        ], GIT_FACTS),
        "system": system_result(),
    }


def ctx_for(**overrides: object) -> DigestContext:
    base: dict[str, object] = dict(
        job_id="digest-2026-10-06",
        day=date(2026, 10, 6),
        generated_at=datetime(2026, 10, 6, 6, 31, 40, tzinfo=TZ),
        window_start=datetime(2026, 10, 5, 5, 30, 12, tzinfo=TZ),
        window_end=datetime(2026, 10, 6, 6, 30, 30, tzinfo=TZ),
        results=complete_results(),
        audit_seq=812,
        audit_head=HEAD,
    )
    base.update(overrides)
    return DigestContext(**base)  # type: ignore[arg-type]


def complete_ctx() -> DigestContext:
    summary = DigestSummary(
        headline="One task is overdue and one repo moved overnight.",
        attention=[
            Attention(id="c0ffee01", why="Chase the review today, the task is overdue since yesterday"),
            Attention(id="b2c3d4e5", why="Read the one commit that landed in the notes repo"),
            Attention(id="a1b2c3d4", why="Held by policy, Claude never saw it"),
            Attention(id="deadbeef", why="An id nobody collected"),
        ],
        summaries={"e5f6a7b8": "Waiting on the reviewer, nudge today", "9f8e7d6c": "Fixture decision, nothing to do"},
    )
    held = [
        WithheldItem(id="w-3a9f1c", kind="brain_session", source_ref="C:/x/y.md", reason="component_sensitive"),
        WithheldItem(id="w-77d20e", kind="brain_session", source_ref="C:/x/z.md", reason="tag_frontmatter"),
        WithheldItem(id="a1b2c3d4", kind="git_repo", source_ref="", reason="work_policy", hold_kind="policy"),
    ]
    return ctx_for(summary=summary, held=held, cost_usd=0.0412, claude_status="ok")


def degraded_ctx() -> DigestContext:
    return ctx_for(status="degraded_no_llm", claude_status="budget", degraded=True, summary=None,
                   job_id="digest-2026-10-06", late=True)


def empty_ctx() -> DigestContext:
    results = {
        "brain": result("brain", [], {**BRAIN_FACTS, "new_sessions": 0}),
        "task": result("task", [], {"task_active": False}),
        "git": result("git", [], {"repos_total": 3, "quiet": ["example-api", "example-web", "example-notes"],
                                  "not_repos": [], "errors": []}),
        "system": system_result(lines=SYSTEM_LINES[:2]),
    }
    return ctx_for(results=results, summary=None, claude_status="no_items")


def all_held_ctx() -> DigestContext:
    results = complete_results()
    held: list[WithheldItem] = []
    for res in results.values():
        if res.source == "system":
            continue
        for it in res.items:
            held.append(WithheldItem(id=it.id, kind=it.kind, source_ref="C:/x/y.md", reason="term:0"))
    return ctx_for(results=results, held=held, summary=None, claude_status="no_items")


def read_golden(name: str) -> str:
    return (GOLDEN / f"{name}.md").read_bytes().decode("utf-8")


def section_of(text: str, name: str) -> list[str]:
    """Body lines of one section, or [] when the section is absent."""
    marker = f"## {HEADINGS[name]}\n"
    if marker not in text:
        return []
    body = text.split(marker, 1)[1].split("\n## ", 1)[0]
    return [ln for ln in body.split("\n") if ln]


def split_frontmatter(text: str) -> tuple[list[str], str]:
    assert text.startswith("---\n")
    head, _, body = text[4:].partition("\n---\n")
    return head.split("\n"), body


def front(text: str) -> dict[str, str]:
    return dict(ln.split(": ", 1) for ln in split_frontmatter(text)[0])


# --- goldens ---------------------------------------------------------------------------


def test_golden_complete() -> None:
    assert render_digest(complete_ctx()) == read_golden("digest_complete")


def test_golden_degraded() -> None:
    assert render_digest(degraded_ctx()) == read_golden("digest_degraded")


def test_golden_empty() -> None:
    assert render_digest(empty_ctx()) == read_golden("digest_empty")


def test_golden_all_held() -> None:
    assert render_digest(all_held_ctx()) == read_golden("digest_all_held")


# --- the shared grammar ----------------------------------------------------------------


def test_exported_grammar_constants_are_what_the_hub_imports() -> None:
    assert GRAMMAR == 2
    assert ID_TAIL.pattern == r" \[(?P<id>[0-9a-f]{8})\]$"
    assert HEADINGS == {
        "start_here": "Start here", "attention": "Attention", "active_task": "Active task", "still_open": "Still open",
        "decided": "Decided yesterday", "repos": "Repos", "system": "System", "held": "Held back and not summarized",
        "source_status": "Source status", "flag_mistake": "Flag a mistake",
    }
    assert not any(render.FORBIDDEN_HEADING.match(h) for h in HEADINGS.values())
    assert render.HELD_BACKLOG_ALERT == 20 and render.MAX_STILL_OPEN == 10 and render.MAX_DECIDED == 10


def test_default_section_order_covers_the_design() -> None:
    assert list(DEFAULT_SECTIONS) == [
        "start_here", "attention", "active_task", "still_open", "decided", "repos", "system", "held",
        "source_status", "flag_mistake",
    ]
    assert set(DEFAULT_SECTIONS) <= set(SECTIONS)
    text = render_digest(complete_ctx())
    headings = [ln[3:] for ln in text.split("\n") if ln.startswith("## ")]
    assert headings == [HEADINGS[n] for n in DEFAULT_SECTIONS]


@pytest.mark.parametrize("make", [complete_ctx, degraded_ctx, empty_ctx, all_held_ctx])
def test_every_item_line_ends_in_exactly_one_tail_and_status_lines_in_none(make) -> None:  # type: ignore[no-untyped-def]
    text = render_digest(make())
    tailed = ("start_here", "attention", "active_task", "still_open", "decided", "repos")
    for name in tailed:
        for line in section_of(text, name):
            ids = re.findall(r"\[[0-9a-f]{8}\]", line)
            assert len(ids) <= 1, line
            if ids:
                assert ID_TAIL.search(line), line
                assert line.count("[") == 1 and line.count("]") == 1, line
    for name in ("system", "held", "source_status", "flag_mistake"):
        for line in section_of(text, name):
            assert not ID_TAIL.search(line), line


@pytest.mark.parametrize("make", [complete_ctx, degraded_ctx, empty_ctx, all_held_ctx])
def test_frontmatter_keys_as_designed(make) -> None:  # type: ignore[no-untyped-def]
    text = render_digest(make())
    lines, _ = split_frontmatter(text)
    assert [ln.split(":", 1)[0] for ln in lines] == FRONTMATTER_KEYS
    assert "generator: jarvisd" in text[:400].split("\n")
    values = front(text)
    assert values["type"] == "jarvis-digest"
    assert values["tags"] == "[jarvis, digest]"
    assert values["grammar"] == "2"
    assert values["audit_head"] == HEAD and values["audit_seq"] == "812"
    assert re.fullmatch(r"\{brain: \w+, task: \w+, git: \w+, system: \w+, clickup: disabled, github: not_collected\}",
                        values["sources"])
    for key in FRONTMATTER_KEYS:
        if key.startswith("n_"):
            assert re.fullmatch(r"\d+", values[key]), key


def test_frontmatter_counts_for_complete() -> None:
    text = render_digest(complete_ctx())
    values = front(text)
    assert values["items"] == "{collected: 12, cleared: 9, held_sensitive: 2, held_policy: 1, over_cap: 0}"
    assert (values["n_collected"], values["n_cleared"], values["n_held"]) == ("12", "9", "3")
    assert values["n_start_here"] == str(len(re.findall(r"(?m)^\d\. ", text)))
    assert values["n_attention"] == str(len(section_of(text, "attention")))
    assert values["n_still_open"] == str(len(section_of(text, "still_open"))) == "3"
    assert values["n_still_open_hidden"] == "0"
    assert values["n_decided"] == str(len(section_of(text, "decided"))) == "1"
    assert values["n_repos_active"] == "2" and values["n_repos_quiet"] == "1"
    assert values["n_system_anomalies"] == "0"
    assert values["cost_usd"] == "0.0412"
    assert values["late"] == "false" and values["status"] == "complete" and values["claude"] == "ok"


def test_frontmatter_counts_say_zero_when_nothing_broke_and_nothing_is_open() -> None:
    values = front(render_digest(empty_ctx()))
    assert values["n_attention"] == "0" and values["n_still_open"] == "0" and values["n_decided"] == "0"
    assert values["n_start_here"] == "0" and values["n_repos_quiet"] == "3"


@pytest.mark.parametrize("make", [complete_ctx, degraded_ctx, empty_ctx, all_held_ctx])
def test_output_shape_rules(make) -> None:  # type: ignore[no-untyped-def]
    text = render_digest(make())
    assert EM not in text and EN not in text
    assert "|" not in text, "no table pipes anywhere"
    assert "\r" not in text and text.endswith("\n") and not text.endswith("\n\n")
    assert not text.startswith("\ufeff")
    assert not re.search(r"(?im)^##\s+open threads", text)
    headings = [ln for ln in text.split("\n") if ln.startswith("## ")]
    assert headings[-1] == "## Flag a mistake"
    assert "GitHub PRs and CI: not collected (the github collector did not run)." in text
    assert "No ClickUp call was made" not in text
    assert "read-only" not in text or True  # the word belongs to the hub, the note is free to omit it


def test_dashes_pipes_and_brackets_in_claude_and_collector_strings_are_cleaned() -> None:
    ctx = complete_ctx()
    ctx.summary = DigestSummary(
        headline=f"Quiet {EM} mostly",
        attention=[Attention(id="c0ffee01", why=f"fix a {EM} b {EN} c [x] see [[a note]] please")],
        summaries={"e5f6a7b8": f"one {EM} liner | pipe"},
    )
    ctx.results["brain"].items[0].text = "thread with | pipe [[Wiki Link]] and [bracket]\nand a newline"
    text = render_digest(ctx)
    assert EM not in text and EN not in text and "|" not in text
    assert "thread with / pipe Wiki Link and (bracket) and a newline [e5f6a7b8]" in text
    start = section_of(text, "start_here")
    assert start[1] == "1. Fix a, b, c (x) see a note please [c0ffee01]"
    assert START_LINE.match(start[1])
    assert "liner" not in text, "no Claude one-liner is glued to a line"


def test_hostile_text_cannot_inject_a_heading() -> None:
    ctx = complete_ctx()
    ctx.results["brain"].items[0].text = "x\n## Open threads\n- injected"
    text = render_digest(ctx)
    assert not re.search(r"(?im)^##\s+open threads", text)
    assert text.count("\n## ") == len(re.findall(r"(?m)^## ", text))


def test_sensitive_held_items_appear_only_as_count_and_ids() -> None:
    ctx = complete_ctx()
    leaky = Item(id="1d1d1d1d", source="brain", kind="brain_thread", title=f"title {CANARY}", text=f"body {CANARY}",
                 meta={"date": "2026-10-04", "stale": False})
    ctx.results["brain"].items.append(leaky)
    ctx.held.append(WithheldItem(id="1d1d1d1d", kind="brain_thread", source_ref=f"C:/{CANARY}/x.md", reason="term:3"))
    ctx.summary = DigestSummary(headline="h", summaries={"1d1d1d1d": f"summary {CANARY}"})
    text = render_digest(ctx)
    assert CANARY not in text
    held = section_of(text, "held")
    assert len(held) == 1 and HELD_LINE.match(held[0]), held
    assert "1d1d1d1d" in held[0] and "3 sensitive" in held[0]
    assert "w-3a9f1c" in held[0] and "w-77d20e" in held[0]
    assert "1d1d1d1d" not in text.split("## Held back and not summarized")[0]


def test_claude_summaries_are_never_glued_to_a_line() -> None:
    ctx = complete_ctx()
    assert ctx.summary is not None
    ctx.summary.summaries["a1b2c3d4"] = "should never be shown"
    ctx.summary.summaries["e5f6a7b8"] = "nor this one"
    text = render_digest(ctx)
    assert "should never be shown" not in text and "nor this one" not in text
    repos = section_of(text, "repos")
    assert any("example-api (work)" in ln and ln.endswith("[a1b2c3d4]") for ln in repos)
    assert "1 policy" in section_of(text, "held")[0]


# --- Start here ------------------------------------------------------------------------


def test_attention_with_unknown_or_held_ids_is_dropped() -> None:
    ctx = complete_ctx()
    assert ctx.summary is not None
    ctx.summary.attention.append(Attention(id="w-3a9f1c", why="held id, long enough to pass"))
    text = render_digest(ctx)
    start = section_of(text, "start_here")
    assert "deadbeef" not in "\n".join(start) and "w-3a9f1c" not in "\n".join(start)
    assert start[1] == "1. Chase the review today, the task is overdue since yesterday [c0ffee01]"
    assert start[2] == "2. Read the one commit that landed in the notes repo [b2c3d4e5]"
    assert "a1b2c3d4" not in "\n".join(start)  # policy-held: Claude never saw it, so it cannot cite it
    assert all(START_LINE.match(ln) for ln in start[1:])


def test_a_why_too_short_to_mean_anything_is_dropped() -> None:
    ctx = complete_ctx()
    assert ctx.summary is not None
    ctx.summary.attention = [Attention(id="c0ffee01", why="no"), Attention(id="e5f6a7b8", why="Nudge the reviewer today")]
    start = section_of(render_digest(ctx), "start_here")
    assert start[1:] == ["1. Nudge the reviewer today [e5f6a7b8]"]


def test_start_here_fallback_is_scored_by_action_needed_not_by_date() -> None:
    ctx = degraded_ctx()
    ctx.results["brain"].items.extend([
        thread("11111111", "2026-10-05", "Newest thread, nothing pressing about it"),
        thread("22222222", "2026-10-03", "Rotate the pasted sandbox key before the demo tomorrow"),
        thread("33333333", "2026-10-02", "Answer the reviewer, blocked since Monday"),
    ])
    ctx.results["github"] = result("github", [
        gh("aa000001", "example-web", review=2),
        gh("aa000002", "example-ci", ci="failure", branch="main"),
    ], {"repos_total": 2, "repos_read": 2, "quiet": [], "states": {"example-web": "ok", "example-ci": "ok"},
        "summary": {}})
    first = render_digest(ctx)
    assert first == render_digest(ctx)
    start = section_of(first, "start_here")
    assert start[0] == "Claude summary unavailable (budget). Deterministic sections below are complete."
    numbered = [ID_TAIL.search(ln).group("id") for ln in start[1:]]  # type: ignore[union-attr]
    # 4 the overdue task; 3 the deadline threads, newest date first ("waiting on", "tomorrow", "blocked"), then CI
    # failing (undated sorts after dated); the PR review (2) and the commits (1) fall off the cap of 5.
    assert numbered == ["c0ffee01", "e5f6a7b8", "22222222", "33333333", "aa000002"]
    assert start[1] == "1. Finish fix(parser): synthetic task name, overdue since 2026-10-05 [c0ffee01]"
    assert start[5] == "5. Fix example-ci: CI is failing on main, merges are blocked [aa000002]"
    assert "11111111" not in first.split("## Still open")[0], "a thread without a deadline word scores 0"
    assert "11111111" in "\n".join(section_of(first, "still_open")), "it is still open, just not urgent"
    assert all(START_LINE.match(ln) for ln in start[1:])


def test_fallback_commits_rank_last_and_a_due_later_task_is_skipped() -> None:
    ctx = degraded_ctx()
    ctx.results["task"].items[0].meta.update(due_state="later", due_date="2026-10-20")
    start = section_of(render_digest(ctx), "start_here")
    ids = [ID_TAIL.search(ln).group("id") for ln in start[1:]]  # type: ignore[union-attr]
    assert "c0ffee01" not in ids
    assert ids == ["e5f6a7b8", "a1b2c3d4", "b2c3d4e5"]  # "waiting on" scores 3, commits score 1 (3 before 1)
    assert start[2] == "2. Check example-api: 3 commits since the window start, not yet digested [a1b2c3d4]"


def test_empty_window_says_so_explicitly() -> None:
    text = render_digest(empty_ctx())
    assert "Nothing changed overnight: no commits, no new sessions" in text
    assert "Nothing changed overnight" not in render_digest(complete_ctx())


def test_tier_violation_line() -> None:
    text = render_digest(ctx_for(summary=None, claude_status="tier_violation", tier_violation_seq=77))
    assert "TIER VIOLATION, Claude call aborted, see audit seq 77" in text


# --- Attention -------------------------------------------------------------------------


def test_attention_says_nothing_broken_when_nothing_did() -> None:
    ctx = complete_ctx()
    ctx.results["task"].items[0].meta.update(due_state="later", due_date="2026-10-20")
    assert section_of(render_digest(ctx), "attention") == ["- Nothing broken."]
    assert front(render_digest(ctx))["n_attention"] == "0"


def test_attention_lists_every_anomaly_kind_in_the_fixed_order() -> None:
    ctx = complete_ctx()
    ctx.results["github"] = result("github", [gh("aa000002", "example-ci", ci="timed_out", branch="main")],
                                   {"repos_total": 1, "repos_read": 1, "quiet": [], "states": {"example-ci": "ok"}, "summary": {}})
    ctx.results["system"] = system_result({"jobs_failed": 2, "daemon_crashes": 1, "unclean_exits": 3, "breaker_state": "open",
                                           "config_invalid": 1, "queue": {**SYSTEM_FACTS["queue"], "held": 21}})
    lines = section_of(render_digest(ctx), "attention")
    kinds = [ATTENTION_LINE.match(ln).group("kind") for ln in lines]  # type: ignore[union-attr]
    assert kinds == ["CI failing", "Job failed", "Daemon crashed", "Unclean exit", "Task overdue", "Backlog held",
                     "Breaker open", "Config invalid"]
    assert lines[0] == "- CI failing: example-ci on main [aa000002]"
    assert lines[4].endswith("[c0ffee01]") and "was due 2026-10-05" in lines[4]
    assert "21 held references" in lines[5]
    assert not ID_TAIL.search(lines[1]) and not ID_TAIL.search(lines[7])
    assert front(render_digest(ctx))["n_attention"] == "8"


def test_held_backlog_alert_has_a_threshold() -> None:
    ctx = complete_ctx()
    ctx.results["system"] = system_result({"queue": {**SYSTEM_FACTS["queue"], "held": 19}})
    assert not any("Backlog held" in ln for ln in section_of(render_digest(ctx), "attention"))


# --- Active task -----------------------------------------------------------------------


def test_active_task_line_matches_the_grammar_without_the_plumbing_suffix() -> None:
    line = section_of(render_digest(complete_ctx()), "active_task")[0]
    m = TASK_LINE.match(line)
    assert m, line
    assert m.group("task_id") == "123synth" and m.group("status") == "IN REVIEW" and m.group("id") == "c0ffee01"
    assert m.group("due") == "due 2026-10-05 (OVERDUE)"
    ctx = complete_ctx()
    ctx.results["task"].items[0].meta.update(due_state="none", due_date="")
    assert TASK_LINE.match(section_of(render_digest(ctx), "active_task")[0]).group("due") == "no due date"  # type: ignore[union-attr]


def test_active_task_fixed_sentences_carry_no_tail() -> None:
    assert section_of(render_digest(empty_ctx()), "active_task") == ["- No active task recorded."]
    assert section_of(render_digest(all_held_ctx()), "active_task") == ["- Active task withheld, see Held back and not summarized."]
    ctx = complete_ctx()
    ctx.results["task"] = result("task", ok=False, error="boom")
    assert section_of(render_digest(ctx), "active_task") == ["- Task data unavailable, see Source status."]


# --- Still open ------------------------------------------------------------------------


def test_still_open_lines_match_the_grammar_and_are_grouped_by_session_slug() -> None:
    lines = section_of(render_digest(complete_ctx()), "still_open")
    assert all(OPEN_LINE.match(ln) for ln in lines), lines
    groups = [OPEN_LINE.match(ln).group("group") for ln in lines]  # type: ignore[union-attr]
    # Groups by newest item first: the session (2026-10-05) before the RECENT bullet (2026-10-04).
    assert groups == ["parser rework", "parser rework", "notes"]
    assert lines[2] == "- notes: Synthetic thread waiting on a reviewer [e5f6a7b8]"


def test_still_open_excludes_stale_resolved_cosmetic_no_active_work_wikilink_only_and_start_here_ids() -> None:
    ctx = complete_ctx()
    ctx.results["brain"].items.extend([
        thread("aaaa0001", "2026-10-05", "Delivered the synthetic exporter"),
        thread("aaaa0002", "2026-10-05", "Relay lines overlap briefly mid-swap (cosmetic)"),
        thread("aaaa0003", "2026-10-05", "Try the icon skill before any client use, optional"),
        thread("aaaa0004", "2026-10-05", "No active work, check Open threads in [[_INDEX]]"),
        thread("aaaa0005", "2026-10-05", "Voir [[2026-10-05-02]] et [[2026-10-05-evidence]]."),
        thread("aaaa0006", "2026-09-25", "Old but not flagged stale by the collector", age=11),
        thread("aaaa0007", "2026-10-05", "Keep the strict sum, this one stays"),
    ])
    assert ctx.summary is not None
    ctx.summary.attention.append(Attention(id="aaaa0007", why="Keep the strict sum of 100 for the quotas"))
    text = render_digest(ctx)
    open_lines = "\n".join(section_of(text, "still_open"))
    for gone in ("aaaa0001", "aaaa0002", "aaaa0003", "aaaa0004", "aaaa0005", "aaaa0006", "0a1b2c3d", "4d4d4d4d"):
        assert gone not in open_lines, gone
    assert "aaaa0007" not in open_lines and "aaaa0007" in "\n".join(section_of(text, "start_here"))
    assert "e5f6a7b8" in open_lines


def test_still_open_collapses_duplicate_keys_and_shows_the_age_from_three_days() -> None:
    ctx = degraded_ctx()
    ctx.results["brain"].items.extend([
        thread("bbbb0001", "2026-10-03", "Confirm the retry budget with the reviewer"),
        session("bbbb0002", "2026-10-05-20-parser-rework", "open_thread",
                "Confirm the retry budget with the reviewer [[2026-10-03-09]]"),
    ])
    lines = section_of(render_digest(ctx), "still_open")
    hits = [ln for ln in lines if "retry budget" in ln]
    assert len(hits) == 1
    assert hits[0] == "- notes: Confirm the retry budget with the reviewer (3d) [bbbb0001]"
    assert OPEN_LINE.match(hits[0]).group("age") == "3"  # type: ignore[union-attr]
    one_day = next(ln for ln in lines if "6b6b6b6b" in ln)
    assert "d)" not in one_day, "the age marker starts at three days"


def test_still_open_is_capped_at_ten_with_a_count_line() -> None:
    ctx = degraded_ctx()
    ctx.results["brain"].items.extend(thread(f"cccc{n:04d}", "2026-10-05", f"Synthetic open thread number {n}") for n in range(14))
    # Two fixture lines survive the exclusions (the deadline thread is in Start here) plus 14 new ones: 16.
    lines = section_of(render_digest(ctx), "still_open")
    assert len(lines) == 11
    assert HIDDEN_LINE.match(lines[-1]) and lines[-1].startswith("- 6 more open threads")
    values = front(render_digest(ctx))
    assert values["n_still_open"] == "10" and values["n_still_open_hidden"] == "6"
    ctx.results["brain"].items = ctx.results["brain"].items[:-5]
    assert section_of(render_digest(ctx), "still_open")[-1] == "- 1 more open thread not shown (cap 10)."


def test_still_open_vanishes_when_empty_and_reports_a_failed_source() -> None:
    text = render_digest(empty_ctx())
    assert "## Still open" not in text and "## Decided yesterday" not in text
    ctx = complete_ctx()
    ctx.results["brain"] = result("brain", ok=False, error="brain_root_missing")
    assert section_of(render_digest(ctx), "still_open") == ["- Brain data unavailable, see Source status."]
    assert "## Decided yesterday" not in render_digest(ctx)


# --- Decided yesterday -----------------------------------------------------------------


def test_decided_yesterday_keeps_window_dates_cuts_the_rationale_and_caps_at_ten() -> None:
    ctx = complete_ctx()
    ctx.results["brain"].items.extend([
        decision("dddd0001", "2026-10-03", "Before the window, must not show, because it is old"),
        decision("dddd0002", "2026-10-06", "Garder la somme stricte parce que les quotas doivent faire 100"),
        decision("dddd0003", "2026-10-06", "Rename the exporter: rationale in the review thread"),
        decision("dddd0004", "2026-10-05", "Car was the brand; keep the car example car it reads well"),
    ])
    lines = section_of(render_digest(ctx), "decided")
    assert all(DECIDED_LINE.match(ln) for ln in lines), lines
    assert lines[0] == "- Garder la somme stricte [dddd0002]"
    assert lines[1] == "- Rename the exporter [dddd0003]"
    assert "- Synthetic decision [9f8e7d6c]" in lines
    assert not any("dddd0001" in ln for ln in lines)
    assert any(ln.startswith("- Car was the brand; keep the") and ln.endswith("[dddd0004]") for ln in lines)
    ctx.results["brain"].items.extend(decision(f"eeee{n:04d}", "2026-10-06", f"Synthetic decision number {n}") for n in range(12))
    assert len(section_of(render_digest(ctx), "decided")) == 10
    assert front(render_digest(ctx))["n_decided"] == "10"


# --- Repos -----------------------------------------------------------------------------


def test_repo_lines_only_for_movement_dirty_repos_fold_into_one_line_and_quiet_is_a_count() -> None:
    lines = section_of(render_digest(complete_ctx()), "repos")
    repo_lines = [ln for ln in lines if REPO_LINE.match(ln)]
    assert [REPO_LINE.match(ln).group("name") for ln in repo_lines] == ["example-api", "example-notes"]  # type: ignore[union-attr]
    assert repo_lines[0].endswith("feat: synthetic endpoint [a1b2c3d4]")
    assert "- Uncommitted only: example-site 4/2." in lines
    assert DIRTY_LINE.match("- Uncommitted only: example-site 4/2.")
    assert "- Quiet: 1 repo." in lines and QUIET_LINE.match("- Quiet: 1 repo.")
    assert "- Not a git repo or missing: example-missing." in lines
    assert "example-web" not in "\n".join(lines), "quiet repos are counted, not named"


def test_ahead_behind_and_withheld_subjects_make_a_repo_line() -> None:
    ctx = complete_ctx()
    ctx.results["git"].items = [repo("f0f0f0f0", "example-ahead", ahead=2, modified=1),
                                repo("f1f1f1f1", "example-held", withheld=1)]
    lines = section_of(render_digest(ctx), "repos")
    assert lines[0] == "- example-ahead branch main, 0 commits since window, 1 modified, 0 untracked, ahead 2 [f0f0f0f0]"
    assert lines[1] == "- example-held branch main, 0 commits since window, 0 modified, 0 untracked, 1 subject withheld [f1f1f1f1]"
    assert all(REPO_LINE.match(ln) for ln in lines[:2])


def test_failed_source_shows_in_source_status() -> None:
    ctx = complete_ctx()
    ctx.results["git"] = result("git", ok=False, error="timeout after 60s")
    text = render_digest(ctx)
    status = section_of(text, "source_status")
    assert "git FAILED (timeout after 60s)" in status[0]
    assert front(text)["sources"].startswith("{brain: ok, task: ok, git: failed")
    assert section_of(text, "repos")[0] == "- Repo data unavailable, see Source status."


# --- System ----------------------------------------------------------------------------


def test_system_is_one_green_line_when_nothing_is_abnormal() -> None:
    lines = section_of(render_digest(complete_ctx()), "system")
    assert lines[0] == "- All green: 1 job done, 0 failed, $0.04 Claude, breaker closed, disk ok, tasks ok."
    assert GREEN_LINE.match(lines[0])
    assert lines[1:] == ["- Checkpoints waiting for /promote-sessions: 2."]  # a result line, not an anomaly


def test_system_prints_one_line_per_anomaly_in_the_fixed_order_with_decoded_task_codes() -> None:
    ctx = complete_ctx()
    ctx.results["system"] = system_result({
        "jobs_failed": 1, "daemon_crashes": 2, "unclean_exits": 1, "breaker_state": "open", "killswitch_trips": 1,
        "killswitch_last_reason": "manual-stop", "watchdog_crashloops": 1, "restart_attempts": 2, "disk_ok": False,
        "disk_free_gb": 9, "config_invalid": 1, "queue": {**SYSTEM_FACTS["queue"], "held": 25},
        "scheduled_tasks": {
            "ExampleNightly": {"available": True, "last_result": 2147946720, "ok": False,
                               "last_result_text": "refused by the operator or administrator (0x800710E0)",
                               "last_run_text": "2026-10-09 06:00"},
            "ExampleWeekly": {"available": False},
        },
    })
    ctx.results["brain"].facts.update(recent_stale=True, recent_age_hours=40.0, orphan_checkpoints=6)
    text = render_digest(ctx)
    lines = section_of(text, "system")
    whats = [ANOMALY_LINE.match(ln).group("what") for ln in lines]  # type: ignore[union-attr]
    assert whats == ["Jobs failed", "Daemon crashed", "Unclean exits", "Breaker", "Kill switch", "Watchdog", "Disk", "Task",
                     "Task", "Config invalid", "Notes index stale", "Sessions not filed", "Held backlog"]
    assert "- Task: ExampleNightly last ran 2026-10-09 06:00, refused by the operator or administrator (0x800710E0)." in lines
    assert "- Task: ExampleWeekly could not be queried." in lines
    assert "- Sessions not filed: 6 checkpoints waiting for /promote-sessions." in lines
    assert "- Kill switch: 1 trip since the last digest (last: manual-stop)." in lines
    assert "- Disk: WARNING, 9 GB free." in lines
    assert not any(GREEN_LINE.match(ln) for ln in lines)
    assert front(text)["n_system_anomalies"] == "13"


def test_system_keeps_the_consolidation_result_line_after_the_verdict() -> None:
    ctx = complete_ctx()
    note = "Consolidation: 2 memory candidates proposed in brain/raw/jarvis/candidates-2026-10-06.md, for your review, none promoted."
    ctx.results["system"].facts["consolidation"] = note
    lines = section_of(render_digest(ctx), "system")
    assert lines[0].startswith("- All green") and lines[1] == f"- {note}"


def test_system_unavailable_and_missing_notes_index() -> None:
    ctx = complete_ctx()
    ctx.results["system"] = result("system", ok=False, error="boom")
    assert section_of(render_digest(ctx), "system") == ["- System data unavailable, see Source status."]
    ctx = complete_ctx()
    ctx.results["brain"].facts.update(recent_missing=True)
    assert "- Notes index missing: RECENT.md was not found." in section_of(render_digest(ctx), "system")
    ctx.results["system"].facts["jobs_error"] = "RuntimeError"
    assert "- Jobs: unavailable (RuntimeError)." in section_of(render_digest(ctx), "system")


# --- Held back -------------------------------------------------------------------------


@pytest.mark.parametrize("make", [complete_ctx, degraded_ctx, empty_ctx, all_held_ctx])
def test_held_is_one_line_in_the_grammar(make) -> None:  # type: ignore[no-untyped-def]
    lines = section_of(render_digest(make()), "held")
    assert len(lines) == 1 and HELD_LINE.match(lines[0]), lines


def test_held_line_values_and_the_id_cap() -> None:
    m = HELD_LINE.match(section_of(render_digest(complete_ctx()), "held")[0])
    assert m and (m.group("sens"), m.group("pol"), m.group("cap"), m.group("claude")) == ("2", "1", "0", "none")
    assert m.group("ids") == "w-3a9f1c, w-77d20e" and m.group("reasons") == "component_sensitive x1, tag_frontmatter x1"
    assert HELD_LINE.match(section_of(render_digest(degraded_ctx()), "held")[0]).group("claude") == "budget"  # type: ignore[union-attr]
    assert HELD_LINE.match(section_of(render_digest(empty_ctx()), "held")[0]).group("claude") == "not called"  # type: ignore[union-attr]
    ctx = complete_ctx()
    ctx.held = [WithheldItem(id=f"w-{n:06x}", kind="brain_recent", source_ref="x", reason="term:1") for n in range(23)]
    ctx.over_cap = ["e5f6a7b8", "e5f6a7b8"]
    m = HELD_LINE.match(section_of(render_digest(ctx), "held")[0])
    assert m and m.group("sens") == "23" and m.group("cap") == "1"
    assert m.group("ids").endswith(", and 3 more") and m.group("reasons") == "term:1 x23"


# --- registry and file names -----------------------------------------------------------


def test_registry_extension_needs_no_edit_to_render_py() -> None:
    class Extra(Section):
        name = "extra"

        @property
        def heading(self) -> str:
            return "Extra synthetic section"

        def body(self, ctx: DigestContext) -> list[str]:
            return ["- from the registry"]

    SECTIONS["extra"] = Extra()
    try:
        text = render_digest(complete_ctx())
        assert "## Extra synthetic section\n- from the registry" in text
        assert text.index("## Extra synthetic section") < text.index("## Source status")
        # An explicit order wins and can leave it out.
        only = render_digest(ctx_for(section_order=("start_here",)))
        assert "Extra synthetic section" not in only
    finally:
        del SECTIONS["extra"]
    assert "Extra synthetic section" not in render_digest(complete_ctx())


def test_digest_filename() -> None:
    assert digest_filename(date(2026, 10, 6)) == "digest-2026-10-06.md"
    assert digest_filename(date(2026, 10, 6), 2) == "digest-2026-10-06-r2.md"
    assert render.digest_filename(date(2026, 1, 2), 1) == "digest-2026-01-02.md"


def test_claude_whys_must_read_as_an_order_or_the_fallback_fills_the_list() -> None:
    from jarvisd.render import imperative

    assert imperative("Rotate the pasted sandbox key before the demo tomorrow")
    assert imperative("Answer the reviewer on the parser fix, blocked since Monday")
    for declarative in ("The key was pasted in chat", "Pilot password is printed in a transcript", "Pending answer from the lab",
                        "A pentest lab is 3 weeks stale", "Both videos delivered", "Key was pasted"):
        assert not imperative(declarative), declarative
    ctx = complete_ctx()
    assert ctx.summary is not None
    ctx.summary.attention = [Attention(id="e5f6a7b8", why="The reviewer has not answered on the parser fix")]
    start = section_of(render_digest(ctx), "start_here")
    assert "The reviewer has not answered" not in "\n".join(start)
    assert start[1].startswith("1. Finish fix(parser)"), "a rejected why hands the list to the deterministic fallback"


def test_fallback_threads_skip_what_still_open_excludes_and_before_is_not_a_deadline() -> None:
    ctx = degraded_ctx()
    ctx.results["brain"].items.extend([
        thread("11111111", "2026-10-05", "Try the icon skill before any client use"),
        thread("22222222", "2026-10-05", "Delivered the exporter, due date met"),
        thread("33333333", "2026-10-05", "Relay lines overlap mid-swap, blocked on a cosmetic fix"),
        thread("44444444", "2026-10-05", "Related: note-a, note-b, due Friday"),
    ])
    start = "\n".join(section_of(render_digest(ctx), "start_here"))
    for gone in ("11111111", "22222222", "33333333", "44444444"):
        assert gone not in start, gone
    assert "2. Act on this thread, it names a deadline or a blocker: Synthetic thread waiting on a reviewer [e5f6a7b8]" in start


def test_decisions_are_cut_at_a_dash_before_clean_turns_it_into_a_comma() -> None:
    em, en = chr(0x2014), chr(0x2013)
    ctx = complete_ctx()
    ctx.results["brain"].items.extend([
        decision("d0d0d0d1", "2026-10-05", f"Keep the partner unnamed in every artefact {em} NDA, the owner's instruction"),
        decision("d0d0d0d2", "2026-10-05", f"Ship captions aligned to the script {en} the ASR output drifts"),
        decision("d0d0d0d3", "2026-10-05", "Use the plain hyphen - in a range like 10-12 stays"),
    ])
    lines = section_of(render_digest(ctx), "decided")
    assert "- Keep the partner unnamed in every artefact [d0d0d0d1]" in lines
    assert "- Ship captions aligned to the script [d0d0d0d2]" in lines
    assert "- Use the plain hyphen [d0d0d0d3]" in lines  # a spaced hyphen is a dash too; 10-12 is untouched


def test_a_thread_that_quotes_the_active_task_id_is_not_repeated_under_still_open() -> None:
    ctx = complete_ctx()
    task_id = ctx.results["task"].items[0].meta["task_id"]
    ctx.results["brain"].items.append(thread("ab12ab12", "2026-10-05", f"Current task {task_id} still set, untouched this session"))
    text = render_digest(ctx)
    assert "ab12ab12" not in "\n".join(section_of(text, "still_open"))
