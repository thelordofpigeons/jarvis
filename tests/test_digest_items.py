"""The digest pipeline's phase 3 and 4 writes: the item sidecar, the history and the weekly review.

Runs the real pipeline from tests/test_digest_e2e.py (fake claude, real git, synthetic vault) and checks
`state/runs/<job>/items.json`, `state/item-history.json` and `raw/jarvis/weekly-*.md` against
docs/hub-rework-contract.md sections 6 and 9. Everything is synthetic.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import CANARY
from jarvisd import weekly
from jarvisd.common import iso, sha256_hex
from jarvisd.config import Config
from jarvisd.digest import run_digest_job
from jarvisd.render import SIDECAR_SECTIONS
from test_digest_e2e import SESSION, Rig, build, new_job, plant_clean_day, set_age, tree, write

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def sidecar(rig: Rig, job_id: str) -> dict[str, Any]:
    return json.loads((rig.state.dir / "runs" / job_id / "items.json").read_text(encoding="utf-8"))


def history_of(rig: Rig) -> dict[str, Any]:
    return json.loads((rig.state.dir / "item-history.json").read_text(encoding="utf-8"))


def manifest(rig: Rig, job_id: str) -> dict[str, Any]:
    return json.loads((rig.state.dir / "runs" / job_id / "run.json").read_text(encoding="utf-8"))


def decide(rig: Rig, record: dict[str, Any], action: str, until: str | None, note: str) -> Path:
    """Write one attention file the way the hub does (contract section 7): one file per key, ids and key only."""
    key = record["key"]
    name = "-".join(key.split())[:72] + "-" + sha256_hex(key)[:8]
    return write(rig.state.dir / "attention" / f"{name}.json", json.dumps({
        "key": key, "id": record["id"], "action": action, "until": until, "decided_at": iso(rig.clock.now), "note": note}))


def plant_sensitive(rig: Rig, tmp_vault: Path) -> None:
    """A tagged session with the canary, and a RECENT bullet that shares its date (derived sensitive)."""
    now = rig.clock.now
    prev = (rig.day - timedelta(days=1)).isoformat()
    tagged = write(tmp_vault / "sessions" / f"{prev}-09.md", SESSION.format(day=prev, extra="tags: [sensitive]\n") + f"\n{CANARY}\n")
    set_age(tagged, now, 5.0)
    recent = tmp_vault / "RECENT.md"
    recent.write_text(recent.read_text(encoding="utf-8").replace("[2026-10-04]", f"[{prev}]"), encoding="utf-8", newline="\n")
    set_age(recent, now, 2.0)


# --- sidecar and history -------------------------------------------------------------------


def test_sidecar_and_history_come_from_the_rendered_note_and_never_hold_a_held_id(
    tmp_cfg: Config, tmp_path: Path, tmp_vault: Path
) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    plant_sensitive(rig, tmp_vault)
    job = new_job(rig)
    result = run_digest_job(job, rig.deps, mode="daemon")
    note = rig.note().read_text(encoding="utf-8")

    doc = sidecar(rig, job.id)
    assert doc["job_id"] == job.id and doc["date"] == rig.day.isoformat()
    records = doc["items"]
    assert records, "the clean session gives at least one item line"
    for r in records:
        assert set(r) == {"key", "id", "section", "group", "date", "text", "rank", "since"}
        assert r["section"] in SIDECAR_SECTIONS and r["since"] == "new" and r["key"] and "[" not in r["text"]
        assert f"[{r['id']}]" in note, "every record is a line of the note"
    assert len({r["id"] for r in records}) == len(records), "an id is recorded once"
    assert {r["section"] for r in records} >= {"still_open"}

    held_ids = {h["id"] for h in rig.store.held()}
    # The sensitive ids are the ones the Held line names; the policy-held work task is printed and recorded.
    held_line = re.search(r"^- Held: (\d+) sensitive \(ids ([^;]+); ", note, re.MULTILINE)
    assert held_line is not None, "the run really had sensitive-held items"
    sensitive = set(held_line.group(2).split(", "))
    assert len(sensitive) == int(held_line.group(1)) >= 2 and result["items"]["held"] == len(held_ids)
    stored = json.dumps(doc) + (rig.state.dir / "item-history.json").read_text(encoding="utf-8")
    for hid in sensitive:
        assert hid not in stored, hid
    assert CANARY not in stored and "telos" not in stored.lower()

    hist = history_of(rig)
    assert hist["last_run"] == job.id and hist["updated"] == rig.day.isoformat()
    assert set(hist["items"]) == {r["key"] for r in records}
    for r in records:
        h = hist["items"][r["key"]]
        assert h["id"] == r["id"] and h["text"] == r["text"] and h["times_shown"] == 1 and h["sections"] == [r["section"]]
        assert h["first_seen"] == h["last_seen"] == rig.day.isoformat() and h["status"] == "open"
        assert h["snoozed_until"] is None and h["resolved_at"] is None
    recorded = rig.events("items_recorded")
    assert len(recorded) == 1 and recorded[0]["records"] == len(records) and recorded[0]["since_new"] == len(hist["items"])
    assert not {"text", "title", "key"} & set(recorded[0])
    assert manifest(rig, job.id)["stages"]["items"] == "ok"
    assert "n_since_new: " in note and "n_since_resolved: 0" in note


def test_a_dry_run_and_a_refused_write_leave_no_sidecar_and_no_history(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    dry = new_job(rig, dry_run=True)
    result = run_digest_job(dry, rig.deps, mode="manual")
    assert result["status"] == "dry_run" and "n_since_new:" in result["note"], "the dry note renders the counts in memory"
    assert not (rig.state.dir / "runs" / dry.id / "items.json").exists()
    assert not (rig.state.dir / "item-history.json").exists()
    write(rig.note(f"digest-{rig.day.isoformat()}-r2.md"), "someone else's note")  # foreign: the writer refuses it
    forced = new_job(rig, f"digest-{rig.day.isoformat()}-r2", force=True)
    result = run_digest_job(forced, rig.deps, mode="manual")
    assert result["status"] == "failed" and result["reason"] == "vault_denied:existing_file_not_ours"
    assert not (rig.state.dir / "runs" / forced.id / "items.json").exists()
    assert not (rig.state.dir / "item-history.json").exists()
    assert "items" not in manifest(rig, forced.id)["stages"]


def test_done_and_snooze_decisions_age_the_next_notes(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    day = rig.day.isoformat()
    first = new_job(rig)
    run_digest_job(first, rig.deps, mode="daemon")
    records = sidecar(rig, first.id)["items"]
    still = [r for r in records if r["section"] == "still_open"]
    assert len(still) >= 2, still
    done_rec, snooze_rec = still[0], still[1]
    decide(rig, done_rec, "done", None, first.id)
    later = (rig.day + timedelta(days=2)).isoformat()
    decide(rig, snooze_rec, "snooze", later, first.id)

    second = new_job(rig, f"digest-{day}-r2", force=True)
    run_digest_job(second, rig.deps, mode="manual")
    note2 = rig.note(f"digest-{day}-r2.md").read_text(encoding="utf-8")
    assert done_rec["id"] not in note2 and snooze_rec["id"] not in note2
    assert "n_since_resolved: 1" in note2
    ids2 = {r["id"] for r in sidecar(rig, second.id)["items"]}
    assert done_rec["id"] not in ids2 and snooze_rec["id"] not in ids2
    hist = history_of(rig)
    assert hist["items"][done_rec["key"]]["status"] == "done" and hist["items"][done_rec["key"]]["resolved_at"] == day
    assert hist["items"][snooze_rec["key"]]["status"] == "snoozed" and hist["items"][snooze_rec["key"]]["snoozed_until"] == later
    for r in sidecar(rig, second.id)["items"]:
        assert hist["items"][r["key"]]["times_shown"] == 2

    # The snooze expires (the hub replaces an expired file; here the date is today): the thread is back, as returned.
    decide(rig, snooze_rec, "snooze", day, first.id)
    third = new_job(rig, f"digest-{day}-r3", force=True)
    run_digest_job(third, rig.deps, mode="manual")
    note3 = rig.note(f"digest-{day}-r3.md").read_text(encoding="utf-8")
    assert done_rec["id"] not in note3, "a done is for ever"
    assert snooze_rec["id"] in note3 and "n_since_returned: 1" in note3 and "n_since_resolved: 0" in note3
    back = next(r for r in sidecar(rig, third.id)["items"] if r["id"] == snooze_rec["id"])
    assert back["since"] == "returned"
    assert history_of(rig)["items"][snooze_rec["key"]]["status"] == "open"


# --- the weekly review ---------------------------------------------------------------------


def plant_last_week_run(rig: Rig, cost: float = 0.0123) -> str:
    week = weekly.previous_week(rig.day)
    monday, _ = weekly.week_bounds(week)
    day = monday + timedelta(days=1)
    stamp = iso(datetime.combine(day, time(6, 30), tzinfo=timezone.utc))
    job_id = f"digest-{day.isoformat()}"
    write(rig.state.dir / "runs" / job_id / "run.json", json.dumps({
        "job_id": job_id, "status": "complete", "started_at": stamp, "finished_at": stamp, "stages": {"write": "ok"},
        "counts": {"collected": 3}, "cost_usd": cost, "paths": {"note": f"raw/jarvis/{job_id}.md"}, "hashes": {},
        "config_sha256": None, "audit_seq": 1}))
    return week


def test_the_weekly_review_is_written_once_on_the_first_run_of_a_new_week(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    plant_sensitive(rig, tmp_vault)
    week = plant_last_week_run(rig)
    before = tree(tmp_vault)
    job = new_job(rig)
    run_digest_job(job, rig.deps, mode="daemon")

    rel = f"raw/jarvis/weekly-{week}.md"
    assert tree(tmp_vault) - before == {f"raw/jarvis/digest-{rig.day.isoformat()}.md", rel}
    path = tmp_vault / rel
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\ntype: jarvis-weekly\ngenerator: jarvisd\n") and f"# Week {week}" in text
    assert "- 1 run, 0 failed, $0.01 Claude." in text and "## Cost by day" in text
    assert "- None." in text and CANARY not in text and "telos" not in text.lower()
    assert manifest(rig, job.id)["stages"]["weekly"] == "ok"
    events = rig.events("weekly_written")
    assert len(events) == 1 and events[0]["week"] == week and events[0]["rel"] == rel
    assert any(r["rel"] == rel for r in rig.events("vault_write")), "the same vault writer and audit as the digest"

    data = path.read_bytes()
    again = new_job(rig, f"digest-{rig.day.isoformat()}-r2", force=True)
    run_digest_job(again, rig.deps, mode="manual")
    assert path.read_bytes() == data and len(rig.events("weekly_written")) == 1
    assert "weekly" not in manifest(rig, again.id)["stages"]


def test_no_weekly_note_when_the_week_before_has_nothing_to_review(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    job = new_job(rig)
    run_digest_job(job, rig.deps, mode="daemon")
    assert not list((tmp_vault / "raw" / "jarvis").glob("weekly-*.md"))
    assert manifest(rig, job.id)["stages"]["weekly"] == "skipped"
    assert rig.events("weekly_written") == []


def test_a_refused_weekly_write_never_fails_the_digest(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    rig = build(tmp_cfg, tmp_path, tmp_vault)
    plant_clean_day(rig)
    week = plant_last_week_run(rig)
    write(tmp_vault / "raw" / "jarvis" / f"weekly-{week}.md", "a hand-written review")  # present: left alone
    job = new_job(rig)
    result = run_digest_job(job, rig.deps, mode="daemon")
    assert result["status"] == "complete"
    assert (tmp_vault / "raw" / "jarvis" / f"weekly-{week}.md").read_text(encoding="utf-8") == "a hand-written review"
    assert rig.events("weekly_written") == [] and rig.events("vault_violation") == []
