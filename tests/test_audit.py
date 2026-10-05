"""audit: hash chain, tamper detection, rotation, redaction, concurrency, fallback."""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import FakeClock

from jarvisd import __version__
from jarvisd.audit import GENESIS, AuditLog
from jarvisd.common import canonical_json, sha256_hex


def _log(tmp_path: Path, clock: FakeClock | None = None, **kw: object) -> AuditLog:
    kw.setdefault("mirror_stdout", False)
    return AuditLog(tmp_path / "logs" / "jarvisd-audit.jsonl", clock=clock, **kw)  # type: ignore[arg-type]


def _lines(path: Path) -> list[bytes]:
    return path.read_bytes().split(b"\n")[:-1]


@pytest.mark.parametrize("where", ["start", "middle", "end"])
def test_tamper_detected_at_exact_seq(tmp_path: Path, where: str) -> None:
    log = _log(tmp_path)
    for i in range(50):
        log.emit("tick", n=i)
    assert log.verify() == (True, None)

    lines = _lines(log.path)
    victim = bytearray(lines[22])  # record 23 (seq starts at 1)
    index = {"start": 2, "middle": len(victim) // 2, "end": len(victim) - 2}[where]
    victim[index] ^= 0x01
    lines[22] = bytes(victim)
    log.path.write_bytes(b"\n".join(lines) + b"\n")

    assert log.verify() == (False, 23)


def test_deleted_and_reordered_records_are_detected(tmp_path: Path) -> None:
    log = _log(tmp_path)
    for i in range(10):
        log.emit("tick", n=i)
    lines = _lines(log.path)

    log.path.write_bytes(b"\n".join(lines[:4] + lines[5:]) + b"\n")  # record 5 removed
    assert log.verify() == (False, 5)

    swapped = lines[:]
    swapped[2], swapped[3] = swapped[3], swapped[2]
    log.path.write_bytes(b"\n".join(swapped) + b"\n")
    assert log.verify() == (False, 3)


def test_fresh_chain_shape(tmp_path: Path, clock: FakeClock) -> None:
    log = _log(tmp_path, clock, run_id="run-1")
    assert log.head() == (0, GENESIS)
    first = log.emit("daemon_start", previous_exit="clean")
    second = log.emit("job_state", job_id="j-9", state="running")

    raw = log.path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    parsed = [json.loads(line) for line in _lines(log.path)]
    assert parsed == [first, second]

    assert list(first)[:2] == ["ts", "event"]
    assert list(first)[:9] == ["ts", "event", "seq", "prev", "h", "run_id", "job_id", "pid", "ver"]
    assert first["ts"] == "2026-10-06T05:31:00.000+00:00"
    assert (first["seq"], first["prev"], first["run_id"], first["ver"]) == (1, GENESIS, "run-1", __version__)
    assert first["previous_exit"] == "clean"
    assert second["seq"] == 2 and second["prev"] == first["h"] and second["job_id"] == "j-9"

    body = {k: v for k, v in second.items() if k != "h"}
    assert second["h"] == sha256_hex(second["prev"] + canonical_json(body))
    assert log.head() == (2, second["h"])
    assert log.verify() == (True, None)


def test_redaction_guard(tmp_path: Path) -> None:
    log = _log(tmp_path)
    em = chr(0x2014)
    rec = log.emit("probe", prompt="secret prompt", content="c", text="t", body="b", title="x",
                   note="y" * 900, nested={"Text": "dropped", "keep": 1, "deep": [{"body": "z", "n": 2}]},
                   dashed=f"a {em} b", when=datetime(2026, 1, 1, tzinfo=timezone.utc), odd=object)
    for key in ("prompt", "content", "text", "body", "title"):
        assert key not in rec
    assert len(rec["note"]) == 500 and rec["note"].endswith("...")
    assert rec["nested"] == {"keep": 1, "deep": [{"n": 2}]}
    assert rec["dashed"] == "a, b"
    assert rec["when"] == "2026-01-01T00:00:00+00:00"
    assert isinstance(rec["odd"], str)
    assert "secret prompt" not in log.path.read_text(encoding="utf-8")
    assert log.verify() == (True, None)


def test_reserved_field_names_cannot_forge_the_chain(tmp_path: Path) -> None:
    log = _log(tmp_path)
    rec = log.emit("probe", seq=999, prev="abc", h="def", ts="then", pid=0)
    assert rec["seq"] == 1 and rec["prev"] == GENESIS and rec["ts"] != "then"
    assert rec["x_seq"] == 999 and rec["x_h"] == "def"
    assert log.verify() == (True, None)


def test_emit_never_raises_and_leaves_fallback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "audit.jsonl"
    bad.mkdir()  # a directory where the log file should be: every append fails
    log = AuditLog(bad, mirror_stdout=False)
    rec = log.emit("gate_decision", item_id="i1", prompt="never stored")
    assert rec["event"] == "gate_decision" and rec["unchained"] is True
    fallback = Path(str(bad) + ".fallback")
    assert fallback.exists()
    stored = json.loads(fallback.read_text(encoding="utf-8").splitlines()[0])
    assert stored["event"] == "gate_decision" and stored["item_id"] == "i1" and "prompt" not in stored
    assert "[warn] audit write failed" in capsys.readouterr().err
    assert log.head() == (0, GENESIS)
    assert log.verify([]) == (True, None)


def test_emit_survives_unusable_directory(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    log = AuditLog(blocker / "sub" / "audit.jsonl", mirror_stdout=False)
    log.emit("x")  # neither the log nor the fallback can be written; still no exception
    assert log.rotate_if_due() is False


def test_stdout_mirror_only_when_stdout_exists(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    log = AuditLog(tmp_path / "a.jsonl")
    rec = log.emit("hello")
    assert json.loads(capsys.readouterr().out.strip()) == rec
    monkeypatch.setattr(sys, "stdout", None)
    log.emit("under_pythonw")  # no exception without a stdout
    assert log.verify() == (True, None)


def test_four_threads_get_unique_contiguous_seq(tmp_path: Path) -> None:
    log = _log(tmp_path)

    def worker(n: int) -> None:
        for i in range(25):
            log.emit("tick", thread=n, i=i)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [r["seq"] for r in log.records()]
    assert seqs == list(range(1, 101))
    assert log.verify() == (True, None)


def test_two_instances_on_one_file_share_one_chain(tmp_path: Path) -> None:
    first, second = _log(tmp_path), _log(tmp_path)
    for i in range(5):
        first.emit("a", i=i)
        second.emit("b", i=i)
    assert [r["seq"] for r in first.records()] == list(range(1, 11))
    assert first.verify() == (True, None)


def test_restart_continues_the_chain(tmp_path: Path) -> None:
    first = _log(tmp_path)
    first.emit("one")
    last = first.emit("two")
    reopened = _log(tmp_path)
    assert reopened.head() == (2, last["h"])
    assert reopened.emit("three")["seq"] == 3
    assert reopened.verify() == (True, None)


def test_torn_last_line_is_quarantined_not_glued(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.emit("one")
    log.emit("two")
    with open(log.path, "ab") as fh:
        fh.write(b'{"ts":"2026-10-06T05:31:00.000+00:00","event":"half')  # crash mid-write
    reopened = _log(tmp_path)
    assert reopened.emit("three")["seq"] == 3
    assert reopened.verify() == (True, None)
    fallback = Path(str(log.path) + ".fallback").read_text(encoding="utf-8")
    assert "torn_tail_quarantined" in fallback and "half" in fallback


def test_rotation_keeps_seq_and_prev_continuous(tmp_path: Path, clock: FakeClock) -> None:
    log = _log(tmp_path, clock, max_bytes=600)
    for i in range(30):
        clock.advance(seconds=1)
        log.emit("tick", n=i)

    rotated = log.rotated_files()
    assert len(rotated) >= 3
    assert all(p.name.startswith("jarvisd-audit.") and p.name.endswith("Z.jsonl") for p in rotated)

    records = log.records()
    assert [r["seq"] for r in records] == list(range(1, len(records) + 1))
    for before, after in zip(records, records[1:]):
        assert after["prev"] == before["h"]
    assert log.verify() == (True, None)
    assert log.verify(log.all_files()) == (True, None)

    # The first record of each new file names the head it continues from.
    first_of_new = json.loads(_lines(log.path)[0])
    assert first_of_new["event"] == "audit_rotated"
    last_of_old = json.loads(_lines(rotated[-1])[-1])
    assert first_of_new["prev"] == last_of_old["h"] == first_of_new["prev_file_head"]
    assert first_of_new["seq"] == last_of_old["seq"] + 1


def test_tamper_inside_a_rotated_file_reports_its_seq(tmp_path: Path, clock: FakeClock) -> None:
    log = _log(tmp_path, clock, max_bytes=600)
    for i in range(30):
        clock.advance(seconds=1)
        log.emit("tick", n=i)
    oldest = log.rotated_files()[0]
    lines = _lines(oldest)
    victim = json.loads(lines[1])
    victim["n"] = 4242
    lines[1] = json.dumps(victim, separators=(",", ":")).encode("utf-8")
    oldest.write_bytes(b"\n".join(lines) + b"\n")
    assert log.verify() == (False, victim["seq"])


def test_deleting_a_middle_rotated_file_is_detected(tmp_path: Path, clock: FakeClock) -> None:
    log = _log(tmp_path, clock, max_bytes=600)
    for i in range(30):
        clock.advance(seconds=1)
        log.emit("tick", n=i)
    rotated = log.rotated_files()
    gone_first_seq = json.loads(_lines(rotated[1])[0])["seq"]
    rotated[1].unlink()
    assert log.verify() == (False, gone_first_seq)


def test_rotation_by_month(tmp_path: Path, clock: FakeClock) -> None:
    clock.set(datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc))
    log = _log(tmp_path, clock)
    log.emit("a")
    clock.set(datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc))
    assert log.rotate_if_due() is False
    clock.set(datetime(2026, 11, 1, 0, 5, tzinfo=timezone.utc))
    assert log.rotate_if_due() is True
    assert len(log.rotated_files()) == 1
    assert log.rotate_if_due() is False
    events = [r["event"] for r in log.records()]
    assert events == ["a", "audit_rotated"]
    assert log.records()[1]["reason"] == "month"
    assert log.verify() == (True, None)


def test_rotated_head_survives_a_restart_with_an_empty_live_file(tmp_path: Path, clock: FakeClock) -> None:
    log = _log(tmp_path, clock, max_bytes=300)
    for i in range(6):
        clock.advance(seconds=1)
        log.emit("tick", n=i)
    newest_rotated = json.loads(_lines(log.rotated_files()[-1])[-1])
    seq, head = newest_rotated["seq"], newest_rotated["h"]
    log.path.unlink()  # the live file vanished; the rotated ones remain
    reopened = _log(tmp_path, clock, max_bytes=300)
    assert reopened.head() == (seq, head)
    assert reopened.emit("after")["seq"] == seq + 1
    assert reopened.verify() == (True, None)


def test_prune_deletes_only_rotated_files_older_than_keep_days(tmp_path: Path, clock: FakeClock) -> None:
    log = _log(tmp_path, clock, max_bytes=300, keep_days=180)
    for i in range(4):
        log.emit("tick", n=i)
    old_count = len(log.rotated_files())
    assert old_count >= 1
    clock.advance(days=100)
    for i in range(4):
        log.emit("tick", n=i)
    clock.advance(days=100)  # the first batch is now 200 days old, the second 100
    removed = log.prune()
    assert len(removed) == old_count
    assert log.rotated_files() and log.path.exists()
    # The remaining files still verify: the oldest survivor becomes the anchor.
    assert log.verify() == (True, None)


def test_records_filter_and_read_bom_files(tmp_path: Path, clock: FakeClock) -> None:
    log = _log(tmp_path, clock)
    log.emit("keep")
    clock.advance(hours=2)
    log.emit("drop")
    clock.advance(hours=2)
    log.emit("keep")
    assert [r["seq"] for r in log.records(events=["keep"])] == [1, 3]
    assert [r["seq"] for r in log.records(since=clock.now - timedelta(hours=3))] == [2, 3]
    assert [r["seq"] for r in log.records(since=(clock.now - timedelta(hours=1)).isoformat())] == [3]

    # A file saved by a tool that adds a UTF-8 BOM is still readable and still verifies.
    log.path.write_bytes(b"\xef\xbb\xbf" + log.path.read_bytes())
    assert len(log.records()) == 3
    assert log.verify() == (True, None)


def test_cost_on_sums_claude_calls_by_local_date(tmp_path: Path, clock: FakeClock) -> None:
    clock.set(datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc))
    log = _log(tmp_path, clock)
    log.emit("claude_call", call_id="a", total_cost_usd=0.0123)
    log.emit("claude_call", call_id="b", total_cost_usd=0.02)
    log.emit("claude_intent", call_id="a", reserved_usd=0.5)  # not a settled cost
    log.emit("claude_call", call_id="c", total_cost_usd="n/a")  # ignored, not a number
    clock.advance(hours=48)
    log.emit("claude_call", call_id="d", total_cost_usd=0.5)

    day_one = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc).astimezone().date()
    day_three = clock.now.astimezone().date()
    assert log.cost_on(day_one) == pytest.approx(0.0323)
    assert log.cost_on(day_one.isoformat()) == pytest.approx(0.0323)
    assert log.cost_on(day_three) == pytest.approx(0.5)
    assert log.cost_on(day_one + timedelta(days=1)) == 0.0
    # Explicit zone: the same instants fall on the UTC date.
    assert log.cost_on(datetime(2026, 10, 6).date(), tz=timezone.utc) == pytest.approx(0.0323)
