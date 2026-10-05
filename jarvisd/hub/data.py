"""Everything the hub shows, read from the daemon's own files and never written.

Why this is not just `cli.collect_status`: several existing read paths have write side effects
that are right for the daemon and wrong for a viewer. JobStore creates its folders and moves a
corrupt job file to failed/, AuditLog.head() truncates a torn last line, daemon_running()
creates state/daemon.lock. The hub opens files for reading only, takes no lock, creates no
folder and quarantines nothing. It still goes through the existing models and helpers for
every format (Job, RunManifest, AuditLog.records and verify, Budget.snapshot, Breaker.peek),
so a format change shows up in one place.

Held references are shown by id, kind and reason. `source_ref` (a path) is dropped here, so no
view can print it: that stays terminal-only (`jarvis held`, design D5).

Layer L3 (hub). Imports the layers below it; nothing outside the hub imports this module.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jarvisd import __version__, daemon
from jarvisd import local as local_tier
from jarvisd.audit import AuditLog
from jarvisd.cli import _digest_files, _raw_dir, _status_lines
from jarvisd.common import iso, local_now, parse_iso
from jarvisd.config import Config
from jarvisd.hub.mdhtml import section, split_front_matter
from jarvisd.jobstore import JOB_STATES, STATES, _pid_alive
from jarvisd.models import Job, RunManifest
from jarvisd.scheduler import DIGEST_KIND, due_at
from jarvisd.state import StateStore

DIGEST_ID = re.compile(r"^digest-\d{4}-\d{2}-\d{2}(?:-r\d+)?$")
MAX_NOTE_BYTES = 2_000_000
# Fields that are chain plumbing, not content, so the Audit view leaves them out of the details.
_AUDIT_PLUMBING = {"ts", "event", "seq", "prev", "h", "run_id", "pid", "ver"}
_ZERO_BREAKER = {"state": "closed", "reason": "", "opened_at": None, "until": None,
                 "consecutive_failures": 0, "requires_human_reset": False, "probe_at": None}

_REPO_LINE = re.compile(
    r"^- (?P<name>\S+?)(?: \((?P<tag>[^)]+)\))?:? (?:branch (?P<branch>.+?), )?"
    r"(?P<commits>\d+) commits? since window, (?P<modified>\d+) modified, (?P<untracked>\d+) untracked"
    r"(?P<rest>.*)$")
_ITEM_ID = re.compile(r"\[([0-9a-f]{8})\]")


def _read_json(path: Path) -> Any | None:
    """A JSON file, or None for missing, unreadable or malformed. Retries a Windows sharing clash."""
    for attempt in range(4):
        try:
            return json.loads(path.read_bytes().decode("utf-8-sig"))
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == 3:
                return None
            time.sleep(0.02)
        except (OSError, ValueError):
            return None
    return None


def _json_files(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.glob("*.json") if not p.name.startswith("."))
    except OSError:
        return []


@dataclass
class AuditSnapshot:
    """The audit log as it was at one file signature: parsed once, verified once."""

    signature: tuple[Any, ...]
    records: list[dict[str, Any]]
    verified: bool
    broken_at: int | None
    files: int

    @property
    def head(self) -> tuple[int, str]:
        if not self.records:
            return 0, ""
        last = self.records[-1]
        return int(last.get("seq", 0)), str(last.get("h", ""))


@dataclass
class HubData:
    cfg: Config
    clock: Callable[[], datetime] | None = None
    _audit_cache: AuditSnapshot | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # --- small helpers ---------------------------------------------------------------------

    def now(self) -> datetime:
        return (self.clock or local_now)()

    @property
    def queue_dir(self) -> Path:
        return Path(self.cfg.paths.queue)

    @property
    def state_dir(self) -> Path:
        return Path(self.cfg.daemon.state_dir)

    def _state(self) -> StateStore | None:
        """The state store, only when its folder exists (the constructor would create it)."""
        if not self.state_dir.is_dir():
            return None
        return StateStore.from_config(self.cfg, clock=self.clock)

    # --- audit ---------------------------------------------------------------------------------

    def _audit_log(self) -> AuditLog | None:
        path = daemon.audit_path(self.cfg)
        if not path.parent.is_dir():
            return None  # the constructor would create the folder
        return AuditLog(path, self.cfg.retention.audit_max_bytes, self.cfg.retention.audit_keep_days,
                        clock=self.clock, mirror_stdout=False)

    def audit(self) -> AuditSnapshot:
        """Parsed and verified audit log, recomputed only when a file's size or mtime moved."""
        log = self._audit_log()
        files = log.all_files() if log is not None else []
        signature: list[Any] = []
        for path in files:
            try:
                st = path.stat()
            except OSError:
                continue
            signature.append((path.name, st.st_size, st.st_mtime_ns))
        sig = tuple(signature)
        with self._lock:
            cached = self._audit_cache
            if cached is not None and cached.signature == sig:
                return cached
            if log is None or not files:
                snap = AuditSnapshot(sig, [], True, None, 0)
            else:
                ok, bad = log.verify()
                if not ok:  # the daemon may have been mid-append; look once more before saying broken
                    time.sleep(0.3)
                    ok, bad = log.verify()
                snap = AuditSnapshot(sig, log.records(), ok, bad, len(files))
            self._audit_cache = snap
            return snap

    def audit_rows(self, limit: int) -> list[dict[str, Any]]:
        """The newest `limit` records, newest first, as display rows (no hash chain plumbing)."""
        rows = []
        for rec in reversed(self.audit().records[-max(limit, 0):]):
            details = {k: v for k, v in rec.items() if k not in _AUDIT_PLUMBING}
            rows.append({"seq": rec.get("seq"), "ts": rec.get("ts"), "event": rec.get("event"),
                         "hash": str(rec.get("h", ""))[:12], "details": details})
        return rows

    def cost_on(self, day: date) -> float:
        """claude_call cost for one local date: AuditLog.cost_on's rule, over the cached records."""
        tz = self.now().tzinfo
        total = 0.0
        for rec in self.audit().records:
            if rec.get("event") != "claude_call":
                continue
            cost = rec.get("total_cost_usd")
            if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost):
                continue
            try:
                stamp = parse_iso(str(rec.get("ts")))
            except ValueError:
                continue
            if stamp.astimezone(tz).date() == day:
                total += cost
        return round(total, 6)

    def witness(self, seq: int | None) -> dict[str, Any]:
        """What the audit log says about a run's recorded seq: the record's hash, or that it is gone."""
        if seq is None:
            return {"seq": None, "found": False, "hash": ""}
        for rec in self.audit().records:
            if rec.get("seq") == seq:
                return {"seq": seq, "found": True, "hash": str(rec.get("h", ""))[:12]}
        return {"seq": seq, "found": False, "hash": ""}

    # --- queue ---------------------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        return {state: len(_json_files(self.queue_dir / state)) for state in STATES}

    def jobs(self, state: str) -> list[Job]:
        """Readable jobs of one state, oldest first. A corrupt file is skipped, not moved."""
        if state not in JOB_STATES:
            raise ValueError(f"not a job state: {state!r}")
        found: list[Job] = []
        for path in _json_files(self.queue_dir / state):
            raw = _read_json(path)
            if not isinstance(raw, dict):
                continue
            try:
                found.append(Job.model_validate(raw))
            except ValidationError:
                continue
        return sorted(found, key=lambda j: (j.created_at, j.id))

    def held(self, day: str | None = None) -> list[dict[str, Any]]:
        """Held references without their source path. `day` keeps those recorded for that date."""
        out: list[dict[str, Any]] = []
        for path in _json_files(self.queue_dir / "held"):
            raw = _read_json(path)
            if not isinstance(raw, dict):
                continue
            ids = [str(d) for d in raw.get("digest_ids", []) if isinstance(d, str)]
            if day is not None and not any(d == f"digest-{day}" or d.startswith(f"digest-{day}-") for d in ids):
                continue
            out.append({"id": str(raw.get("id", path.stem)), "kind": str(raw.get("kind", "")),
                        "reason": str(raw.get("reason", "")), "first_seen": raw.get("first_seen"),
                        "last_seen": raw.get("last_seen"), "expires_at": raw.get("expires_at"),
                        "digest_ids": ids})
        return sorted(out, key=lambda r: (str(r["last_seen"] or ""), r["id"]), reverse=True)

    # --- digests -------------------------------------------------------------------------------

    def digest_files(self) -> list[Path]:
        return _digest_files(_raw_dir(self.cfg), None)

    def read_digest(self, path: Path) -> dict[str, Any] | None:
        try:
            if path.stat().st_size > MAX_NOTE_BYTES:
                return None
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            return None
        meta, body = split_front_matter(text)
        return {"job_id": path.stem, "path": path.name, "meta": meta, "body": body}

    def latest_digest(self) -> dict[str, Any] | None:
        for path in reversed(self.digest_files()):
            note = self.read_digest(path)
            if note is not None:
                return note
        return None

    def digest_by_id(self, job_id: str) -> dict[str, Any] | None:
        """One digest note. The id must look like digest-YYYY-MM-DD[-rN], so no path can be spelled."""
        if not DIGEST_ID.match(job_id):
            return None
        for path in self.digest_files():
            if path.stem == job_id:
                return self.read_digest(path)
        return None

    def repos(self) -> dict[str, Any]:
        """The Repos section of the latest digest: parsed rows, plus every other line as text."""
        digest = self.latest_digest()
        if digest is None:
            return {"digest": None, "rows": [], "other": []}
        rows: list[dict[str, str]] = []
        other: list[str] = []
        for line in section(digest["body"], "Repos"):
            if not line.strip():
                continue
            m = _REPO_LINE.match(line)
            if not m:
                other.append(line.lstrip("- ").strip())
                continue
            rest = m.group("rest")
            ident = _ITEM_ID.search(rest)
            note = _ITEM_ID.sub("", rest).strip().lstrip(",:; ").strip()
            rows.append({"name": m.group("name"), "tag": m.group("tag") or "", "branch": m.group("branch") or "",
                         "commits": m.group("commits"), "modified": m.group("modified"),
                         "untracked": m.group("untracked"), "id": ident.group(1) if ident else "", "note": note})
        return {"digest": {"job_id": digest["job_id"], "date": digest["meta"].get("date", ""),
                           "generated_at": digest["meta"].get("generated_at", "")}, "rows": rows, "other": other}

    # --- runs ----------------------------------------------------------------------------------

    def runs(self) -> list[dict[str, Any]]:
        """The digest ledger, newest first: run manifests joined with their queue job."""
        rows: dict[str, dict[str, Any]] = {}
        try:
            manifest_paths = sorted((self.state_dir / "runs").glob("*/run.json"))
        except OSError:
            manifest_paths = []
        for path in manifest_paths:
            raw = _read_json(path)
            if not isinstance(raw, dict):
                continue
            try:
                m = RunManifest.model_validate(raw)
            except ValidationError:
                continue
            rows[m.job_id] = {
                "job_id": m.job_id, "status": m.status, "stamp": m.finished_at or m.started_at or "",
                "counts": m.counts, "cost_usd": m.cost_usd, "audit_seq": m.audit_seq,
            }
        for state in JOB_STATES:
            for job in self.jobs(state):
                if job.kind != DIGEST_KIND or job.id in rows:
                    continue
                result = job.result or {}
                rows[job.id] = {
                    "job_id": job.id, "status": str(result.get("status") or job.state),
                    "stamp": job.history[-1].ts if job.history else job.created_at, "counts": {},
                    "cost_usd": result.get("cost_usd", job.cost_usd), "audit_seq": None,
                }
        notes = {p.stem for p in self.digest_files()}
        out = []
        for row in rows.values():
            m = re.match(r"^digest-(\d{4}-\d{2}-\d{2})", row["job_id"])
            row["date"] = m.group(1) if m else str(row["stamp"])[:10]
            row["has_note"] = row["job_id"] in notes and bool(DIGEST_ID.match(row["job_id"]))
            row["witness"] = self.witness(row["audit_seq"])
            out.append(row)
        return sorted(out, key=lambda r: (r["stamp"], r["job_id"]), reverse=True)

    # --- status --------------------------------------------------------------------------------

    def budget(self, state: StateStore | None = None) -> dict[str, Any]:
        """Today's ledger as Budget.snapshot reports it, or an untouched one when there is no state folder."""
        state = state or self._state()
        if state is not None:
            return state.budget.snapshot()
        cap, calls = self.cfg.claude.daily_budget_usd, self.cfg.claude.daily_calls
        return {"date": self.now().date().isoformat(), "spent_usd": 0.0, "reserved_usd": 0.0, "calls": 0,
                "by_purpose": {}, "daily_budget_usd": cap, "daily_calls": calls, "remaining_usd": cap,
                "calls_left": calls}

    def breaker(self, state: StateStore | None = None) -> dict[str, Any]:
        state = state or self._state()
        return state.breaker.peek() if state is not None else dict(_ZERO_BREAKER)

    def flags(self) -> dict[str, Any]:
        """The cheap facts every page header shows: liveness, kill file, pause."""
        state = self._state()
        running, age = self.daemon_alive(state.read_heartbeat() if state else None, self.now())
        return {"running": running, "age": age, "kill": state.killed() if state else False,
                "pause": state.pause_info() if (state and state.paused()) else None}

    def _last_digest(self, now: datetime) -> dict[str, Any] | None:
        best: tuple[str, Job] | None = None
        for job in self.jobs("done"):
            if job.kind != DIGEST_KIND or not job.result or job.result.get("status") == "noop":
                continue
            stamp = job.history[-1].ts if job.history else job.created_at
            if best is None or stamp > best[0]:
                best = (stamp, job)
        if best is None:
            return None
        stamp, job = best
        result = job.result or {}
        return {"job_id": job.id, "path": result.get("note_path"), "status": result.get("status"),
                "cost_usd": result.get("cost_usd", 0.0),
                "age_hours": round((now - parse_iso(stamp)).total_seconds() / 3600, 1)}

    def daemon_alive(self, beat: dict[str, Any] | None, now: datetime) -> tuple[bool, float | None]:
        """Running means a fresh heartbeat from a live pid. The CLI probes the single-instance
        lock instead, which creates the lock file; a viewer must not."""
        if beat is None:
            return False, None
        try:
            age = round((now - parse_iso(str(beat["ts"]))).total_seconds(), 1)
        except (KeyError, ValueError):
            return False, None
        fresh = age <= max(3 * self.cfg.daemon.heartbeat_seconds, 120)
        try:
            alive = _pid_alive(int(beat.get("pid", 0)))
        except (TypeError, ValueError):
            alive = False
        return fresh and alive, age

    def status(self) -> dict[str, Any]:
        """The same keys as `jarvis status --json`, read without side effects."""
        cfg, now = self.cfg, self.now()
        state = self._state()
        beat = state.read_heartbeat() if state else None
        running, age = self.daemon_alive(beat, now)
        today = now.date()
        done_today = any((self.queue_dir / s / f"digest-{today.isoformat()}.json").exists() for s in STATES)
        next_day = today + timedelta(days=1) if done_today else today
        watermark = state.watermark.get() if state else None
        snap = self.audit()
        seq, head = snap.head
        budget, breaker = self.budget(state), self.breaker(state)
        return {
            "running": running,
            "pid": beat.get("pid") if (beat and running) else None,
            "heartbeat_age_s": age,
            "version": beat.get("version") if beat else __version__,
            "mode": beat.get("mode") if beat else None,
            "claude_cli_version": _last_cli_version_from(snap.records, now),
            "local_tier": local_tier.gate_state(cfg),
            "breaker": breaker,
            "budget": budget,
            "queue": self.counts(),
            "last_digest": self._last_digest(now),
            "watermark": iso(watermark) if watermark else None,
            "next_due": iso(due_at(cfg, next_day, now.tzinfo)),
            "kill": state.killed() if state else False,
            "pause": state.pause_info() if (state and state.paused()) else None,
            "held_count": len(self.held()),
            "audit": {"seq": seq, "head": head or "0" * 64},
        }

    @staticmethod
    def status_lines(data: dict[str, Any]) -> list[str]:
        """The exact text `jarvis status` prints (the CLI's own formatter)."""
        return _status_lines(data)


def _last_cli_version_from(records: list[dict[str, Any]], now: datetime) -> str | None:
    """Same rule as cli._last_cli_version, over records already in memory."""
    floor = now - timedelta(days=60)
    for rec in reversed(records):
        if rec.get("event") != "daemon_start" or not rec.get("claude_cli_version"):
            continue
        try:
            if parse_iso(str(rec.get("ts"))) < floor:
                continue
        except ValueError:
            continue
        return str(rec["claude_cli_version"])
    return None
