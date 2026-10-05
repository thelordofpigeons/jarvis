"""ClickUp collector (design D7, plan T13): the user's open tasks through the claude.ai connector.

Behind `[digest].clickup_enabled` (default false) and behind a passing `jarvis clickup check
--live` on this machine. The only network path is `ClaudeClient.complete_static`, the second
`claude -p` profile (`clickup_read`): a constant prompt with three validated date or id
tokens, two read tools allowed, every other tool denied, an empty working directory and a
10 cent cap. No vault text, no item text and no file is ever part of that call, and this
module reads no ClickUp file and no credential: the connector authenticates by itself, so
nothing secret is ever opened, named or passed on.

What comes back is untrusted. It is parsed into `ClickUpTask` models (ids, dates and lengths
checked), turned into `clickup_task` items marked `work` (the workspace is a work one,
so by default they render in the digest and are never summarized by Claude) and handed to the
normal gates like any other item. The task URL is rebuilt from the validated id; a URL from
the model is ignored. Free text lives in `title` and `text` only, never in `meta`, because
the tier gate reads path-like `meta` strings as paths.

Failure never fails the digest: every problem becomes `CollectResult(ok=False, error=<code>)`
and the renderer prints "ClickUp: unavailable (<code>)". A reply that used a tool outside the
allowlist, or that carried a permission denial, is thrown away unread and audited as
`injection_signal` (that happens inside the client).

Limits, stated plainly:
- The profile cannot hide the other connectors and MCP servers of the account from the model
  (no --strict-mcp-config); it can only refuse their tools. docs/v1-operations.md says so.
- `jarvis clickup check --live` is the only thing that proves the flag set works against the
  real connector. Until it has passed, this collector answers `check_not_run`.
- A task the model left out is not in the digest. The filter (assignee, status, dates) is the
  model's reading of a prompt, not a query this code ran; the count is printed so a quiet
  day and a failed filter can be told apart by the reader.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jarvisd.claude import (
    CLICKUP_ALLOWED_TOOLS,
    CLICKUP_REQUIRED_FLAGS,
    CLICKUP_TEMPLATE,
    ClaudeClient,
    ClaudeUnavailable,
    CLICKUP_BUILTIN_DENIED,
    CLICKUP_OTHER_TOOLS,
    build_clickup_argv,
    strip_fence,
)
from jarvisd.collectors import CollectContext
from jarvisd.collectors.task import due_state
from jarvisd.common import iso, short_id
from jarvisd.config import Config
from jarvisd.dispatch import PayloadBlocked
from jarvisd.models import CollectResult, Item

PURPOSE = "clickup_read"
MAX_TASKS = 30
DUE_HORIZON = timedelta(hours=48)
# Longer than the runner's default: connector schemas load before the first tool call.
COLLECTOR_TIMEOUT_S = 90.0
TASK_URL = "https://app.clickup.com/t/{task_id}"
REFUSAL_KINDS = frozenset({"breaker", "budget", "killed", "disabled"})

_TASK_ID = re.compile(r"^[0-9A-Za-z_-]{1,20}$")
_STATUS = re.compile(r"^[A-Za-z0-9 _-]{1,40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class SchemaError(ValueError):
    """The reply was JSON but not an array of objects."""


class AuditSink(Protocol):
    def emit(self, event: str, **fields: Any) -> Any: ...


def _one_line(value: str, limit: int) -> str:
    return " ".join(_CONTROL.sub(" ", value).split())[:limit]


class ClickUpTask(BaseModel):
    """One task as the model reports it. Only these fields are read; anything else is ignored."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    task_id: str
    name: str = Field(min_length=1)
    status: str = ""
    due: date | None = None
    updated: date | None = None
    list_name: str = Field(default="", alias="list")

    @field_validator("task_id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _TASK_ID.fullmatch(value):
            raise ValueError("task_id has an unexpected shape")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        cleaned = _one_line(value, 200)
        if not cleaned:
            raise ValueError("empty name")
        return cleaned

    @field_validator("status", "list_name")
    @classmethod
    def _short(cls, value: str) -> str:
        return _one_line(value, 80)

    @field_validator("due", "updated", mode="before")
    @classmethod
    def _day(cls, value: Any) -> Any:
        # A timestamp is cut to its date; null and "" mean none.
        if value in (None, ""):
            return None
        return value[:10] if isinstance(value, str) and len(value) > 10 else value


def parse_tasks(text: str) -> tuple[list[ClickUpTask], int, str]:
    """(tasks, dropped, partial_reason) from the model's text.

    ValueError (json.JSONDecodeError) when the text is not JSON; SchemaError (a ValueError)
    when it is JSON but not an array. A single bad element is dropped and counted, more than
    30 are cut, and either makes the result partial. Duplicates by id keep the first.
    """
    data = json.loads(strip_fence(text))
    if not isinstance(data, list):
        raise SchemaError("expected a JSON array")
    reason = "over_30" if len(data) > MAX_TASKS else ""
    tasks: list[ClickUpTask] = []
    seen: set[str] = set()
    dropped = 0
    for raw in data[:MAX_TASKS]:
        try:
            task = ClickUpTask.model_validate(raw)
        except ValidationError:
            dropped += 1
            continue
        if task.task_id in seen:
            continue
        seen.add(task.task_id)
        tasks.append(task)
    if dropped and not reason:
        reason = f"dropped_{dropped}"
    return tasks, dropped, reason


def _midnight(day: date) -> str:
    return iso(datetime.combine(day, dtime.min, tzinfo=timezone.utc))


def to_item(task: ClickUpTask, today: date) -> Item:
    state = due_state(task.due, today)
    parts = [f"status {task.status or 'unknown'}"]
    if task.list_name:
        parts.append(f"list {task.list_name}")
    return Item(
        id=short_id("clickup", task.task_id), source="clickup", kind="clickup_task", title=task.name,
        text=", ".join(parts), ts=_midnight(task.updated) if task.updated else None,
        # The workspace belongs to the employer: metadata stays out of Claude unless the owner
        # turned [digest].work_metadata_to_claude on in the local file.
        work=True, priority=1,
        meta={"task_id": task.task_id, "status": task.status if _STATUS.fullmatch(task.status) else "",
              "due_state": state, "due_date": task.due.isoformat() if task.due else "",
              "updated": task.updated.isoformat() if task.updated else ""},
    )


def task_url(task_id: str) -> str:
    """The link the digest prints, built from the validated id and nothing the model wrote."""
    return TASK_URL.format(task_id=task_id)


def _params(cfg: Config, now: datetime, window_start: datetime) -> dict[str, str]:
    return {
        "assignee": cfg.digest.clickup_user_id or "me",
        "due_before": (now + DUE_HORIZON).date().isoformat(),
        "updated_after": window_start.date().isoformat(),
    }


class ClickUpCollector:
    """Collector named `clickup`. Built by the composition root only when the flag is on."""

    name = "clickup"
    timeout_s = COLLECTOR_TIMEOUT_S

    def __init__(self, client: ClaudeClient, audit: AuditSink) -> None:
        self.client = client
        self.audit = audit

    def _skipped(self, why: str) -> CollectResult:
        return CollectResult(source=self.name, ok=True, facts={"skipped": why, "tasks": 0, "partial": False})

    def _failed(self, error: str) -> CollectResult:
        return CollectResult(source=self.name, ok=False, error=error)

    def collect(self, ctx: CollectContext) -> CollectResult:
        cfg = ctx.cfg
        if not cfg.digest.clickup_enabled:
            return self._skipped("flag_off")
        if not self.client.enabled:
            return self._skipped("disabled")
        status = self.client.clickup_check_status()
        if status:
            return self._failed(status)
        try:
            reply = self.client.complete_static(CLICKUP_TEMPLATE, _params(cfg, ctx.now, ctx.window_start), PURPOSE,
                                                job_id=ctx.job_id)
        except ClaudeUnavailable as exc:
            return self._failed(exc.kind)
        except (PayloadBlocked, TypeError):
            return self._failed("payload_blocked")
        if not reply.result.strip():
            return self._failed("empty_reply")
        try:
            tasks, dropped, partial = parse_tasks(reply.result)
        except SchemaError:
            return self._failed("bad_schema")
        except ValueError:
            return self._failed("bad_json")
        today = ctx.now.date()
        return CollectResult(
            source=self.name, ok=True, items=[to_item(t, today) for t in tasks],
            facts={"tasks": len(tasks), "partial": bool(partial), "partial_reason": partial, "dropped": dropped},
        )


# --- jarvis clickup check ------------------------------------------------------------------


@dataclass(frozen=True)
class CheckRow:
    name: str
    ok: bool
    detail: str
    refused: bool = False


def argv_problems(cfg: Config) -> list[str]:
    """What is wrong with the profile's argv as built from this configuration. Empty means sound."""
    argv = build_clickup_argv(cfg, binary="claude")
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    problems: list[str] = []
    for flag in ("--strict-mcp-config", "--tools"):
        if flag in argv:
            problems.append(f"{flag} must not be in this profile")
    if argv[argv.index("--setting-sources") + 1] != "":
        problems.append("--setting-sources must be empty")
    for flag in ("--disable-slash-commands", "--no-session-persistence"):
        if flag not in argv:
            problems.append(f"{flag} is missing")
    if argv[argv.index("--permission-prompts") + 1] != "none":
        problems.append("--permission-prompts must be none")
    if float(argv[argv.index("--max-budget-usd") + 1]) > 0.5:
        problems.append("the budget cap is above 0.50 USD")
    extra_allowed = [t for t in allowed if t not in CLICKUP_ALLOWED_TOOLS and t != "ToolSearch"]
    if extra_allowed:
        problems.append(f"unexpected allowed tools: {', '.join(extra_allowed)}")
    if set(allowed) & set(denied):
        problems.append("a tool is both allowed and denied")
    wanted = [t for t in (*CLICKUP_BUILTIN_DENIED, *CLICKUP_OTHER_TOOLS) if t not in allowed]
    missing = [t for t in wanted if t not in denied]
    if missing:
        problems.append(f"{len(missing)} tool(s) not denied, first: {missing[0]}")
    return problems


def _cwd_row(client: ClaudeClient) -> CheckRow:
    path = client.cwd
    if not path.exists():
        return CheckRow("claude-cwd", True, f"{path.as_posix()} does not exist yet; it is created empty on first use")
    try:
        with os.scandir(path) as it:
            entries = sum(1 for _ in it)
    except OSError as exc:
        return CheckRow("claude-cwd", False, f"cannot list {path.as_posix()}: {type(exc).__name__}")
    if entries:
        return CheckRow("claude-cwd", False, f"{path.as_posix()} holds {entries} entries; the profile needs it empty")
    return CheckRow("claude-cwd", True, f"{path.as_posix()} is empty")


def _record_row(client: ClaudeClient, cfg: Config) -> CheckRow:
    status = client.clickup_check_status()
    flag = cfg.digest.clickup_enabled
    if status == "":
        record = client.read_clickup_check() or {}
        when = str(record.get("ts", "unknown time"))
        tail = "the section is on" if flag else "set [digest].clickup_enabled = true in jarvis.local.toml to use it"
        return CheckRow("live check record", True, f"passed at {when} for this flag set; {tail}")
    notes = {
        "check_not_run": "none yet; run jarvis clickup check --live once on this machine",
        "check_stale": "the flag set changed since the last live check; run it again",
        "check_failed": "the last live check failed; read its output, fix the cause, run it again",
    }
    return CheckRow("live check record", True, notes.get(status, status))


def _live(client: ClaudeClient, cfg: Config, now: datetime) -> CheckRow:
    """The one paid call, on the same code path as the collector, and its record."""
    window_start = now - timedelta(hours=cfg.digest.window_hours_default)
    try:
        reply = client.complete_static(CLICKUP_TEMPLATE, _params(cfg, now, window_start), PURPOSE)
    except ClaudeUnavailable as exc:
        refused = exc.kind in REFUSAL_KINDS
        if not refused:
            client.record_clickup_check(ok=False, detail=exc.kind)
        hint = ""
        if "ToolSearch" in exc.detail:
            hint = ". The model reached for ToolSearch: set [digest].clickup_allow_tool_search = true and run this again"
        elif exc.kind == "tool_violation":
            hint = ". A tool outside the allowlist or a permission denial; see the injection_signal audit record"
        suffix = f" ({exc.detail})" if exc.detail else ""
        return CheckRow("live call", False, f"{exc.kind}{suffix}{hint}", refused=refused)
    except (PayloadBlocked, TypeError):
        return CheckRow("live call", False, "payload_blocked", refused=True)
    problem = ""
    tasks: list[ClickUpTask] = []
    partial = ""
    if not reply.result.strip():
        problem = "empty_reply: the reply had no text"
    else:
        try:
            tasks, _dropped, partial = parse_tasks(reply.result)
        except SchemaError:
            problem = "bad_schema: the reply was not a JSON array of tasks"
        except ValueError:
            problem = "bad_json: the reply was not JSON"
    if problem:
        client.record_clickup_check(ok=False, detail=problem.split(":", 1)[0], cost_usd=reply.total_cost_usd,
                                    input_tokens=reply.total_input_tokens, tools_used=reply.tool_names)
        return CheckRow("live call", False, problem)
    search = "ToolSearch" in reply.tool_names
    client.record_clickup_check(ok=True, detail=partial, cost_usd=reply.total_cost_usd,
                                input_tokens=reply.total_input_tokens, tools_used=reply.tool_names,
                                tasks=len(tasks))
    used = ", ".join(reply.tool_names) or "none"
    return CheckRow("live call", True, (
        f"{len(tasks)} tasks, permission denials 0, tools used: {used}; ToolSearch "
        f"{'was used' if search else 'was not needed'}; {reply.total_input_tokens} input tokens; "
        f"cost {reply.total_cost_usd:.4f} USD" + (f"; partial ({partial})" if partial else "")))


def run_check(client: ClaudeClient, cfg: Config, now: datetime, *, live: bool) -> list[CheckRow]:
    """Verify the flag set on this machine. Offline rows are free; `live` adds the one paid call."""
    rows: list[CheckRow] = []
    report = client.preflight()
    rows.append(CheckRow("claude binary", report.ok,
                         f"{report.binary} {report.version}".strip() if report.ok else (report.reason or "not found")))
    missing = client.clickup_missing_flags() if report.binary else tuple(CLICKUP_REQUIRED_FLAGS)
    rows.append(CheckRow("flags in --help", not missing,
                         f"all {len(CLICKUP_REQUIRED_FLAGS)} present" if not missing else f"missing: {', '.join(missing)}"))
    problems = argv_problems(cfg)
    allowed = build_clickup_argv(cfg, binary="claude")
    n_allowed = len(allowed[allowed.index("--allowedTools") + 1].split(","))
    n_denied = len(allowed[allowed.index("--disallowedTools") + 1].split(","))
    rows.append(CheckRow("argv", not problems,
                         "; ".join(problems) if problems else
                         f"{n_allowed} tools allowed, {n_denied} denied, cap {cfg.digest.clickup_max_budget_usd:g} USD, "
                         "no --strict-mcp-config, no --tools"))
    rows.append(_cwd_row(client))
    rows.append(CheckRow("flag", True, f"[digest].clickup_enabled is {str(cfg.digest.clickup_enabled).lower()}"))
    rows.append(_record_row(client, cfg))
    if live:
        rows.append(_live(client, cfg, now))
        if rows[-1].ok:
            rows.append(_record_row(client, cfg))
    else:
        rows.append(CheckRow("live call", True, (
            f"not run. Add --live to spend up to {cfg.digest.clickup_max_budget_usd:g} USD on one real call "
            "and record the result")))
    return rows


def cmd_check(cfg: Config, client: ClaudeClient, audit: AuditSink, now: datetime, *, live: bool,
              as_json: bool) -> int:
    """Print the rows and return the exit code: 0 all pass, 1 a failure, 3 refused by a safety control."""
    started = time.monotonic()
    rows = run_check(client, cfg, now, live=live)
    failed = [r for r in rows if not r.ok]
    refused = any(r.refused for r in rows)
    audit.emit("clickup_check", live=live, ok=not failed, failed=[r.name for r in failed],
               duration_ms=int((time.monotonic() - started) * 1000))
    if as_json:
        print(json.dumps([{"name": r.name, "ok": r.ok, "detail": r.detail} for r in rows], indent=2))
    else:
        for r in rows:
            print(f"{'PASS' if r.ok else 'FAIL'}  {r.name}: {r.detail}")
        print(f"{len(rows) - len(failed)} passed, {len(failed)} failed.")
    if refused:
        return 3
    return 1 if failed else 0
