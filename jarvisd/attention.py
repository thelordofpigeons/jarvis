"""Done and Snooze on a digest item (docs/hub-rework-contract.md, section 7): one implementation, two doors.

The hub's Today buttons (`POST /today/<id>/done` and `/snooze` in jarvisd/hub/app.py) and the terminal
twin `jarvis attend <id> --done | --until <date>` (`cmd_attend`, wired by the CLI) call `decide`, so the
click and the command cannot drift apart. A decision is one small JSON file under `state/attention/`,
named by the item's stable key; the digest writer reads that folder while it builds the next note and
excludes the key (done: for ever; snoozed: until the date), and the hub hides the line at once.

Rules, all tested in tests/test_attention.py:
- The item id must be in the latest run's sidecar (`state/runs/<job>/items.json`, written by the digest
  writer from the same filtered view as the note, so a held item is never there). The key comes from
  that record, never from the form. No sidecar, or an unknown id, is "not found".
- `until` is `tomorrow`, `3d`, `monday` (the next Monday, never today) or a `YYYY-MM-DD` after today and
  within 90 days; anything else is "invalid".
- One lock, in-process and cross-process, on the attention folder, so two clicks or a click and a
  command cannot both write.
- A decision in force (a done, or a snooze whose date has not passed) answers "decided" and changes
  nothing; an expired snooze is replaced.
- Every write is one `atomic_write_text` of one file inside `state/attention/`, and nothing else is
  written anywhere. The audit gets `attention_decided` with the item id, the action, the date and the
  digest run id, or `attention_decide_failed` with a short error code; never the text and never the key.

Layer L3 (beside jarvisd/inbox.py). Imports fsio, common and audit only; the hub imports it.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jarvisd.audit import AuditLog
from jarvisd.common import iso, local_now, sha256_hex
from jarvisd.fsio import FileBusy, FileLock, atomic_write_text, lock_path_for, path_lock

ATTENTION_DIR = "attention"
SIDECAR_NAME = "items.json"
LOCK_WAIT_S = 20.0
MAX_SNOOZE_DAYS = 90
UNTIL_CHOICES = ("tomorrow", "3d", "monday")
ACTIONS = ("done", "snooze")
ITEM_ID = re.compile(r"^[0-9a-f]{8}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAME_CHARS = re.compile(r"[^\w-]+")
MAX_SIDECAR_BYTES = 2_000_000


@dataclass(frozen=True)
class Outcome:
    """What a decision did. `code` is one of: done, snoozed, not_found, invalid, busy, decided, save_failed.
    `message` is plain text for a page or a terminal; it never carries the item text or its key."""

    ok: bool
    code: str
    message: str
    item_id: str = ""
    until: str | None = None


class _Stop(Exception):
    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.code)
        self.outcome = outcome


# --- paths and names -----------------------------------------------------------------------------------------------


def attention_dir(state_dir: str | Path) -> Path:
    """`state/attention/`: the one folder a decision may write in."""
    return Path(state_dir) / ATTENTION_DIR


def attention_name(key: str) -> str:
    """The file stem of a key: its words joined by hyphens (cut at 72) plus 8 hex of its hash, so two keys with
    the same first 72 characters still get two files and the name stays readable in a folder listing."""
    words = "-".join(key.split())[:72]
    return _NAME_CHARS.sub("-", words).strip("-") + "-" + sha256_hex(key)[:8]


def decision_path(state_dir: str | Path, key: str) -> Path:
    return attention_dir(state_dir) / f"{attention_name(key)}.json"


def sidecar_path(state_dir: str | Path, job_id: str) -> Path:
    return Path(state_dir) / "runs" / job_id / SIDECAR_NAME


# --- reading (the writer's files; the hub reads them the same way) ----------------------------------------------------


def _read_json(path: Path, cap: int = MAX_SIDECAR_BYTES) -> Any | None:
    try:
        if path.stat().st_size > cap:
            return None
        return json.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return None


def sidecar_items(doc: Any) -> list[dict[str, Any]]:
    """The item records of a parsed sidecar, keeping only the ones with a string key and a well-formed id."""
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        return []
    out: list[dict[str, Any]] = []
    for raw in doc["items"]:
        if not isinstance(raw, dict):
            continue
        key, item_id = raw.get("key"), raw.get("id")
        if isinstance(key, str) and key.strip() and isinstance(item_id, str) and ITEM_ID.match(item_id):
            out.append({"key": key, "id": item_id, "section": str(raw.get("section") or ""),
                        "group": str(raw.get("group") or ""), "date": str(raw.get("date") or ""),
                        "text": str(raw.get("text") or ""), "rank": raw.get("rank"), "since": str(raw.get("since") or "")})
    return out


def load_sidecar(state_dir: str | Path, job_id: str | None) -> list[dict[str, Any]]:
    """The records of one run's sidecar, in page order; [] when there is none (notes written before phase 3)."""
    if not job_id or "/" in job_id or "\\" in job_id or job_id.startswith("."):
        return []
    return sidecar_items(_read_json(sidecar_path(state_dir, job_id)))


