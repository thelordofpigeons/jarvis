"""The hub's digest parser (jarvisd/hub/digestparse.py): the contract's grammar 2 on real `render_digest` output,
the grammar 1 fallback on a note shaped like the ones written before the rework, and the tolerance rules (a
missing heading is zero, an unknown line is kept as text).

Every name is synthetic; the grammar 2 notes come from the tests/test_render.py fixtures.
"""
from __future__ import annotations

import re

from jarvisd import render
from jarvisd.hub import digestparse as dp
from jarvisd.hub.mdhtml import split_front_matter
from jarvisd.render import render_digest
from test_hub import DIGEST, YESTERDAY
from test_render import all_held_ctx, complete_ctx, degraded_ctx, empty_ctx

# A note as the renderer wrote it before grammar 2 (no `grammar` key, a Brain section, ids mid-line).
GRAMMAR_1 = """---
type: jarvis-digest
generator: jarvisd
job_id: digest-2026-10-06
date: 2026-10-06
status: complete
items: {collected: 10, cleared: 7, held_sensitive: 2, held_policy: 1, over_cap: 0}
---
# JARVIS morning digest, Tuesday 2026-10-06

## Start here
One task is overdue and one repo moved overnight.
1. [c0ffee01] Review task is overdue since yesterday
2. [b2c3d4e5] One commit landed in the notes repo

## Active task
- 123synth fix(parser): synthetic task name, status IN REVIEW, due 2026-10-05 (OVERDUE). No ClickUp call was made (v1).

## Brain: open threads and decisions
- [2026-10-04] Synthetic thread waiting on a reviewer [e5f6a7b8] Waiting on the reviewer, nudge today
- [2026-09-20] (stale) Synthetic stale thread [0a1b2c3d]
- Decision [2026-10-03] Synthetic decision, because it is a fixture [9f8e7d6c] Fixture decision, nothing to do
- Session 2026-10-05-20, entry point: Continue at fixture.py:10, wire the synthetic thing [5a5a5a5a]
- Session 2026-10-05-20, open thread: Synthetic follow up one [6b6b6b6b]

## Repos
- example-api (work) branch test, 3 commits since window, 2 modified, 1 untracked: fix: synthetic parser bug [a1b2c3d4]
- example-notes: 1 commit since window, 0 modified, 0 untracked [b2c3d4e5]
- Quiet: example-web.
- Not a git repo or missing: example-missing.
- GitHub example-api (work): no open PRs, CI failure on main [a1b2c3d4]

## What JARVIS did while you slept
- Jobs: 1 done, 0 failed. Claude calls: 1 ($0.04, 3.2k tokens). Vault writes: 1. Breaker: closed.
- ExampleNightly: last run 2026-10-06 02:30, result 0.

## Held back and not summarized
- Sensitive, never read or sent: 2 items (ids w-3a9f1c, w-77d20e; reasons: component_sensitive x1, tag_frontmatter x1). Run `jarvis held` in a terminal.
- Policy (work metadata to Claude disabled): 1 item, rendered above without summary.
- Over size cap: 0 items. Claude unavailable: none.
"""


def _parse(text: str) -> dict:
    meta, body = split_front_matter(text)
    return dp.parse_note(body, meta)


# --- grammar 2, the fixture note ------------------------------------------------------------------------------


# --- the weekly review note (contract section 9) ---------------------------------------------------------------------


