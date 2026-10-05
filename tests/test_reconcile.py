"""scheduler.reconcile: the idempotent "should today's digest exist yet" decision."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeClock

from jarvisd.audit import AuditLog
from jarvisd.common import iso
from jarvisd.config import Config
from jarvisd.jobstore import JobStore
from jarvisd.models import Job
from jarvisd.scheduler import due_at, reconcile
from jarvisd.state import StateStore

# The "local" zone of these tests. reconcile reads the local date from the zone of the
# datetime it is given, so the table does not depend on the machine's own time zone.
LOCAL = timezone(timedelta(hours=2))


def at(hour: int, minute: int = 0, day: int = 6) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=LOCAL)


@dataclass
class Env:
    cfg: Config
    clock: FakeClock
    state: StateStore
    store: JobStore
    audit: AuditLog
    recent: Path

    def run(self, now: datetime) -> str | None:
        self.clock.set(now)
        return reconcile(now, self.cfg, self.state, self.store, self.audit)

    def freshen_recent(self, when: datetime) -> None:
        stamp = when.timestamp()
        os.utime(self.recent, (stamp, stamp))


@pytest.fixture
def env(tmp_cfg: Config, tmp_vault: Path, clock: FakeClock, tmp_path: Path) -> Env:
    clock.set(at(0))
    audit = AuditLog(tmp_path / "jarvis" / "logs" / "audit.jsonl", clock=clock, mirror_stdout=False)
    state = StateStore.from_config(tmp_cfg, clock=clock, tz=LOCAL)
    store = JobStore.from_config(tmp_cfg, clock=clock, audit=audit)
    e = Env(tmp_cfg, clock, state, store, audit, tmp_vault / "RECENT.md")
    e.freshen_recent(at(3, 30))  # the nightly rebuild already ran today
    return e


def _pending(env: Env) -> list[str]:
    return sorted(p.stem for p in (env.cfg.paths.queue / "pending").glob("*.json"))


def _old_digest(env: Env, day: int) -> Job:
    created = at(6, 31, day)
    job = Job(
        id=f"digest-2026-10-{day:02d}", kind="morning_digest", key=f"2026-10-{day:02d}",
        job_class="observe_only", latency_class="background_batch",
        created_at=iso(created), not_before=iso(created),
    )
    assert env.store.enqueue(job)
    return job


# --- the headline test -----------------------------------------------------------------


def test_thousand_calls_two_threads_one_job(env: Env) -> None:
    now = at(6, 31)
    env.clock.set(now)
    barrier = threading.Barrier(2)
    winners: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(500):
                got = reconcile(now, env.cfg, env.state, env.store, env.audit)
                if got is not None:
                    with lock:
                        winners.append(got)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert winners == ["digest-2026-10-06"]
    assert _pending(env) == ["digest-2026-10-06"]
    assert env.store.counts()["pending"] == 1


# --- the table -------------------------------------------------------------------------


def test_before_due_time_nothing(env: Env) -> None:
    assert env.run(at(6, 29)) is None
    assert env.run(at(0, 5)) is None
    assert _pending(env) == []


def test_at_0631_one_job_with_the_documented_shape(env: Env) -> None:
    assert env.run(at(6, 31)) == "digest-2026-10-06"
    job = env.store.get("digest-2026-10-06")
    assert job is not None
    assert (job.kind, job.key, job.job_class, job.latency_class) == (
        "morning_digest", "2026-10-06", "observe_only", "background_batch")
    assert job.origin == "schedule" and job.state == "pending"
    assert job.created_at == iso(at(6, 31)) and job.not_before == iso(at(6, 31))
    assert job.deadline == iso(at(6, 31) + timedelta(hours=3))
    assert job.max_attempts == 3 and job.attempts == 0
    assert job.config_sha256 == env.cfg.sha256
    assert job.window is not None and job.window.end == iso(at(6, 31))
    assert [(h.from_state, h.to, h.note) for h in job.history] == [(None, "pending", "reconcile")]
    assert [r["job_id"] for r in env.audit.records(events=["job_enqueued"])] == ["digest-2026-10-06"]


def test_exactly_at_the_due_minute_counts_as_due(env: Env) -> None:
    assert env.run(at(6, 30)) == "digest-2026-10-06"


def test_already_present_in_any_state_none(env: Env) -> None:
    assert env.run(at(6, 31)) == "digest-2026-10-06"
    assert env.run(at(6, 32)) is None  # pending
    claimed = env.store.claim_next(at(6, 33))
    assert claimed is not None
    assert env.run(at(6, 34)) is None  # running
    env.store.complete(claimed, {"status": "complete"})
    assert env.run(at(9, 0)) is None  # done
    assert env.run(at(23, 59)) is None
    assert _pending(env) == []


def test_a_failed_digest_is_not_silently_recreated(env: Env) -> None:
    env.run(at(6, 31))
    job = env.store.claim_next(at(6, 32))
    assert job is not None
    env.store.fail(job, "boom")
    assert env.run(at(7, 0)) is None


def test_clock_jump_from_2300_to_0940_one_catchup(env: Env) -> None:
    assert env.run(at(6, 31, day=5)) == "digest-2026-10-05"  # yesterday ran on time
    yesterday = env.store.claim_next(at(6, 32, day=5))
    assert yesterday is not None
    env.store.complete(yesterday, {"status": "complete"})
    assert env.run(at(23, 0, day=5)) is None  # evening: today's job already exists
    assert env.run(at(9, 40)) == "digest-2026-10-06"
    job = env.store.get("digest-2026-10-06")
    assert job is not None and job.origin == "catchup"
    assert env.run(at(9, 41)) is None


def test_fifteen_minutes_late_is_still_a_scheduled_run(env: Env) -> None:
    assert env.run(at(6, 45)) == "digest-2026-10-06"
    job = env.store.get("digest-2026-10-06")
    assert job is not None and job.origin == "schedule"


def test_sixteen_minutes_late_is_a_catchup(env: Env) -> None:
    assert env.run(at(6, 46)) == "digest-2026-10-06"
    job = env.store.get("digest-2026-10-06")
    assert job is not None and job.origin == "catchup"


def test_three_day_gap_one_job_and_older_pending_are_coalesced(env: Env) -> None:
    _old_digest(env, 3)
    _old_digest(env, 4)
    assert env.run(at(8, 15)) == "digest-2026-10-06"
    assert _pending(env) == ["digest-2026-10-06"]
    for day in (3, 4):
        old = env.store.get(f"digest-2026-10-{day:02d}")
        assert old is not None
        assert old.state == "failed"
        assert old.last_error == "coalesced_into:digest-2026-10-06"
    assert (env.cfg.paths.queue / "failed" / "digest-2026-10-03.json").exists()
    assert env.store.counts()["pending"] == 1


def test_coalescing_leaves_todays_own_pending_job_alone(env: Env) -> None:
    env.run(at(6, 31))
    assert env.run(at(6, 40)) is None
    assert _pending(env) == ["digest-2026-10-06"]


def test_stale_recent_waits_until_due_plus_ninety_minutes(env: Env) -> None:
    env.freshen_recent(at(22, 0, day=5))  # yesterday evening: the nightly has not run yet
    assert env.run(at(6, 31)) is None
    assert env.run(at(7, 59)) is None
    assert _pending(env) == []
    assert env.run(at(8, 0)) == "digest-2026-10-06"  # due + 90 min: run anyway


def test_fresh_recent_does_not_wait(env: Env) -> None:
    env.freshen_recent(at(0, 1))
    assert env.run(at(6, 31)) == "digest-2026-10-06"


def test_missing_recent_does_not_block(env: Env) -> None:
    env.recent.unlink()
    assert env.run(at(6, 31)) == "digest-2026-10-06"


def test_paused_enqueues_nothing_until_resumed(env: Env) -> None:
    env.state.set_pause(reason="owner")
    assert env.run(at(6, 31)) is None
    assert _pending(env) == []
    env.state.clear_pause()
    assert env.run(at(6, 32)) == "digest-2026-10-06"


def test_killed_enqueues_nothing(env: Env) -> None:
    (env.state.dir / "KILL").write_text("", encoding="utf-8")
    assert env.run(at(6, 31)) is None


def test_time_zone_change_mid_day_never_double_fires_one_date(env: Env) -> None:
    zone_east = timezone(timedelta(hours=2))
    zone_west = timezone(timedelta(hours=1))
    first = datetime(2026, 10, 6, 6, 31, tzinfo=zone_east)
    assert env.run(first) == "digest-2026-10-06"
    # Same instant and later instants seen from the other zone: same date, same job.
    for offset in (0, 10, 120, 600):
        later = (first + timedelta(minutes=offset)).astimezone(zone_west)
        assert env.run(later) is None
    # Evening in the eastern zone is still the same date in the western one; the id is the date, not the instant.
    assert env.run(datetime(2026, 10, 6, 23, 30, tzinfo=zone_east).astimezone(zone_west)) is None
    assert env.store.counts()["pending"] == 1
    assert sorted(p.stem for p in (env.cfg.paths.queue).glob("*/*.json")) == ["digest-2026-10-06"]


def test_zone_change_to_an_earlier_zone_waits_for_that_zones_due_time(env: Env) -> None:
    # 06:10 in the eastern zone is 05:10 in the western one: not due in either zone, no job in either.
    assert env.run(datetime(2026, 10, 6, 6, 10, tzinfo=timezone(timedelta(hours=2)))) is None
    assert env.run(datetime(2026, 10, 6, 5, 25, tzinfo=timezone(timedelta(hours=1)))) is None
    assert _pending(env) == []


# --- windows ---------------------------------------------------------------------------


def test_first_window_uses_the_default_hours(env: Env) -> None:
    env.run(at(6, 31))
    job = env.store.get("digest-2026-10-06")
    assert job is not None and job.window is not None
    assert job.window.start == iso(at(6, 31) - timedelta(hours=env.cfg.digest.window_hours_default))


def test_window_starts_at_the_watermark(env: Env) -> None:
    mark = at(6, 31, day=5)
    env.state.watermark.advance(mark, "digest-2026-10-05")
    env.run(at(6, 31))
    job = env.store.get("digest-2026-10-06")
    assert job is not None and job.window is not None
    assert job.window.start == iso(mark)


def test_window_is_capped_at_the_maximum(env: Env) -> None:
    env.state.watermark.advance(at(6, 0, day=1), "digest-2026-10-01")
    env.run(at(6, 31))
    job = env.store.get("digest-2026-10-06")
    assert job is not None and job.window is not None
    assert job.window.start == iso(at(6, 31) - timedelta(hours=env.cfg.digest.window_hours_max))


def test_a_watermark_in_the_future_never_makes_an_inverted_window(env: Env) -> None:
    env.state.watermark.advance(at(12, 0, day=9), "digest-future")
    env.run(at(6, 31))
    job = env.store.get("digest-2026-10-06")
    assert job is not None and job.window is not None
    assert job.window.start <= job.window.end


# --- due_at ----------------------------------------------------------------------------


def test_due_at_uses_the_configured_time(env: Env) -> None:
    assert due_at(env.cfg, date(2026, 10, 6), LOCAL) == at(6, 30)
    cfg: Any = env.cfg.model_copy(deep=True)
    cfg.digest.run_at = "07:05"
    assert due_at(cfg, date(2026, 10, 6), LOCAL) == at(7, 5)


def test_due_at_defaults_to_the_machine_zone(env: Env) -> None:
    got = due_at(env.cfg, date(2026, 10, 6))
    assert got.tzinfo is not None
    assert (got.hour, got.minute) == (6, 30)
    assert got.date() == date(2026, 10, 6)
