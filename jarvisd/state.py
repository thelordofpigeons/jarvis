"""Crash-safe operational state outside the vault (design sections 5, 7 and 10).

Everything lives in one directory (`state/`) as small JSON files written with an atomic
replace, so a crash leaves the old file or the new one, never half. Four things are
tracked here and nowhere else:

- the budget ledger: money and call count per local date, with reservations that survive
  a crash because the reservation is persisted before the paid call starts;
- the circuit breaker around the Claude call;
- the watermark: the end of the last successfully written digest window;
- liveness: heartbeat, the clean-shutdown marker, the KILL and PAUSE files, and the
  single-instance lock;
- the item history (`item-history.json`, written by the digest only) and the attention
  decisions under `attention/` (written by the hub and the CLI, read here), the writer side
  of docs/hub-rework-contract.md sections 6 and 7.

Read-modify-write on a file takes an in-process lock plus a cross-process FileLock on a
sidecar, because the CLI can run while the daemon is up. State that goes wrong fails
closed: an unreadable budget counts as spent, an unreadable breaker is open and needs a
human, an unreadable PAUSE pauses. The only exception is the watermark, where "unknown"
safely means "use the default window".
"""
from __future__ import annotations

import json
import math
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from jarvisd import __version__
from jarvisd.common import iso, now_utc, parse_iso
from jarvisd.fsio import FileBusy, FileLock, atomic_write_text, lock_path_for, path_lock

if TYPE_CHECKING:
    from jarvisd.config import Config

Clock = Callable[[], datetime]

_MONEY_EPSILON = 1e-9  # float slack so 0.1 + 0.2 never trips a cap that is met exactly
_LOCK_TIMEOUT = 10.0


class BudgetRefused(Exception):
    """The ledger said no. `reason` is "usd" or "calls"; `snapshot` is the ledger at refusal."""

    def __init__(self, reason: str, snapshot: dict[str, Any]) -> None:
        super().__init__(f"budget refused: {reason}")
        self.reason = reason
        self.snapshot = snapshot


class AlreadyRunning(Exception):
    """Another daemon (this process or another) holds state/daemon.lock."""


@dataclass(frozen=True)
class Reservation:
    """A persisted claim on part of today's budget, to be settled or released."""

    id: str
    purpose: str
    usd: float