def test_parse_weekly_reads_the_six_sections_and_keeps_unknown_lines() -> None:
    from test_hub import WEEKLY

    meta, body = split_front_matter(WEEKLY)
    week = dp.parse_weekly(body, meta)
    assert week["type"] == "jarvis-weekly" and week["week"] == "2026-W40" and week["from"] == "2026-09-28"
    assert week["counts"] == {"runs": 7, "failed": 1, "decided": 1, "dropped": 1, "done": 1, "snoozed": 1, "flagged": 1}
    assert week["missing"] == []
    s = week["sections"]
    assert s["runs"]["rows"][0]["fields"] == {"runs": "7", "failed": "1", "usd": "0.19"}
    assert s["runs"]["rows"][0]["text"] == "7 runs, 1 failed, $0.19 Claude."
    assert s["decided"]["rows"][0]["id"] == "9f8e7d6c" and s["decided"]["rows"][0]["fields"]["date"] == "2026-10-03"
    assert s["decided"]["rows"][0]["text"] == "2026-10-03: Keep the strict sum of 100 for the scoring quotas"
    assert s["dropped"]["rows"][0]["fields"]["first"] == "2026-09-20" and s["dropped"]["rows"][0]["id"] == "d0d0d0d0"
    assert [r["fields"]["action"] for r in s["snoozed"]["rows"]] == ["Done", "Snoozed until 2026-10-09"]
    assert s["snoozed"]["other"] == ["a line the parser does not know"]
    assert s["flagged"]["rows"][0]["fields"]["should"] == "hold" and s["flagged"]["rows"][0]["leak"] is True
    assert s["flagged"]["rows"][0]["text"] == "2026-10-01 09:12: w-b33f54"
    assert [r["fields"]["usd"] for r in s["cost"]["rows"]] == ["0.03", "0.05"]
    assert all(not sec["none"] for sec in s.values())


def test_parse_weekly_treats_none_lines_and_missing_headings_as_empty() -> None:
    body = "# Week 2026-W41\n\n## Runs\n- None.\n\n## Decided this week\n- None.\n\n## Cost by day\n- 2026-10-06: 1 run, $0.02.\n"
    week = dp.parse_weekly(body, {"week": "2026-W41", "n_runs": "0"})
    assert week["sections"]["runs"]["none"] is True and week["sections"]["runs"]["rows"] == []
    assert sorted(week["missing"]) == ["dropped", "flagged", "snoozed"]
    assert week["sections"]["dropped"] == {"rows": [], "other": [], "none": False, "present": False}
    assert week["counts"]["runs"] == 0 and week["counts"]["failed"] is None
    assert week["sections"]["cost"]["rows"][0]["fields"]["runs"] == "1"


def test_parse_weekly_headings_are_the_writers() -> None:
    theirs = render.WEEKLY_HEADINGS
    if isinstance(theirs, dict):
        assert dp.WEEKLY_HEADINGS == {k: theirs[k] for k in dp.WEEKLY_SECTIONS}
    else:
        assert tuple(dp.WEEKLY_HEADINGS[k] for k in dp.WEEKLY_SECTIONS) == tuple(theirs)
    assert list(dp.WEEKLY_HEADINGS) == list(dp.WEEKLY_SECTIONS)


def test_parse_weekly_on_the_real_render_weekly_output() -> None:
    from datetime import date, datetime, timezone

    ctx = render.WeeklyContext(
        week="2026-W40", start=date(2026, 9, 28), end=date(2026, 10, 4), generated_at=datetime(2026, 10, 5, 5, 31, tzinfo=timezone.utc),
        runs=[{"date": "2026-10-03", "status": "complete", "cost_usd": 0.03}, {"date": "2026-10-04", "status": "failed", "cost_usd": 0.0}],
        decided=[{"date": "2026-10-03", "text": "Keep the strict sum of 100 for the scoring quotas", "id": "9f8e7d6c"}],
        dropped=[{"text": "An old synthetic thread nobody mentioned again", "first": "2026-09-20", "last": "2026-09-28", "id": "d0d0d0d0"}],
        attended=[{"action": "done", "until": None, "date": "2026-10-02", "text": "Rotate the pasted sandbox key", "id": "5c4d04ed"},
                  {"action": "snooze", "until": "2026-10-09", "date": "2026-10-01", "text": "Confirm the retry budget", "id": "6b6b6b6b"}],
        flagged=[{"ts": "2026-10-01 09:12", "id": "w-b33f54", "should": "hold", "leak": True}])
    meta, body = split_front_matter(render.render_weekly(ctx))
    week = dp.parse_weekly(body, meta)
    assert week["missing"] == [] and week["type"] == "jarvis-weekly" and week["week"] == "2026-W40"
    for key, sec in week["sections"].items():
        assert sec["other"] == [], (key, sec["other"])  # every line of the real note matches the contract's regex
        assert sec["rows"] or sec["none"], key
    assert week["sections"]["runs"]["rows"][0]["fields"] == {"runs": "2", "failed": "1", "usd": "0.03"}
    assert week["sections"]["snoozed"]["rows"][0]["fields"]["action"] == "Done"
    assert week["sections"]["flagged"]["rows"][0]["leak"] is True
    assert week["counts"]["done"] == 1 and week["counts"]["snoozed"] == 1


