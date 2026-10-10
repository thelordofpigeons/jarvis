"""Aging, the item sidecar, the "since yesterday" counts and the weekly note (contract sections 6 and 9).

The contexts come from tests/test_render.py; the history and the attention decisions are hand built
dicts in the shape the writer stores. Every name is synthetic.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

import pytest

from conftest import CANARY
from jarvisd import render
from jarvisd.common import norm_key
from jarvisd.models import Attention, Item, WithheldItem
from jarvisd.render import (
    SIDECAR_SECTIONS,
    WEEKLY_HEADINGS,
    Aging,
    WeeklyContext,
    aging_from,
    render_digest,
    render_weekly,
    sidecar_items,
    since_counts,
    weekly_filename,
)
from jarvisd.state import update_history
from test_render import OPEN_LINE, TZ, all_held_ctx, complete_ctx, degraded_ctx, front, section_of, thread

TODAY = date(2026, 10, 6)
THREAD_KEY = norm_key("Synthetic thread waiting on a reviewer")
TASK_KEY = norm_key("fix(parser): synthetic task name")
DECISION_KEY = norm_key("Synthetic decision, because it is a fixture")

# The weekly grammar, as the contract states it (section 9).
WEEKLY_LINES = {
    "runs": re.compile(r"^- (?P<runs>\d+) runs?, (?P<failed>\d+) failed, \$(?P<usd>\d+\.\d{2}) Claude\.$"),
    "decided": re.compile(r"^- (?P<date>\d{4}-\d{2}-\d{2}): (?P<text>[^\[]{3,200}) \[(?P<id>[0-9a-f]{8})\]$"),
    "dropped": re.compile(r"^- (?P<text>[^\[]{3,200}) \((?P<first>\d{4}-\d{2}-\d{2}) to (?P<last>\d{4}-\d{2}-\d{2})\) "
                          r"\[(?P<id>[0-9a-f]{8})\]$"),
    "snoozed": re.compile(r"^- (?P<action>Done|Snoozed until \d{4}-\d{2}-\d{2}) (?P<date>\d{4}-\d{2}-\d{2}): "
                          r"(?P<text>[^\[]{3,200}) \[(?P<id>[0-9a-f]{8})\]$"),
    "flagged": re.compile(r"^- (?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}): (?P<id>[0-9a-f]{8}|w-[0-9a-f]{6}), "
                          r"should (?P<should>escalate|hold|skip|other)(?P<leak>, leak)?\.$"),
    "cost": re.compile(r"^- (?P<date>\d{4}-\d{2}-\d{2}): (?P<runs>\d+) runs?, \$(?P<usd>\d+\.\d{2})\.$"),
}


def record(status: str = "open", sections: list[str] | None = None, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {"id": "e5f6a7b8", "text": "Synthetic thread waiting on a reviewer", "first_seen": "2026-10-04",
                               "last_seen": "2026-10-05", "times_shown": 1, "sections": sections or ["still_open"],
                               "status": status, "snoozed_until": None, "resolved_at": None}
    base.update(extra)
    return base


def history(**items: dict[str, object]) -> dict[str, object]:
    return {"updated": "2026-10-05", "last_run": "digest-2026-10-05", "items": dict(items)}


# --- the sidecar -----------------------------------------------------------------------


def test_sidecar_records_every_rendered_item_line_once_in_page_order() -> None:
    ctx = complete_ctx()
    text = render_digest(ctx)
    records = sidecar_items(ctx)
    assert [r["id"] for r in records] == ["c0ffee01", "b2c3d4e5", "5a5a5a5a", "6b6b6b6b", "e5f6a7b8", "9f8e7d6c", "a1b2c3d4"]
    assert [r["section"] for r in records] == ["start_here", "start_here", "still_open", "still_open", "still_open", "decided", "repos"]
    assert [r["rank"] for r in records] == [1, 2, 1, 2, 3, 1, 1]
    for r in records:
        assert set(r) == {"key", "id", "section", "group", "date", "text", "rank", "since"}
        assert r["section"] in SIDECAR_SECTIONS and r["since"] == "new" and "[" not in r["text"]
        assert f"{r['text']} [{r['id']}]" in text or r["section"] == "active_task", r
    start = records[0]
    # The task has no key of its own and its text is a status line, so the key is its title.
    assert start["key"] == TASK_KEY and start["text"] == "Chase the review today, the task is overdue since yesterday"
    still = records[2]
    assert still == {"key": norm_key("Continue at fixture.py:10, wire the synthetic thing"), "id": "5a5a5a5a",
                     "section": "still_open", "group": "parser rework", "date": "2026-10-05",
                     "text": "Continue at fixture.py:10, wire the synthetic thing", "rank": 1, "since": "new"}
    assert records[5]["key"] == DECISION_KEY and records[5]["text"] == "Synthetic decision"
    assert records[6]["key"] == "example api" and records[6]["group"] == "notes"
    # c0ffee01 is printed three times (Start here, Attention, Active task) and recorded once, Start here first.
    assert sum(1 for r in records if r["id"] == "c0ffee01") == 1


def test_sidecar_strips_the_group_prefix_and_the_age_from_still_open_lines() -> None:
    ctx = degraded_ctx()
    ctx.results["brain"].items.append(thread("bbbb0001", "2026-10-03", "Confirm the retry budget with the reviewer"))
    line = next(ln for ln in section_of(render_digest(ctx), "still_open") if "bbbb0001" in ln)
    assert line == "- notes: Confirm the retry budget with the reviewer (3d) [bbbb0001]"
    rec = next(r for r in sidecar_items(ctx) if r["id"] == "bbbb0001")
    assert rec["text"] == "Confirm the retry budget with the reviewer" and rec["group"] == "notes" and rec["date"] == "2026-10-03"


def test_a_sensitive_held_item_never_reaches_a_sidecar_record() -> None:
    ctx = complete_ctx()
    leaky = Item(id="1d1d1d1d", source="brain", kind="brain_thread", title=f"title {CANARY}", text=f"body {CANARY}",
                 meta={"date": "2026-10-05", "stale": False, "age_days": 1, "key": norm_key(f"body {CANARY}")})
    ctx.results["brain"].items.append(leaky)
    ctx.held.append(WithheldItem(id="1d1d1d1d", kind="brain_thread", source_ref=f"C:/{CANARY}/x.md", reason="term:3"))
    records = sidecar_items(ctx)
    assert "1d1d1d1d" not in {r["id"] for r in records}
    assert CANARY not in json.dumps(records)
    assert sidecar_items(all_held_ctx()) == []
    # The history built from these records cannot hold it either: there is nothing to apply.
    hist = history()
    update_history(hist, records, [], TODAY, "digest-2026-10-06")
    assert CANARY not in json.dumps(hist) and "1d1d1d1d" not in json.dumps(hist)


# --- aging rules -----------------------------------------------------------------------


def test_a_done_key_never_renders_in_any_section_and_leaves_no_record() -> None:
    ctx = complete_ctx()
    ctx.aging = aging_from(history(), [{"key": TASK_KEY, "id": "c0ffee01", "action": "done", "until": None},
                                      {"key": THREAD_KEY, "id": "e5f6a7b8", "action": "done", "until": None}], TODAY)
    text = render_digest(ctx)
    assert "c0ffee01" not in text and "e5f6a7b8" not in text
    assert section_of(text, "attention") == ["- Nothing broken."]
    assert section_of(text, "active_task") == ["- No active task recorded."]
    assert {r["id"] for r in sidecar_items(ctx)}.isdisjoint({"c0ffee01", "e5f6a7b8"})
    # Two clicks and one decision resolved this run; the decision is counted once it prints.
    assert front(text)["n_since_resolved"] == "3"
    # A done stays done whatever the text says: history alone, no attention file.
    ctx.aging = aging_from(history(**{THREAD_KEY: record("done", resolved_at="2026-10-01")}), [], TODAY)
    assert "e5f6a7b8" not in render_digest(ctx)


def test_a_snoozed_key_is_absent_until_its_date_and_returns_early_when_its_text_changes() -> None:
    snooze = [{"key": THREAD_KEY, "id": "e5f6a7b8", "action": "snooze", "until": "2026-10-08"}]
    ctx = degraded_ctx()  # the fallback would otherwise pick the "waiting on" thread for Start here
    ctx.aging = aging_from(history(), snooze, TODAY)
    text = render_digest(ctx)
    assert "e5f6a7b8" not in text, "snoozed: out of Start here, Still open and the fallback"
    assert "e5f6a7b8" not in {r["id"] for r in sidecar_items(ctx)}
    assert front(text)["n_since_returned"] == "0"
    # The date arrives: the writer's history says snoozed, the file says until today, so the key returns.
    ctx.aging = aging_from(history(**{THREAD_KEY: record("snoozed", snoozed_until="2026-10-08")}), snooze, date(2026, 10, 8))
    text = render_digest(ctx)
    assert "e5f6a7b8" in text
    rec = next(r for r in sidecar_items(ctx) if r["id"] == "e5f6a7b8")
    assert rec["since"] == "returned" and front(text)["n_since_returned"] == "1"
    # A changed text is a new key: it is back at once, as new, while the old key stays snoozed.
    ctx = degraded_ctx()
    ctx.results["brain"].items[0] = thread("e5f6a7b8", "2026-10-04", "Synthetic thread waiting on a reviewer, now due Friday")
    ctx.aging = aging_from(history(**{THREAD_KEY: record("snoozed", snoozed_until="2026-10-08")}), snooze, TODAY)
    text = render_digest(ctx)
    assert "e5f6a7b8" in text
    rec = next(r for r in sidecar_items(ctx) if r["id"] == "e5f6a7b8")
    assert rec["since"] == "new" and rec["key"] != THREAD_KEY


def test_a_thread_shown_twice_in_start_here_moves_to_still_open_with_its_age() -> None:
    ctx = complete_ctx()
    ctx.results["brain"].items.append(thread("bbbb0001", "2026-10-03", "Confirm the retry budget with the reviewer, blocked"))
    key = norm_key("Confirm the retry budget with the reviewer, blocked")
    assert ctx.summary is not None
    ctx.summary.attention = [Attention(id="bbbb0001", why="Confirm the retry budget with the reviewer, it blocks the release")]
    first = render_digest(ctx)
    assert section_of(first, "start_here")[1].endswith("[bbbb0001]")
    assert "bbbb0001" not in "\n".join(section_of(first, "still_open"))
    # Two mornings in Start here spend the budget (contract 6.1 rule 3): Claude's pick is dropped, the
    # fallback does not score the thread, and it is printed under Still open with its age.
    ctx.aging = aging_from(history(**{key: record(sections=["start_here", "start_here"], id="bbbb0001", last_seen="2026-10-05",
                                                 text="Confirm the retry budget with the reviewer, blocked")}), [], TODAY)
    second = render_digest(ctx)
    assert "bbbb0001" not in "\n".join(section_of(second, "start_here"))
    line = next(ln for ln in section_of(second, "still_open") if "bbbb0001" in ln)
    assert line == "- notes: Confirm the retry budget with the reviewer, blocked (3d) [bbbb0001]"
    assert OPEN_LINE.match(line).group("age") == "3"  # type: ignore[union-attr]
    # One morning does not: the budget is two.
    ctx.aging = aging_from(history(**{key: record(sections=["start_here"], id="bbbb0001")}), [], TODAY)
    assert section_of(render_digest(ctx), "start_here")[1].endswith("[bbbb0001]")
    # The fallback honours the same budget.
    ctx.summary = None
    ctx.aging = aging_from(history(**{key: record(sections=["start_here", "start_here"], id="bbbb0001")}), [], TODAY)
    assert "bbbb0001" not in "\n".join(section_of(render_digest(ctx), "start_here"))


def test_a_decision_never_renders_twice() -> None:
    ctx = complete_ctx()
    first = render_digest(ctx)
    assert section_of(first, "decided") == ["- Synthetic decision [9f8e7d6c]"]
    assert front(first)["n_since_resolved"] == "1", "a decision is resolved the morning it prints"
    hist = history()
    update_history(hist, sidecar_items(ctx), [], TODAY, "digest-2026-10-06")
    filed = hist["items"][DECISION_KEY]  # type: ignore[index]
    assert filed["status"] == "done" and filed["resolved_at"] == "2026-10-06" and filed["sections"] == ["decided"]
    # The forced rerun, and every later morning: the same decision item is collected again and never prints.
    ctx.aging = aging_from(hist, [], TODAY)
    second = render_digest(ctx)
    assert "## Decided yesterday" not in second and "9f8e7d6c" not in second
    values = front(second)
    assert values["n_decided"] == "0" and values["n_since_resolved"] == "0" and values["n_since_new"] == "0"


def test_since_counts_cover_new_resolved_dropped_and_returned() -> None:
    ctx = complete_ctx()
    hist = history(**{
        THREAD_KEY: record(),  # known: not new
        "an idle thread nobody collected": record("open", id="ab12ab12", text="An idle thread nobody collected", last_seen="2026-09-20"),
        "a dropped thread collected again": record("dropped", id="ab12ab13", last_seen="2026-09-20", resolved_at="2026-10-01"),
    })
    ctx.results["brain"].items.append(thread("ab12ab13", "2026-10-05", "A dropped thread collected again"))
    ctx.aging = aging_from(hist, [], TODAY)
    assert ctx.aging.dropping == {"an idle thread nobody collected"}
    records = sidecar_items(ctx)
    counts = since_counts(ctx, records)
    assert counts == {"n_since_new": 6, "n_since_resolved": 1, "n_since_dropped": 1, "n_since_returned": 0}
    assert front(render_digest(ctx))["n_since_dropped"] == "1"
    update_history(hist, records, [], TODAY, "digest-2026-10-06")
    items = hist["items"]  # type: ignore[assignment]
    assert items["an idle thread nobody collected"]["status"] == "dropped"  # type: ignore[index]
    assert items["a dropped thread collected again"]["status"] == "open"  # type: ignore[index]
    assert items["a dropped thread collected again"]["first_seen"] == "2026-10-04"  # type: ignore[index]


def test_aging_from_is_tolerant_of_a_malformed_history_and_a_done_beats_a_snooze() -> None:
    assert aging_from({}, [], TODAY) == Aging()
    assert aging_from({"items": "nonsense"}, [{"nope": 1}, "text"], TODAY) == Aging()
    a = aging_from(history(**{THREAD_KEY: record("snoozed", snoozed_until="2026-10-20")}),
                   [{"key": THREAD_KEY, "action": "done"}], TODAY)
    assert THREAD_KEY in a.done and THREAD_KEY not in a.snoozed and THREAD_KEY in a.resolved
    a = aging_from(history(**{THREAD_KEY: record("done")}), [{"key": THREAD_KEY, "action": "done"}], TODAY)
    assert a.resolved == frozenset(), "a done already filed is not resolved again"


# --- the weekly note -------------------------------------------------------------------


def weekly_ctx(**overrides: object) -> WeeklyContext:
    base: dict[str, object] = dict(
        week="2026-W41", start=date(2026, 10, 5), end=date(2026, 10, 11),
        generated_at=datetime(2026, 10, 12, 6, 31, 40, tzinfo=TZ),
        runs=[{"date": "2026-10-06", "status": "complete", "cost_usd": 0.0412},
              {"date": "2026-10-07", "status": "failed", "cost_usd": 0.0},
              {"date": "2026-10-07", "status": "complete", "cost_usd": 0.01}],
        decided=[{"date": "2026-10-06", "text": "Synthetic decision", "id": "9f8e7d6c"}],
        dropped=[{"text": "Synthetic stale thread", "first": "2026-09-20", "last": "2026-09-28", "id": "0a1b2c3d"}],
        attended=[{"action": "done", "until": None, "date": "2026-10-08", "text": "Synthetic thread waiting on a reviewer", "id": "e5f6a7b8"},
                  {"action": "snooze", "until": "2026-10-13", "date": "2026-10-09", "text": "Synthetic follow up one", "id": "6b6b6b6b"}],
        flagged=[{"ts": "2026-10-09 06:31", "id": "c0ffee01", "should": "hold", "leak": False},
                 {"ts": "2026-10-10 07:00", "id": "w-3a9f1c", "should": None, "leak": True}],
    )
    base.update(overrides)
    return WeeklyContext(**base)  # type: ignore[arg-type]


def weekly_section(text: str, key: str) -> list[str]:
    marker = f"## {WEEKLY_HEADINGS[key]}\n"
    assert marker in text, key
    body = text.split(marker, 1)[1].split("\n## ", 1)[0]
    return [ln for ln in body.split("\n") if ln]


def test_weekly_note_has_the_front_matter_the_title_and_the_six_sections_in_order() -> None:
    text = render_weekly(weekly_ctx())
    head, _, body = text[4:].partition("\n---\n")
    keys = [ln.split(":", 1)[0] for ln in head.split("\n")]
    assert keys == ["type", "generator", "generator_version", "week", "from", "to", "generated", "cost_usd", "n_runs",
                    "n_failed", "n_decided", "n_dropped", "n_done", "n_snoozed", "n_flagged", "tags"]
    values = dict(ln.split(": ", 1) for ln in head.split("\n"))
    assert values["type"] == "jarvis-weekly" and values["generator"] == "jarvisd" and values["week"] == "2026-W41"
    assert values["from"] == "2026-10-05" and values["to"] == "2026-10-11" and values["cost_usd"] == "0.0512"
    assert (values["n_runs"], values["n_failed"], values["n_decided"], values["n_dropped"]) == ("3", "1", "1", "1")
    assert (values["n_done"], values["n_snoozed"], values["n_flagged"]) == ("1", "1", "2")
    assert body.startswith("# Week 2026-W41\n\n## Runs\n")
    assert [ln[3:] for ln in text.split("\n") if ln.startswith("## ")] == list(WEEKLY_HEADINGS.values())
    assert "generator: jarvisd" in text[:400], "the vault writer's marker rule"
    assert text.endswith("\n") and not text.endswith("\n\n") and "\r" not in text and "|" not in text


def test_weekly_lines_match_the_contract_grammar_newest_first() -> None:
    text = render_weekly(weekly_ctx())
    for key, pattern in WEEKLY_LINES.items():
        lines = weekly_section(text, key)
        assert lines and all(pattern.match(ln) for ln in lines), (key, lines)
    assert weekly_section(text, "runs") == ["- 3 runs, 1 failed, $0.05 Claude."]
    assert weekly_section(text, "decided") == ["- 2026-10-06: Synthetic decision [9f8e7d6c]"]
    assert weekly_section(text, "dropped") == ["- Synthetic stale thread (2026-09-20 to 2026-09-28) [0a1b2c3d]"]
    assert weekly_section(text, "snoozed") == ["- Snoozed until 2026-10-13 2026-10-09: Synthetic follow up one [6b6b6b6b]",
                                               "- Done 2026-10-08: Synthetic thread waiting on a reviewer [e5f6a7b8]"]
    assert weekly_section(text, "flagged") == ["- 2026-10-10 07:00: w-3a9f1c, should other, leak.",
                                               "- 2026-10-09 06:31: c0ffee01, should hold."]
    assert weekly_section(text, "cost") == ["- 2026-10-07: 2 runs, $0.01.", "- 2026-10-06: 1 run, $0.04."]


def test_weekly_empty_sections_print_none_and_the_cap_is_thirty() -> None:
    text = render_weekly(weekly_ctx(runs=[], decided=[], dropped=[], attended=[], flagged=[]))
    assert weekly_section(text, "runs") == ["- 0 runs, 0 failed, $0.00 Claude."]
    for key in ("decided", "dropped", "snoozed", "flagged", "cost"):
        assert weekly_section(text, key) == ["- None."], key
    many = [{"date": "2026-10-06", "text": f"Synthetic decision number {n}", "id": f"{n:08x}"} for n in range(40)]
    assert len(weekly_section(render_weekly(weekly_ctx(decided=many)), "decided")) == 30


def test_weekly_text_is_cleaned_and_a_held_id_only_ever_appears_as_an_id() -> None:
    em = chr(0x2014)
    ctx = weekly_ctx(decided=[{"date": "2026-10-06", "text": f"Keep [[x]] | the {em} plan", "id": "9f8e7d6c"}],
                     flagged=[{"ts": "2026-10-09 06:31", "id": "w-3a9f1c", "should": "escalate", "leak": False}])
    text = render_weekly(ctx)
    assert em not in text and "|" not in text and "[[" not in text
    assert "- 2026-10-06: Keep x / the, plan [9f8e7d6c]" in text
    assert "- 2026-10-09 06:31: w-3a9f1c, should escalate." in text
    assert weekly_filename("2026-W41") == "weekly-2026-W41.md"
    with pytest.raises(ValueError):
        render_weekly(weekly_ctx(week="week 41"))
    with pytest.raises(ValueError):
        render_weekly(weekly_ctx(generated_at=datetime(2026, 10, 12, 6, 31)))


def test_the_hub_parses_the_real_weekly_note_without_leftovers() -> None:
    dp = pytest.importorskip("jarvisd.hub.digestparse")
    from jarvisd.hub.mdhtml import split_front_matter

    text = render_weekly(weekly_ctx())
    meta, body = split_front_matter(text)
    parsed = dp.parse_weekly(body, meta)
    assert parsed["week"] == "2026-W41" and parsed["missing"] == []
    for key, section in parsed["sections"].items():
        assert section["present"] and section["other"] == [], key
    assert [r["id"] for r in parsed["sections"]["snoozed"]["rows"]] == ["6b6b6b6b", "e5f6a7b8"]
    assert parsed["counts"]["runs"] == 3 and parsed["counts"]["flagged"] == 2
    empty = render_weekly(weekly_ctx(runs=[], decided=[], dropped=[], attended=[], flagged=[]))
    meta, body = split_front_matter(empty)
    parsed = dp.parse_weekly(body, meta)
    assert all(s["none"] for k, s in parsed["sections"].items() if k != "runs")


def test_render_py_exports_what_the_hub_imports_for_phase_three() -> None:
    assert render.SIDECAR_SECTIONS == ("start_here", "attention", "active_task", "still_open", "decided", "repos")
    assert render.START_HERE_BUDGET == 2 and render.MAX_WEEKLY == 30
    assert list(WEEKLY_HEADINGS) == ["runs", "decided", "dropped", "snoozed", "flagged", "cost"]
    assert render.start_here_picks is render.pick_start_here
