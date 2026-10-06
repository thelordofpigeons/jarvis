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


# Projects view: how much digest history is read, and the fixed risk threshold for uncommitted work.
_HISTORY_DIGESTS = 60
DIRTY_RISK_DAYS = 2
_FAILING_CI_TOKENS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
_GH_LINE = re.compile(r"^- GitHub (?P<name>\S+?)(?: \(work\))?: (?P<rest>.*)$")
_GH_QUIET = re.compile(r"(?P<name>[^\s,()]+) \((?P<bits>[^)]*)\)")
_PRS = re.compile(r"^(\d+) open PRs?\b")
_COMMA_OUTSIDE_PARENS = re.compile(r",\s*(?![^()]*\))")


def _ci_token(parts: list[str]) -> str:
    for part in parts:
        part = part.strip()
        if part == "no CI runs":
            return "none"
        if part.startswith("CI "):
            return re.sub(r"[^a-z0-9_]+", "_", part[3:].split(" on ")[0].lower()).strip("_") or "unknown"
    return ""


def _parse_repo_lines(lines: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """(git facts by repo name, github facts by repo name) from the digest Repos section. A repo named in the
    Quiet line has zero counts; one that is in neither is simply absent, so nothing is invented for it."""
    git: dict[str, dict[str, Any]] = {}
    gh: dict[str, dict[str, Any]] = {}
    for line in lines:
        text = line.strip()
        if text.startswith("- Quiet:"):
            for name in text[len("- Quiet:"):].rstrip(". ").split(","):
                if name.strip():
                    git.setdefault(name.strip(), {"branch": "", "commits": 0, "modified": 0, "untracked": 0})
        elif text.startswith("- GitHub quiet:"):
            for m in _GH_QUIET.finditer(text[len("- GitHub quiet:"):]):
                gh[m.group("name")] = {"prs": 0, "ci": _ci_token(m.group("bits").split(","))}
        elif text.startswith("- GitHub "):
            m = _GH_LINE.match(_ITEM_ID.sub("", text).strip())
            if m:
                parts = _COMMA_OUTSIDE_PARENS.split(m.group("rest").split(": ", 1)[0])
                prs = _PRS.match(parts[0].strip())
                gh[m.group("name")] = {"prs": int(prs.group(1)) if prs else 0, "ci": _ci_token(parts[1:])}
        else:
            m = _REPO_LINE.match(text)
            if m:
                git[m.group("name")] = {"branch": m.group("branch") or "", "commits": int(m.group("commits")),
                                        "modified": int(m.group("modified")), "untracked": int(m.group("untracked"))}
    return git, gh


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

    # --- projects and ledger ---------------------------------------------------------------------

    def proposals(self) -> list[dict[str, Any]]:
        """state/proposals/*.json as plain dicts, defensively: a missing folder is zero proposals, a file that is
        not a JSON object is skipped. `decided_at` is the file mtime (the proposal has no decision stamp)."""
        out: list[dict[str, Any]] = []
        for path in _json_files(self.state_dir / "proposals"):
            raw = _read_json(path)
            if not isinstance(raw, dict):
                continue
            try:
                decided = iso(datetime.fromtimestamp(path.stat().st_mtime, tz=self.now().tzinfo))
            except OSError:
                continue
            evidence = raw.get("evidence")
            ref = raw.get("tracker_ref")
            out.append({"id": str(raw.get("id") or path.stem), "title": str(raw.get("title") or ""),
                        "project": str(raw.get("project") or ""), "status": str(raw.get("status") or ""),
                        "tracker_ref": ref if isinstance(ref, str) and ref else None,
                        "evidence": [str(e) for e in evidence] if isinstance(evidence, list) else [],
                        "decided_at": decided})
        return out

    def reminders(self) -> list[dict[str, Any]]:
        """Due dates of accepted and still-open proposals, soonest first. Rejected proposals and ones with no
        usable date are left out. An edited due date beats the model's hint. No tracker is asked: ClickUp due
        dates reach the hub only through the digest, so this is the part that is known locally."""
        today = self.now().date()
        out: list[dict[str, Any]] = []
        for path in _json_files(self.state_dir / "proposals"):
            raw = _read_json(path)
            if not isinstance(raw, dict) or raw.get("status") not in ("proposed", "confirmed", "edited_confirmed"):
                continue
            edits = raw.get("edits")
            text = (edits.get("due") if isinstance(edits, dict) else None) or raw.get("due_hint")
            try:
                due = date.fromisoformat(str(text))
            except ValueError:
                continue
            days = (due - today).days
            ref = raw.get("tracker_ref")
            out.append({"id": str(raw.get("id") or path.stem), "title": str(raw.get("title") or ""),
                        "project": str(raw.get("project") or ""), "status": str(raw["status"]),
                        "accepted": raw["status"] != "proposed", "due": due.isoformat(), "days": days,
                        "bucket": "overdue" if days < 0 else "today" if days == 0 else "soon" if days <= 7 else "later",
                        "tracker_ref": ref if isinstance(ref, str) and ref else None})
        return sorted(out, key=lambda r: (r["due"], r["id"]))

    def _digest_history(self) -> list[dict[str, Any]]:
        """Parsed Repos and Active task sections of the newest digest notes, oldest first, one per day."""
        by_day: dict[date, dict[str, Any]] = {}
        for path in self.digest_files()[-_HISTORY_DIGESTS:]:
            note = self.read_digest(path)
            m = re.match(r"^digest-(\d{4}-\d{2}-\d{2})", note["job_id"]) if note else None
            if note is None or m is None:
                continue
            repos, github = _parse_repo_lines(section(note["body"], "Repos"))
            day = date.fromisoformat(m.group(1))
            # Files come oldest first and a forced rerun sorts after its original, so the later note wins.
            by_day[day] = {"date": day, "repos": repos, "github": github,
                           "task": " ".join(" ".join(section(note["body"], "Active task")).split())}
        return [by_day[d] for d in sorted(by_day)]

    def projects(self) -> list[dict[str, Any]]:
        """One row per [digest].repos entry, in config order, derived only from the digest notes, the proposals
        folder and the config: the hub runs no git and no model. Risks are fixed rules (docs/hub.md)."""
        history = self._digest_history()
        today = self.now().date()
        proposals = self.proposals()
        latest = history[-1] if history else None
        rows: list[dict[str, Any]] = []
        for repo in self.cfg.digest.repos:
            name = repo.name
            facts = latest["repos"].get(name) if latest else None
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
            risks: list[str] = []
            if stale:
                risks.append(f"stale repo, no activity in the {days_since} days of digests on file" if idle_floor
                             else f"stale repo, no activity for {days_since} days")
            if dirty_days is not None and dirty_days >= DIRTY_RISK_DAYS:
                risks.append(f"uncommitted work for {dirty_days} days")
            if gh.get("ci") in _FAILING_CI_TOKENS:
                risks.append("CI failing")
            if "(OVERDUE)" in task_line:
                risks.append("active task overdue")
            rows.append({
                "name": name, "work": repo.work, "known": known,
                "branch": facts["branch"] if facts else "",
                "commits": facts["commits"] if facts else 0,
                "modified": facts["modified"] if facts else 0,
                "untracked": facts["untracked"] if facts else 0,
                "prs": gh.get("prs"), "ci": gh.get("ci", ""),
                "days_since": days_since, "idle_floor": idle_floor, "dirty_days": dirty_days, "stale": stale,
                "task": task_line, "risks": risks,
                "open_proposals": sum(1 for p in proposals if p["project"] == name and p["status"] == "proposed"),
            })
        return rows

    def _manifests(self) -> list[tuple[str, RunManifest]]:
        found: list[tuple[str, RunManifest]] = []
        try:
            paths = sorted((self.state_dir / "runs").glob("*/run.json"))
        except OSError:
            return found
        for path in paths:
            raw = _read_json(path)
            if not isinstance(raw, dict):
                continue
            try:
                found.append((path.parent.name, RunManifest.model_validate(raw)))
            except ValidationError:
                continue
        return found

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