def test_grammar_2_fixture_parses_every_block_with_nothing_left_over() -> None:
    p = _parse(DIGEST)
    assert p["grammar"] == 2 and p["missing"] == []
    assert p["headline"].startswith("One task is overdue")
    assert [(s["id"], s["text"]) for s in p["start_here"]] == [
        ("5c4d04ed", "Rotate the pasted sandbox key before the demo tomorrow"),
        ("8d94ef0d", "Answer the reviewer on the parser fix, blocked since Monday")]
    assert p["attention"] == [{"kind": "CI failing", "text": "alpha-repo on main, 2 runs in a row", "id": "58d5ed8d"}]
    assert p["nothing_broken"] is False
    task = p["active_task"]
    assert task["task_id"] == "123synth" and task["status"] == "IN REVIEW" and task["overdue"] is True and task["id"] == "c0ffee01"
    assert [(r["group"], r["age"], r["id"]) for r in p["still_open"]] == [
        ("parser rework", 4, "6b6b6b6b"), ("notes", None, "e5f6a7b8"), ("notes", None, "0a1b2c3d")]
    assert p["still_open_hidden"] == 0
    assert p["decided"] == [{"text": "Keep the strict sum of 100 for the scoring quotas", "id": "9f8e7d6c"}]
    repos = p["repos"]
    assert repos["git"]["alpha-repo"] == {"branch": "main", "commits": 2, "modified": 1, "untracked": 3}
    assert repos["git"]["beta-repo"] == {"branch": "", "commits": 0, "modified": 4, "untracked": 0}
    assert repos["quiet_n"] == 2 and repos["github_quiet_n"] == 1
    assert repos["github"]["alpha-repo"] == {"prs": 0, "ci": "failure"}
    assert repos["other"] == ["GitHub not read: 2 repos (no_access 1, no_remote 1)."]
    assert p["system"] == [{"what": "Task", "detail": "ExampleNightly last ran 2026-10-06 02:30, refused by the operator or "
                                                      "administrator (0x800710E0)"}]
    assert p["all_green"] is None
    held = p["held"]
    assert held["sens"] == 2 and held["pol"] == 1 and held["cap"] == 0 and held["claude"] == "ok"
    assert held["ids"] == ["w-b33f54", "w-dc2799"] and held["reasons"] == "term:3 x2"
    assert p["counts"] == {"collected": 9, "cleared": 6, "held": 3}
    assert all(v == [] for v in p["other"].values()), p["other"]


def test_grammar_2_quiet_note() -> None:
    p = _parse(YESTERDAY)
    assert p["nothing_broken"] is True and p["attention"] == [] and p["start_here"] == []
    assert p["headline"] == "Quiet night."
    assert p["all_green"].startswith("All green: 1 job done")
    assert p["repos"]["quiet_n"] == 3 and p["repos"]["git"] == {"beta-repo": {"branch": "", "commits": 0, "modified": 2, "untracked": 0}}
    assert set(p["missing"]) == {"active_task", "still_open", "decided", "held"}
    assert p["counts"]["held"] == 1  # n_held wins over the items dict