def decision_record(raw: Any) -> dict[str, Any] | None:
    """One attention file as a record, or None when it is not one."""
    if not isinstance(raw, dict) or raw.get("action") not in ACTIONS:
        return None
    key, item_id = raw.get("key"), raw.get("id")
    if not isinstance(key, str) or not key or not isinstance(item_id, str) or not ITEM_ID.match(item_id):
        return None
    until = raw.get("until")
    return {"key": key, "id": item_id, "action": str(raw["action"]),
            "until": until if isinstance(until, str) and _DATE.match(until) else None,
            "decided_at": str(raw.get("decided_at") or ""), "note": str(raw.get("note") or "")}


def in_force(record: dict[str, Any], today: date) -> bool:
    """A done is in force for ever; a snooze until its date (on that day the item is open again)."""
    if record.get("action") == "done":
        return True
    try:
        return date.fromisoformat(str(record.get("until"))) > today
    except ValueError:
        return False


def load_decisions(state_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Every readable decision, by key. A missing folder is no decisions; nothing is created."""
    folder = attention_dir(state_dir)
    out: dict[str, dict[str, Any]] = {}
    try:
        paths = sorted(p for p in folder.glob("*.json") if not p.name.startswith("."))
    except OSError:
        return out
    for path in paths:
        rec = decision_record(_read_json(path, 65_536))
        if rec is not None:
            out[rec["key"]] = rec
    return out


# --- the decision -------------------------------------------------------------------------------------------------


def resolve_until(value: str | None, today: date) -> date | None:
    """The snooze date for a form value, or None when the value is not one of the contract's choices."""
    text = (value or "").strip().lower()
    if text == "tomorrow":
        return today + timedelta(days=1)
    if text == "3d":
        return today + timedelta(days=3)
    if text == "monday":
        return today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    if not _DATE.match(text):
        return None
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None
    if day <= today or (day - today).days > MAX_SNOOZE_DAYS:
        return None
    return day


class _Locked:
    """In-process and cross-process exclusion for one decision (the Inbox pattern). Not reentrant."""

    def __init__(self, folder: Path) -> None:
        self._thread = path_lock(folder / "decide")
        self._file = FileLock(lock_path_for(folder / "decide"), timeout=LOCK_WAIT_S)
        self._have_thread = False

    def __enter__(self) -> "_Locked":
        busy = Outcome(False, "busy", "Another decision is being processed right now. Try again in a moment.")
        if not self._thread.acquire(timeout=LOCK_WAIT_S):
            raise _Stop(busy)
        self._have_thread = True
        try:
            if not self._file.acquire():
                raise _Stop(busy)
        except OSError as exc:
            self._thread.release()
            self._have_thread = False
            raise _Stop(Outcome(False, "busy", f"The attention folder could not be locked ({type(exc).__name__}).")) from exc
        except _Stop:
            self._thread.release()
            self._have_thread = False
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._file.release()
        if self._have_thread:
            self._have_thread = False
            self._thread.release()


def decide(state_dir: str | Path, audit: AuditLog, job_id: str | None, item_id: str, action: str,
           until_text: str | None = None, *, clock: Callable[[], datetime] | None = None) -> Outcome:
    """Record Done or Snooze for the item `item_id` of the digest run `job_id`.

    `job_id` is the latest digest note's id (the hub's view of "latest"); its sidecar names the items that
    can be decided. Returns an Outcome, never raises for a user mistake.
    """
    now = (clock or local_now)()
    today = now.date()
    if action not in ACTIONS:
        return Outcome(False, "invalid", "Unknown action; only Done and Snooze exist. Nothing was changed.", item_id)
    if not ITEM_ID.match(item_id or ""):
        return Outcome(False, "not_found", "No item with that id in the latest digest. Nothing was changed.", item_id)
    record = next((r for r in load_sidecar(state_dir, job_id) if r["id"] == item_id), None)
    if record is None:
        return Outcome(False, "not_found", "No item with that id in the latest digest (older notes have no item list, "
                       "so their lines cannot be decided). Nothing was changed.", item_id)
    until: date | None = None
    if action == "snooze":
        until = resolve_until(until_text, today)
        if until is None:
            return Outcome(False, "invalid", "Snooze needs tomorrow, 3d, monday or a date after today within 90 days. "
                           "Nothing was changed.", item_id)
    key = record["key"]
    path = decision_path(state_dir, key)
    try:
        with _Locked(attention_dir(state_dir)):
            existing = decision_record(_read_json(path, 65_536))
            if existing is not None and in_force(existing, today):
                what = "done" if existing["action"] == "done" else f"snoozed until {existing['until']}"
                return Outcome(False, "decided", f"This item is already {what}; nothing was changed.", item_id,
                               existing["until"])
            doc = {"key": key, "id": item_id, "action": action, "until": until.isoformat() if until else None,
                   "decided_at": iso(now), "note": job_id}
            try:
                atomic_write_text(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
            except (OSError, FileBusy) as exc:
                audit.emit("attention_decide_failed", item_id=item_id, action=action, digest_run_id=job_id,
                           error=f"save_failed:{type(exc).__name__}")
                return Outcome(False, "save_failed", f"The decision could not be saved ({type(exc).__name__}); "
                               "the item is still open.", item_id)
            audit.emit("attention_decided", item_id=item_id, action=action, until=doc["until"], digest_run_id=job_id)
            if action == "done":
                return Outcome(True, "done", "Done. The item leaves the page now and the next digest.", item_id)
            return Outcome(True, "snoozed", f"Snoozed until {doc['until']}. The item returns on that day unless its text "
                           "changes first.", item_id, doc["until"])
    except _Stop as stop:
        return stop.outcome


# --- the terminal door -----------------------------------------------------------------------------------------------


def latest_job_id(raw_dir: Path) -> str | None:
    """The newest digest note's id in the raw folder, the way the hub picks it (a rerun sorts after its original)."""
    found: list[tuple[str, int, str]] = []
    try:
        names = [p.name for p in raw_dir.iterdir()] if raw_dir.is_dir() else []
    except OSError:
        return None
    for name in names:
        m = re.match(r"^(digest-(\d{4}-\d{2}-\d{2})(?:-r(\d+))?)\.md$", name)
        if m:
            found.append((m.group(2), int(m.group(3) or 1), m.group(1)))
    return sorted(found)[-1][2] if found else None


def cmd_attend(ctx: Any, args: Any) -> int:
    """`jarvis attend <id> --done` or `jarvis attend <id> --until tomorrow|3d|monday|YYYY-MM-DD`.

    `ctx` is the CLI's Ctx; this module never imports the CLI. Exit 0 only when the decision was made.
    """
    from jarvisd.cli import _raw_dir  # the digest folder, found the way the CLI finds it

    action = "done" if getattr(args, "done", False) else "snooze"
    out = decide(ctx.cfg.daemon.state_dir, ctx.audit(), latest_job_id(_raw_dir(ctx.cfg)), args.id, action,
                 getattr(args, "until", None), clock=ctx.clock)
    print(out.message)
    return 0 if out.ok else 1