# --- small JSON helpers ----------------------------------------------------------------


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    """Read a JSON object. Returns (data, status) with status missing, ok or corrupt.

    A reader can collide with a writer's os.replace on Windows (PermissionError for a few
    milliseconds), so that case is retried before it is called anything else.
    """
    for attempt in range(6):
        try:
            raw = path.read_bytes()
            break
        except FileNotFoundError:
            return None, "missing"
        except PermissionError:
            if attempt == 5:
                return None, "corrupt"
            time.sleep(0.02)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None, "corrupt"
    if not isinstance(data, dict):
        return None, "corrupt"
    return data, "ok"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def _money(value: float) -> float:
    return round(float(value), 6)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize a read-modify-write on `path` across threads and processes."""
    with path_lock(path):
        with FileLock(lock_path_for(path), timeout=_LOCK_TIMEOUT):
            yield


# --- budget ----------------------------------------------------------------------------


class Budget:
    """Daily spend ledger. `reserve` before the call, `settle` after, `release` if it never ran."""

    def __init__(self, store: "StateStore") -> None:
        self._s = store
        self.path = store.dir / "budget.json"

    def _fresh(self, today: str) -> dict[str, Any]:
        return {"schema": 1, "date": today, "spent_usd": 0.0, "reserved_usd": 0.0,
                "calls": 0, "by_purpose": {}, "reservations": {}}

    def _fail_closed(self, today: str) -> dict[str, Any]:
        ledger = self._fresh(today)
        # An unreadable ledger cannot prove money was not spent, so the day is treated as spent.
        ledger["spent_usd"] = _money(self._s.daily_budget_usd)
        ledger["corrupt_recovered"] = True
        return ledger

    def _load(self, *, quarantine: bool) -> tuple[dict[str, Any], bool]:
        """Return (ledger for today, dirty). Dirty means the caller should persist it."""
        today = self._s.local_date().isoformat()
        raw, status = _read_json(self.path)
        if status == "missing":
            return self._fresh(today), False
        if status == "ok" and raw is not None:
            try:
                ledger = self._coerce(raw)
            except (TypeError, ValueError, KeyError):
                status = "corrupt"
            else:
                if ledger["date"] != today:
                    return self._fresh(today), True  # rollover: yesterday's open reservations are void
                return ledger, False
        if quarantine:
            try:
                os.replace(self.path, self.path.with_name("budget.json.corrupt"))
            except OSError:
                pass
        return self._fail_closed(today), quarantine

    @staticmethod
    def _coerce(raw: dict[str, Any]) -> dict[str, Any]:
        reservations = raw.get("reservations", {})
        by_purpose = raw.get("by_purpose", {})
        if not isinstance(reservations, dict) or not isinstance(by_purpose, dict):
            raise TypeError("bad ledger shape")
        clean: dict[str, dict[str, Any]] = {}
        for rid, res in reservations.items():
            clean[str(rid)] = {"purpose": str(res["purpose"]), "usd": _money(res["usd"]),
                               "ts": str(res.get("ts", ""))}
        date_text = str(raw["date"])
        date.fromisoformat(date_text)
        ledger = {
            "schema": 1,
            "date": date_text,
            "spent_usd": _money(raw["spent_usd"]),
            "calls": int(raw["calls"]),
            "by_purpose": {str(k): _money(v) for k, v in by_purpose.items()},
            "reservations": clean,
        }
        ledger["reserved_usd"] = _money(sum(r["usd"] for r in clean.values()))
        if raw.get("corrupt_recovered"):
            ledger["corrupt_recovered"] = True
        return ledger

    def _view(self, ledger: dict[str, Any]) -> dict[str, Any]:
        cap = self._s.daily_budget_usd
        reserved = _money(sum(r["usd"] for r in ledger["reservations"].values()))
        return {
            "date": ledger["date"],
            "spent_usd": ledger["spent_usd"],
            "reserved_usd": reserved,
            "calls": ledger["calls"],
            "by_purpose": dict(ledger["by_purpose"]),
            "daily_budget_usd": cap,
            "daily_calls": self._s.daily_calls,
            "remaining_usd": max(0.0, _money(cap - ledger["spent_usd"] - reserved)),
            "calls_left": max(0, self._s.daily_calls - ledger["calls"]),
        }

    def _save(self, ledger: dict[str, Any]) -> None:
        ledger["reserved_usd"] = _money(sum(r["usd"] for r in ledger["reservations"].values()))
        ledger["spent_usd"] = _money(ledger["spent_usd"])
        _write_json(self.path, ledger)

    def snapshot(self) -> dict[str, Any]:
        """Today's ledger as numbers. Read-only: it never rolls the file over or renames anything."""
        ledger, _ = self._load(quarantine=False)
        return self._view(ledger)

    def reserve(self, purpose: str, usd: float) -> Reservation:
        """Claim `usd` of today's budget and one call, or raise BudgetRefused.

        Refuses when spent + reserved + usd would pass daily_budget_usd, or when the call
        count has reached daily_calls. The reservation is on disk before this returns, so
        a crash mid-call still counts the full cap (design section 7).
        """
        if not isinstance(usd, (int, float)) or isinstance(usd, bool) or not math.isfinite(usd) or usd <= 0:
            raise ValueError(f"reservation must be a positive finite amount, got {usd!r}")
        with _locked(self.path):
            ledger, dirty = self._load(quarantine=True)
            snap = self._view(ledger)
            refusal: str | None = None
            if ledger["calls"] >= self._s.daily_calls:
                refusal = "calls"
            elif snap["spent_usd"] + snap["reserved_usd"] + usd > self._s.daily_budget_usd + _MONEY_EPSILON:
                refusal = "usd"
            if refusal is not None:
                if dirty:
                    self._save(ledger)
                raise BudgetRefused(refusal, snap)
            rid = uuid.uuid4().hex[:12]
            ledger["reservations"][rid] = {"purpose": purpose, "usd": _money(usd), "ts": iso(self._s.now())}
            ledger["calls"] += 1
            self._save(ledger)
            return Reservation(rid, purpose, _money(usd))

    def settle(self, reservation: Reservation | str, actual_usd: float) -> None:
        """Replace a reservation with what the call really cost.

        A reservation from before a date rollover no longer exists in today's ledger; its
        real cost still lands in today's spend (under "late_settle") rather than vanishing.
        """
        if not math.isfinite(actual_usd) or actual_usd < 0:
            raise ValueError(f"actual cost must be finite and not negative, got {actual_usd!r}")
        rid = reservation.id if isinstance(reservation, Reservation) else str(reservation)
        with _locked(self.path):
            ledger, _ = self._load(quarantine=True)
            held = ledger["reservations"].pop(rid, None)
            purpose = held["purpose"] if held else "late_settle"
            ledger["spent_usd"] = ledger["spent_usd"] + float(actual_usd)
            ledger["by_purpose"][purpose] = _money(ledger["by_purpose"].get(purpose, 0.0) + float(actual_usd))
            self._save(ledger)

    def release(self, reservation: Reservation | str) -> None:
        """Give back a reservation for a call that never started. Frees the money and the call."""
        rid = reservation.id if isinstance(reservation, Reservation) else str(reservation)
        with _locked(self.path):
            ledger, _ = self._load(quarantine=True)
            if ledger["reservations"].pop(rid, None) is not None:
                ledger["calls"] = max(0, ledger["calls"] - 1)
            self._save(ledger)