def test_contract_regexes_reject_what_they_should() -> None:
    body = ("## Start here\nHeadline.\n1. lower case start is not a ranked line [5c4d04ed]\n"
            "2. Too short [5c4d04ed]\n3. A fine line that says what to do today [5c4d04ed] trailing\n"
            "## Attention\n- Something else: text [5c4d04ed]\n- CI failing: ok\n")
    p = dp.parse_note(body, {"grammar": "2"})
    assert p["start_here"] == []
    assert len(p["other"]["start_here"]) == 3
    assert p["attention"] == [{"kind": "CI failing", "text": "ok", "id": None}]
    assert p["other"]["attention"] == ["- Something else: text [5c4d04ed]"]


# --- grammar 2, real render_digest output ------------------------------------------------------------------------


def test_the_real_complete_note_parses_against_the_contract() -> None:
    p = _parse(render_digest(complete_ctx()))
    assert p["grammar"] == render.GRAMMAR == 2
    assert p["headline"] == "One task is overdue and one repo moved overnight."
    assert [s["id"] for s in p["start_here"]] == ["c0ffee01", "b2c3d4e5"]
    assert all(s["text"][0].isupper() and "[" not in s["text"] for s in p["start_here"])
    assert [a["kind"] for a in p["attention"]] == ["Task overdue"] and p["attention"][0]["id"] == "c0ffee01"
    assert p["active_task"]["task_id"] == "123synth" and p["active_task"]["id"] == "c0ffee01" and p["active_task"]["overdue"]
    rows = {r["id"]: r for r in p["still_open"]}
    assert set(rows) == {"5a5a5a5a", "6b6b6b6b", "e5f6a7b8"}
    assert rows["e5f6a7b8"]["group"] == "notes" and rows["6b6b6b6b"]["group"] == "parser rework"
    assert p["decided"] == [{"text": "Synthetic decision", "id": "9f8e7d6c"}]
    git = p["repos"]["git"]
    assert git["example-api"]["commits"] == 3 and git["example-notes"]["commits"] == 1
    assert git["example-site"] == {"branch": "", "commits": 0, "modified": 4, "untracked": 2}
    assert p["repos"]["quiet_n"] == 1
    assert p["all_green"] is not None and p["all_green"].startswith("All green: 1 job done, 0 failed")
    assert p["held"]["sens"] == 2 and p["held"]["pol"] == 1 and p["held"]["ids"] == ["w-3a9f1c", "w-77d20e"]
    assert p["counts"] == {"collected": 12, "cleared": 9, "held": 3}
    assert p["other"]["start_here"] == [] and p["other"]["still_open"] == [] and p["other"]["decided"] == []
    assert p["other"]["held"] == [] and p["other"]["attention"] == []


def test_the_real_degraded_empty_and_all_held_notes_parse() -> None:
    for ctx in (degraded_ctx(), empty_ctx(), all_held_ctx()):
        p = _parse(render_digest(ctx))
        assert p["grammar"] == 2 and isinstance(p["start_here"], list)
        for key, lines in p["other"].items():
            for line in lines:
                assert not dp.ID_TAIL.search(line), f"{key}: an item line the parser did not understand: {line}"
    p = _parse(render_digest(empty_ctx()))
    assert p["start_here"] == [] and p["active_task"] is None and p["active_task_text"] == "No active task recorded."
    assert p["still_open"] == [] and p["decided"] == [] and p["nothing_broken"] is True
    assert "still_open" in p["missing"] and "decided" in p["missing"]  # omitted when empty, as the contract says


def test_every_item_line_of_the_real_notes_ends_in_one_id_tail() -> None:
    for ctx in (complete_ctx(), degraded_ctx(), all_held_ctx()):
        _, body = split_front_matter(render_digest(ctx))
        for heading in ("start_here", "still_open", "decided"):
            for line in dp.section(body, dp.HEADINGS[heading]):
                if re.match(r"^(?:- |\d\. )", line) and "more open thread" not in line:
                    assert dp.ID_TAIL.search(line), line
                    assert len(re.findall(r"\[[0-9a-f]{8}\]", line)) == 1, line


# --- grammar 1 fallback -------------------------------------------------------------------------------------------


