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

One model per request: each route calls one method that returns everything its view needs
(`today`, `projects_page`, `activity`, `status`), and the views never call back in here. Folders
that are read on every request (held, proposals, manifests, the newest queue files, the digest
notes) are parsed once per directory signature (name, size, mtime_ns of each file), the way the
audit log already is, so `/api/status`, which the face polls every 5 s, reads almost nothing warm.

Layer L3 (hub). Imports the layers below it; nothing outside the hub imports this module.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
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
from jarvisd.hub.digestparse import FAILING_CI_TOKENS, parse_note, repo_rows
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

# Projects view: how much digest history is read, and the fixed risk threshold for uncommitted work.
_HISTORY_DIGESTS = 60
DIRTY_RISK_DAYS = 2
_FAILING_CI_TOKENS = FAILING_CI_TOKENS
# Today: how many waiting proposals get an inline Confirm. Activity: the width of its strip.
WAITING_SHOWN = 3
STRIP_DAYS = 7
NEWEST_JOBS = 3
_REPO_LINE = re.compile(r"^- (?P<name>\S+?)(?: \((?P<tag>[^)]+)\))?:? (?:branch (?P<branch>.+?), )?"
                        r"(?P<commits>\d+) commits? since window")


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


def _signature(paths: Iterable[Path]) -> tuple[tuple[str, int, int], ...]:
    """(name, size, mtime_ns) per file: what a folder looks like without reading it."""
    out = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        out.append((path.name, st.st_size, st.st_mtime_ns))
    return tuple(out)


def _job_of(path: Path) -> Job | None:
    raw = _read_json(path)
    if not isinstance(raw, dict):
        return None
    try:
        return Job.model_validate(raw)
    except ValidationError:
        return None


def _job_stamp(job: Job) -> str:
    return job.history[-1].ts if job.history else job.created_at


