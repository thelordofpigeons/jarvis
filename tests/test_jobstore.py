"""jobstore: O_EXCL enqueue, atomic state moves, held references, recovery, pruning."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock

from jarvisd.audit import AuditLog
from jarvisd.common import iso
from jarvisd.jobstore import STATES, JobStore, UnsafeRoot
from jarvisd.models import Job, WithheldItem

UTC = timezone.utc


def _job(job_id: str = "digest-2026-10-06", *, created: datetime | None = None,
         not_before: datetime | None = None, **kw: Any) -> Job:
    created = created or datetime(2026, 10, 6, 4, 31, tzinfo=UTC)
    return Job(
        id=job_id, kind="morning_digest", key=job_id.removeprefix("digest-"),
        job_class="observe_only", latency_class="background_batch",
        created_at=iso(created), not_before=iso(not_before or created), **kw,
    )


def _store(tmp_path: Path, clock: FakeClock, **kw: Any) -> JobStore:
    return JobStore(tmp_path / "queue", clock=clock, **kw)


def _audit(tmp_path: Path, clock: FakeClock) -> AuditLog:
    return AuditLog(tmp_path / "logs" / "audit.jsonl", clock=clock, mirror_stdout=False)


def _dir_of(tmp_path: Path, job_id: str) -> list[str]:
    return [s for s in STATES if (tmp_path / "queue" / s / f"{job_id}.json").exists()]


# --- root safety -----------------------------------------------------------------------


def test_refuses_a_root_under_brain(tmp_path: Path, clock: FakeClock) -> None:
    with pytest.raises(UnsafeRoot):
        JobStore(tmp_path / "brain" / "queue", clock=clock)
    with pytest.raises(UnsafeRoot):
        JobStore(tmp_path / "Brain" / "x" / "queue", clock=clock)


def test_refuses_an_explicit_brain_root_and_stfolder_ancestor(tmp_path: Path, clock: FakeClock) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(UnsafeRoot):
        JobStore(vault / "queue", clock=clock, brain_root=vault)
    synced = tmp_path / "synced"
    (synced / ".stfolder").mkdir(parents=True)
    with pytest.raises(UnsafeRoot):
        JobStore(synced / "deep" / "queue", clock=clock)
    JobStore(tmp_path / "plain" / "queue", clock=clock, brain_root=vault)  # fine


def test_creates_the_five_directories(tmp_path: Path, clock: FakeClock) -> None:
    _store(tmp_path, clock)
    for name in ("pending", "running", "done", "failed", "held"):
        assert (tmp_path / "queue" / name).is_dir()


# --- enqueue ---------------------------------------------------------------------------


def test_enqueue_is_idempotent_across_all_state_dirs(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    assert store.exists("digest-2026-10-06") is None
    assert store.enqueue(_job()) is True
    assert store.exists("digest-2026-10-06") == "pending"
    assert store.enqueue(_job()) is False

    for state in ("running", "done", "failed", "held"):
        job_id = f"digest-in-{state}"
        (tmp_path / "queue" / state / f"{job_id}.json").write_text("{}", encoding="utf-8")
        assert store.exists(job_id) == state
    for state in ("running", "done", "failed"):
        assert store.enqueue(_job(f"digest-in-{state}")) is False
        assert not (tmp_path / "queue" / "pending" / f"digest-in-{state}.json").exists()


def test_enqueue_under_eight_threads_has_exactly_one_winner(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        won = store.enqueue(_job())
        with lock:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False] * 7 + [True]
    assert _dir_of(tmp_path, "digest-2026-10-06") == ["pending"]


def test_two_stores_on_one_root_still_have_one_winner(tmp_path: Path, clock: FakeClock) -> None:
    a, b = _store(tmp_path, clock), _store(tmp_path, clock)
    assert [a.enqueue(_job()), b.enqueue(_job())] == [True, False]


def test_enqueued_file_is_complete_utf8_lf_without_bom(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job())
    raw = (tmp_path / "queue" / "pending" / "digest-2026-10-06.json").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    data = json.loads(raw)
    assert data["class"] == "observe_only" and data["schema"] == 1 and data["state"] == "pending"
    assert store.get("digest-2026-10-06") == _job()
    assert not [p for p in (tmp_path / "queue" / "pending").iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", "", "x" * 200, "con.", ".hidden", "a b"])
def test_unsafe_ids_are_refused(tmp_path: Path, clock: FakeClock, bad: str) -> None:
    store = _store(tmp_path, clock)
    with pytest.raises(ValueError):
        store.exists(bad)
    with pytest.raises(ValueError):
        store.hold(WithheldItem(id=bad, kind="k", source_ref="r", reason="x"), "digest-2026-10-06")


# --- claim, transitions ----------------------------------------------------------------


def test_claim_next_honours_not_before_and_age_order(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    t0 = datetime(2026, 10, 6, 4, 0, tzinfo=UTC)
    store.enqueue(_job("digest-b", created=t0 + timedelta(minutes=10)))
    store.enqueue(_job("digest-a", created=t0))
    store.enqueue(_job("digest-later", created=t0 - timedelta(hours=1), not_before=t0 + timedelta(hours=3)))

    assert store.claim_next(t0 - timedelta(minutes=1)) is None  # nothing is due yet

    first = store.claim_next(t0 + timedelta(hours=1))
    second = store.claim_next(t0 + timedelta(hours=1))
    third = store.claim_next(t0 + timedelta(hours=1))
    assert first is not None and second is not None
    assert (first.id, second.id) == ("digest-a", "digest-b")
    assert third is None  # digest-later is not due yet
    later = store.claim_next(t0 + timedelta(hours=3))
    assert later is not None and later.id == "digest-later"


def test_claim_moves_the_file_and_counts_the_attempt(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job())
    job = store.claim_next(clock())
    assert job is not None
    assert job.state == "running" and job.attempts == 1
    assert _dir_of(tmp_path, job.id) == ["running"]
    on_disk = json.loads((tmp_path / "queue" / "running" / f"{job.id}.json").read_text(encoding="utf-8"))
    assert on_disk["state"] == "running" and on_disk["attempts"] == 1
    assert on_disk["history"][-1]["from"] == "pending" and on_disk["history"][-1]["to"] == "running"


def test_update_persists_in_place(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job())
    job = store.claim_next(clock())
    assert job is not None
    job.cost_usd = 0.04
    job.tier = "claude"
    store.update(job)
    assert _dir_of(tmp_path, job.id) == ["running"]
    again = store.get(job.id)
    assert again is not None and again.cost_usd == 0.04 and again.tier == "claude"


def test_complete_and_fail_move_to_their_directories(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job("digest-ok"))
    store.enqueue(_job("digest-bad"))
    ok = store.claim_next(clock())
    bad = store.claim_next(clock())
    assert ok is not None and bad is not None
    store.complete(ok, {"status": "complete", "claude_calls": 1})
    store.fail(bad, "boom")
    assert _dir_of(tmp_path, ok.id) == ["done"] and _dir_of(tmp_path, bad.id) == ["failed"]
    done = store.get(ok.id)
    failed = store.get(bad.id)
    assert done is not None and done.state == "done" and done.result == {"status": "complete", "claude_calls": 1}
    assert failed is not None and failed.state == "failed" and failed.last_error == "boom"
    assert store.counts() == {"pending": 0, "running": 0, "done": 1, "failed": 1, "held": 0}


def test_retry_goes_back_to_pending_with_a_delay(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job())
    job = store.claim_next(clock())
    assert job is not None
    store.retry(job, "timeout", timedelta(minutes=10))
    assert _dir_of(tmp_path, job.id) == ["pending"]
    back = store.get(job.id)
    assert back is not None
    assert back.state == "pending" and back.attempts == 1 and back.last_error == "timeout"
    assert back.not_before == iso(clock() + timedelta(minutes=10))
    assert store.claim_next(clock()) is None
    again = store.claim_next(clock() + timedelta(minutes=10))
    assert again is not None and again.attempts == 2


def test_retry_can_leave_the_attempt_unconsumed(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job())
    job = store.claim_next(clock())
    assert job is not None
    store.retry(job, "network_not_ready", 600, consume_attempt=False)
    back = store.get(job.id)
    assert back is not None and back.attempts == 0


def test_attempts_at_max_go_to_failed(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job(max_attempts=2))
    for expected in (1, 2):
        job = store.claim_next(clock())
        assert job is not None and job.attempts == expected
        store.retry(job, f"try {expected}", timedelta(seconds=0))
    assert _dir_of(tmp_path, "digest-2026-10-06") == ["failed"]
    final = store.get("digest-2026-10-06")
    assert final is not None and final.last_error is not None and "try 2" in final.last_error
    assert store.claim_next(clock()) is None


def test_pending_job_already_at_max_attempts_is_failed_not_claimed(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job(attempts=3, max_attempts=3))
    store.enqueue(_job("digest-2026-10-07", created=clock() + timedelta(seconds=1)))
    claimed = store.claim_next(clock() + timedelta(minutes=1))
    assert claimed is not None and claimed.id == "digest-2026-10-07"
    assert _dir_of(tmp_path, "digest-2026-10-06") == ["failed"]


def test_every_transition_is_recorded_in_history(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job())
    job = store.claim_next(clock())
    assert job is not None
    store.complete(job, {"status": "complete"})
    done = store.get(job.id)
    assert done is not None
    assert [(h.from_state, h.to) for h in done.history] == [("pending", "running"), ("running", "done")]


# --- recovery --------------------------------------------------------------------------


def test_recover_running_preserves_attempts(tmp_path: Path, clock: FakeClock) -> None:
    audit = _audit(tmp_path, clock)
    store = _store(tmp_path, clock, audit=audit)
    store.enqueue(_job())
    claimed = store.claim_next(clock())
    assert claimed is not None and claimed.attempts == 1

    fresh = _store(tmp_path, clock, audit=audit)  # the restarted daemon
    assert fresh.recover_running() == ["digest-2026-10-06"]
    back = fresh.get("digest-2026-10-06")
    assert back is not None and back.state == "pending" and back.attempts == 1
    assert _dir_of(tmp_path, back.id) == ["pending"]
    assert [r["job_id"] for r in audit.records(events=["job_recover"])] == ["digest-2026-10-06"]
    again = fresh.claim_next(clock())
    assert again is not None and again.attempts == 2


def test_recover_running_fails_a_job_out_of_attempts(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job(max_attempts=1))
    store.claim_next(clock())
    assert store.recover_running() == []
    assert _dir_of(tmp_path, "digest-2026-10-06") == ["failed"]
    job = store.get("digest-2026-10-06")
    assert job is not None and job.last_error is not None and "attempts" in job.last_error


# --- corruption and inconsistency ------------------------------------------------------


def test_corrupt_job_file_goes_to_failed_with_a_readable_error(tmp_path: Path, clock: FakeClock) -> None:
    audit = _audit(tmp_path, clock)
    store = _store(tmp_path, clock, audit=audit)
    (tmp_path / "queue" / "pending" / "digest-broken.json").write_bytes(b'{"id": "digest-brok')
    (tmp_path / "queue" / "pending" / "digest-wrong-shape.json").write_text('{"id": 5}', encoding="utf-8")
    store.enqueue(_job("digest-good", created=clock() + timedelta(seconds=5)))

    claimed = store.claim_next(clock() + timedelta(minutes=1))  # must not raise
    assert claimed is not None and claimed.id == "digest-good"
    for name in ("digest-broken", "digest-wrong-shape"):
        assert _dir_of(tmp_path, name) == ["failed"]
        job = store.get(name)
        assert job is not None and job.state == "failed"
        assert job.last_error is not None and job.last_error.startswith("corrupt_job_file")
    assert len(audit.records(events=["job_failed"])) == 2
    assert store.counts()["pending"] == 0


def test_mismatch_between_directory_and_state_field_is_audited(tmp_path: Path, clock: FakeClock) -> None:
    audit = _audit(tmp_path, clock)
    store = _store(tmp_path, clock, audit=audit)
    store.enqueue(_job())
    path = tmp_path / "queue" / "pending" / "digest-2026-10-06.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["state"] = "running"
    path.write_text(json.dumps(data), encoding="utf-8")

    job = store.get("digest-2026-10-06")
    assert job is not None and job.state == "pending"  # the directory wins
    events = audit.records(events=["queue_inconsistent"])
    assert len(events) == 1
    assert events[0]["job_id"] == "digest-2026-10-06"
    assert events[0]["directory"] == "pending" and events[0]["state_field"] == "running"


# --- held references -------------------------------------------------------------------

ALLOWED_HELD_KEYS = {"schema", "id", "kind", "source_ref", "reason", "first_seen", "last_seen",
                     "expires_at", "digest_ids"}


def _ref(item_id: str = "w-3a9f1c") -> WithheldItem:
    return WithheldItem(id=item_id, kind="brain_session", source_ref="C:/synthetic/brain/sessions/x.md",
                        reason="path_under_sensitive")


def test_held_file_contains_only_the_allowed_keys(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.hold(_ref(), "digest-2026-10-06")
    path = tmp_path / "queue" / "held" / "w-3a9f1c.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == ALLOWED_HELD_KEYS
    assert data["schema"] == 1 and data["digest_ids"] == ["digest-2026-10-06"]
    assert data["first_seen"] == data["last_seen"] == iso(clock())
    assert data["expires_at"] == iso(clock() + timedelta(days=14))
    assert "title" not in data and "text" not in data


def test_hold_merges_digest_ids_and_keeps_first_seen(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.hold(_ref(), "digest-2026-10-06")
    first_seen = iso(clock())
    clock.advance(days=1)
    store.hold(_ref(), "digest-2026-10-07")
    store.hold(_ref(), "digest-2026-10-07")  # repeat in the same digest adds nothing
    data = json.loads((tmp_path / "queue" / "held" / "w-3a9f1c.json").read_text(encoding="utf-8"))
    assert data["digest_ids"] == ["digest-2026-10-06", "digest-2026-10-07"]
    assert data["first_seen"] == first_seen and data["last_seen"] == iso(clock())
    assert data["expires_at"] == iso(clock() + timedelta(days=14))
    assert set(data) == ALLOWED_HELD_KEYS


def test_held_by_date_and_all(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.hold(_ref("w-aaaaaa"), "digest-2026-10-06")
    store.hold(_ref("w-bbbbbb"), "digest-2026-10-07-r2")
    store.hold(_ref("w-cccccc"), "digest-2026-10-06")
    assert [h["id"] for h in store.held()] == ["w-aaaaaa", "w-bbbbbb", "w-cccccc"]
    assert [h["id"] for h in store.held("2026-10-06")] == ["w-aaaaaa", "w-cccccc"]
    assert [h["id"] for h in store.held(datetime(2026, 10, 7).date())] == ["w-bbbbbb"]
    assert store.held("2026-10-09") == []


def test_expire_held_removes_fourteen_day_old_references(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.hold(_ref("w-old000"), "digest-2026-10-06")
    clock.advance(days=10)
    store.hold(_ref("w-new000"), "digest-2026-10-16")
    clock.advance(days=4, minutes=1)  # w-old000 is now 14 days and a minute old
    assert store.expire_held(clock()) == ["w-old000"]
    assert [h["id"] for h in store.held()] == ["w-new000"]
    assert store.expire_held(clock()) == []


def test_a_job_cannot_see_the_held_directory_as_a_job(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.hold(_ref(), "digest-2026-10-06")
    assert store.claim_next(clock() + timedelta(days=1)) is None
    assert store.recover_running() == []
    assert store.counts()["held"] == 1


# --- housekeeping ----------------------------------------------------------------------


def test_prune_removes_old_done_and_failed_jobs_only(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock, done_days=60, failed_days=60)
    store.enqueue(_job("digest-old-done", created=clock()))
    store.enqueue(_job("digest-old-failed", created=clock()))
    old_done = store.claim_next(clock())
    old_failed = store.claim_next(clock())
    assert old_done is not None and old_failed is not None
    store.complete(old_done, {"status": "complete"})
    store.fail(old_failed, "boom")
    clock.advance(days=30)
    store.enqueue(_job("digest-recent", created=clock()))
    store.enqueue(_job("digest-waiting", created=clock() + timedelta(seconds=1)))
    recent = store.claim_next(clock() + timedelta(minutes=1))
    assert recent is not None and recent.id == "digest-recent"
    store.complete(recent, {"status": "complete"})
    clock.advance(days=31)  # old ones are 61 days old, the recent one 31

    removed = store.prune(clock())
    assert sorted(removed) == ["digest-old-done", "digest-old-failed"]
    assert store.exists("digest-recent") == "done"
    assert store.exists("digest-waiting") == "pending"  # only finished jobs are pruned


def test_counts_on_an_empty_store(tmp_path: Path, clock: FakeClock) -> None:
    assert _store(tmp_path, clock).counts() == {"pending": 0, "running": 0, "done": 0, "failed": 0, "held": 0}


def test_from_config_builds_a_store_that_knows_the_vault(tmp_cfg: Any, clock: FakeClock) -> None:
    store = JobStore.from_config(tmp_cfg, clock=clock)
    assert store.root == tmp_cfg.paths.queue
    store.hold(_ref(), "digest-2026-10-06")
    assert store.held()[0]["expires_at"] == iso(clock() + timedelta(days=tmp_cfg.retention.held_days))


# --- leases: a live claimer is not a dead daemon (review fix) ----------------------------


def _claim_with_pid(tmp_path: Path, clock: FakeClock, pid: int, monkeypatch: pytest.MonkeyPatch) -> JobStore:
    """A job claimed by a process that is not this one (the CLI's `run-digest --claude`)."""
    from jarvisd import jobstore

    monkeypatch.setattr(jobstore.os, "getpid", lambda: pid)
    store = _store(tmp_path, clock)
    store.enqueue(_job())
    assert store.claim_next(clock()) is not None
    monkeypatch.undo()
    return store


def test_recover_running_leaves_a_job_whose_claimer_is_alive(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvisd import jobstore

    audit = _audit(tmp_path, clock)
    _claim_with_pid(tmp_path, clock, 424242, monkeypatch)
    monkeypatch.setattr(jobstore, "_pid_alive", lambda pid: pid == 424242)
    daemon_side = _store(tmp_path, clock, audit=audit)  # the daemon starting at logon
    assert daemon_side.recover_running() == []
    assert _dir_of(tmp_path, "digest-2026-10-06") == ["running"]
    skipped = audit.records(events=["job_recover"])
    assert [(r["job_id"], r["outcome"]) for r in skipped] == [("digest-2026-10-06", "skipped_live_owner")]
    assert daemon_side.claim_next(clock()) is None  # nobody can claim a second copy


def test_recover_running_takes_back_a_job_whose_claimer_is_dead(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvisd import jobstore

    _claim_with_pid(tmp_path, clock, 424242, monkeypatch)
    monkeypatch.setattr(jobstore, "_pid_alive", lambda pid: False)
    assert _store(tmp_path, clock).recover_running() == ["digest-2026-10-06"]


def test_a_stale_lease_does_not_protect_a_job_forever(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jarvisd import jobstore

    _claim_with_pid(tmp_path, clock, 424242, monkeypatch)
    monkeypatch.setattr(jobstore, "_pid_alive", lambda pid: True)  # the pid was reused by something else
    clock.advance(hours=7)
    assert _store(tmp_path, clock).recover_running() == ["digest-2026-10-06"]


def test_a_lease_from_this_very_process_is_recoverable(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job())
    store.claim_next(clock())
    assert _store(tmp_path, clock).recover_running() == ["digest-2026-10-06"]


def test_a_job_without_a_lease_file_is_recovered_as_before(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job())
    store.claim_next(clock())
    for lease in (tmp_path / "queue" / "leases").glob("*.json"):
        lease.unlink()
    assert _store(tmp_path, clock).recover_running() == ["digest-2026-10-06"]


def test_leases_are_removed_when_a_job_leaves_running(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.enqueue(_job("digest-2026-10-06"))
    job = store.claim_next(clock())
    assert job is not None and list((tmp_path / "queue" / "leases").glob("*.json"))
    store.complete(job, {"status": "complete"})
    assert not list((tmp_path / "queue" / "leases").glob("*.json"))
    store.enqueue(_job("digest-2026-10-07"))
    again = store.claim_next(clock())
    assert again is not None
    store.retry(again, "x", 0)
    assert not list((tmp_path / "queue" / "leases").glob("*.json"))


def test_pid_alive_knows_this_process_and_a_dead_one() -> None:
    import os
    import subprocess
    import sys

    from jarvisd.jobstore import _pid_alive

    assert _pid_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert _pid_alive(child.pid) is False
    assert _pid_alive(0) is False