# --- breaker ---------------------------------------------------------------------------


class Breaker:
    """Circuit breaker for the Claude call: closed, open (cooling down), half_open (one probe)."""

    def __init__(self, store: "StateStore") -> None:
        self._s = store
        self.path = store.dir / "breaker.json"

    @staticmethod
    def _closed() -> dict[str, Any]:
        return {"schema": 1, "state": "closed", "reason": "", "opened_at": None, "until": None,
                "consecutive_failures": 0, "requires_human_reset": False, "probe_at": None}

    def _load(self) -> dict[str, Any]:
        raw, status = _read_json(self.path)
        if status == "missing":
            return self._closed()
        if status != "ok" or raw is None or raw.get("state") not in {"closed", "open", "half_open"}:
            # An unreadable breaker must not quietly turn into a closed one.
            data = self._closed()
            data.update(state="open", reason="breaker_state_unreadable", requires_human_reset=True,
                        opened_at=iso(self._s.now()))
            return data
        data = self._closed()
        data.update(raw)
        return data

    def _save(self, data: dict[str, Any]) -> None:
        _write_json(self.path, data)

    def _open(self, data: dict[str, Any], reason: str, requires_reset: bool) -> None:
        now = self._s.now()
        data.update(state="open", reason=reason, opened_at=iso(now), probe_at=None,
                    until=iso(now + self._s.breaker_cooldown))
        data["requires_human_reset"] = bool(data.get("requires_human_reset")) or requires_reset

    def peek(self) -> dict[str, Any]:
        """The stored breaker state, with no side effect (for `jarvis status`)."""
        return self._load()

    def blocked(self) -> bool:
        """True when `is_open` would say True, without handing out a probe or changing anything.

        For callers that have more checks to pass (network, budget) before they can really
        call: they ask this first, and claim the half-open probe with `is_open` only once
        nothing else can stop them.
        """
        with _locked(self.path):
            data = self._load()
            state = data["state"]
            if state == "closed":
                return False
            if data["requires_human_reset"]:
                return True
            now = self._s.now()
            if state == "open":
                until = parse_iso(data["until"]) if data["until"] else now
                return now < until
            if state == "half_open":
                probe_at = parse_iso(data["probe_at"]) if data.get("probe_at") else None
                return probe_at is not None and now - probe_at < self._s.breaker_cooldown
            return True

    def release_probe(self) -> None:
        """Give back a half-open probe that was handed out but never used (the call did not start)."""
        with _locked(self.path):
            data = self._load()
            if data["state"] == "half_open" and not data["requires_human_reset"]:
                data["probe_at"] = None
                self._save(data)

    def is_open(self) -> bool:
        """True means do not call. False means you may; in half_open exactly one caller gets False.

        This is the permission check, not a pure read: after the cooldown it moves open to
        half_open and hands the probe to the caller that asked first.
        """
        with _locked(self.path):
            data = self._load()
            state = data["state"]
            if state == "closed":
                return False
            if data["requires_human_reset"]:
                return True
            now = self._s.now()
            if state == "open":
                until = parse_iso(data["until"]) if data["until"] else now
                if now < until:
                    return True
                data.update(state="half_open", probe_at=iso(now))
                self._save(data)
                return False
            if state == "half_open":
                probe_at = parse_iso(data["probe_at"]) if data.get("probe_at") else None
                if probe_at is not None and now - probe_at < self._s.breaker_cooldown:
                    return True
                data["probe_at"] = iso(now)  # the last probe vanished (a crash); allow a new one
                self._save(data)
                return False
            return True

    def record_success(self) -> None:
        """A call worked. Closes a half-open breaker; never clears one that needs a human."""
        with _locked(self.path):
            data = self._load()
            if data["requires_human_reset"]:
                return
            if data["state"] == "open":
                return  # a straggler finishing during cooldown proves nothing about now
            data.update(state="closed", reason="", opened_at=None, until=None, probe_at=None,
                        consecutive_failures=0)
            self._save(data)

    def record_failure(self, reason: str = "") -> bool:
        """Count a failed call. Returns True when this failure opened (or re-opened) the breaker."""
        with _locked(self.path):
            data = self._load()
            data["consecutive_failures"] = int(data["consecutive_failures"]) + 1
            opened = False
            if data["state"] == "half_open":
                self._open(data, reason or "probe_failed", False)
                opened = True
            elif data["state"] == "closed" and data["consecutive_failures"] >= self._s.breaker_threshold:
                self._open(data, reason or "consecutive_failures", False)
                opened = True
            self._save(data)
            return opened

    def trip(self, reason: str, requires_reset: bool = False) -> None:
        """Open now. With requires_reset only `reset` closes it (isolation breach, `wrong --leak`)."""
        with _locked(self.path):
            data = self._load()
            self._open(data, reason, requires_reset)
            self._save(data)

    def reset(self, reason: str = "") -> None:
        """The human close. Clears everything, including the requires-reset flag."""
        with _locked(self.path):
            data = self._closed()
            data["last_reset_reason"] = reason
            data["last_reset_at"] = iso(self._s.now())
            self._save(data)


