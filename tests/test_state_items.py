"""state.ItemHistory and update_history: the writer's memory of item keys (contract section 6.1)."""
from __future__ import annotations

import json
from datetime import date, timezone
from pathlib import Path

from conftest import FakeClock
from jarvisd.state import ATTENTION_DIR, ITEM_HISTORY_FILE, StateStore, update_history


def _store(tmp_path: Path, clock: FakeClock) -> StateStore:
    return StateStore(tmp_path / "state", clock=clock, tz=timezone.utc)


def rec(key: str, item_id: str, section: str = "still_open", text: str | None = None) -> dict[str, object]:
    return {"key": key, "id": item_id, "section": section, "group": "notes", "date": "2026-10-05",
            "text": text or key.capitalize(), "rank": 1, "since": "new"}


def test_update_history_files_what_was_shown_and_decisions_at_first_print() -> None:
    history: dict[str, object] = {"updated": None, "last_run": None, "items": {}}
    records = [rec("rotate the key", "c0ffee01", "start_here", "Rotate the key"),
               rec("old decision", "9f8e7d6c", "decided", "Old decision")]
    counts = update_history(history, records, [], date(2026, 10, 6), "digest-2026-10-06")
    assert counts == {"new": 2, "resolved": 1, "dropped": 0, "returned": 0}
    items = history["items"]
    assert items["rotate the key"] == {  # type: ignore[index]
        "id": "c0ffee01", "text": "Rotate the key", "first_seen": "2026-10-06", "last_seen": "2026-10-06",
        "times_shown": 1, "sections": ["start_here"], "status": "open", "snoozed_until": None, "resolved_at": None}
    decision = items["old decision"]  # type: ignore[index]
    assert decision["status"] == "done" and decision["resolved_at"] == "2026-10-06" and decision["sections"] == ["decided"]
    assert history["updated"] == "2026-10-06" and history["last_run"] == "digest-2026-10-06"
    # The next morning: shown again, so the budget counters move and first_seen stays.
    counts = update_history(history, records[:1], [], date(2026, 10, 7), "digest-2026-10-07")
    assert counts == {"new": 0, "resolved": 0, "dropped": 0, "returned": 0}
    shown = items["rotate the key"]  # type: ignore[index]
    assert shown["times_shown"] == 2 and shown["sections"] == ["start_here", "start_here"]
    assert shown["first_seen"] == "2026-10-06" and shown["last_seen"] == "2026-10-07"
    assert len(shown["sections"]) == shown["times_shown"]
    assert decision["times_shown"] == 1, "a decision not printed again is not counted again"
    # The same key printed twice in one run (two ids) is one record and one count.
    update_history(history, [rec("twice", "aaaa0001"), rec("twice", "aaaa0002")], [], date(2026, 10, 7), "r")
    assert items["twice"]["times_shown"] == 1 and items["twice"]["id"] == "aaaa0001"  # type: ignore[index]


def test_update_history_applies_done_and_snooze_decisions() -> None:
    history: dict[str, object] = {"items": {}}
    today = date(2026, 10, 6)
    update_history(history, [rec("a", "aaaaaaaa"), rec("b", "bbbbbbbb")], [], today, "r1")
    done = {"key": "a", "id": "aaaaaaaa", "action": "done", "until": None}
    snooze = {"key": "b", "id": "bbbbbbbb", "action": "snooze", "until": "2026-10-09"}
    counts = update_history(history, [], [done, snooze], date(2026, 10, 7), "r2")
    assert counts["resolved"] == 1 and counts["returned"] == 0
    items = history["items"]
    assert items["a"]["status"] == "done" and items["a"]["resolved_at"] == "2026-10-07"  # type: ignore[index]
    assert items["b"]["status"] == "snoozed" and items["b"]["snoozed_until"] == "2026-10-09"  # type: ignore[index]
    # A done is final: a later decision or record changes nothing about it.
    update_history(history, [rec("a", "aaaaaaaa")], [done, {"key": "a", "action": "snooze", "until": "2026-12-01"}],
                   date(2026, 10, 8), "r3")
    assert items["a"]["status"] == "done" and items["a"]["resolved_at"] == "2026-10-07"  # type: ignore[index]
    # The snooze ends and the key is printed again: back, counted as returned, no snooze date left.
    counts = update_history(history, [rec("b", "bbbbbbbb")], [snooze], date(2026, 10, 9), "r4")
    assert counts["returned"] == 1
    assert items["b"]["status"] == "open" and items["b"]["snoozed_until"] is None  # type: ignore[index]
    # A snooze that ends while the key is not collected reopens it quietly.
    update_history(history, [], [{"key": "b", "action": "snooze", "until": "2026-10-10"}], date(2026, 10, 9), "r5")
    assert items["b"]["status"] == "snoozed"  # type: ignore[index]
    counts = update_history(history, [], [], date(2026, 10, 10), "r6")
    assert items["b"]["status"] == "open" and counts["returned"] == 0  # type: ignore[index]
    # A decision for a key the history never saw gets a record whose text is the key (ids only elsewhere).
    update_history(history, [], [{"key": "ghost key", "id": "cccccccc", "action": "done"}], date(2026, 10, 10), "r7")
    assert items["ghost key"]["status"] == "done" and items["ghost key"]["text"] == "ghost key"  # type: ignore[index]
    # Malformed decisions are ignored.
    before = json.dumps(history, sort_keys=True)
    update_history(history, [], [{"action": "done"}, {"key": "a", "action": "nope"}, "text"], date(2026, 10, 10), "r7")  # type: ignore[list-item]
    assert json.dumps(history, sort_keys=True) == before


