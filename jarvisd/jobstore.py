"""JSON-file job queue with held references (design sections 3 and 5).

Layout under the queue root: `pending/ running/ done/ failed/ held/`, one `<id>.json` per
job. The directory is the truth and the job's `state` field is advisory: a mismatch is
audited as `queue_inconsistent` and the directory wins. A state change is an `os.replace`
across directories, then a rewrite of the file with the new state and a history entry; a
crash between the two leaves one file in one directory, never two.

Concurrency: every mutation runs under an in-process lock plus a cross-process FileLock
on `<root>/.queue.lock`, so the daemon, the CLI and two threads cannot interleave a move
with an enqueue of the same id. `enqueue` additionally creates the pending file
exclusively, so even a writer that ignored the lock could not overwrite an existing job.

`leases/` holds one small file per running job: the pid of the process that claimed it and
when. The daemon lock proves no other *daemon* is alive, but `jarvis run-digest` claims jobs
without it, so `recover_running` leaves a running job alone while its claimer is still alive.

`held/` holds content-free references to things that were withheld (id, kind, source
path, reason, dates, digest ids). No title and no text ever land there. Only the human
CLI reads it; no Claude-bound module may import `JobStore.held` (import-graph test).

This module is not allowed near the vault: the constructor refuses a root under a
directory named `brain`, under the configured brain root, or inside a Syncthing folder
(an ancestor holding `.stfolder`), because queue files must not sync between machines.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError

from jarvisd.common import iso, now_utc, parse_iso
from jarvisd.fsio import FileLock, atomic_write_text, path_lock
from jarvisd.models import HistoryEntry, Job, WithheldItem

if TYPE_CHECKING:
    from jarvisd.audit import AuditLog
    from jarvisd.config import Config

Clock = Callable[[], datetime]

STATES: tuple[str, ...] = ("pending", "running", "done", "failed", "held")
JOB_STATES: tuple[str, ...] = ("pending", "running", "done", "failed")
HELD_KEYS: tuple[str, ...] = ("schema", "id", "kind", "source_ref", "reason", "first_seen",
                              "last_seen", "expires_at", "digest_ids")

# Ids become file names: no separators, no leading dot, no trailing dot, bounded length.
_SAFE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9_-])?$")
_LOCK_TIMEOUT = 30.0
LEASE_DIR = "leases"
# A digest job is bounded by its three hour deadline. A lease older than this belongs to a
# claimer that is gone even if its pid has since been given to some other process.
LEASE_MAX_AGE = timedelta(hours=4)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _pid_alive(pid: int) -> bool:
    """True when a process with this id is running. Never signals the process.

    `os.kill(pid, 0)` is not usable on Windows (signal 0 is CTRL_C_EVENT there), so ask the
    kernel for the exit code instead.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.GetLastError() == _ERROR_ACCESS_DENIED  # exists, but not ours to open
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class UnsafeRoot(ValueError):
    """The queue root is somewhere queue files must never live."""


class JobNotFound(KeyError):
    """The job has no file in any state directory."""


def _check_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.match(value):
        raise ValueError(f"unsafe id for a queue file name: {value!r}")
    return value


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _short_error(exc: BaseException) -> str:
    """One readable line that never echoes the offending input."""
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()[:3]]
        return "; ".join(parts)[:200]
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON at char {exc.pos}"
    return f"{type(exc).__name__}: {str(exc)[:120]}"


