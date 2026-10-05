"""Task collector (design section 8): the five `~/.claude/current-task*` files.

The five names are fixed constants. There is no glob and no directory walk, so
`.credentials.json` and `clickup-config.json`, which sit in the same folder, cannot be
reached by accident. Each file is its own read root for `tier.safe_read_text`.

The result is one `active_task` item. It is marked `work = true` because the task comes from
the work ClickUp workspace, so with the default policy it renders deterministically and is
not summarized by Claude. v1 makes no ClickUp call: everything here is local state.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

from jarvisd.collectors import CollectContext, withheld_ref
from jarvisd.common import parse_iso, short_id
from jarvisd.models import CollectResult, Item, WithheldItem
from jarvisd.tier import safe_read_text

FILES = {
    "id": "current-task",
    "name": "current-task-name",
    "status": "current-task-status",
    "step": "current-task-step",
    "due": "current-task-due",
}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_FIELD_CHARS = 200


def default_base_dir() -> Path:
    return Path.home() / ".claude"


def parse_due(value: str, tz: object) -> date | None:
    """Parse the due field: a date, an ISO timestamp, or epoch seconds or milliseconds."""
    text = value.strip()
    if not text:
        return None
    try:
        if _ISO_DATE.match(text):
            return date.fromisoformat(text)
        if text.isdigit() and len(text) >= 10:
            seconds = int(text) / (1000.0 if len(text) >= 13 else 1.0)
            return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(tz).date()  # type: ignore[arg-type]
        return parse_iso(text).astimezone(tz).date()  # type: ignore[arg-type]
    except (ValueError, OverflowError, OSError):
        return None


def due_state(due: date | None, today: date) -> str:
    if due is None:
        return "none"
    if due < today:
        return "overdue"
    return "today" if due == today else "later"


class TaskCollector:
    """Collector named `task`. `base_dir` is injectable so tests never touch the real folder."""

    name = "task"

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else default_base_dir()

    def _read_field(self, ctx: CollectContext, filename: str, withheld: list[WithheldItem]) -> str:
        path = self.base_dir / filename
        # Absent is normal (no active task); safe_read_text would call it a tier error.
        if not path.is_file():
            return ""
        text = safe_read_text(path, ctx.cfg, [path], max_bytes=4096)
        if isinstance(text, WithheldItem):
            withheld.append(withheld_ref("task_field", text.source_ref, text.reason))
            return ""
        return " ".join(text.split())[:MAX_FIELD_CHARS]

    def collect(self, ctx: CollectContext) -> CollectResult:
        withheld: list[WithheldItem] = []
        values = {key: self._read_field(ctx, filename, withheld) for key, filename in FILES.items()}
        task_id = values["id"]
        if not task_id:
            return CollectResult(
                source=self.name, ok=True, withheld=withheld, facts={"task_active": False}
            )
        if any(w.kind == "task_field" for w in withheld):
            # One unreadable field means we cannot vouch for the item as a whole.
            return CollectResult(
                source=self.name, ok=True, withheld=withheld, facts={"task_active": True, "task_withheld": True}
            )
        due = parse_due(values["due"], ctx.now.tzinfo)
        state = due_state(due, ctx.now.date())
        title = values["name"] or task_id
        status = values["status"]
        step = values["step"]
        text = f"status {status or 'unknown'}" + (f", step {step}" if step else "")
        item = Item(
            id=short_id("task", task_id, title),
            source="task",
            kind="active_task",
            title=title,
            text=text,
            work=True,
            priority=0,
            paths=[str(self.base_dir / FILES["id"])],
            meta={
                "task_id": task_id,
                "status": status,
                "step": step,
                "due_state": state,
                "due_date": due.isoformat() if due else "",
            },
        )
        return CollectResult(
            source=self.name,
            ok=True,
            items=[item],
            withheld=withheld,
            facts={"task_active": True, "due_state": state},
        )
