"""Durable file IO for a Windows desktop: atomic replace, locked append, file lock.

Why this exists: Obsidian, Basic Memory, Syncthing and antivirus all open files briefly,
and on Windows an open handle makes os.replace raise PermissionError. A retry with
backoff turns that into a short wait instead of a lost write (design section 4c).
Only this module, audit, jobstore, state, vault and claude open files for writing
(AST test, design section 14).
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import TracebackType

try:  # Windows only; the daemon never runs elsewhere, tests on other OSes skip locking
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None  # type: ignore[assignment]

# Seconds to wait before retry 1..6 when os.replace raises PermissionError.
BACKOFF_SECONDS: tuple[float, ...] = (0.2, 0.5, 1.0, 2.0, 4.0, 8.0)


class FileBusy(OSError):
    """A file stayed locked or held by another process for the whole retry budget."""


_path_locks: dict[str, threading.RLock] = {}
_path_locks_guard = threading.Lock()


def path_lock(path: str | os.PathLike[str]) -> threading.RLock:
    """In-process lock shared by every user of one path (threads, several instances).

    msvcrt locks only protect against other handles, and two threads in this process
    would otherwise race on the same temp name or the same log head.
    """
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _path_locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = _path_locks[key] = threading.RLock()
        return lock


def _remove_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_temp(tmp: Path, data: bytes) -> None:
    # Bytes, not text mode: text mode would turn LF into CRLF on Windows.
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _replace_with_retry(tmp: Path, target: Path, retries: int) -> None:
    for attempt in range(retries + 1):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:
            if attempt >= retries:
                raise FileBusy(f"could not replace {target.name} after {retries} retries: {exc}") from exc
            time.sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)])


def atomic_write_text(path: str | os.PathLike[str], text: str, *, retries: int = 6) -> None:
    """Write text to path so a reader sees the old file or the new one, never half.

    The temp file is hidden (leading dot) so Syncthing ignores a partial copy, lives in the
    same directory so os.replace stays on one volume, and is always removed. UTF-8, no BOM,
    LF kept as given. Raises FileBusy when the target stays locked past the last retry.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    data = text.encode("utf-8")
    with path_lock(target):
        try:
            _write_temp(tmp, data)
            _replace_with_retry(tmp, target, retries)
        finally:
            _remove_quiet(tmp)


def append_line(path: str | os.PathLike[str], line: str, *, fsync: bool = True) -> None:
    """Append one line (LF added if missing) with no cross-process locking.

    Callers that already hold a FileLock use this; everyone else uses append_line_locked.
    """
    if "\n" in line.rstrip("\n") or "\r" in line:
        raise ValueError("append_line takes exactly one line")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (line.rstrip("\n") + "\n").encode("utf-8")
    with open(target, "ab") as fh:
        fh.write(data)
        fh.flush()
        if fsync:
            os.fsync(fh.fileno())


def lock_path_for(path: str | os.PathLike[str]) -> Path:
    """Sidecar lock file for a data file. The data file itself is never byte-locked,
    because Windows byte-range locks are mandatory and would block readers."""
    target = Path(path)
    return target.with_name(target.name + ".lock")


def append_line_locked(path: str | os.PathLike[str], line: str, *, fsync: bool = True,
                       timeout: float = 10.0) -> None:
    """Append one line under a cross-process lock. Raises FileBusy if the lock is not won."""
    with path_lock(path):
        with FileLock(lock_path_for(path), timeout=timeout):
            append_line(path, line, fsync=fsync)


class FileLock:
    """Exclusive advisory lock on one byte of a lock file, via msvcrt.locking.

    Not reentrant: a second holder is denied, in another process or in this one. The OS
    drops the lock if the holder dies, so a crash never leaves a stale lock.
    """

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 30.0, poll: float = 0.02) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.poll = poll
        self._fd: int | None = None
        self._mutex = threading.Lock()  # one holder per instance across threads
        self._held_mutex = False

    @property
    def locked(self) -> bool:
        return self._fd is not None

    def _try_os_lock(self, fd: int) -> bool:
        if msvcrt is None:  # pragma: no cover
            return True  # no OS locking available; the in-process mutex still applies
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def acquire(self, timeout: float | None = None) -> bool:
        """Try to take the lock for up to timeout seconds. Returns False when not won."""
        limit = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + max(limit, 0.0)
        if not self._mutex.acquire(timeout=max(limit, 0.0)):
            return False
        self._held_mutex = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        except OSError:
            self._release_mutex()
            raise
        while True:
            if self._try_os_lock(fd):
                self._fd = fd
                return True
            if time.monotonic() >= deadline:
                os.close(fd)
                self._release_mutex()
                return False
            time.sleep(self.poll)

    def _release_mutex(self) -> None:
        if self._held_mutex:
            self._held_mutex = False
            self._mutex.release()

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                if msvcrt is not None:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # closing the handle releases it anyway
            finally:
                os.close(fd)
        self._release_mutex()

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise FileBusy(f"lock {self.path.name} is held by another holder")
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.release()