class JobStore:
    def __init__(self, root: str | os.PathLike[str], *, clock: Clock | None = None,
                 audit: "AuditLog | None" = None, brain_root: str | os.PathLike[str] | None = None,
                 held_days: int = 14, done_days: int = 60, failed_days: int = 60) -> None:
        self.root = Path(root)
        self._refuse_unsafe_root(brain_root)
        self._clock: Clock = clock or now_utc
        self._audit = audit
        self.held_days = held_days
        self.done_days = done_days
        self.failed_days = failed_days
        for name in (*STATES, LEASE_DIR):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: "Config", *, clock: Clock | None = None,
                    audit: "AuditLog | None" = None) -> "JobStore":
        return cls(cfg.paths.queue, clock=clock, audit=audit, brain_root=cfg.paths.brain_root,
                   held_days=cfg.retention.held_days, done_days=cfg.retention.done_job_days,
                   failed_days=cfg.retention.failed_job_days)

    # --- setup and helpers ---

    def _refuse_unsafe_root(self, brain_root: str | os.PathLike[str] | None) -> None:
        absolute = Path(os.path.abspath(self.root))
        if any(part.lower() == "brain" for part in absolute.parts):
            raise UnsafeRoot(f"queue root {self.root} is under a brain directory")
        if brain_root is not None:
            vault = _norm(Path(brain_root))
            here = _norm(absolute)
            if here == vault or here.startswith(vault + os.sep):
                raise UnsafeRoot(f"queue root {self.root} is inside the vault")
        for folder in (absolute, *absolute.parents):
            if (folder / ".stfolder").exists():
                raise UnsafeRoot(f"queue root {self.root} is inside a Syncthing folder ({folder})")

    @contextmanager
    def _guard(self) -> Iterator[None]:
        lock_file = self.root / ".queue.lock"
        with path_lock(lock_file):
            with FileLock(lock_file, timeout=_LOCK_TIMEOUT):
                yield

    def _emit(self, event: str, **fields: Any) -> None:
        if self._audit is not None:
            self._audit.emit(event, **fields)

    def _path(self, state: str, job_id: str) -> Path:
        return self.root / state / f"{_check_id(job_id)}.json"

    def _find(self, job_id: str) -> str | None:
        _check_id(job_id)
        for state in STATES:
            if (self.root / state / f"{job_id}.json").exists():
                return state
        return None

    def _ids(self, state: str) -> list[str]:
        folder = self.root / state
        return sorted(p.stem for p in folder.glob("*.json") if not p.name.startswith("."))

    @staticmethod
    def _dump(job: Job) -> str:
        return json.dumps(job.to_dict(), indent=2, ensure_ascii=False) + "\n"

    def _write(self, state: str, job: Job) -> None:
        atomic_write_text(self._path(state, job.id), self._dump(job))

    # --- leases: who claimed a running job ---

    def _lease_path(self, job_id: str) -> Path:
        return self.root / LEASE_DIR / f"{_check_id(job_id)}.json"

    def _write_lease(self, job_id: str) -> None:
        record = {"pid": os.getpid(), "ts": iso(self._clock())}
        try:
            atomic_write_text(self._lease_path(job_id), json.dumps(record) + "\n")
        except OSError:
            pass  # a missing lease only means the job is recovered the old way

    def _drop_lease(self, job_id: str) -> None:
        try:
            self._lease_path(job_id).unlink(missing_ok=True)
        except OSError:
            pass

    def _live_owner(self, job_id: str) -> int | None:
        """Pid of a still-running claimer other than this process, else None."""
        try:
            record = json.loads(self._lease_path(job_id).read_text(encoding="utf-8"))
            pid = int(record["pid"])
            claimed = parse_iso(str(record["ts"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if pid == os.getpid() or self._clock() - claimed > LEASE_MAX_AGE:
            return None
        return pid if _pid_alive(pid) else None

    def _move(self, job_id: str, src: str, dst: str) -> None:
        source, target = self._path(src, job_id), self._path(dst, job_id)
        for attempt in range(5):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))

    # --- loading, with quarantine ---

    def _load(self, state: str, job_id: str) -> Job | None:
        """Read one job file. Corrupt files are moved to failed/ and reported as None."""
        path = self._path(state, job_id)
        try:
            raw = path.read_bytes()
        except (FileNotFoundError, PermissionError):
            return None
        try:
            job = Job.model_validate(json.loads(raw.decode("utf-8-sig")))
            if job.id != job_id:
                raise ValueError("id does not match the file name")
        except (ValueError, ValidationError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
            self._quarantine(state, job_id, _short_error(exc))
            return None
        if job.state != state:
            self._emit("queue_inconsistent", job_id=job_id, directory=state, state_field=job.state)
            job.state = state  # type: ignore[assignment]  # the directory wins
        return job

    def _quarantine(self, state: str, job_id: str, reason: str) -> None:
        """Move an unreadable job to failed/ with a readable error, without crashing the loop.

        The raw bytes are kept next to it as `<id>.corrupt` for a human; `<id>.json` becomes
        a small valid failed job so every other call can treat it like any failed job.
        """
        now = iso(self._clock())
        corrupt = self.root / "failed" / f"{job_id}.corrupt"
        try:
            os.replace(self._path(state, job_id), corrupt)
        except OSError:
            return
        target = self._path("failed", job_id)
        if not target.exists():
            stub = Job(
                id=job_id, kind="unknown", key=job_id, job_class="unknown", latency_class="unknown",
                state="failed", created_at=now, not_before=now,
                last_error=f"corrupt_job_file: {reason}",
                history=[HistoryEntry(ts=now, from_state=state, to="failed", note="corrupt")],
            )
            self._write("failed", stub)
        self._emit("job_failed", job_id=job_id, reason="corrupt_job_file", directory=state, detail=reason)

    # --- queries ---

    def exists(self, job_id: str) -> str | None:
        """Name of the state directory holding `<job_id>.json`, across all five, or None."""
        return self._find(job_id)

    def get(self, job_id: str) -> Job | None:
        with self._guard():
            state = self._find(job_id)
            if state is None or state == "held":
                return None
            return self._load(state, job_id)

    def jobs(self, state: str) -> list[Job]:
        """Every readable job in one state directory, oldest first."""
        if state not in JOB_STATES:
            raise ValueError(f"not a job state: {state!r}")
        with self._guard():
            loaded = [self._load(state, job_id) for job_id in self._ids(state)]
        found = [job for job in loaded if job is not None]
        return sorted(found, key=lambda j: (parse_iso(j.created_at), j.id))

    def counts(self) -> dict[str, int]:
        return {state: len(self._ids(state)) for state in STATES}

    # --- enqueue ---

    def _create_exclusive(self, path: Path, text: str) -> bool:
        """Create `path` only if it does not exist, with the whole content visible at once.

        The content goes to a hidden temp file first and is hard-linked into place, which
        fails if the name is taken. A reader therefore never sees a half-written pending
        file. Where hard links are unavailable it falls back to O_EXCL on the final name.
        """
        data = text.encode("utf-8")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            try:
                os.link(tmp, path)
                return True
            except FileExistsError:
                return False
            except OSError:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                try:
                    fd = os.open(path, flags)
                except FileExistsError:
                    return False
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                return True
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def enqueue(self, job: Job) -> bool:
        """Add a pending job. False if that id exists in any state directory (idempotent)."""
        if job.state != "pending":
            raise ValueError("only a pending job can be enqueued")
        _check_id(job.id)
        with self._guard():
            if self._find(job.id) is not None:
                return False
            return self._create_exclusive(self._path("pending", job.id), self._dump(job))

    # --- transitions ---

    def _transition(self, job: Job, to: str, note: str) -> None:
        """Move a job's file to `to`, then rewrite it with the new state and a history entry."""
        src = self._find(job.id)
        if src is None or src == "held":
            raise JobNotFound(job.id)
        if src != to:
            self._move(job.id, src, to)
        job.history.append(HistoryEntry(ts=iso(self._clock()), from_state=src, to=to, note=note))
        job.state = to  # type: ignore[assignment]
        self._write(to, job)
        if to != "running":
            self._drop_lease(job.id)
        self._emit("job_state", job_id=job.id, from_state=src, to_state=to, note=note,
                   attempts=job.attempts)

    def claim_next(self, now: datetime) -> Job | None:
        """Take the oldest pending job whose `not_before` has passed and mark it running.

        The attempt is counted and persisted here, before any work starts. A job that
        already used all its attempts goes to failed instead of being claimed. Corrupt
        files are quarantined and skipped, so one bad file cannot stop the queue.
        """
        with self._guard():
            due: list[Job] = []
            for job_id in self._ids("pending"):
                job = self._load("pending", job_id)
                if job is not None and parse_iso(job.not_before) <= now:
                    due.append(job)
            due.sort(key=lambda j: (parse_iso(j.created_at), j.id))
            for job in due:
                if job.attempts >= job.max_attempts:
                    job.last_error = job.last_error or "attempts_exhausted"
                    self._transition(job, "failed", "attempts_exhausted")
                    continue
                job.attempts += 1
                self._transition(job, "running", "claimed")
                self._write_lease(job.id)
                return job
            return None

    def update(self, job: Job) -> None:
        """Persist the job's fields where it currently is. The state never changes here."""
        with self._guard():
            state = self._find(job.id)
            if state is None or state == "held":
                raise JobNotFound(job.id)
            job.state = state  # type: ignore[assignment]
            self._write(state, job)

    def complete(self, job: Job, result: dict[str, Any]) -> None:
        with self._guard():
            job.result = result
            self._transition(job, "done", "complete")

    def fail(self, job: Job, error: str) -> None:
        with self._guard():
            job.last_error = error
            self._transition(job, "failed", "failed")

    def retry(self, job: Job, error: str, delay: timedelta | float = 0.0,
              *, consume_attempt: bool = True) -> None:
        """Send a running job back to pending after `delay`, or to failed if out of attempts.

        `consume_attempt=False` hands back the attempt taken at claim time, for waits that
        are not the job's fault (no network after wake, killed by the kill switch).
        """
        wait = delay if isinstance(delay, timedelta) else timedelta(seconds=float(delay))
        with self._guard():
            job.last_error = error
            if not consume_attempt:
                job.attempts = max(0, job.attempts - 1)
            if job.attempts >= job.max_attempts:
                self._transition(job, "failed", "attempts_exhausted")
                return
            job.not_before = iso(self._clock() + wait)
            self._transition(job, "pending", "retry")

    def coalesce_older(self, kind: str, before_key: str, into_id: str) -> list[str]:
        """Fail pending jobs of `kind` whose key sorts before `before_key`, as merged into `into_id`.

        One lock hold covers the listing and the moves, so a job claimed in between is not
        failed by mistake. Returns the ids moved to failed.
        """
        moved: list[str] = []
        with self._guard():
            for job_id in self._ids("pending"):
                job = self._load("pending", job_id)
                if job is None or job.id == into_id or job.kind != kind or not job.key < before_key:
                    continue
                job.last_error = f"coalesced_into:{into_id}"
                self._transition(job, "failed", "coalesced")
                moved.append(job_id)
        return moved

    def recover_running(self) -> list[str]:
        """Handle jobs left in running/ by a dead daemon. Returns the ids put back to pending.

        The daemon lock proves no other daemon owns them, but a manual `jarvis run-digest` claims
        jobs without that lock: a running job whose lease names a live process is left alone
        (audited as `skipped_live_owner`). Attempts are preserved (the claim already counted
        one); a job that has none left goes to failed.
        """
        recovered: list[str] = []
        with self._guard():
            for job_id in self._ids("running"):
                job = self._load("running", job_id)
                if job is None:
                    continue
                owner = self._live_owner(job_id)
                if owner is not None:
                    self._emit("job_recover", job_id=job_id, outcome="skipped_live_owner", owner_pid=owner)
                    continue
                if job.attempts >= job.max_attempts:
                    job.last_error = "attempts_exhausted_after_restart"
                    self._transition(job, "failed", "recovered_exhausted")
                    self._emit("job_recover", job_id=job_id, outcome="failed", attempts=job.attempts)
                    continue
                self._transition(job, "pending", "recovered")
                self._emit("job_recover", job_id=job_id, outcome="pending", attempts=job.attempts)
                recovered.append(job_id)
        return recovered

    # --- held references ---

    def hold(self, ref: WithheldItem, job_id: str) -> dict[str, Any]:
        """Record that an item was withheld from `job_id`. Merges digest ids on repeat.

        Only the keys in HELD_KEYS are written: the reference is deliberately content-free.
        """
        _check_id(ref.id)
        now = self._clock()
        path = self._path("held", ref.id)
        with self._guard():
            previous: dict[str, Any] = {}
            try:
                loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    previous = loaded
            except (OSError, ValueError):
                previous = {}
            digest_ids = [str(x) for x in previous.get("digest_ids", []) if isinstance(x, str)]
            if job_id not in digest_ids:
                digest_ids.append(job_id)
            record = {
                "schema": 1,
                "id": ref.id,
                "kind": ref.kind,
                "source_ref": ref.source_ref,
                "reason": ref.reason,
                "first_seen": str(previous.get("first_seen") or iso(now)),
                "last_seen": iso(now),
                "expires_at": iso(now + timedelta(days=self.held_days)),
                "digest_ids": digest_ids,
            }
            atomic_write_text(path, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
            return record

    def held(self, date_filter: date | str | None = None) -> list[dict[str, Any]]:
        """Held references, by id. With a date: only those recorded for that day's digests."""
        wanted = date_filter.isoformat() if isinstance(date_filter, date) else date_filter
        found: list[dict[str, Any]] = []
        for ref_id in self._ids("held"):
            try:
                data = json.loads(self._path("held", ref_id).read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if wanted is not None:
                prefix = f"digest-{wanted}"
                ids = data.get("digest_ids", [])
                if not any(isinstance(d, str) and (d == prefix or d.startswith(prefix + "-")) for d in ids):
                    continue
            found.append(data)
        return found

    def expire_held(self, now: datetime) -> list[str]:
        """Delete references whose `expires_at` has passed. Returns the removed ids."""
        removed: list[str] = []
        with self._guard():
            for ref_id in self._ids("held"):
                path = self._path("held", ref_id)
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                    expires = parse_iso(str(data["expires_at"]))
                except (OSError, ValueError, KeyError, TypeError):
                    continue  # unreadable references are left for a human, not silently dropped
                if expires <= now:
                    path.unlink(missing_ok=True)
                    removed.append(ref_id)
        return removed

    # --- housekeeping ---

    def _finished_at(self, job: Job, path: Path) -> datetime:
        if job.history:
            return parse_iso(job.history[-1].ts)
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()

    def prune(self, now: datetime | None = None, *, done_days: int | None = None,
              failed_days: int | None = None) -> list[str]:
        """Delete done and failed jobs older than their retention. Returns the removed ids."""
        moment = now or self._clock()
        limits = {"done": timedelta(days=self.done_days if done_days is None else done_days),
                  "failed": timedelta(days=self.failed_days if failed_days is None else failed_days)}
        removed: list[str] = []
        with self._guard():
            for state, limit in limits.items():
                for job_id in self._ids(state):
                    job = self._load(state, job_id)
                    if job is None:
                        continue
                    path = self._path(state, job_id)
                    try:
                        if moment - self._finished_at(job, path) <= limit:
                            continue
                        path.unlink(missing_ok=True)
                        (self.root / state / f"{job_id}.corrupt").unlink(missing_ok=True)
                    except OSError:
                        continue
                    removed.append(job_id)
        return removed
