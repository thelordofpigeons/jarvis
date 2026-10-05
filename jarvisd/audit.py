"""Append-only, hash-chained JSONL audit log (design section 13).

Each record carries seq, prev and h, with h = sha256(prev + canonical_json(record without
h)). Editing, deleting or reordering a record breaks the chain at a known seq. This is
tamper-evident, not tamper-proof: a process under the same account can rewrite the file and
the chain together. The out-of-band witness (audit_seq and audit_head in each digest's
frontmatter, synced elsewhere) is what closes that gap.

emit never raises. A failed write goes to stderr and to a .fallback file, because an audit
problem must never stop the daemon from honouring the kill switch or a budget.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jarvisd import __version__, fsio
from jarvisd.common import canonical_json, iso, parse_iso, sha256_hex, strip_dashes

GENESIS = "0" * 64
MAX_STRING = 500
# Keys that could carry prompt or vault text. The audit holds ids, hashes, counts, costs.
DROPPED_KEYS = frozenset({"prompt", "content", "text", "body", "title"})
_LAYOUT = ("ts", "event", "seq", "prev", "h", "run_id", "job_id", "pid", "ver")
_ROTATED_STAMP = "%Y%m%dT%H%M%S%f"
_ROTATED_RE = r"\d{8}T\d{12}Z"
_BOM = b"\xef\xbb\xbf"
_LOCK_TIMEOUT = 10.0


def _warn(message: str) -> None:
    # Same shape as bin/watchdog.py JsonlLog. stderr can be None under pythonw.
    try:
        if sys.stderr is not None:
            print(f"[warn] {message}", file=sys.stderr)
    except Exception:
        pass


def _clean_str(value: str) -> str:
    text = strip_dashes(value).encode("utf-8", "replace").decode("utf-8")
    if len(text) > MAX_STRING:
        text = text[: MAX_STRING - 3] + "..."
    return text


def redact(value: Any, depth: int = 0) -> Any:
    """Make a value safe to log: JSON types only, long strings cut, text-bearing keys dropped."""
    if depth > 8:
        return "<too deep>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, dict):
        return {_clean_str(str(k)): redact(v, depth + 1) for k, v in value.items()
                if str(k).lower() not in DROPPED_KEYS}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v, depth + 1) for v in value]
    if isinstance(value, datetime):
        return iso(value) if value.tzinfo is not None else _clean_str(value.isoformat())
    if isinstance(value, Path):
        return _clean_str(value.as_posix())
    return _clean_str(str(value))


def _parse_line(raw: bytes) -> dict[str, Any] | None:
    """One log line to a dict, or None if blank or damaged. Tolerates a UTF-8 BOM."""
    raw = raw.strip()
    if raw.startswith(_BOM):
        raw = raw[len(_BOM):]
    if not raw:
        return None
    try:
        rec = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _is_chain_record(rec: dict[str, Any] | None) -> bool:
    return (rec is not None and isinstance(rec.get("seq"), int) and not isinstance(rec.get("seq"), bool)
            and isinstance(rec.get("h"), str) and isinstance(rec.get("prev"), str))


def _last_record(path: Path) -> dict[str, Any] | None:
    """Last parseable chain record of a file, reading from the end."""
    try:
        size = path.stat().st_size
        if size == 0:
            return None
        block = 65536
        with open(path, "rb") as fh:
            while True:
                start = max(0, size - block)
                fh.seek(start)
                lines = fh.read(size - start).split(b"\n")
                if start > 0:
                    lines = lines[1:]  # the first piece may be cut mid-line
                for raw in reversed(lines):
                    rec = _parse_line(raw)
                    if _is_chain_record(rec):
                        return rec
                if start == 0:
                    return None
                block *= 4
    except OSError:
        return None


def _first_record_ts(path: Path) -> datetime | None:
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                rec = _parse_line(raw)
                if rec is not None:
                    return parse_iso(str(rec.get("ts")))
    except (OSError, ValueError):
        pass
    return None


class AuditLog:
    """One chained log file plus its rotated predecessors.

    path is the live file (logs/jarvisd-audit.jsonl). Rotated files sit beside it as
    <stem>.<UTC>.jsonl. clock is injectable so tests control ts, month rotation and cost_on.
    """

    def __init__(self, path: str | os.PathLike[str], max_bytes: int = 20_000_000, keep_days: int = 180,
                 *, run_id: str | None = None, clock: Callable[[], datetime] | None = None,
                 mirror_stdout: bool = True) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.keep_days = keep_days
        self.run_id = run_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._mirror = mirror_stdout
        self._flock = fsio.FileLock(fsio.lock_path_for(self.path), timeout=_LOCK_TIMEOUT)
        self._seq = 0
        self._hash = GENESIS
        self._size = -1  # file size after our last look; -1 means "not synced yet"
        self._first_ts: datetime | None = None
        self._rotate_warned = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _warn(f"audit directory not creatable: {exc}")

    # ---- emit -------------------------------------------------------------------------

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one chained record and return it. Never raises."""
        try:
            with fsio.path_lock(self.path), self._flock:
                self._sync_head()
                self._rotate_locked_if_due()
                record = self._append_locked(event, fields)
        except Exception as exc:  # the audit must not take the daemon down with it
            record = self._write_fallback(event, fields, exc)
        self._echo(record)
        return record

    def _append_locked(self, event: str, fields: dict[str, Any]) -> dict[str, Any]:
        fields = dict(fields)
        run_id = fields.pop("run_id", self.run_id)
        job_id = fields.pop("job_id", None)
        extra = {(f"x_{k}" if k in _LAYOUT else k): redact(v) for k, v in fields.items()
                 if str(k).lower() not in DROPPED_KEYS}
        base: dict[str, Any] = {
            "ts": iso(self._clock(), "milliseconds"),
            "event": _clean_str(str(event)),
            "seq": self._seq + 1,
            "prev": self._hash,
            "run_id": redact(run_id),
            "job_id": redact(job_id),
            "pid": os.getpid(),
            "ver": __version__,
            **extra,
        }
        digest = sha256_hex(base["prev"] + canonical_json(base))
        record: dict[str, Any] = {k: base[k] for k in ("ts", "event", "seq", "prev")}
        record["h"] = digest
        record.update({k: v for k, v in base.items() if k not in record})
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        fsio.append_line(self.path, line)
        self._seq, self._hash = base["seq"], digest
        self._size = self.path.stat().st_size
        if self._first_ts is None and self._size == len(line.encode("utf-8")) + 1:
            self._first_ts = parse_iso(base["ts"])
        return record

    def _echo(self, record: dict[str, Any]) -> None:
        # Under pythonw sys.stdout is None; a write to a closed console must not matter.
        if not self._mirror or sys.stdout is None:
            return
        try:
            print(json.dumps(record, ensure_ascii=False), flush=True)
        except Exception:
            pass

    def _write_fallback(self, event: str, fields: dict[str, Any], exc: Exception) -> dict[str, Any]:
        record: dict[str, Any] = {"ts": iso(datetime.now(timezone.utc), "milliseconds"),
                                  "event": _clean_str(str(event)), "unchained": True,
                                  "error": _clean_str(f"{type(exc).__name__}: {exc}")}
        try:
            record.update({(f"x_{k}" if k in record else k): redact(v) for k, v in fields.items()
                           if str(k).lower() not in DROPPED_KEYS})
        except Exception:
            pass
        _warn(f"audit write failed for {event!r}: {exc}")
        try:
            fsio.append_line_locked(Path(str(self.path) + ".fallback"),
                                    json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        except Exception as fb_exc:
            _warn(f"audit fallback write failed too: {fb_exc}")
        return record

    # ---- head tracking ----------------------------------------------------------------

    def _stat_size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _sync_head(self) -> None:
        """Re-read the head when the file is not the one we last wrote (other process, restart)."""
        size = self._stat_size()
        if size == self._size:
            return
        if size == 0:
            self._first_ts = None
            rec = self._newest_rotated_tail()
        else:
            self._quarantine_torn_tail(size)
            self._first_ts = None
            rec = _last_record(self.path)
        if rec is None:
            self._seq, self._hash = 0, GENESIS
        else:
            self._seq, self._hash = rec["seq"], rec["h"]
        self._size = self._stat_size()

    def _newest_rotated_tail(self) -> dict[str, Any] | None:
        for rotated in reversed(self.rotated_files()):
            rec = _last_record(rotated)
            if rec is not None:
                return rec
        return None

    def _quarantine_torn_tail(self, size: int) -> None:
        """Move a half-written last line (a crash mid-append) to .fallback and cut it off.

        Without this the next record would be glued onto the fragment and the chain would
        be broken by our own crash. The fragment is kept, not discarded.
        """
        with open(self.path, "rb") as fh:
            fh.seek(size - 1)
            if fh.read(1) == b"\n":
                return
            cut, block = 0, 65536
            while True:
                start = max(0, size - block)
                fh.seek(start)
                chunk = fh.read(size - start)
                pos = chunk.rfind(b"\n")
                if pos >= 0:
                    cut = start + pos + 1
                    break
                if start == 0:
                    break
                block *= 4
            fh.seek(cut)
            fragment = fh.read()
        fsio.append_line(Path(str(self.path) + ".fallback"), json.dumps(
            {"event": "torn_tail_quarantined", "bytes": len(fragment),
             "fragment": _clean_str(fragment.decode("utf-8", "replace"))}, ensure_ascii=False))
        with open(self.path, "r+b") as fh:
            fh.truncate(cut)
            fh.flush()
            os.fsync(fh.fileno())

    def head(self) -> tuple[int, str]:
        """(seq, hash) of the newest record, (0, 64 zeros) for an empty log. Never raises."""
        try:
            with fsio.path_lock(self.path), self._flock:
                self._sync_head()
        except Exception as exc:
            _warn(f"audit head read failed: {exc}")
        return self._seq, self._hash

    # ---- rotation ---------------------------------------------------------------------

    def rotated_files(self) -> list[Path]:
        """Rotated predecessors, oldest first (the UTC stamp in the name sorts)."""
        pattern = re.compile(
            "^" + re.escape(self.path.stem) + r"\." + _ROTATED_RE + re.escape(self.path.suffix) + "$")
        try:
            names = [p for p in self.path.parent.iterdir() if pattern.match(p.name)]
        except OSError:
            return []
        return sorted(names, key=lambda p: p.name)

    def all_files(self) -> list[Path]:
        files = self.rotated_files()
        if self.path.exists():
            files.append(self.path)
        return files

    def _due_reason(self) -> str | None:
        if self._size <= 0:
            return None
        if self._size >= self.max_bytes:
            return "size"
        if self._first_ts is None:
            self._first_ts = _first_record_ts(self.path)
        now = self._clock().astimezone(timezone.utc)
        if self._first_ts is not None:
            first = self._first_ts.astimezone(timezone.utc)
            if (first.year, first.month) != (now.year, now.month):
                return "month"
        return None

    def _rotate_locked_if_due(self) -> bool:
        reason = self._due_reason()
        return self._rotate_locked(reason) if reason else False

    def _rotated_target(self) -> Path:
        when = self._clock().astimezone(timezone.utc)
        existing = self.rotated_files()
        if existing:
            newest = re.search(_ROTATED_RE, existing[-1].name)
            if newest:
                last = datetime.strptime(newest.group(0)[:-1], _ROTATED_STAMP).replace(tzinfo=timezone.utc)
                when = max(when, last + timedelta(microseconds=1))  # keep name order = time order
        while True:
            name = f"{self.path.stem}.{when.strftime(_ROTATED_STAMP)}Z{self.path.suffix}"
            target = self.path.with_name(name)
            if not target.exists():
                return target
            when += timedelta(microseconds=1)

    def _rotate_locked(self, reason: str) -> bool:
        old_seq, old_hash = self._seq, self._hash
        target = self._rotated_target()
        try:
            os.replace(self.path, target)
        except OSError as exc:
            if not self._rotate_warned:  # one warning, not one per emit
                self._rotate_warned = True
                _warn(f"audit rotation postponed: {exc}")
            return False
        self._rotate_warned = False
        self._size = 0
        self._first_ts = None
        # The chain continues across files: seq and prev carry on, and the first record of
        # the new file names the old head so the file stands on its own for a human reader.
        self._append_locked("audit_rotated", {"rotated_to": target.name, "reason": reason,
                                              "prev_file_seq": old_seq, "prev_file_head": old_hash})
        return True

    def rotate_if_due(self) -> bool:
        """Rotate by size or calendar month (UTC). Returns True when it rotated. Never raises."""
        try:
            with fsio.path_lock(self.path), self._flock:
                self._sync_head()
                return self._rotate_locked_if_due()
        except Exception as exc:
            _warn(f"audit rotate failed: {exc}")
            return False

    def prune(self, now: datetime | None = None) -> list[str]:
        """Delete rotated files older than keep_days (by the stamp in the name). Housekeeping."""
        cutoff = (now or self._clock()).astimezone(timezone.utc) - timedelta(days=self.keep_days)
        removed: list[str] = []
        for rotated in self.rotated_files():
            stamp = re.search(_ROTATED_RE, rotated.name)
            if not stamp:
                continue
            try:
                when = datetime.strptime(stamp.group(0)[:-1], _ROTATED_STAMP).replace(tzinfo=timezone.utc)
                if when < cutoff:
                    rotated.unlink()
                    removed.append(rotated.name)
            except (OSError, ValueError) as exc:
                _warn(f"audit prune skipped {rotated.name}: {exc}")
        return removed

    # ---- reading ----------------------------------------------------------------------

    def records(self, since: datetime | str | None = None,
                events: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """All parseable records, oldest first, across rotated files. Damaged lines are skipped
        here; verify is the tool that reports them."""
        floor = parse_iso(since) if isinstance(since, str) else since
        wanted = set(events) if events is not None else None
        out: list[dict[str, Any]] = []
        for path in self.all_files():
            try:
                with open(path, "rb") as fh:
                    for raw in fh:
                        rec = _parse_line(raw)
                        if rec is None:
                            continue
                        if wanted is not None and rec.get("event") not in wanted:
                            continue
                        if floor is not None:
                            try:
                                if parse_iso(str(rec.get("ts"))) < floor:
                                    continue
                            except ValueError:
                                continue
                        out.append(rec)
            except OSError as exc:
                _warn(f"audit read failed for {path.name}: {exc}")
        return out

    def cost_on(self, day: date | str, tz: timezone | None = None) -> float:
        """Sum claude_call.total_cost_usd for one date. The date is the machine's local date
        unless tz is given, because the daily budget is a local-day budget."""
        target = date.fromisoformat(day) if isinstance(day, str) else day
        total = 0.0
        for rec in self.records(events=("claude_call",)):
            cost = rec.get("total_cost_usd")
            if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost):
                continue
            try:
                stamp = parse_iso(str(rec.get("ts")))
            except ValueError:
                continue
            if stamp.astimezone(tz).date() == target:
                total += cost
        return round(total, 6)

    # ---- verify -----------------------------------------------------------------------

    def verify(self, paths: Iterable[str | os.PathLike[str]] | None = None) -> tuple[bool, int | None]:
        """Walk the chain. Returns (True, None) or (False, seq of the first broken record).

        A record is broken when it cannot be parsed, its seq or prev does not follow the one
        before, or its hash does not match its content. The reported seq is the one that was
        expected, so a corrupted line still names its position. The first record seen is the
        anchor (older files may have been pruned); a log that starts at seq 1 must start at
        the genesis hash.
        """
        files = [Path(p) for p in paths] if paths is not None else self.all_files()
        expect_seq: int | None = None
        expect_prev = GENESIS
        for path in files:
            with open(path, "rb") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    rec = _parse_line(raw)
                    if expect_seq is None:
                        expect_seq = rec["seq"] if _is_chain_record(rec) else 1
                        expect_prev = rec["prev"] if _is_chain_record(rec) and expect_seq != 1 else GENESIS
                    if not _is_chain_record(rec) or not self._record_ok(rec, expect_seq, expect_prev):
                        return False, expect_seq
                    expect_seq += 1
                    expect_prev = rec["h"]
        return True, None

    @staticmethod
    def _record_ok(rec: dict[str, Any], expect_seq: int, expect_prev: str) -> bool:
        if rec["seq"] != expect_seq or rec["prev"] != expect_prev:
            return False
        body = {k: v for k, v in rec.items() if k != "h"}
        try:
            return sha256_hex(rec["prev"] + canonical_json(body)) == rec["h"]
        except ValueError:
            return False  # NaN or similar smuggled into the JSON
