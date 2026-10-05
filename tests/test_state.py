"""state: budget ledger, breaker, watermark, heartbeat, KILL/PAUSE, single-instance lock."""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import FakeClock

from jarvisd import ROOT
from jarvisd.state import AlreadyRunning, BudgetRefused, StateStore


def _store(tmp_path: Path, clock: FakeClock, **kw: object) -> StateStore:
    kw.setdefault("tz", timezone.utc)
    return StateStore(tmp_path / "state", clock=clock, **kw)  # type: ignore[arg-type]


# --- budget ----------------------------------------------------------------------------


def test_reservation_survives_restart(tmp_path: Path, clock: FakeClock) -> None:
    first = _store(tmp_path, clock, daily_budget_usd=2.0, daily_calls=6)
    first.budget.reserve("digest", 0.5)
    del first  # crash simulation: nothing is flushed or closed on purpose

    second = _store(tmp_path, clock, daily_budget_usd=2.0, daily_calls=6)
    snap = second.budget.snapshot()
    assert snap["reserved_usd"] == pytest.approx(0.5)
    assert snap["calls"] == 1
    # The surviving reservation counts against the cap: 0.5 + 1.6 > 2.0.
    with pytest.raises(BudgetRefused) as err:
        second.budget.reserve("digest", 1.6)
    assert err.value.reason == "usd"
    second.budget.reserve("digest", 1.5)  # exactly at the cap is allowed


def test_budget_refuses_at_usd_cap(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock, daily_budget_usd=1.0, daily_calls=10)
    store.budget.reserve("a", 0.5)
    store.budget.reserve("b", 0.5)
    with pytest.raises(BudgetRefused) as err:
        store.budget.reserve("c", 0.01)
    assert err.value.reason == "usd"
    assert store.budget.snapshot()["calls"] == 2  # a refusal consumes nothing


def test_budget_refuses_at_call_cap(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock, daily_budget_usd=100.0, daily_calls=2)
    store.budget.reserve("a", 0.1)
    store.budget.reserve("b", 0.1)
    with pytest.raises(BudgetRefused) as err:
        store.budget.reserve("c", 0.1)
    assert err.value.reason == "calls"


def test_zero_call_cap_refuses_everything(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock, daily_calls=0)
    with pytest.raises(BudgetRefused):
        store.budget.reserve("a", 0.1)


def test_settle_replaces_the_reservation(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock, daily_budget_usd=2.0)
    res = store.budget.reserve("digest", 0.5)
    store.budget.settle(res, 0.04)
    snap = store.budget.snapshot()
    assert snap["reserved_usd"] == 0
    assert snap["spent_usd"] == pytest.approx(0.04)
    assert snap["calls"] == 1
    assert snap["by_purpose"] == {"digest": pytest.approx(0.04)}
    # The freed headroom is usable again.
    store.budget.reserve("digest", 1.9)


def test_release_gives_back_money_and_the_call(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock, daily_budget_usd=1.0, daily_calls=1)
    res = store.budget.reserve("digest", 0.9)
    store.budget.release(res)
    snap = store.budget.snapshot()
    assert (snap["reserved_usd"], snap["calls"]) == (0, 0)
    store.budget.reserve("digest", 0.9)


def test_rollover_resets_at_the_local_date(tmp_path: Path, clock: FakeClock) -> None:
    local = timezone(timedelta(hours=2))
    # 21:59Z is 23:59 local, 22:01Z is 00:01 on the next local date.
    clock.set(datetime(2026, 10, 6, 21, 59, tzinfo=timezone.utc))
    store = _store(tmp_path, clock, tz=local, daily_budget_usd=1.0, daily_calls=1)
    res = store.budget.reserve("digest", 0.5)
    store.budget.settle(res, 0.5)
    assert store.budget.snapshot()["date"] == "2026-10-06"
    with pytest.raises(BudgetRefused):
        store.budget.reserve("digest", 0.1)

    clock.advance(minutes=2)
    snap = store.budget.snapshot()
    assert snap["date"] == "2026-10-07"
    assert (snap["spent_usd"], snap["calls"]) == (0, 0)
    store.budget.reserve("digest", 0.5)
    assert _store(tmp_path, clock, tz=local).budget.snapshot()["date"] == "2026-10-07"


def test_settle_after_rollover_still_counts_the_spend(tmp_path: Path, clock: FakeClock) -> None:
    clock.set(datetime(2026, 10, 6, 23, 59, tzinfo=timezone.utc))
    store = _store(tmp_path, clock)
    res = store.budget.reserve("digest", 0.5)
    clock.advance(minutes=5)
    store.budget.settle(res, 0.2)
    assert store.budget.snapshot()["spent_usd"] == pytest.approx(0.2)