# --- watermark -------------------------------------------------------------------------


class Watermark:
    """End of the last digest window that was successfully written. It only moves forward."""

    def __init__(self, store: "StateStore") -> None:
        self.path = store.dir / "watermark.json"

    def record(self) -> dict[str, Any] | None:
        data, status = _read_json(self.path)
        return data if status == "ok" else None

    def get(self) -> datetime | None:
        data = self.record()
        if not data or not data.get("last_success_end"):
            return None
        try:
            return parse_iso(str(data["last_success_end"]))
        except ValueError:
            return None  # unknown means "use the default window", which is safe

    def advance(self, end: datetime, job_id: str = "") -> bool:
        """Move the watermark to `end` if that is later. Returns whether it moved."""
        end_text = iso(end)  # raises on a naive datetime
        with _locked(self.path):
            current = self.get()
            if current is not None and parse_iso(end_text) <= current:
                return False
            _write_json(self.path, {"schema": 1, "last_success_end": end_text, "job_id": job_id})
            return True


# --- item history and attention decisions (docs/hub-rework-contract.md sections 6 and 7) ---

ITEM_HISTORY_FILE = "item-history.json"
ATTENTION_DIR = "attention"
# An open key not shown for more than this many days is dropped from the daily note and listed in the
# weekly review (contract 6.1 rule 5). The same number as common.STALE_AFTER_DAYS, restated here so this
# module keeps its single import of common.
DROP_AFTER_DAYS = 7
_ATTENTION_ACTIONS = frozenset({"done", "snooze"})


def _empty_history() -> dict[str, Any]:
    return {"updated": None, "last_run": None, "items": {}}


def _fresh_record(today: str) -> dict[str, Any]:
    return {"id": "", "text": "", "first_seen": today, "last_seen": today, "times_shown": 0, "sections": [],
            "status": "open", "snoozed_until": None, "resolved_at": None}


