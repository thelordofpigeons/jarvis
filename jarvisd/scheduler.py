"""Scheduling decisions for the morning digest (design section 10).

This file holds the pure part, `due_at` and `reconcile`, and `SchedulerHost`, the thin
APScheduler wrapper that decides WHEN the daemon's callbacks fire. The pure functions start
no thread or timer; only `SchedulerHost.start()` does, and it never decides anything itself.

`reconcile` is idempotent by construction. The job id is `digest-<local date>`, and
`JobStore.enqueue` creates it exclusively, so any number of calls from any number of
threads or processes yield one job per date. "Local date" is the calendar date in the
time zone of the `now` that is passed in, never a global zone lookup: the daemon passes
`local_now()`, tests pass a fixed zone, and a change of time zone changes the date
a call sees but can never make one date fire twice.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from jarvisd.common import iso
from jarvisd.models import HistoryEntry, Job, JobWindow

if TYPE_CHECKING:
    from jarvisd.audit import AuditLog
    from jarvisd.config import Config
    from jarvisd.jobstore import JobStore
    from jarvisd.state import StateStore

DIGEST_KIND = "morning_digest"
CATCHUP_AFTER = timedelta(minutes=15)  # later than this past the due time counts as a catch-up
RECENT_WAIT = timedelta(minutes=90)  # how long to wait for the nightly RECENT.md rebuild
DEADLINE_AFTER = timedelta(hours=3)


def due_at(cfg: "Config", day: date, tz: tzinfo | None = None) -> datetime:
    """When the digest for `day` is due: `[digest].run_at` on that date, as an aware time.

    `tz` None means the machine's local zone.
    """
    hour, minute = cfg.digest.run_at_hm()
    wall = datetime.combine(day, time(hour, minute))
    return wall.replace(tzinfo=tz) if tz is not None else wall.astimezone()


def _recent_is_stale(cfg: "Config", midnight: datetime) -> bool:
    """True when the vault's RECENT.md exists but was last written before today began.

    A missing file is not "stale": there is nothing to wait for, and the digest's brain
    section reports the gap on its own.
    """
    try:
        mtime = (cfg.paths.brain_root / "RECENT.md").stat().st_mtime
    except OSError:
        return False
    return mtime < midnight.timestamp()


def _window(cfg: "Config", state: "StateStore", now: datetime) -> JobWindow:
    """Since the watermark, never longer than the maximum, default length on a first run."""
    watermark = state.watermark.get()
    if watermark is None:
        start = now - timedelta(hours=cfg.digest.window_hours_default)
    else:
        start = max(watermark, now - timedelta(hours=cfg.digest.window_hours_max))
    return JobWindow(start=iso(min(start, now)), end=iso(now))


def reconcile(now: datetime, cfg: "Config", state: "StateStore", store: "JobStore",
              audit: "AuditLog | None") -> str | None:
    """Enqueue today's digest if it is due and absent. Returns the new job id, else None.

    Design section 10, steps 2 to 7 (the kill check, step 1, belongs to the daemon tick;
    KILL is honoured here too so a stray call cannot enqueue under it).
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("reconcile needs a timezone-aware 'now'")
    if state.killed() or state.paused():
        return None

    today = now.date()
    due = due_at(cfg, today, now.tzinfo)
    if now < due:
        return None

    job_id = f"digest-{today.isoformat()}"
    if store.exists(job_id) is not None:
        return None  # in any state directory: the entire duplicate guard, with O_EXCL below

    midnight = datetime.combine(today, time(0, 0), tzinfo=now.tzinfo)
    if now < due + RECENT_WAIT and _recent_is_stale(cfg, midnight):
        return None  # wait for the nightly rebuild; after the wait the digest states the age

    origin = "catchup" if now - due > CATCHUP_AFTER else "schedule"
    created = iso(now)
    job = Job(
        id=job_id, kind=DIGEST_KIND, key=today.isoformat(), job_class="observe_only",
        latency_class="background_batch", origin=origin,
        created_at=created, not_before=created, deadline=iso(now + DEADLINE_AFTER),
        window=_window(cfg, state, now), config_sha256=cfg.sha256 or None,
        history=[HistoryEntry(ts=created, from_state=None, to="pending", note="reconcile")],
    )
    if not store.enqueue(job):
        return None  # another caller won the race between exists() and enqueue()

    if audit is not None:
        audit.emit("job_enqueued", job_id=job_id, kind=DIGEST_KIND, origin=origin,
                   key=job.key, window_start=job.window.start if job.window else None,
                   window_end=job.window.end if job.window else None)
    # A multi-day gap leaves older pending digests; one fresh job covers their window.
    store.coalesce_older(DIGEST_KIND, job.key, job_id)
    return job_id