def test_grammar_1_note_maps_brain_to_still_open_and_decided() -> None:
    p = _parse(GRAMMAR_1)
    assert p["grammar"] == 1
    assert p["headline"] == "One task is overdue and one repo moved overnight."
    assert [s["id"] for s in p["start_here"]] == ["c0ffee01", "b2c3d4e5"]
    assert p["start_here"][0]["text"] == "Review task is overdue since yesterday"
    # no Attention section in grammar 1: the two facts the note carries are derived
    assert "attention" in p["missing"]
    assert [a["kind"] for a in p["attention"]] == ["Task overdue", "CI failing"]
    assert p["active_task"]["task_id"] == "123synth" and p["active_task"]["id"] is None
    assert p["active_task"]["overdue"] is True
    rows = {r["id"]: r for r in p["still_open"]}
    assert set(rows) == {"e5f6a7b8", "0a1b2c3d", "5a5a5a5a", "6b6b6b6b"}
    assert rows["e5f6a7b8"]["text"] == "Synthetic thread waiting on a reviewer"  # the gloss after the id is dropped
    assert rows["e5f6a7b8"]["group"] == "notes" and rows["6b6b6b6b"]["group"] == "notes"
    assert rows["0a1b2c3d"]["age"] == 16  # 2026-09-20 to 2026-10-06
    assert p["decided"] == [{"text": "Synthetic decision", "id": "9f8e7d6c"}]  # rationale cut at "because"
    assert p["repos"]["git"]["example-api"]["commits"] == 3 and p["repos"]["git"]["example-web"]["commits"] == 0
    assert p["repos"]["quiet_n"] is None and p["repos"]["github"]["example-api"]["ci"] == "failure"
    assert p["held"]["sens"] == 2 and p["held"]["pol"] == 1 and p["held"]["ids"] == ["w-3a9f1c", "w-77d20e"]
    assert p["system"] == [] and p["all_green"] is None
    assert p["other"]["system"][0].startswith("Jobs: 1 done")  # raw lines kept for the view
    assert p["other"]["still_open"] == [] and p["other"]["start_here"] == []
    assert p["counts"] == {"collected": 10, "cleared": 7, "held": 3}


def test_grammar_1_skips_no_active_work_entry_points_and_duplicate_session_lines() -> None:
    body = ("## Brain: open threads and decisions\n"
            "- Session 2026-10-05-20-some-slug, entry point: No active work, check the index [5a5a5a5a]\n"
            "- Session 2026-10-05-20-some-slug, open thread: Same thread twice [6b6b6b6b]\n"
            "- Session 2026-10-05-21-other, open thread: same thread twice [7c7c7c7c]\n")
    p = dp.parse_note(body, {"date": "2026-10-06"})
    assert [(r["id"], r["group"]) for r in p["still_open"]] == [("6b6b6b6b", "some slug")]


# --- tolerance ------------------------------------------------------------------------------------------------


def test_missing_headings_are_zero_and_unknown_lines_are_kept() -> None:
    p = dp.parse_note("## Start here\nOnly a headline.\n- a stray bullet\n", {"grammar": "2"})
    assert p["headline"] == "Only a headline." and p["start_here"] == []
    assert p["other"]["start_here"] == ["- a stray bullet"]
    for key in ("attention", "active_task", "still_open", "decided", "repos", "system", "held"):
        assert key in p["missing"], key
    assert p["attention"] == [] and p["nothing_broken"] is False and p["repos"]["git"] == {}


def test_strip_id_removes_the_tail_and_any_inner_token() -> None:
    assert dp.strip_id("- text here [c0ffee01]") == ("- text here", "c0ffee01")
    assert dp.strip_id("1. [c0ffee01] Old shape") == ("1. Old shape", "c0ffee01")
    assert dp.strip_id("no id at all") == ("no id at all", None)
    assert dp.strip_id("two [aaaaaaaa] ids [bbbbbbbb]") == ("two ids", "bbbbbbbb")


