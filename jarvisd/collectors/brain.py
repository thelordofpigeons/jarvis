"""Brain collector (design section 8): RECENT.md, new session notes, checkpoint counts.

Reads exactly three things, all through `tier.safe_read_text` or a directory listing:
- `brain/RECENT.md`, whose bullets become `brain_thread` and `brain_decision` items;
- session notes in `brain/sessions/` newer than the window (never `jarvis-*`), whose
  `## Next session entry point` and `## Open threads` sections become `brain_session` items;
- the `*.json` files directly in `brain/session-checkpoints/`, which are only counted.

It never touches `telos/`, `notes/`, `insights/` or `raw/`, and a checkpoint is never opened.

Derived sensitivity (design section 6): RECENT.md is generated from session notes. A bullet
whose date matches a withheld session may paraphrase it, so it gets the tag
`derived_from_sensitive_session` and gate 1 holds it. The taint also covers sessions older
than the window: they are scanned (and the content thrown away) only to learn whether
their date is tainted. Over-holding is the correct direction.

Withholding granularity (design sections 6 and 8):
- Path, size, decoding and tag rules (frontmatter tags, inline hashtags, `sensitive: true`,
  the `telos/sensitive` literal) stay file-level. One of them anywhere in RECENT.md or a
  session note withholds the whole file. For RECENT.md that shows up as `recent_withheld`.
- Term hits (`[gates].sensitive_terms`) are per unit. Both files are read with
  `safe_read_text(terms=False)`, then every RECENT.md bullet and every session-note line is
  scanned with `tier.scan_terms`. A unit that hits becomes one content-free `WithheldItem`
  (reason `term:<index>`, source_ref the file path plus a position id such as
  `RECENT.md#thread-2026-10-04-2`), and the other units flow on as normal items. The
  position id counts bullets per section and date in file order, before any is dropped, so
  it does not shift when a neighbour is held. Gate 1 in dispatch still runs on every item.
- Only a file-level withhold (path, size, decoding or tag rule) taints a date for the RECENT.md
  derived tag. A term hit in a session note holds that line only, because every RECENT.md
  bullet is term-scanned on its own and a term elsewhere says nothing about the other bullets.

Limits:
- Session dates come from the file name (`YYYY-MM-DD-...`), falling back to the mtime date.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timezone
from pathlib import Path

from jarvisd.collectors import CollectContext, withheld_ref
from jarvisd.common import iso, short_id
from jarvisd.models import CollectResult, Item, WithheldItem
from jarvisd.tier import DERIVED_TAG, safe_read_text, scan_terms

STALE_AFTER_DAYS = 7
RECENT_WARN_HOURS = 30.0
MAX_SESSIONS = 50
MAX_PROBES = 40
MAX_BULLETS_PER_SECTION = 5
MAX_BULLET_CHARS = 400
TITLE_CHARS = 80

_BULLET = re.compile(r"^\s*-\s*\[(\d{4}-\d{2}-\d{2})\]\s*(.+?)\s*$")
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")
# Copied in spirit from brain-nightly.py (_section): header, then lines up to the next "## ".
_SECTION_TEMPLATE = r"^##[ \t]*{header}[^\n]*\n(.*?)(?=^## |\Z)"


def _clip(text: str, limit: int) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "."


def _midnight_utc(day: date) -> str:
    return iso(datetime.combine(day, time.min, tzinfo=timezone.utc))


def _session_day(name: str, mtime: datetime, tz: object) -> date:
    m = _DATE_PREFIX.match(name)
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return mtime.astimezone(tz).date()  # type: ignore[arg-type]


def _section_body(text: str, header: str) -> str:
    m = re.search(_SECTION_TEMPLATE.format(header=header), text, re.DOTALL | re.IGNORECASE | re.MULTILINE)
    return m.group(1) if m else ""


def _bullets(body: str) -> list[str]:
    out = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:].strip():
            out.append(_clip(stripped[2:], MAX_BULLET_CHARS))
    return out[:MAX_BULLETS_PER_SECTION]


def _entry_lines(body: str) -> list[str]:
    """The entry point is a sentence, sometimes a bullet. Take non-empty lines either way."""
    out = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if stripped:
            out.append(_clip(stripped, MAX_BULLET_CHARS))
    return out[:MAX_BULLETS_PER_SECTION]


class _Bullet:
    __slots__ = ("section", "day", "text", "raw", "ref")

    def __init__(self, section: str, day: date, text: str, raw: str, ref: str) -> None:
        self.section = section
        self.day = day
        self.text = text
        self.raw = raw  # the untrimmed bullet line, scanned for terms but never emitted
        self.ref = ref  # position id: section, date and the ordinal among that pair


def _parse_recent(text: str) -> list[_Bullet]:
    bullets: list[_Bullet] = []
    section: str | None = None
    ordinals: dict[tuple[str, date], int] = {}
    for line in text.splitlines():
        if line.startswith("## "):
            head = line[3:].strip().casefold()
            if head == "open threads":
                section = "thread"
            elif head in ("recent decisions", "decisions"):
                section = "decision"
            else:
                section = None
            continue
        m = _BULLET.match(line)
        if section and m:
            try:
                day = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            n = ordinals[(section, day)] = ordinals.get((section, day), 0) + 1
            bullets.append(
                _Bullet(section, day, _clip(m.group(2), MAX_BULLET_CHARS), line, f"{section}-{day.isoformat()}-{n}")
            )
    return bullets


class BrainCollector:
    """Collector named `brain`. Stateless: everything comes from the context."""

    name = "brain"

    def collect(self, ctx: CollectContext) -> CollectResult:
        cfg = ctx.cfg
        brain = Path(cfg.paths.brain_root)
        if not brain.is_dir():
            return CollectResult(source=self.name, ok=False, error="brain_root_missing")
        sessions_dir = brain / "sessions"
        recent_path = brain / "RECENT.md"
        today = ctx.now.date()
        tz = ctx.now.tzinfo

        withheld: list[WithheldItem] = []
        facts: dict[str, object] = {}
        bullets: list[_Bullet] = []

        recent_text = self._read_recent(ctx, recent_path, facts, withheld)
        if recent_text is not None:
            bullets = _parse_recent(recent_text)

        tainted, session_items, new_sessions = self._scan_sessions(ctx, sessions_dir, bullets, withheld, tz)

        kept = self._split_held_bullets(ctx, bullets, recent_path, facts, withheld)
        items = self._bullet_items(kept, tainted, today, recent_path)
        items.extend(session_items)

        facts["orphan_checkpoints"] = self._count_checkpoints(brain / "session-checkpoints")
        facts["new_sessions"] = new_sessions
        facts["withheld_count"] = len(withheld)
        return CollectResult(source=self.name, ok=True, items=items, withheld=withheld, facts=facts)

    # --- RECENT.md ---------------------------------------------------------------------

    def _read_recent(
        self, ctx: CollectContext, path: Path, facts: dict[str, object], withheld: list[WithheldItem]
    ) -> str | None:
        facts["recent_missing"] = False
        facts["recent_withheld"] = False
        if not path.is_file():
            facts["recent_missing"] = True
            return None
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        age_h = round((ctx.now - mtime).total_seconds() / 3600.0, 1)
        facts["recent_mtime"] = iso(mtime)
        facts["recent_age_hours"] = age_h
        facts["recent_stale"] = age_h > RECENT_WARN_HOURS
        # Terms are scanned per bullet by the caller; every other rule stays file-level here.
        text = safe_read_text(path, ctx.cfg, [path], terms=False)
        if isinstance(text, WithheldItem):
            facts["recent_withheld"] = True
            withheld.append(withheld_ref("brain_recent", text.source_ref, text.reason))
            return None
        return text

    @staticmethod
    def _split_held_bullets(
        ctx: CollectContext,
        bullets: list[_Bullet],
        recent_path: Path,
        facts: dict[str, object],
        withheld: list[WithheldItem],
    ) -> list[_Bullet]:
        """Drop each bullet with a term hit and leave a content-free reference in its place."""
        kept: list[_Bullet] = []
        held = 0
        for b in bullets:
            hit = scan_terms(b.raw, ctx.cfg) or scan_terms(b.text, ctx.cfg)
            if hit is None:
                kept.append(b)
                continue
            held += 1
            withheld.append(withheld_ref("brain_recent", f"{recent_path.as_posix()}#{b.ref}", hit.code))
        facts["recent_held_bullets"] = held
        return kept

    def _bullet_items(
        self, bullets: list[_Bullet], tainted: set[date], today: date, recent_path: Path
    ) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()
        for b in bullets:
            kind = "brain_thread" if b.section == "thread" else "brain_decision"
            item_id = short_id("brain", b.section, b.day.isoformat(), b.text)
            if item_id in seen:
                continue
            seen.add(item_id)
            age_days = max(0, (today - b.day).days)
            stale = age_days > STALE_AFTER_DAYS
            tags = (["stale"] if stale else []) + ([DERIVED_TAG] if b.day in tainted else [])
            items.append(
                Item(
                    id=item_id,
                    source="brain",
                    kind=kind,
                    title=_clip(b.text, TITLE_CHARS),
                    text=b.text,
                    ts=_midnight_utc(b.day),
                    tags=tags,
                    paths=[str(recent_path)],
                    priority=1 if b.section == "thread" else 2,
                    meta={"date": b.day.isoformat(), "age_days": age_days, "stale": stale, "section": b.section},
                )
            )
        return items

    # --- sessions ----------------------------------------------------------------------

    def _scan_sessions(
        self,
        ctx: CollectContext,
        sessions_dir: Path,
        bullets: list[_Bullet],
        withheld: list[WithheldItem],
        tz: object,
    ) -> tuple[set[date], list[Item], int]:
        tainted: set[date] = set()
        items: list[Item] = []
        if not sessions_dir.is_dir():
            return tainted, items, 0
        entries = self._list_sessions(sessions_dir)
        recent_days = {b.day for b in bullets}
        in_window = [e for e in entries if e[2] >= ctx.window_start][:MAX_SESSIONS]
        in_window_names = {e[0].name for e in in_window}
        probes = [
            e for e in entries
            if e[0].name not in in_window_names and _session_day(e[0].name, e[2], tz) in recent_days
        ][:MAX_PROBES]

        new_sessions = 0
        for path, _, mtime in in_window:
            day = _session_day(path.name, mtime, tz)
            text = safe_read_text(path, ctx.cfg, [sessions_dir], terms=False)
            if isinstance(text, WithheldItem):
                tainted.add(day)
                withheld.append(withheld_ref("brain_session", text.source_ref, text.reason))
                continue
            new_sessions += 1
            # Term hits do not taint the date: every RECENT.md bullet is term-scanned on its
            # own, so a term elsewhere in the note says nothing about the other bullets.
            items.extend(self._session_items(path, mtime, text, ctx, withheld))
        for path, _, mtime in probes:
            # Content is discarded: this read only decides whether the date is tainted.
            if isinstance(safe_read_text(path, ctx.cfg, [sessions_dir], terms=False), WithheldItem):
                tainted.add(_session_day(path.name, mtime, tz))
        return tainted, items, new_sessions

    @staticmethod
    def _list_sessions(sessions_dir: Path) -> list[tuple[Path, str, datetime]]:
        found: list[tuple[Path, str, datetime]] = []
        with os.scandir(sessions_dir) as it:
            for entry in it:
                name = entry.name
                if not name.casefold().endswith(".md") or name.casefold().startswith("jarvis-"):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                found.append((Path(entry.path), name, mtime))
        found.sort(key=lambda e: e[1], reverse=True)  # names start with the date: newest first
        return found

    @staticmethod
    def _session_items(
        path: Path, mtime: datetime, text: str, ctx: CollectContext, withheld: list[WithheldItem]
    ) -> list[Item]:
        stem = path.stem
        out: list[Item] = []
        parts = (
            ("entry_point", "entry point", _entry_lines(_section_body(text, "Next session entry point"))),
            ("open_thread", "open thread", _bullets(_section_body(text, "Open threads"))),
        )
        for section, label, lines in parts:
            for n, line in enumerate(lines, start=1):
                hit = scan_terms(line, ctx.cfg)
                if hit is not None:
                    ref = f"{path.as_posix()}#{section}-{n}"
                    withheld.append(withheld_ref("brain_session", ref, hit.code))
                    continue
                out.append(
                    Item(
                        id=short_id("brain", "session", stem, section, line),
                        source="brain",
                        kind="brain_session",
                        title=f"Session {stem} {label}",
                        text=line,
                        ts=iso(mtime),
                        paths=[str(path)],
                        priority=1,
                        meta={"session": stem, "section": section},
                    )
                )
        return out

    # --- checkpoints -------------------------------------------------------------------

    @staticmethod
    def _count_checkpoints(directory: Path) -> int:
        """Count top-level *.json only. processed/ and from-old-machine/ are subdirectories."""
        if not directory.is_dir():
            return 0
        count = 0
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    if entry.is_file() and entry.name.casefold().endswith(".json"):
                        count += 1
                except OSError:
                    continue
        return count