def test_update_history_drops_idle_keys_and_reopens_them_when_collected_again() -> None:
    history: dict[str, object] = {"items": {}}
    update_history(history, [rec("a", "aaaaaaaa")], [], date(2026, 9, 20), "r1")
    items = history["items"]
    counts = update_history(history, [], [], date(2026, 9, 27), "r2")  # exactly seven days: still open
    assert counts["dropped"] == 0 and items["a"]["status"] == "open"  # type: ignore[index]
    counts = update_history(history, [], [], date(2026, 9, 28), "r3")
    assert counts["dropped"] == 1
    assert items["a"]["status"] == "dropped" and items["a"]["resolved_at"] == "2026-09-28"  # type: ignore[index]
    counts = update_history(history, [], [], date(2026, 9, 29), "r4")
    assert counts["dropped"] == 0, "dropped once, not every morning"
    counts = update_history(history, [rec("a", "aaaaaaaa")], [], date(2026, 10, 1), "r5")
    assert counts == {"new": 0, "resolved": 0, "dropped": 0, "returned": 0}
    back = items["a"]  # type: ignore[index]
    assert back["status"] == "open" and back["first_seen"] == "2026-09-20" and back["resolved_at"] is None
    assert back["times_shown"] == 2 and back["last_seen"] == "2026-10-01"
    # A snoozed or done key is never dropped.
    update_history(history, [rec("s", "bbbbbbbb")], [{"key": "s", "action": "snooze", "until": "2026-12-01"}], date(2026, 10, 1), "r6")
    update_history(history, [], [], date(2026, 11, 1), "r7")
    assert items["s"]["status"] == "snoozed"  # type: ignore[index]


def test_item_history_store_round_trips_and_reads_the_attention_files(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    assert store.items.path == store.dir / ITEM_HISTORY_FILE and store.items.attention_dir == store.dir / ATTENTION_DIR
    assert store.items.load() == {"updated": None, "last_run": None, "items": {}}
    assert store.items.decisions() == []
    folder = store.dir / ATTENTION_DIR
    folder.mkdir()
    (folder / "a-aaaaaaaa.json").write_text(json.dumps({"key": "a", "id": "aaaaaaaa", "action": "done", "until": None,
                                                        "decided_at": "2026-10-06T07:00:00+00:00", "note": "digest-2026-10-06"}),
                                            encoding="utf-8")
    (folder / "bad.json").write_text("not json", encoding="utf-8")
    (folder / "other.json").write_text(json.dumps({"key": "b", "action": "nope"}), encoding="utf-8")
    (folder / "notes.txt").write_text(json.dumps({"key": "c", "action": "done"}), encoding="utf-8")
    assert [d["key"] for d in store.items.decisions()] == ["a"]
    counts = store.items.apply([rec("a", "aaaaaaaa"), rec("b", "bbbbbbbb")], store.items.decisions(), date(2026, 10, 6),
                               "digest-2026-10-06")
    assert counts == {"new": 2, "resolved": 1, "dropped": 0, "returned": 0}
    data = json.loads((store.dir / ITEM_HISTORY_FILE).read_text(encoding="utf-8"))
    assert data["items"]["a"]["status"] == "done" and data["items"]["b"]["status"] == "open"
    assert data["last_run"] == "digest-2026-10-06" and store.items.load() == data
    assert not list(store.dir.glob(".item-history.json.*.tmp")), "the atomic write leaves no temp file"
    # A corrupt file reads as empty and is replaced by the next apply, never raised through the digest.
    (store.dir / ITEM_HISTORY_FILE).write_text("{broken", encoding="utf-8")
    assert store.items.load()["items"] == {}
    store.items.apply([rec("c", "cccccccc")], [], date(2026, 10, 7), "digest-2026-10-07")
    assert set(store.items.load()["items"]) == {"c"}