@dataclass
class AuditSnapshot:
    """The audit log as it was at one file signature: parsed once, verified once, indexed by seq once."""

    signature: tuple[Any, ...]
    records: list[dict[str, Any]]
    verified: bool
    broken_at: int | None
    files: int
    index: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for rec in self.records:
            seq = rec.get("seq")
            if isinstance(seq, int):
                self.index[seq] = rec

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
    _dir_cache: dict[str, tuple[tuple[Any, ...], Any]] = field(default_factory=dict, init=False, repr=False)
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

    def _cached(self, key: str, paths: list[Path], build: Callable[[list[Path]], Any]) -> Any:
        """`build(paths)` once per folder signature; the same value until a file is added, removed or changed."""
        sig = _signature(paths)
        with self._lock:
            hit = self._dir_cache.get(key)
            if hit is not None and hit[0] == sig:
                return hit[1]
        value = build(paths)
        with self._lock:
            self._dir_cache[key] = (sig, value)
        return value

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
        sig = _signature(files)
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
        rec = self.audit().index.get(seq)
        if rec is None:
            return {"seq": seq, "found": False, "hash": ""}
        return {"seq": seq, "found": True, "hash": str(rec.get("h", ""))[:12]}

    # --- queue ---------------------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        return {state: len(_json_files(self.queue_dir / state)) for state in STATES}

    def jobs(self, state: str) -> list[Job]:
        """Readable jobs of one state, oldest first. A corrupt file is skipped, not moved."""
        if state not in JOB_STATES:
            raise ValueError(f"not a job state: {state!r}")

        def build(paths: list[Path]) -> list[Job]:
            found = [j for j in (_job_of(p) for p in paths) if j is not None]
            return sorted(found, key=lambda j: (j.created_at, j.id))

        return self._cached(f"jobs:{state}", _json_files(self.queue_dir / state), build)

    def newest_jobs(self, state: str) -> list[Job]:
        """The jobs behind the NEWEST_JOBS most recently modified files of one state, newest first. The cost does
        not grow with the age of the queue, and the result is cached by the folder's signature."""
        folder = self.queue_dir / state
        try:
            files = sorted(_json_files(folder), key=lambda p: p.stat().st_mtime, reverse=True)[:NEWEST_JOBS]
        except OSError:
            files = []

        def build(paths: list[Path]) -> list[Job]:
            found = [j for j in (_job_of(p) for p in paths) if j is not None]
            return sorted(found, key=_job_stamp, reverse=True)

        return self._cached(f"newest:{state}", files, build)

    def held(self, day: str | None = None) -> list[dict[str, Any]]:
        """Held references without their source path. `day` keeps those recorded for that date."""

        def build(paths: list[Path]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for path in paths:
                raw = _read_json(path)
                if not isinstance(raw, dict):
                    continue
                ids = [str(d) for d in raw.get("digest_ids", []) if isinstance(d, str)]
                out.append({"id": str(raw.get("id", path.stem)), "kind": str(raw.get("kind", "")),
                            "reason": str(raw.get("reason", "")), "first_seen": raw.get("first_seen"),
                            "last_seen": raw.get("last_seen"), "expires_at": raw.get("expires_at"),
                            "digest_ids": ids})
            return sorted(out, key=lambda r: (str(r["last_seen"] or ""), r["id"]), reverse=True)

        refs: list[dict[str, Any]] = self._cached("held", _json_files(self.queue_dir / "held"), build)
        if day is None:
            return list(refs)
        return [r for r in refs if any(d == f"digest-{day}" or d.startswith(f"digest-{day}-") for d in r["digest_ids"])]

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
        lines = [ln for ln in section(digest["body"], "Repos") if ln.strip()]
        other = [ln.lstrip("- ").strip() for ln in lines if not _REPO_LINE.match(ln.strip())]
        return {"digest": {"job_id": digest["job_id"], "date": digest["meta"].get("date", ""),
                           "generated_at": digest["meta"].get("generated_at", "")},
                "rows": repo_rows(lines), "other": other}

    def _digest_history(self) -> list[dict[str, Any]]:
        """Parsed facts of the newest digest notes, oldest first, one per day: repos, GitHub, the active task, the
        number of decisions and the held count. Cached by the digest folder's signature."""
        files = self.digest_files()[-_HISTORY_DIGESTS:]

        def build(paths: list[Path]) -> list[dict[str, Any]]:
            by_day: dict[date, dict[str, Any]] = {}
            for path in paths:
                note = self.read_digest(path)
                m = re.match(r"^digest-(\d{4}-\d{2}-\d{2})", note["job_id"]) if note else None
                if note is None or m is None:
                    continue
                parsed = parse_note(note["body"], note["meta"])
                repos = parsed["repos"]
                day = date.fromisoformat(m.group(1))
                # Files come oldest first and a forced rerun sorts after its original, so the later note wins.
                by_day[day] = {"date": day, "job_id": note["job_id"], "grammar": parsed["grammar"],
                               "repos": repos["git"], "github": repos["github"], "quiet_n": repos["quiet_n"],
                               "task": " ".join(" ".join(section(note["body"], "Active task")).split()),
                               "decided": len(parsed["decided"]), "held": parsed["counts"]["held"]}
            return [by_day[d] for d in sorted(by_day)]

        return self._cached("history", files, build)

    @staticmethod
    def delta(latest: dict[str, Any] | None, previous: dict[str, Any] | None) -> dict[str, Any]:
        """What moved between the latest note and the newest note of an earlier date: repo lines keyed by repo,
        then the count lines. Empty lists mean nothing changed. Without an earlier note only the facts that are
        changes by definition (commits since the window, decisions in the window) are reported."""
        repos: dict[str, list[str]] = {}
        lines: list[str] = []
        if latest is None:
            return {"repos": repos, "lines": lines}
        prev_repos = previous["repos"] if previous else {}
        prev_gh = previous["github"] if previous else {}
        for name, facts in latest["repos"].items():
            was = prev_repos.get(name, {"commits": 0, "modified": 0, "untracked": 0})
            out: list[str] = []
            if facts["commits"]:
                out.append(f"{facts['commits']} commit{'' if facts['commits'] == 1 else 's'}")
            dirty, dirty_was = (facts["modified"], facts["untracked"]), (was["modified"], was["untracked"])
            if previous is not None and dirty != dirty_was and any(dirty):
                out.append(f"uncommitted {dirty[0]}/{dirty[1]}, was {dirty_was[0]}/{dirty_was[1]}")
            if out:
                repos[name] = out
        for name, gh in (latest["github"].items() if previous is not None else ()):
            was = prev_gh.get(name, {"prs": 0, "ci": ""})
            failing, failing_was = gh.get("ci") in FAILING_CI_TOKENS, was.get("ci") in FAILING_CI_TOKENS
            if failing != failing_was:
                repos.setdefault(name, []).append("CI now failing" if failing else "CI now green")
            if gh.get("prs", 0) != was.get("prs", 0):
                repos.setdefault(name, []).append(f"{gh.get('prs', 0)} open PRs, was {was.get('prs', 0)}")
        if latest.get("decided"):
            lines.append(f"{latest['decided']} decision{'' if latest['decided'] == 1 else 's'} recorded")
        held_was = previous["held"] if previous else None
        if held_was is not None and latest.get("held") != held_was:
            lines.append(f"held {latest['held']}, was {held_was}")
        return {"repos": repos, "lines": lines}

    # --- runs ----------------------------------------------------------------------------------

    def _manifests(self) -> list[tuple[str, RunManifest]]:
        try:
            paths = sorted((self.state_dir / "runs").glob("*/run.json"))
        except OSError:
            return []

        def build(found: list[Path]) -> list[tuple[str, RunManifest]]:
            out: list[tuple[str, RunManifest]] = []
            for path in found:
                raw = _read_json(path)
                if not isinstance(raw, dict):
                    continue
                try:
                    out.append((path.parent.name, RunManifest.model_validate(raw)))
                except ValidationError:
                    continue
            return out

        return self._cached("manifests", paths, build)

    def runs(self) -> list[dict[str, Any]]:
        """The digest ledger, newest first: run manifests joined with their queue job. Linear: the witness is a
        lookup in the snapshot's seq index, and each queue state is parsed once."""
        rows: dict[str, dict[str, Any]] = {}
        for _, m in self._manifests():
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
                    "stamp": _job_stamp(job), "counts": {},
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

    # --- proposals, projects and ledger -----------------------------------------------------------

    def proposals(self) -> list[dict[str, Any]]:
        """state/proposals/*.json as plain dicts, defensively: a missing folder is zero proposals, a file that is
        not a JSON object is skipped. `decided_at` is the file mtime (the proposal has no decision stamp)."""
        tz = self.now().tzinfo

        def build(paths: list[Path]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for path in paths:
                raw = _read_json(path)
                if not isinstance(raw, dict):
                    continue
                try:
                    decided = iso(datetime.fromtimestamp(path.stat().st_mtime, tz=tz))
                except OSError:
                    continue
                evidence = raw.get("evidence")
                ref = raw.get("tracker_ref")
                edits = raw.get("edits")
                out.append({"id": str(raw.get("id") or path.stem), "title": str(raw.get("title") or ""),
                            "project": str(raw.get("project") or ""), "status": str(raw.get("status") or ""),
                            "tracker_ref": ref if isinstance(ref, str) and ref else None,
                            "evidence": [str(e) for e in evidence] if isinstance(evidence, list) else [],
                            "decided_at": decided, "created_at": str(raw.get("created_at") or ""),
                            "due": (edits.get("due") if isinstance(edits, dict) else None) or raw.get("due_hint")})
            return out

        return self._cached("proposals", _json_files(self.state_dir / "proposals"), build)

    def reminders(self) -> list[dict[str, Any]]:
        """Due dates of accepted and still-open proposals, soonest first. Rejected proposals and ones with no
        usable date are left out. An edited due date beats the model's hint. No tracker is asked: ClickUp due
        dates reach the hub only through the digest, so this is the part that is known locally."""
        today = self.now().date()
        out: list[dict[str, Any]] = []
        for p in self.proposals():
            if p["status"] not in ("proposed", "confirmed", "edited_confirmed"):
                continue
            try:
                due = date.fromisoformat(str(p["due"]))
            except ValueError:
                continue
            days = (due - today).days
            out.append({"id": p["id"], "title": p["title"], "project": p["project"], "status": p["status"],
                        "accepted": p["status"] != "proposed", "due": due.isoformat(), "days": days,
                        "bucket": "overdue" if days < 0 else "today" if days == 0 else "soon" if days <= 7 else "later",
                        "tracker_ref": p["tracker_ref"]})
        return sorted(out, key=lambda r: (r["due"], r["id"]))

    def projects(self) -> list[dict[str, Any]]:
        """One row per [digest].repos entry, in config order, derived only from the digest notes, the proposals
        folder and the config: the hub runs no git and no model. Risks are fixed rules (docs/hub.md). A repo in
        `[hub].always_dirty` never gets the uncommitted-work risk, and dirty counts alone do not make it active."""
        history = self._digest_history()
        today = self.now().date()
        proposals = self.proposals()
        latest = history[-1] if history else None
        previous = next((h for h in reversed(history[:-1]) if latest and h["date"] < latest["date"]), None)
        moved = self.delta(latest, previous)["repos"]
        exempt = {name.lower() for name in self.cfg.hub.always_dirty}
        rows: list[dict[str, Any]] = []
        for repo in self.cfg.digest.repos:
            name = repo.name
            facts = latest["repos"].get(name) if latest else None
            if facts is None and latest and latest.get("quiet_n") is not None:
                facts = {"branch": "", "commits": 0, "modified": 0, "untracked": 0}  # grammar 2: unnamed means quiet
            gh = latest["github"].get(name, {}) if latest else {}
            # Activity is a commit or any uncommitted change in a digest: the notes carry no commit dates.
            active: date | None = None
            for day in history:
                seen = day["repos"].get(name)
                if seen and (seen["commits"] or seen["modified"] or seen["untracked"]):
                    active = day["date"]
            dirty_since: date | None = None
            if facts and (facts["modified"] or facts["untracked"]):
                for day in reversed(history):
                    seen = day["repos"].get(name)
                    if not (seen and (seen["modified"] or seen["untracked"])):
                        break
                    dirty_since = day["date"]
            known = facts is not None
            # No activity anywhere in the digests on file: the repo has been idle at least as long as the history
            # reaches back, so that span is a floor, and it can still trip the badge. Without this the longest
            # idle repos (older than the history) would be the only ones that never get flagged.
            idle_floor = known and active is None and bool(history)
            if idle_floor:
                active = history[0]["date"]
            days_since = (today - active).days if (known and active is not None) else None
            dirty_days = (today - dirty_since).days if dirty_since is not None else None
            stale = days_since is not None and days_since >= self.cfg.hub.stale_days
            keywords = [k.lower() for k in self.cfg.hub.task_projects.get(name, []) if k]
            task = latest["task"] if latest else ""
            task_line = task if (task and any(k in task.lower() for k in keywords)) else ""
            always_dirty = name.lower() in exempt
            risks: list[str] = []
            if stale:
                # The plus sign is the idle floor: no activity in any digest on file, so at least that long.
                risks.append(f"stale repo, idle {days_since}+ days" if idle_floor else f"stale repo, idle {days_since} days")
            if dirty_days is not None and dirty_days >= DIRTY_RISK_DAYS and not always_dirty:
                risks.append(f"uncommitted work for {dirty_days} days")
            if gh.get("ci") in _FAILING_CI_TOKENS:
                risks.append("CI failing")
            if "(OVERDUE)" in task_line:
                risks.append("active task overdue")
            dirty = bool(facts and (facts["modified"] or facts["untracked"]))
            is_active = bool(facts and facts["commits"]) or bool(gh.get("prs")) or bool(risks) or (dirty and not always_dirty)
            rows.append({
                "name": name, "work": repo.work, "known": known,
                "branch": facts["branch"] if facts else "",
                "commits": facts["commits"] if facts else 0,
                "modified": facts["modified"] if facts else 0,
                "untracked": facts["untracked"] if facts else 0,
                "prs": gh.get("prs"), "ci": gh.get("ci", ""),
                "days_since": days_since, "idle_floor": idle_floor, "dirty_days": dirty_days, "stale": stale,
                "task": task_line, "risks": risks, "always_dirty": always_dirty, "active": is_active,
                "delta": moved.get(name, []),
                "open_proposals": sum(1 for p in proposals if p["project"] == name and p["status"] == "proposed"),
            })
        return rows

    def projects_page(self) -> dict[str, Any]:
        """Everything the Projects view needs: the rows split Active first, and the old Repos table."""
        rows = self.projects()
        return {"rows": rows, "active": [r for r in rows if r["active"]], "quiet": [r for r in rows if not r["active"]],
                "raw": self.repos(), "stale_days": self.cfg.hub.stale_days}

    def ledger(self) -> dict[str, Any]:
        """Everything delivered, newest first, plus a rollup per month. Delivered means a confirmed proposal with a
        tracker link, a digest run that wrote a note, a consolidation run that wrote its candidates note, or a
        proposals run that wrote its proposals (its paid call is real spend, so the monthly sum includes it)."""
        entries: list[dict[str, Any]] = []
        for p in self.proposals():
            if p["status"] in ("confirmed", "edited_confirmed") and p["tracker_ref"]:
                entries.append({"kind": "proposal", "stamp": p["decided_at"], "ref": p["id"], "title": p["title"],
                                "project": p["project"], "cost": None, "links": [p["tracker_ref"]],
                                "evidence": p["evidence"]})
        notes = {p.stem for p in self.digest_files()}
        for job_id, m in self._manifests():
            stamp = m.finished_at or m.started_at or ""
            if not stamp:
                continue
            if DIGEST_ID.match(job_id) and m.status in ("complete", "written"):
                entries.append({"kind": "digest", "stamp": stamp, "ref": job_id, "title": "Morning digest",
                                "project": "", "cost": m.cost_usd, "links": [],
                                "evidence": [f"/digest/{job_id}"] if job_id in notes else []})
            elif job_id.startswith("propose-") and m.status == "written":
                made = int(m.counts.get("proposals", 0))
                entries.append({"kind": "proposal_run", "stamp": stamp, "ref": job_id,
                                "title": f"Proposals run, {made} new", "project": "", "cost": m.cost_usd, "links": [],
                                "evidence": []})
            elif job_id.startswith("consolidate-") and m.status == "written":
                note = m.paths.get("note", "")
                entries.append({"kind": "consolidation", "stamp": stamp, "ref": job_id, "title": "Consolidation note",
                                "project": "", "cost": m.cost_usd, "links": [],
                                "evidence": [note.rsplit("/", 1)[-1]] if note else []})
        tz = self.now().tzinfo
        for e in entries:  # one offset for every kind, so string order is time order and months are local months
            try:
                e["stamp"] = parse_iso(str(e["stamp"])).astimezone(tz).isoformat(timespec="seconds")
            except ValueError:
                e["stamp"] = ""
        entries = [e for e in entries if e["stamp"]]
        entries.sort(key=lambda e: (e["stamp"], e["ref"]), reverse=True)
        months: dict[str, dict[str, Any]] = {}
        for e in entries:
            row = months.setdefault(str(e["stamp"])[:7], {"count": 0, "cost": 0.0, "proposals": 0, "digests": 0,
                                                          "notes": 0, "runs": 0})
            row["count"] += 1
            row["cost"] = round(row["cost"] + (e["cost"] or 0.0), 6)
            row[{"proposal": "proposals", "digest": "digests", "consolidation": "notes",
                 "proposal_run": "runs"}[e["kind"]]] += 1
        rollup = [{"month": k, **v} for k, v in sorted(months.items(), reverse=True)]
        return {"entries": entries, "rollup": rollup}

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
        """The newest finished digest among the NEWEST_JOBS most recently modified files of queue/done (the
        face_signal rule), never the whole folder."""
        best: tuple[str, Job] | None = None
        for job in self.newest_jobs("done"):
            if job.kind != DIGEST_KIND or not job.result or job.result.get("status") == "noop":
                continue
            stamp = _job_stamp(job)
            if best is None or stamp > best[0]:
                best = (stamp, job)
        if best is None:
            return None
        stamp, job = best
        result = job.result or {}
        return {"job_id": job.id, "path": result.get("note_path"), "status": result.get("status"),
                "cost_usd": result.get("cost_usd", 0.0),
                "age_hours": round((now - parse_iso(stamp)).total_seconds() / 3600, 1), "at": stamp}

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
        """The same keys as `jarvis status --json` (plus `last_digest.at` and `face`), read without side effects."""
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
            "face": self.face_signal(),
        }

    def face_signal(self) -> dict[str, Any]:
        """What the /face page needs and the CLI status lacks: proposals waiting and the newest finished job,
        from the newest files of queue/done and queue/failed only."""
        newest: dict[str, Any] | None = None
        for state in ("done", "failed"):
            for job in self.newest_jobs(state):
                stamp = _job_stamp(job)
                if newest is None or stamp > newest["at"]:
                    newest = {"id": job.id, "state": state, "at": stamp}
        pending = sum(1 for p in self.proposals() if p["status"] == "proposed")
        return {"inbox_pending": pending, "last_finished": newest}

    def strip(self, status: dict[str, Any]) -> dict[str, Any]:
        """The one-line status strip: health word, last digest, next digest, waiting, failed. Built from a
        status() dict so the header and `/api/status` cannot disagree; the face poll rebuilds the same text."""
        now = self.now()
        if status.get("kill"):
            health = "Kill switch on."
        elif status.get("pause"):
            health = "Paused."
        elif (status.get("breaker") or {}).get("state", "closed") != "closed":
            health = "Claude calls paused."
        elif status.get("running"):
            health = "Running."
        else:
            health = "Stopped."
        last = status.get("last_digest")
        at = ""
        if last and last.get("at"):
            try:
                at = parse_iso(str(last["at"])).astimezone(now.tzinfo).strftime("%H:%M")
            except ValueError:
                at = "?"
            digest = f"Last digest {at}, failed." if last.get("status") in ("failed", "error") else f"Last digest {at}."
        else:
            digest = "No digest yet."
        nxt = ""
        if status.get("next_due"):
            try:
                due = parse_iso(str(status["next_due"])).astimezone(now.tzinfo)
                when = "today" if due.date() == now.date() else ("tomorrow" if due.date() == now.date() + timedelta(days=1)
                                                                  else due.date().isoformat())
                nxt = f"Next {due.strftime('%H:%M')} {when}."
            except ValueError:
                nxt = ""
        waiting = int((status.get("face") or {}).get("inbox_pending") or 0)
        failed = int((status.get("queue") or {}).get("failed") or 0)
        parts = [health, digest] + ([nxt] if nxt else []) + [f"{waiting} waiting.", f"{failed} failed."]
        # The phone strip is one line: the last digest is named only when it failed or is missing, and "0 failed"
        # is left out. face.js (`stripText`) builds the same short text.
        short = ([digest] if digest != f"Last digest {at}." else []) + ([nxt] if nxt else []) + [f"{waiting} waiting."]
        if failed:
            short.append(f"{failed} failed.")
        return {"text": " ".join(parts), "health": health, "waiting": waiting, "failed": failed, "short": " ".join(short)}

    @staticmethod
    def status_lines(data: dict[str, Any]) -> list[str]:
        """The exact text `jarvis status` prints (the CLI's own formatter)."""
        return _status_lines(data)

    # --- one model per route -----------------------------------------------------------------------

    def today(self) -> dict[str, Any]:
        """The Today view: the parsed latest note, the delta against the previous day, the waiting proposals."""
        note = self.latest_digest()
        parsed = parse_note(note["body"], note["meta"]) if note else None
        history = self._digest_history()
        latest = history[-1] if history else None
        previous = next((h for h in reversed(history[:-1]) if latest and h["date"] < latest["date"]), None)
        waiting = sorted((p for p in self.proposals() if p["status"] == "proposed"), key=lambda p: (p["created_at"], p["id"]))
        return {"note": note, "parsed": parsed, "delta": self.delta(latest, previous),
                "waiting": waiting[:WAITING_SHOWN], "waiting_count": len(waiting)}

    def activity(self) -> dict[str, Any]:
        """The Activity view: the 7-day strip, then runs, the ledger, held, the audit and the status lines."""
        now = self.now()
        status = self.status()
        snap = self.audit()
        since = now - timedelta(days=STRIP_DAYS)
        runs = self.runs()

        def recent(stamp: object) -> bool:
            try:
                return parse_iso(str(stamp)) >= since
            except ValueError:
                return False

        week_runs = [r for r in runs if recent(r["stamp"])]
        proposals = self.proposals()
        events = {"proposal_confirmed": 0, "proposal_rejected": 0, "correction": 0}
        for rec in snap.records:
            ev = rec.get("event")
            if ev in events and recent(rec.get("ts")):
                events[ev] += 1
        tiles = {
            "runs": len(week_runs),
            "failed": sum(1 for r in week_runs if r["status"] in ("failed", "error")),
            "cost_usd": round(sum(float(r["cost_usd"] or 0.0) for r in week_runs), 4),
            "proposed": sum(1 for p in proposals if recent(p["created_at"])),
            "confirmed": events["proposal_confirmed"],
            "rejected": events["proposal_rejected"],
            "held": status["held_count"],
            "flagged": events["correction"],
            "verified": snap.verified if snap.files else None,
            "next_due": status["next_due"],
        }
        limit = self.cfg.hub.audit_rows
        return {"tiles": tiles, "runs": runs, "ledger": self.ledger(), "held": self.held(), "audit": snap,
                "audit_rows": self.audit_rows(limit), "audit_limit": limit, "budget": status["budget"],
                "cost_today": self.cost_on(now.date()), "status": status, "status_lines": self.status_lines(status),
                "now": now}


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