def test_corrupt_ledger_fails_closed_for_the_day(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock, daily_budget_usd=2.0)
    store.budget.reserve("digest", 0.1)
    (tmp_path / "state" / "budget.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(BudgetRefused):
        _store(tmp_path, clock, daily_budget_usd=2.0).budget.reserve("digest", 0.1)


def test_reserve_rejects_nonsense_amounts(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            store.budget.reserve("x", bad)


# --- breaker ---------------------------------------------------------------------------


def test_breaker_opens_after_three_failures_and_persists(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    assert store.breaker.is_open() is False
    assert store.breaker.record_failure("timeout") is False
    assert store.breaker.record_failure("timeout") is False
    assert store.breaker.is_open() is False
    assert store.breaker.record_failure("timeout") is True
    assert store.breaker.is_open() is True
    assert _store(tmp_path, clock).breaker.is_open() is True


def test_success_resets_the_failure_count(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.breaker.record_failure("x")
    store.breaker.record_failure("x")
    store.breaker.record_success()
    store.breaker.record_failure("x")
    store.breaker.record_failure("x")
    assert store.breaker.is_open() is False


def test_breaker_cooldown_then_one_probe(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    for _ in range(3):
        store.breaker.record_failure("429")
    clock.advance(minutes=59)
    assert store.breaker.is_open() is True
    clock.advance(minutes=2)
    assert store.breaker.is_open() is False  # the one half-open probe
    assert store.breaker.is_open() is True  # nobody else gets in while it runs
    assert store.breaker.peek()["state"] == "half_open"
    store.breaker.record_success()
    assert store.breaker.is_open() is False
    assert store.breaker.peek()["state"] == "closed"


def test_failed_probe_reopens_for_a_full_cooldown(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    for _ in range(3):
        store.breaker.record_failure("429")
    clock.advance(minutes=61)
    assert store.breaker.is_open() is False
    store.breaker.record_failure("429")
    assert store.breaker.is_open() is True
    clock.advance(minutes=59)
    assert store.breaker.is_open() is True
    clock.advance(minutes=2)
    assert store.breaker.is_open() is False


def test_abandoned_probe_does_not_wedge_the_breaker(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    for _ in range(3):
        store.breaker.record_failure("x")
    clock.advance(minutes=61)
    assert store.breaker.is_open() is False  # probe taken, never reported (crash)
    clock.advance(minutes=61)
    assert store.breaker.is_open() is False  # a new probe is allowed


def test_trip_requires_reset_when_asked(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.breaker.trip("isolation_breach", requires_reset=True)
    clock.advance(hours=48)
    assert store.breaker.is_open() is True
    store.breaker.record_success()  # a stray success cannot clear a human-reset breaker
    assert store.breaker.is_open() is True
    assert store.breaker.peek()["requires_human_reset"] is True
    assert _store(tmp_path, clock).breaker.is_open() is True
    store.breaker.reset("owner checked")
    assert store.breaker.is_open() is False
    assert store.breaker.peek()["state"] == "closed"


def test_trip_without_reset_honours_the_cooldown(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.breaker.trip("rate_limit")
    assert store.breaker.is_open() is True
    clock.advance(minutes=61)
    assert store.breaker.is_open() is False


# --- watermark -------------------------------------------------------------------------


def test_watermark_only_moves_forward(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    assert store.watermark.get() is None
    t1 = datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc)
    t2 = t1 + timedelta(days=1)
    assert store.watermark.advance(t1, "digest-2026-10-05") is True
    assert store.watermark.advance(t2, "digest-2026-10-06") is True
    assert store.watermark.advance(t1, "digest-old") is False
    assert store.watermark.advance(t2, "digest-same") is False
    assert _store(tmp_path, clock).watermark.get() == t2
    assert store.watermark.record()["job_id"] == "digest-2026-10-06"


def test_watermark_rejects_naive_times(tmp_path: Path, clock: FakeClock) -> None:
    with pytest.raises(ValueError):
        _store(tmp_path, clock).watermark.advance(datetime(2026, 10, 5, 4, 30), "x")


def test_corrupt_watermark_reads_as_absent(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.watermark.advance(clock(), "x")
    (tmp_path / "state" / "watermark.json").write_text("garbage", encoding="utf-8")
    assert store.watermark.get() is None


# --- heartbeat, exit marker, KILL, PAUSE -----------------------------------------------


def test_heartbeat_and_previous_exit(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    assert store.previous_exit() == "first"
    assert store.previous_exit_clean() is True

    store.heartbeat("digest-2026-10-06", mode="task")
    beat = store.read_heartbeat()
    assert beat is not None
    assert beat["job_id"] == "digest-2026-10-06" and beat["mode"] == "task"
    assert beat["ts"] == "2026-10-06T05:31:00+00:00"

    # Heartbeat but no marker: the daemon died without an orderly stop.
    assert _store(tmp_path, clock).previous_exit() == "unclean"
    assert _store(tmp_path, clock).previous_exit_clean() is False

    store.mark_clean_shutdown()
    assert _store(tmp_path, clock).previous_exit() == "clean"
    assert _store(tmp_path, clock).previous_exit_clean() is True

    # A new run's first heartbeat removes the marker: the marker only spans stop to start.
    store.heartbeat()
    assert not (tmp_path / "state" / "clean_shutdown").exists()
    assert _store(tmp_path, clock).previous_exit() == "unclean"


def test_heartbeat_is_atomic_and_leaves_no_temp_files(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    for i in range(20):
        store.heartbeat(f"job-{i}")
        store.budget.snapshot()
    store.breaker.trip("x")
    store.watermark.advance(clock(), "j")
    leftovers = [p.name for p in (tmp_path / "state").iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_kill_file(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    assert store.killed() is False
    (tmp_path / "state" / "KILL").write_text("", encoding="utf-8")
    assert store.killed() is True


def test_pause_file_with_expiry(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    assert store.paused() is False
    store.set_pause(until=clock() + timedelta(hours=1), reason="owner")
    assert store.paused() is True
    assert store.pause_info()["reason"] == "owner"
    clock.advance(minutes=61)
    assert store.paused() is False  # expired pause no longer blocks
    store.set_pause()
    clock.advance(days=30)
    assert store.paused() is True  # no end time means until resumed
    store.clear_pause()
    assert store.paused() is False


def test_unreadable_pause_file_pauses(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    (tmp_path / "state" / "PAUSE").write_text("{broken", encoding="utf-8")
    assert store.paused() is True


# --- daemon lock -----------------------------------------------------------------------

_HOLDER = """
import sys, time
from datetime import timezone
from pathlib import Path
from jarvisd.state import StateStore
s = StateStore(Path(sys.argv[1]), tz=timezone.utc)
s.acquire_daemon_lock()
print("locked", flush=True)
time.sleep(60)
"""


def test_second_lock_in_same_process_raises(tmp_path: Path, clock: FakeClock) -> None:
    first = _store(tmp_path, clock)
    first.acquire_daemon_lock()
    try:
        with pytest.raises(AlreadyRunning):
            _store(tmp_path, clock).acquire_daemon_lock()
        with pytest.raises(AlreadyRunning):
            first.acquire_daemon_lock()
    finally:
        first.release_daemon_lock()
    _store(tmp_path, clock).acquire_daemon_lock()


def test_lock_in_another_process_raises_and_frees_when_holder_dies(tmp_path: Path, clock: FakeClock) -> None:
    state_dir = tmp_path / "state"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(state_dir)],
        cwd=ROOT, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(AlreadyRunning):
            _store(tmp_path, clock).acquire_daemon_lock()
    finally:
        holder.kill()
        holder.wait(timeout=10)
        if holder.stdout:
            holder.stdout.close()
    deadline = time.monotonic() + 5
    store = _store(tmp_path, clock)
    while True:
        try:
            store.acquire_daemon_lock()
            break
        except AlreadyRunning:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    store.release_daemon_lock()


def test_from_config_uses_the_configured_caps(tmp_cfg: object, clock: FakeClock) -> None:
    store = StateStore.from_config(tmp_cfg, clock=clock, tz=timezone.utc)  # type: ignore[arg-type]
    assert store.dir == tmp_cfg.daemon.state_dir  # type: ignore[attr-defined]
    snap = store.budget.snapshot()
    assert snap["daily_budget_usd"] == tmp_cfg.claude.daily_budget_usd  # type: ignore[attr-defined]
    assert snap["daily_calls"] == tmp_cfg.claude.daily_calls  # type: ignore[attr-defined]


def test_blocked_is_a_pure_read_that_never_hands_out_the_probe(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    for _ in range(3):
        store.breaker.record_failure("429")
    assert store.breaker.blocked() is True
    clock.advance(minutes=61)
    assert store.breaker.blocked() is False
    assert store.breaker.peek()["state"] == "open"  # still open: nothing was claimed
    assert store.breaker.is_open() is False  # the probe is still there to take
    assert store.breaker.blocked() is True  # and now it is taken


def test_blocked_is_true_for_a_human_reset(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    store.breaker.trip("isolation_breach", requires_reset=True)
    clock.advance(hours=48)
    assert store.breaker.blocked() is True


def test_released_probe_can_be_taken_again(tmp_path: Path, clock: FakeClock) -> None:
    store = _store(tmp_path, clock)
    for _ in range(3):
        store.breaker.record_failure("429")
    clock.advance(minutes=61)
    assert store.breaker.is_open() is False
    store.breaker.release_probe()
    assert store.breaker.is_open() is False  # not another hour of waiting
    store.breaker.record_success()
    assert store.breaker.peek()["state"] == "closed"
    store.breaker.release_probe()  # a no-op when there is no probe out
    assert store.breaker.peek()["state"] == "closed"