# --- the APScheduler host ----------------------------------------------------------------

HOUSEKEEPING_AT = (4, 10)  # local time, after the nightly brain jobs and long before the digest
DISK_CHECK_AT = (1, 9, 0)  # day of month, hour, minute
MISFIRE_GRACE_S = 3600  # a wake from sleep inside the hour still fires the missed run


class SchedulerHost:
    """Fires the daemon's callbacks on the design section 10 schedule.

    Four jobs, all calling idempotent functions, so a double fire or a missed fire costs
    nothing: `tick` every `[daemon].tick_seconds` (first run at once), `digest_cron` at
    `[digest].run_at` (the same callback, so the digest is not left waiting for the next
    tick), `housekeeping` daily at 04:10 and `disk_check` on day 1 at 09:00. Every job runs
    with coalesce on and one instance at a time. A callback that raises is reported to
    `on_error(name, exc)` and never takes the scheduler down.

    Limits: MemoryJobStore only, by design (a restart re-registers the same four jobs); the
    thread pool has two workers, so a long digest run never blocks housekeeping.
    """

    def __init__(self, cfg: "Config", *, on_tick: Callable[[], object], on_housekeeping: Callable[[], object],
                 on_disk_check: Callable[[], object],
                 on_error: Callable[[str, BaseException], None] | None = None,
                 tz: tzinfo | None = None) -> None:
        if tz is None:
            import tzlocal  # a dependency of APScheduler 3.x, so always present with it

            tz = tzlocal.get_localzone()
        self.cfg = cfg
        self.tz: tzinfo = tz
        self._on_error = on_error
        self._described: dict[str, str] = {}
        self._scheduler = BackgroundScheduler(
            timezone=tz,
            executors={"default": ThreadPoolExecutor(2)},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": MISFIRE_GRACE_S},
        )
        hour, minute = cfg.digest.run_at_hm()
        seconds = cfg.daemon.tick_seconds
        self._add("tick", on_tick, IntervalTrigger(seconds=seconds, timezone=tz), f"interval every {seconds}s",
                  next_run_time=datetime.now(tz))
        self._add("digest_cron", on_tick, CronTrigger(hour=hour, minute=minute, timezone=tz))
        self._add("housekeeping", on_housekeeping,
                  CronTrigger(hour=HOUSEKEEPING_AT[0], minute=HOUSEKEEPING_AT[1], timezone=tz))
        self._add("disk_check", on_disk_check,
                  CronTrigger(day=DISK_CHECK_AT[0], hour=DISK_CHECK_AT[1], minute=DISK_CHECK_AT[2], timezone=tz))

    def _add(self, name: str, fn: Callable[[], object], trigger: object, label: str | None = None,
             **extra: object) -> None:
        self._scheduler.add_job(self._guard(name, fn), trigger, id=name, name=name,  # type: ignore[arg-type]
                                replace_existing=True, **extra)
        self._described[name] = label or str(trigger)

    def _guard(self, name: str, fn: Callable[[], object]) -> Callable[[], None]:
        def run() -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001  a job must never stop the scheduler
                if self._on_error is not None:
                    try:
                        self._on_error(name, exc)
                    except Exception:  # noqa: BLE001  nor may the error handler
                        pass

        return run

    def job_ids(self) -> list[str]:
        return list(self._described)

    def describe(self) -> dict[str, str]:
        """Job id to a one-line trigger description, for status output and tests."""
        return dict(self._described)

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running)

    def due_at(self, cfg: "Config", day: date) -> datetime:
        return due_at(cfg, day, self.tz)

    def start(self) -> None:
        self._scheduler.start()

    def stop(self, wait: bool = True) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=wait)
