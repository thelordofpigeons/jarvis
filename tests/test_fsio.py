"""fsio: atomic write with Windows retry, locked append, cross-process file lock."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from jarvisd import ROOT, fsio
from jarvisd.fsio import FileBusy, FileLock, append_line_locked, atomic_write_text

_PROBE = (
    "import sys\n"
    "from jarvisd.fsio import FileLock\n"
    "lock = FileLock(sys.argv[1])\n"
    "print('ACQUIRED' if lock.acquire(timeout=0.3) else 'DENIED')\n"
)


def _probe_other_process(lock_path: Path) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run([sys.executable, "-c", _PROBE, str(lock_path)], capture_output=True,
                         text=True, cwd=str(ROOT), env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record backoff sleeps instead of waiting through them."""
    seen: list[float] = []
    monkeypatch.setattr(fsio.time, "sleep", lambda s: seen.append(s))
    return seen


def _tmp_leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]


def test_atomic_write_retries_then_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                            sleeps: list[float]) -> None:
    target = tmp_path / "out.md"
    real_replace = os.replace
    calls: list[tuple[str, str]] = []

    def flaky(src: str, dst: str) -> None:
        calls.append((str(src), str(dst)))
        # The hidden temp must exist while the replace is being retried.
        assert Path(src).name == f".out.md.{os.getpid()}.tmp"
        assert Path(src).exists()
        if len(calls) <= 2:
            raise PermissionError("held by another process")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    atomic_write_text(target, "hello\n")
    assert len(calls) == 3
    assert sleeps == [0.2, 0.5]
    assert target.read_text(encoding="utf-8") == "hello\n"
    assert _tmp_leftovers(tmp_path) == []


def test_atomic_write_raises_file_busy_after_last_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        sleeps: list[float]) -> None:
    target = tmp_path / "out.md"
    target.write_text("old", encoding="utf-8")
    calls: list[int] = []

    def always_busy(src: str, dst: str) -> None:
        calls.append(1)
        raise PermissionError("busy")

    monkeypatch.setattr(os, "replace", always_busy)
    with pytest.raises(FileBusy):
        atomic_write_text(target, "new")
    assert len(calls) == 7  # first try plus six retries
    assert sleeps == [0.2, 0.5, 1.0, 2.0, 4.0, 8.0]
    assert target.read_text(encoding="utf-8") == "old"  # the old file is untouched
    assert _tmp_leftovers(tmp_path) == []


def test_atomic_write_other_oserror_propagates_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             sleeps: list[float]) -> None:
    def broken(src: str, dst: str) -> None:
        raise OSError("disk exploded")

    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(OSError, match="disk exploded"):
        atomic_write_text(tmp_path / "x.txt", "data")
    assert sleeps == []  # only PermissionError is worth waiting for
    assert _tmp_leftovers(tmp_path) == []


def test_atomic_write_is_utf8_without_bom_and_keeps_lf(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "dir" / "note.md"  # parents are created
    atomic_write_text(target, "café\nsecond line\n")
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.decode("utf-8") == "café\nsecond line\n"


def test_atomic_write_replaces_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
    assert _tmp_leftovers(tmp_path) == []


def test_append_line_locked_adds_newline_and_rejects_embedded_newline(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    append_line_locked(log, "first")
    append_line_locked(log, "second\n")
    assert log.read_bytes() == b"first\nsecond\n"
    with pytest.raises(ValueError):
        append_line_locked(log, "two\nlines")


def test_append_line_locked_threads_do_not_interleave(tmp_path: Path) -> None:
    log = tmp_path / "log.txt"

    def worker(tag: str) -> None:
        for i in range(40):
            append_line_locked(log, f"{tag}-{i}-" + "x" * 200)

    threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 160
    assert all(len(line) > 200 and line.endswith("x" * 200) for line in lines)


def test_file_lock_denies_second_holder_in_another_process(tmp_path: Path) -> None:
    lock_file = tmp_path / "thing.lock"
    lock = FileLock(lock_file)
    assert lock.acquire(timeout=1)
    try:
        assert _probe_other_process(lock_file) == "DENIED"
    finally:
        lock.release()
    assert _probe_other_process(lock_file) == "ACQUIRED"


def test_file_lock_second_holder_in_same_process_is_denied(tmp_path: Path) -> None:
    lock_file = tmp_path / "thing.lock"
    first = FileLock(lock_file)
    second = FileLock(lock_file)
    with first:
        assert first.locked
        assert second.acquire(timeout=0.1) is False
        with pytest.raises(FileBusy):
            with FileLock(lock_file, timeout=0.1):
                pass
    assert not first.locked
    assert second.acquire(timeout=1) is True
    second.release()


def test_file_lock_release_is_idempotent(tmp_path: Path) -> None:
    lock = FileLock(tmp_path / "x.lock")
    lock.release()  # never acquired: harmless
    assert lock.acquire(timeout=1)
    lock.release()
    lock.release()
    assert lock.acquire(timeout=1)
    lock.release()
