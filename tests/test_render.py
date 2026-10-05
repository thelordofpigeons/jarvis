"""Deterministic digest renderer (design section 9): goldens plus the rules around them.

The contexts are hand built from synthetic items. Goldens live in tests/golden and were
read by eye when they were first generated; a diff here is a layout change.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import CANARY
from jarvisd import render
from jarvisd.models import Attention, CollectResult, DigestSummary, Item, WithheldItem
from jarvisd.render import DEFAULT_SECTIONS, SECTIONS, DigestContext, Section, digest_filename, render_digest

GOLDEN = Path(__file__).parent / "golden"
TZ = timezone(timedelta(hours=1))
EM = chr(0x2014)
EN = chr(0x2013)
HEAD = "ab" * 32

FRONTMATTER_KEYS = [
    "type", "generator", "generator_version", "job_id", "date", "generated_at", "window_start",
    "window_end", "status", "late", "claude", "local_tier", "degraded", "cost_usd", "items",
    "sources", "audit_seq", "audit_head", "tags",
]


# --- builders --------------------------------------------------------------------------


def thread(item_id: str, day: str, text: str, *, stale: bool = False) -> Item:
    return Item(id=item_id, source="brain", kind="brain_thread", title=text[:80], text=text,
                tags=["stale"] if stale else [], meta={"date": day, "stale": stale, "section": "thread"})


def decision(item_id: str, day: str, text: str) -> Item:
    return Item(id=item_id, source="brain", kind="brain_decision", title=text[:80], text=text,
                meta={"date": day, "stale": False, "section": "decision"})


def session(item_id: str, name: str, section: str, text: str) -> Item:
    return Item(id=item_id, source="brain", kind="brain_session", title=name, text=text,
                meta={"session": name, "section": section})


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


def system_line(item_id: str, text: str) -> Item:
    return Item(id=item_id, source="system", kind="system_line", title=text, meta={"render": "deterministic"})


def result(source: str, items: list[Item] | None = None, facts: dict | None = None, *, ok: bool = True,
           error: str | None = None) -> CollectResult:
    return CollectResult(source=source, ok=ok, error=error, items=items or [], facts=facts or {})


SYSTEM_LINES = [
    "Jobs: 1 done, 0 failed. Claude calls: 1 ($0.04, 3.2k tokens). Vault writes: 1. Breaker: closed.",
    "Daemon starts since last digest: 2. Unclean exits: 0.",
    "ExampleNightly: last run 2026-10-06 02:30, result 0.",
    "Kill switch trips: 0. Watchdog crashloops: 0.",
]

BRAIN_FACTS = {"recent_age_hours": 4.1, "recent_stale": False, "orphan_checkpoints": 2, "new_sessions": 1,
               "withheld_count": 0}
GIT_FACTS = {"repos_total": 4, "quiet": ["example-web"], "not_repos": ["example-missing"], "errors": []}


def complete_results() -> dict[str, CollectResult]:
    return {
        "brain": result("brain", [
            thread("e5f6a7b8", "2026-10-04", "Synthetic thread waiting on a reviewer"),
            thread("0a1b2c3d", "2026-09-20", "Synthetic stale thread", stale=True),
            decision("9f8e7d6c", "2026-10-03", "Synthetic decision, because it is a fixture"),
            session("5a5a5a5a", "2026-10-05-20", "entry_point", "Continue at fixture.py:10, wire the synthetic thing"),
            session("6b6b6b6b", "2026-10-05-20", "open_thread", "Synthetic follow up one"),
        ], BRAIN_FACTS),
        "task": result("task", [task()], {"task_active": True}),
        "git": result("git", [
            repo("a1b2c3d4", "example-api", branch="test", commits=3, modified=2, untracked=1, work=True,
                 subjects=("fix: synthetic parser bug", "feat: synthetic endpoint")),
            repo("b2c3d4e5", "example-notes", counts_only=True, commits=1, modified=0, untracked=0),
        ], GIT_FACTS),
        "system": result("system", [system_line(f"s{i}", line) for i, line in enumerate(SYSTEM_LINES)]),
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
            Attention(id="c0ffee01", why="Review task is overdue since yesterday"),
            Attention(id="b2c3d4e5", why="One commit landed in the notes repo"),
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
        "system": result("system", [system_line("s0", SYSTEM_LINES[0]), system_line("s1", SYSTEM_LINES[1])]),
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


# --- goldens ---------------------------------------------------------------------------


def test_golden_complete() -> None:
    assert render_digest(complete_ctx()) == read_golden("digest_complete")


def test_golden_degraded() -> None:
    assert render_digest(degraded_ctx()) == read_golden("digest_degraded")


def test_golden_empty() -> None:
    assert render_digest(empty_ctx()) == read_golden("digest_empty")


def test_golden_all_held() -> None:
    assert render_digest(all_held_ctx()) == read_golden("digest_all_held")


# --- layout rules ----------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[list[str], str]:
    assert text.startswith("---\n")
    head, _, body = text[4:].partition("\n---\n")
    return head.split("\n"), body


@pytest.mark.parametrize("make", [complete_ctx, degraded_ctx, empty_ctx, all_held_ctx])
def test_frontmatter_keys_as_designed(make) -> None:  # type: ignore[no-untyped-def]
    text = render_digest(make())
    lines, _ = split_frontmatter(text)
    assert [ln.split(":", 1)[0] for ln in lines] == FRONTMATTER_KEYS
    assert "generator: jarvisd" in text[:400].split("\n")
    values = dict(ln.split(": ", 1) for ln in lines)
    assert values["type"] == "jarvis-digest"
    assert values["tags"] == "[jarvis, digest]"
    assert values["audit_head"] == HEAD and values["audit_seq"] == "812"
    assert re.fullmatch(r"\{brain: \w+, task: \w+, git: \w+, system: \w+, clickup: disabled, github: not_collected\}",
                        values["sources"])


def test_frontmatter_counts_for_complete() -> None:
    values = dict(ln.split(": ", 1) for ln in split_frontmatter(render_digest(complete_ctx()))[0])
    assert values["items"] == "{collected: 10, cleared: 7, held_sensitive: 2, held_policy: 1, over_cap: 0}"
    assert values["cost_usd"] == "0.0412"
    assert values["late"] == "false" and values["status"] == "complete" and values["claude"] == "ok"


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


def test_dashes_and_pipes_in_claude_and_collector_strings_are_cleaned() -> None:
    ctx = complete_ctx()
    ctx.summary = DigestSummary(
        headline=f"Quiet {EM} mostly",
        attention=[Attention(id="c0ffee01", why=f"a {EM} b {EN} c")],
        summaries={"e5f6a7b8": f"one {EM} liner | pipe"},
    )
    ctx.results["brain"].items[0].text = "thread with | pipe\nand a newline"
    text = render_digest(ctx)
    assert EM not in text and EN not in text and "|" not in text
    assert "thread with / pipe and a newline" in text


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
    assert "1d1d1d1d" in text.split("## Held back and not summarized")[1]
    assert "Sensitive, never read or sent: 3 items" in text
    assert "w-3a9f1c" in text and "w-77d20e" in text
    body_before_held = text.split("## Held back and not summarized")[0]
    assert "1d1d1d1d" not in body_before_held


def test_policy_held_items_render_without_a_claude_summary() -> None:
    ctx = complete_ctx()
    assert ctx.summary is not None
    ctx.summary.summaries["a1b2c3d4"] = "should never be shown"
    text = render_digest(ctx)
    assert "should never be shown" not in text
    repos = text.split("## Repos")[1].split("## ")[0]
    assert "example-api (work)" in repos and "[a1b2c3d4]" in repos
    assert "Policy (work metadata to Claude disabled): 1 item" in text


def test_attention_with_unknown_or_held_ids_is_dropped() -> None:
    ctx = complete_ctx()
    assert ctx.summary is not None
    ctx.summary.attention.append(Attention(id="w-3a9f1c", why="held id"))
    text = render_digest(ctx)
    start = text.split("## Start here")[1].split("## ")[0]
    assert "deadbeef" not in start and "w-3a9f1c" not in start
    assert "1. [c0ffee01] Review task is overdue since yesterday" in start
    assert "2. [b2c3d4e5] One commit landed in the notes repo" in start
    assert "a1b2c3d4" not in start  # policy-held: Claude never saw it, so it cannot cite it


def test_start_here_fallback_order_is_deterministic() -> None:
    first = render_digest(degraded_ctx())
    again = render_digest(degraded_ctx())
    assert first == again
    start = first.split("## Start here")[1].split("## ")[0]
    assert "Claude summary unavailable (budget)" in start
    numbered = re.findall(r"(?m)^\d\. \[(\w+)\]", start)
    # overdue task, then repos with commits, then newest open threads
    assert numbered[:3] == ["c0ffee01", "a1b2c3d4", "b2c3d4e5"]
    assert numbered[3] == "e5f6a7b8"
    assert len(numbered) <= 5


def test_fallback_skips_a_due_later_task() -> None:
    ctx = degraded_ctx()
    ctx.results["task"].items[0].meta.update(due_state="later", due_date="2026-10-20")
    start = render_digest(ctx).split("## Start here")[1].split("## ")[0]
    assert "c0ffee01" not in start


def test_empty_window_says_so_explicitly() -> None:
    text = render_digest(empty_ctx())
    assert "Nothing changed overnight: no commits, no new sessions" in text
    assert "Nothing changed overnight" not in render_digest(complete_ctx())


def test_tier_violation_line() -> None:
    text = render_digest(ctx_for(summary=None, claude_status="tier_violation", tier_violation_seq=77))
    assert "TIER VIOLATION, Claude call aborted, see audit seq 77" in text


def test_failed_source_shows_in_source_status() -> None:
    ctx = complete_ctx()
    ctx.results["git"] = result("git", ok=False, error="timeout after 60s")
    text = render_digest(ctx)
    status = text.split("## Source status")[1].split("## ")[0]
    assert "git FAILED (timeout after 60s)" in status
    assert dict(ln.split(": ", 1) for ln in split_frontmatter(text)[0])["sources"].startswith("{brain: ok, task: ok, git: failed")


def test_registry_extension_needs_no_edit_to_render_py() -> None:
    class Extra(Section):
        name = "extra"
        heading = "Extra synthetic section"

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


def test_default_section_order_covers_the_design() -> None:
    assert list(DEFAULT_SECTIONS) == [
        "start_here", "active_task", "brain", "repos", "system", "held", "source_status", "flag_mistake",
    ]
    assert set(DEFAULT_SECTIONS) <= set(SECTIONS)


def test_digest_filename() -> None:
    assert digest_filename(date(2026, 10, 6)) == "digest-2026-10-06.md"
    assert digest_filename(date(2026, 10, 6), 2) == "digest-2026-10-06-r2.md"
    assert render.digest_filename(date(2026, 1, 2), 1) == "digest-2026-01-02.md"