def test_shared_constants_are_the_renderers() -> None:
    assert dp.HEADINGS == render.HEADINGS and dp.ID_TAIL is render.ID_TAIL and dp.GRAMMAR == render.GRAMMAR
    assert dp.ID_TAIL.search(" x [0123abcd]") and not dp.ID_TAIL.search("[0123abcd] x")
    assert not re.search("open threads", dp.HEADINGS["still_open"], re.IGNORECASE)  # the reserved heading
    assert dp.HEADINGS["held"] == "Held back and not summarized"


def test_counts_of_reads_scalars_first_then_the_items_dict() -> None:
    assert dp.counts_of({"items": "{collected: 9, cleared: 6, held_sensitive: 2, held_policy: 1, over_cap: 0}"}) == {
        "collected": 9, "cleared": 6, "held": 3}
    assert dp.counts_of({"n_collected": "87", "n_cleared": "76", "n_held": "11"}) == {"collected": 87, "cleared": 76, "held": 11}
    assert dp.counts_of({}) == {"collected": 0, "cleared": 0, "held": 0}


def test_repo_lines_of_both_grammars() -> None:
    v1 = dp.parse_repo_lines(["- Quiet: a-repo, b-repo.", "- GitHub quiet: a-repo (CI success, 1 stale branch).",
                              "- Not a git repo or missing: c-repo."])
    assert v1["git"] == {"a-repo": {"branch": "", "commits": 0, "modified": 0, "untracked": 0},
                         "b-repo": {"branch": "", "commits": 0, "modified": 0, "untracked": 0}}
    assert v1["github"] == {"a-repo": {"prs": 0, "ci": "success"}} and v1["other"] == ["Not a git repo or missing: c-repo."]
    v2 = dp.parse_repo_lines(["- Uncommitted only: a-repo 3/1, b-repo 0/2.", "- Quiet: 4 repos.",
                              "- GitHub a-repo: 2 open PRs (1 yours, 1 awaiting your review), CI success on main [a1b2c3d4]"])
    assert v2["git"]["a-repo"] == {"branch": "", "commits": 0, "modified": 3, "untracked": 1}
    assert v2["git"]["b-repo"]["untracked"] == 2 and v2["quiet_n"] == 4
    assert v2["github"]["a-repo"] == {"prs": 2, "ci": "success"}


def test_grammar_1_collapses_a_thread_and_its_session_copy_and_applies_the_writer_exclusions() -> None:
    body = ("## Start here\nHeadline.\n1. [aaaa0001] Rotate the sandbox key before the demo\n"
            "## Brain: open threads and decisions\n"
            "- [2026-10-05] Rotate the sandbox key before the demo [aaaa0001]\n"
            "- [2026-10-05] Stop the two preview servers (`npx preview --stop` in each project). [bbbb0001]\n"
            "- [2026-10-05] Relay lines overlap briefly mid-swap (cosmetic). [cccc0001]\n"
            "- [2026-10-05] Delivered the synthetic exporter [dddd0001]\n"
            "- [2026-10-04] Confirm the retry budget with the reviewer [eeee0001]\n"
            "- Session 2026-10-05-20-some-slug, open thread: Stop the two preview servers (npx preview --stop in each project) [bbbb0002]\n"
            "- Session 2026-10-05-20-some-slug, open thread: Related: project_note_a, 2026-10-04-note-b [ffff0001]\n"
            "- Session 2026-10-05-21-other, open thread: Only in a session [abab0001]\n")
    p = dp.parse_note(body, {"date": "2026-10-06"})
    rows = {r["id"]: r for r in p["still_open"]}
    # the Start here pick, the cosmetic line, the delivered line and the reference line are gone
    for gone in ("aaaa0001", "cccc0001", "dddd0001", "ffff0001", "bbbb0002"):
        assert gone not in rows, gone
    # the RECENT.md bullet wins over its session copy and takes the session's group
    assert rows["bbbb0001"]["group"] == "some slug" and rows["eeee0001"]["group"] == "notes"
    assert rows["abab0001"]["group"] == "other"
    assert len(p["still_open"]) == 3 and all("session" not in r for r in p["still_open"])
