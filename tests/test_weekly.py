"""jarvisd/weekly.py: the week helpers and `build_context` over a planted state folder and audit log.

Everything is synthetic; the state folder is built by hand in the shape the writer stores it.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from conftest import FakeClock
from jarvisd import render, weekly
from jarvisd.audit import AuditLog
from jarvisd.state import StateStore
from test_render_items import WEEKLY_LINES, weekly_section

WEEK = "2026-W40"  # 2026-09-28 to 2026-10-04


def test_week_helpers() -> None:
    assert weekly.week_of(date(2026, 10, 9)) == "2026-W41"
    assert weekly.week_of(date(2026, 1, 1)) == "2026-W01"
    assert weekly.previous_week(date(2026, 10, 12)) == "2026-W41"
    assert weekly.previous_week(date(2026, 10, 11)) == "2026-W40"
    assert weekly.week_bounds("2026-W41") == (date(2026, 10, 5), date(2026, 10, 11))
    assert weekly.week_bounds(WEEK) == (date(2026, 9, 28), date(2026, 10, 4))
    for bad in ("2026-W60", "2026-41", "nope", ""):
        with pytest.raises(weekly.BadWeek):
            weekly.week_bounds(bad)


def _manifest(folder: Path, job_id: str, stamp: str, status: str, cost: float) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "run.json").write_text(json.dumps({
        "job_id": job_id, "status": status, "started_at": stamp, "finished_at": stamp, "stages": {}, "counts": {},
        "cost_usd": cost, "paths": {}, "hashes": {}, "config_sha256": None, "audit_seq": 1}), encoding="utf-8")


def _record(item_id: str, text: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {"id": item_id, "text": text, "first_seen": "2026-09-20", "last_seen": "2026-09-30",
                               "times_shown": 2, "sections": ["still_open", "still_open"], "status": "open",
                               "snoozed_until": None, "resolved_at": None}
    base.update(extra)
    return base


@pytest.fixture
def planted(tmp_path: Path, clock: FakeClock) -> tuple[StateStore, AuditLog]:
    store = StateStore(tmp_path / "state", clock=clock, tz=timezone.utc)
    runs = store.dir / "runs"
    _manifest(runs / "digest-2026-09-29", "digest-2026-09-29", "2026-09-29T05:31:00+00:00", "complete", 0.02)
    _manifest(runs / "digest-2026-10-01", "digest-2026-10-01", "2026-10-01T05:31:00+00:00", "failed", 0.0)
    _manifest(runs / "digest-2026-10-01-r2", "digest-2026-10-01-r2", "2026-10-01T09:00:00+00:00", "complete", 0.01)
    _manifest(runs / "digest-2026-10-06", "digest-2026-10-06", "2026-10-06T05:31:00+00:00", "complete", 0.5)  # next week
    (runs / "broken").mkdir()
    (runs / "broken" / "run.json").write_text("{nope", encoding="utf-8")
    (store.dir / "item-history.json").write_text(json.dumps({"updated": "2026-10-04", "last_run": "digest-2026-10-04", "items": {
        "synthetic decision": _record("9f8e7d6c", "Synthetic decision", sections=["decided"], status="done", resolved_at="2026-10-01"),
        "clicked away": _record("aaaa0001", "Clicked away", status="done", resolved_at="2026-10-02"),
        "dropped thread": _record("0a1b2c3d", "Dropped thread", status="dropped", resolved_at="2026-10-03"),
        "old decision": _record("9f8e7d60", "Old decision", sections=["decided"], status="done", resolved_at="2026-09-20"),
        "still open": _record("e5f6a7b8", "Still open thread"),
        "snoozed thread": _record("6b6b6b6b", "Snoozed thread", status="snoozed", snoozed_until="2026-10-10"),
    }}), encoding="utf-8")
    attention = store.dir / "attention"
    attention.mkdir()
    (attention / "clicked-away-00000000.json").write_text(json.dumps({
        "key": "clicked away", "id": "aaaa0001", "action": "done", "until": None,
        "decided_at": "2026-10-02T08:00:00+00:00", "note": "digest-2026-10-02"}), encoding="utf-8")
    (attention / "snoozed-thread-00000000.json").write_text(json.dumps({
        "key": "snoozed thread", "id": "6b6b6b6b", "action": "snooze", "until": "2026-10-10",
        "decided_at": "2026-10-03T08:00:00+00:00", "note": "digest-2026-10-03"}), encoding="utf-8")
    (attention / "unknown-key-00000000.json").write_text(json.dumps({
        "key": "a key history never saw", "id": "bbbb0002", "action": "done", "until": None,
        "decided_at": "2026-10-04T08:00:00+00:00", "note": "digest-2026-10-04"}), encoding="utf-8")
    (attention / "next-week-00000000.json").write_text(json.dumps({
        "key": "still open", "id": "e5f6a7b8", "action": "done", "until": None,
        "decided_at": "2026-10-06T08:00:00+00:00", "note": "digest-2026-10-06"}), encoding="utf-8")
    audit = AuditLog(tmp_path / "logs" / "jarvisd-audit.jsonl", clock=clock, mirror_stdout=False)
    clock.set(datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc))
    audit.emit("correction", item_id="00000000", should="skip", leak=False, note_chars=0)  # the week before
    clock.set(datetime(2026, 10, 1, 7, 15, tzinfo=timezone.utc))
    audit.emit("correction", item_id="c0ffee01", should="hold", leak=False, note_chars=3)
    clock.set(datetime(2026, 10, 3, 18, 45, tzinfo=timezone.utc))
    audit.emit("correction", item_id="w-3a9f1c", should=None, leak=True, note_chars=0)
    clock.set(datetime(2026, 10, 5, 6, 31, tzinfo=timezone.utc))
    return store, audit


def test_build_context_gathers_the_week_from_the_filtered_stores(planted: tuple[StateStore, AuditLog], clock: FakeClock) -> None:
    store, audit = planted
    ctx = weekly.build_context(store, audit, WEEK, clock.now)
    assert (ctx.week, ctx.start, ctx.end) == (WEEK, date(2026, 9, 28), date(2026, 10, 4))
    assert sorted((r["date"], r["status"], r["cost_usd"]) for r in ctx.runs) == [
        ("2026-09-29", "complete", 0.02), ("2026-10-01", "complete", 0.01), ("2026-10-01", "failed", 0.0)]
    assert ctx.decided == [{"date": "2026-10-01", "text": "Synthetic decision", "id": "9f8e7d6c"}]
    assert ctx.dropped == [{"text": "Dropped thread", "first": "2026-09-20", "last": "2026-09-30", "id": "0a1b2c3d"}]
    assert sorted((d["action"], d["date"], d["text"], d["id"], d["until"]) for d in ctx.attended) == [
        ("done", "2026-10-02", "Clicked away", "aaaa0001", None),
        ("done", "2026-10-04", "a key history never saw", "bbbb0002", None),
        ("snooze", "2026-10-03", "Snoozed thread", "6b6b6b6b", "2026-10-10")]
    assert ctx.flagged == [{"ts": "2026-10-01 07:15", "id": "c0ffee01", "should": "hold", "leak": False},
                           {"ts": "2026-10-03 18:45", "id": "w-3a9f1c", "should": "other", "leak": True}]
    assert weekly.has_material(ctx)

    text = render.render_weekly(ctx)
    for key, pattern in WEEKLY_LINES.items():
        lines = weekly_section(text, key)
        assert lines and all(pattern.match(ln) for ln in lines), (key, lines)
    assert weekly_section(text, "runs") == ["- 3 runs, 1 failed, $0.03 Claude."]
    assert weekly_section(text, "cost") == ["- 2026-10-01: 2 runs, $0.01.", "- 2026-09-29: 1 run, $0.02."]
    assert "- Done 2026-10-04: a key history never saw [bbbb0002]" in text
    assert "Old decision" not in text and "Still open thread" not in text and "0.5" not in text


def test_an_empty_week_has_no_material(tmp_path: Path, clock: FakeClock) -> None:
    store = StateStore(tmp_path / "state", clock=clock, tz=timezone.utc)
    audit = AuditLog(tmp_path / "logs" / "jarvisd-audit.jsonl", clock=clock, mirror_stdout=False)
    ctx = weekly.build_context(store, audit, WEEK, clock.now)
    assert not weekly.has_material(ctx)
    text, same = weekly.render_week(store, audit, WEEK, clock.now)
    assert text == render.render_weekly(same) and "- 0 runs, 0 failed, $0.00 Claude." in text
    assert not (tmp_path / "state" / "attention").exists() and not list((tmp_path / "state").glob("*.tmp")), "reads only"


def test_the_weekly_module_opens_nothing_for_writing_and_names_no_vault_root() -> None:
    from test_write_locations import vault_attr_findings, write_findings

    source = Path(weekly.__file__).read_text(encoding="utf-8")
    assert write_findings(source) == [] and vault_attr_findings(source) == []
    assert "brain_root" not in source and ".held(" not in source