def update_history(history: dict[str, Any], records: list[dict[str, Any]], decisions: list[dict[str, Any]],
                   today: date, job_id: str) -> dict[str, int]:
    """Apply one run's sidecar records and the attention decisions to the history, in place.

    Pure: no I/O. Rules (contract 6.1): every record bumps `times_shown`, appends its section and refreshes
    `last_seen`, `id` and `text`; a key printed under Decided is filed done at once; a dropped or snoozed
    key that is printed again reopens with its old `first_seen`; a done decision files the key done; a
    snooze decision sets `snoozed` while `until` is ahead and reopens the key once it is not; finally every
    open key whose `last_seen` is older than DROP_AFTER_DAYS becomes dropped. Returns the counts of what
    changed: new, resolved, dropped, returned.
    """
    items = history.setdefault("items", {})
    if not isinstance(items, dict):
        items = history["items"] = {}
    day = today.isoformat()
    counts = {"new": 0, "resolved": 0, "dropped": 0, "returned": 0}
    seen: set[str] = set()
    for rec in records:
        key = str(rec.get("key") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        entry = items.get(key)
        if not isinstance(entry, dict):
            entry = items[key] = _fresh_record(day)
            counts["new"] += 1
        entry["id"] = str(rec.get("id") or entry.get("id") or "")
        entry["text"] = str(rec.get("text") or entry.get("text") or "")
        entry["last_seen"] = day
        entry["times_shown"] = int(entry.get("times_shown") or 0) + 1
        sections = entry.setdefault("sections", [])
        if not isinstance(sections, list):
            sections = entry["sections"] = []
        sections.append(str(rec.get("section") or ""))
        if rec.get("section") == "decided":
            if entry.get("status") != "done":
                counts["resolved"] += 1
            entry.update(status="done", resolved_at=day)
        elif entry.get("status") in ("dropped", "snoozed"):
            if entry.get("status") == "snoozed":
                counts["returned"] += 1
            entry.update(status="open", resolved_at=None, snoozed_until=None)
    for d in decisions:
        key = str(d.get("key") or "") if isinstance(d, dict) else ""
        action = d.get("action") if isinstance(d, dict) else None
        if not key or action not in _ATTENTION_ACTIONS:
            continue
        entry = items.get(key)
        if not isinstance(entry, dict):
            entry = items[key] = _fresh_record(day)
            entry["id"], entry["text"] = str(d.get("id") or ""), key
        if action == "done":
            if entry.get("status") != "done":
                counts["resolved"] += 1
                entry.update(status="done", resolved_at=day)
        elif entry.get("status") != "done":
            until = str(d.get("until") or "")
            if until > day:
                entry.update(status="snoozed", snoozed_until=until, resolved_at=None)
            elif entry.get("status") == "snoozed":
                entry.update(status="open", snoozed_until=None)
    cutoff = (today - timedelta(days=DROP_AFTER_DAYS)).isoformat()
    for entry in items.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "snoozed" and str(entry.get("snoozed_until") or "") <= day:
            entry.update(status="open", snoozed_until=None)
        if entry.get("status") == "open" and str(entry.get("last_seen") or "") < cutoff:
            entry.update(status="dropped", resolved_at=day)
            counts["dropped"] += 1
    history["updated"] = day
    history["last_run"] = job_id
    return counts


class ItemHistory:
    """`state/item-history.json` and the `state/attention/` decisions: the writer's memory of item keys.

    The digest writer is the only writer of the history (read-modify-write under `_locked`); the hub and the
    CLI write the attention files and read both. Everything stored here came from View-filtered sidecar
    records, so a sensitive-held item has no key, text or id in either place.
    """

    def __init__(self, store: "StateStore") -> None:
        self.path = store.dir / ITEM_HISTORY_FILE
        self.attention_dir = store.dir / ATTENTION_DIR

    def load(self) -> dict[str, Any]:
        """The history, or an empty one when the file is missing or unreadable (a corrupt file is replaced
        on the next apply; nothing here is a safety decision)."""
        data, status = _read_json(self.path)
        if status != "ok" or data is None or not isinstance(data.get("items"), dict):
            return _empty_history()
        return data

    def decisions(self) -> list[dict[str, Any]]:
        """Every readable `state/attention/*.json` with a key and a known action, by file name."""
        if not self.attention_dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        try:
            paths = sorted(p for p in self.attention_dir.iterdir() if p.suffix == ".json" and p.is_file())
        except OSError:
            return []
        for path in paths:
            data, status = _read_json(path)
            if status == "ok" and data is not None and data.get("key") and data.get("action") in _ATTENTION_ACTIONS:
                out.append(data)
        return out

    def apply(self, records: list[dict[str, Any]], decisions: list[dict[str, Any]], today: date,
              job_id: str) -> dict[str, int]:
        """Read, update with `update_history`, write back, under the in-process and cross-process locks."""
        with _locked(self.path):
            history = self.load()
            counts = update_history(history, records, decisions, today, job_id)
            _write_json(self.path, history)
        return counts


# --- the store -------------------------------------------------------------------------


class StateStore:
    """Handle on the `state/` directory.

    `budget`, `breaker` and `watermark` are the three ledgers; the methods here cover
    liveness. `clock` returns an aware time (tests pass a FakeClock). `tz` decides which
    calendar date the budget rolls over on; None means the machine's local zone.
    """

    def __init__(self, dir: str | os.PathLike[str], *, daily_budget_usd: float = 2.00,
                 daily_calls: int = 6, clock: Clock | None = None, tz: tzinfo | None = None,
                 breaker_threshold: int = 3, breaker_cooldown_minutes: int = 60) -> None:
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.daily_budget_usd = float(daily_budget_usd)
        self.daily_calls = int(daily_calls)
        self._clock: Clock = clock or now_utc
        self._tz = tz
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown = timedelta(minutes=breaker_cooldown_minutes)
        self._daemon_lock: FileLock | None = None
        self.budget = Budget(self)
        self.breaker = Breaker(self)
        self.watermark = Watermark(self)
        self.items = ItemHistory(self)

    @classmethod
    def from_config(cls, cfg: "Config", *, clock: Clock | None = None, tz: tzinfo | None = None) -> "StateStore":
        return cls(cfg.daemon.state_dir, daily_budget_usd=cfg.claude.daily_budget_usd,
                   daily_calls=cfg.claude.daily_calls, clock=clock, tz=tz)

    def now(self) -> datetime:
        return self._clock()

    def local_date(self) -> date:
        now = self._clock()
        return (now.astimezone(self._tz) if self._tz is not None else now.astimezone()).date()

    # --- liveness ---

    def heartbeat(self, job_id: str | None = None, *, mode: str = "daemon") -> None:
        """Rewrite heartbeat.json. Beating also removes the clean-shutdown marker: a running
        daemon has not shut down, so the marker only ever spans stop to the next start."""
        _write_json(self.dir / "heartbeat.json", {
            "ts": iso(self.now()), "pid": os.getpid(), "job_id": job_id,
            "version": __version__, "mode": mode,
        })
        try:
            (self.dir / "clean_shutdown").unlink(missing_ok=True)
        except OSError:
            pass

    def read_heartbeat(self) -> dict[str, Any] | None:
        data, status = _read_json(self.dir / "heartbeat.json")
        return data if status == "ok" else None

    def previous_exit(self) -> str:
        """"first" (no trace of an earlier run), "clean" (orderly stop) or "unclean".

        Call it at startup before the first heartbeat, which clears the marker.
        """
        if (self.dir / "clean_shutdown").exists():
            return "clean"
        if (self.dir / "heartbeat.json").exists():
            return "unclean"
        return "first"

    def previous_exit_clean(self) -> bool:
        return self.previous_exit() != "unclean"

    def mark_clean_shutdown(self) -> None:
        """Write the marker. Stop the heartbeat thread first, or its next beat removes it."""
        atomic_write_text(self.dir / "clean_shutdown", iso(self.now()) + "\n")

    def killed(self) -> bool:
        """state/KILL exists: stop, and refuse to restart until a human removes it."""
        return (self.dir / "KILL").exists()

    def set_pause(self, until: datetime | None = None, reason: str = "") -> None:
        _write_json(self.dir / "PAUSE", {
            "until": iso(until) if until is not None else None,
            "reason": reason, "set_at": iso(self.now()),
        })

    def clear_pause(self) -> None:
        (self.dir / "PAUSE").unlink(missing_ok=True)

    def pause_info(self) -> dict[str, Any] | None:
        data, status = _read_json(self.dir / "PAUSE")
        if status == "missing":
            return None
        return data if status == "ok" else {"until": None, "reason": "pause_file_unreadable"}

    def paused(self) -> bool:
        """True while PAUSE exists and has not expired. An unreadable file pauses (fail closed)."""
        info = self.pause_info()
        if info is None:
            return False
        until = info.get("until")
        if not until:
            return True
        try:
            return self.now() < parse_iso(str(until))
        except ValueError:
            return True

    # --- single instance ---

    def acquire_daemon_lock(self) -> FileLock:
        """Take state/daemon.lock for the life of the process, or raise AlreadyRunning.

        The OS drops the lock when the holder dies, so a crash never leaves it stale.
        """
        if self._daemon_lock is not None:
            raise AlreadyRunning("this process already holds the daemon lock")
        lock = FileLock(self.dir / "daemon.lock", timeout=0.0)
        try:
            won = lock.acquire(0.0)
        except (FileBusy, OSError) as exc:
            raise AlreadyRunning(f"cannot open the daemon lock: {exc}") from exc
        if not won:
            raise AlreadyRunning("another daemon holds state/daemon.lock")
        self._daemon_lock = lock
        return lock

    def release_daemon_lock(self) -> None:
        lock, self._daemon_lock = self._daemon_lock, None
        if lock is not None:
            lock.release()
