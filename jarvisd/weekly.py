"""The weekly review note (docs/hub-rework-contract.md section 9, writer side).

`raw/jarvis/weekly-YYYY-Www.md` sums up one ISO week: runs and cost, the decisions filed, the
threads that dropped out of the daily note, what was marked done or snoozed, and what the owner
flagged wrong. Two doors, one implementation: the digest writes it after the first successful note
of a new week (when the file for the week just ended is absent), and `jarvis weekly` prints or
writes the same note by hand. Both go through `deps.vault.write_raw`, the one vault writer, so the
marker rule and the path checks apply exactly as they do to the digest.

Sources, every one of them filtered when it was first persisted:
- `state/item-history.json` and `state/attention/*.json` (written from View-filtered sidecar
  records; a sensitive-held item has no key or text in either);
- `state/runs/*/run.json` (counts, cost and status only);
- the audit's `correction` events (item ids and the `should` code only).

Nothing here reads the vault, a session note or a held reference. The module opens no file for
writing: the history is read through `StateStore.items` and the note goes through the vault.

Layer L3. Imports render, state, audit, common, models and the digest `Deps`.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jarvisd import render
from jarvisd.audit import AuditLog
from jarvisd.common import parse_iso
from jarvisd.models import RunManifest
from jarvisd.state import StateStore

WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
_RUN_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class BadWeek(ValueError):
    """`--week` did not look like 2026-W41 or named a week that does not exist."""


def week_of(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def previous_week(day: date) -> str:
    """The ISO week that ended before `day`'s week began: what a run on any day of a week reviews."""
    return week_of(day - timedelta(days=7))


def week_bounds(week: str) -> tuple[date, date]:
    """(Monday, Sunday) of an ISO week string, or BadWeek."""
    m = WEEK_RE.match(week or "")
    if m is None:
        raise BadWeek("week must look like 2026-W41")
    try:
        monday = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError as exc:
        raise BadWeek(f"no such ISO week: {week}") from exc
    return monday, monday + timedelta(days=6)


def _in_week(day_text: str, start: date, end: date) -> bool:
    return start.isoformat() <= day_text[:10] <= end.isoformat()


def _local_day(stamp: object, tz: tzinfo | None) -> str:
    try:
        moment = parse_iso(str(stamp))
    except ValueError:
        return ""
    return (moment.astimezone(tz) if tz is not None else moment.astimezone()).date().isoformat()


def _manifests(state_dir: Path) -> list[RunManifest]:
    folder = state_dir / "runs"
    if not folder.is_dir():
        return []
    out: list[RunManifest] = []
    try:
        folders = sorted(p for p in folder.iterdir() if p.is_dir() and _RUN_FOLDER.match(p.name))
    except OSError:
        return []
    for run_dir in folders:
        path = run_dir / "run.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            out.append(RunManifest.model_validate(raw))
        except (OSError, ValueError, ValidationError):
            continue
    return out


def _texts(history: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items = history.get("items")
    return {k: v for k, v in items.items() if isinstance(v, dict)} if isinstance(items, dict) else {}


def build_context(state: StateStore, audit: AuditLog, week: str, now: datetime) -> render.WeeklyContext:
    """Gather one week from the state folder and the audit. Reads only."""
    start, end = week_bounds(week)
    tz = now.tzinfo
    items = _texts(state.items.load())

    runs = []
    for m in _manifests(state.dir):
        day = _local_day(m.finished_at or m.started_at or "", tz)
        if day and _in_week(day, start, end):
            runs.append({"date": day, "status": m.status, "cost_usd": float(m.cost_usd or 0.0)})

    decided, dropped = [], []
    for key, rec in items.items():
        resolved = str(rec.get("resolved_at") or "")
        if not resolved or not _in_week(resolved, start, end):
            continue
        if rec.get("status") == "done" and "decided" in list(rec.get("sections") or []):
            decided.append({"date": resolved, "text": rec.get("text") or key, "id": rec.get("id") or ""})
        elif rec.get("status") == "dropped":
            dropped.append({"text": rec.get("text") or key, "first": rec.get("first_seen") or resolved,
                            "last": rec.get("last_seen") or resolved, "id": rec.get("id") or ""})

    attended = []
    for d in state.items.decisions():
        day = _local_day(d.get("decided_at"), tz)
        if not day or not _in_week(day, start, end):
            continue
        key = str(d.get("key") or "")
        rec = items.get(key) or {}
        attended.append({"action": d.get("action"), "until": d.get("until"), "date": day,
                         "text": rec.get("text") or key, "id": d.get("id") or rec.get("id") or ""})

    flagged = []
    floor = datetime.combine(start - timedelta(days=1), time.min, tzinfo=timezone.utc)
    for rec in audit.records(since=floor, events=["correction"]):
        day = _local_day(rec.get("ts"), tz)
        if not day or not _in_week(day, start, end):
            continue
        try:
            stamp = parse_iso(str(rec.get("ts")))
            shown = (stamp.astimezone(tz) if tz is not None else stamp.astimezone()).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
        flagged.append({"ts": shown, "id": str(rec.get("item_id") or ""), "should": rec.get("should") or "other",
                        "leak": bool(rec.get("leak"))})

    return render.WeeklyContext(week=week, start=start, end=end, generated_at=now, runs=runs, decided=decided,
                                dropped=dropped, attended=attended, flagged=flagged)


def has_material(ctx: render.WeeklyContext) -> bool:
    """False when every section would read `- None.` and the runs line would say zero: a fresh install or a
    week the daemon slept through gets no note."""
    return bool(ctx.runs or ctx.decided or ctx.dropped or ctx.attended or ctx.flagged)


def render_week(state: StateStore, audit: AuditLog, week: str, now: datetime) -> tuple[str, render.WeeklyContext]:
    ctx = build_context(state, audit, week, now)
    return render.render_weekly(ctx), ctx


def write_weekly(deps: Any, week: str, job_id: str) -> Any:
    """Render the week and hand it to the vault writer. Raises what `write_raw` raises."""
    text, _ = render_week(deps.state, deps.audit, week, deps.clock())
    return deps.vault.write_raw(render.weekly_filename(week), text, job_id)


def cmd_weekly(ctx: Any, args: argparse.Namespace) -> int:
    """`jarvis weekly [--week YYYY-Www] [--dry-run]`: print the note, or write it through the vault."""
    from jarvisd.cli import EXIT_FAIL, EXIT_OK, UsageError
    from jarvisd.vault import VaultWriteDenied

    now = ctx.now()
    week = args.week or previous_week(now.date())
    try:
        week_bounds(week)
    except BadWeek as exc:
        raise UsageError(str(exc)) from exc
    deps = ctx.deps(claude_enabled=False)
    text, built = render_week(deps.state, deps.audit, week, now)
    if args.dry_run:
        print(text, end="")
        print(f"Dry run for {week}. Nothing was written." if has_material(built)
              else f"Dry run for {week}. Nothing was written; the digest would skip this week (nothing to review).")
        return EXIT_OK
    try:
        written = deps.vault.write_raw(render.weekly_filename(week), text, f"weekly-{week}-manual")
    except VaultWriteDenied as exc:
        print(f"The weekly note was not written: vault refused ({exc.reason}).")
        return EXIT_FAIL
    except OSError as exc:
        print(f"The weekly note was not written: {type(exc).__name__}.")
        return EXIT_FAIL
    print(f"Weekly review written: {written.path.as_posix()}")
    return EXIT_OK
