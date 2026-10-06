"""Tracker adapters (plan Q2): where a confirmed proposal becomes a task.

The hub's Inbox is the only caller. A human click there is the `always_confirm` of [trust];
nothing in the daemon creates a task on its own. Two adapters sit behind one Protocol:

- MarkdownTracker (the default) appends a block to brain/raw/jarvis/confirmed-tasks.md, through
  jarvisd/vault.py and nowhere else, and returns a file url. Nothing leaves the machine.
- ClickUpTracker POSTs to the ClickUp REST API v2 with urllib. The token is read from the
  environment variable named in [tracker].clickup_token_env at call time, never from a file, and
  is scrubbed from every error string and audit record. A create is never retried (it would
  duplicate the task); redirects are refused so the token cannot travel to another host.

Only proposal fields that already cleared the gates reach a request: title, project, rationale,
evidence ids, due. Held items are never named here. Every outward attempt is audited (intent,
then result), and `dry_run` logs the exact request instead of sending it.

The proposal is read as a mapping or as an object with the same attribute names, so this module
does not depend on how the proposal store models it.

Layer L2. Imports common, config, audit and vault.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Protocol

from jarvisd.audit import AuditLog
from jarvisd.common import iso, local_now, sha256_hex, strip_dashes
from jarvisd.config import Config
from jarvisd.vault import VaultWriteDenied, VaultWriter

CONFIRMED_NAME = "confirmed-tasks.md"
TITLE_CHARS = 200
RATIONALE_CHARS = 4000
MAX_EVIDENCE = 20
ERROR_BODY_CHARS = 160
RESPONSE_BYTES = 1_000_000

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,100}")
# Item ids come from collectors and may carry a source prefix; they never hold whitespace.
_EVIDENCE_ID = re.compile(r"[^\s\x00-\x1f\x7f]{1,200}")
_LIST_ID = re.compile(r"[0-9]{1,20}")
_EXTERNAL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SPACES = re.compile(r"\s+")
_LOOPBACK = re.compile(r"^https?://(localhost|127(\.\d{1,3}){3}|\[::1\])(:\d+)?(/|$)", re.IGNORECASE)

HEADER = (
    "---\n"
    "type: jarvis-confirmed-tasks\n"
    "generator: jarvisd\n"
    "---\n"
    "# Confirmed tasks\n"
    "\n"
    "Tasks confirmed by a click in the JARVIS Inbox, oldest first. Appended by jarvisd only.\n"
)


@dataclass(frozen=True)
class TrackerResult:
    """Outcome of one create. `error` is a short code plus, for HTTP, a scrubbed body excerpt."""

    ok: bool
    url: str | None = None
    external_id: str | None = None
    error: str | None = None
    dry_run: bool = False


class TrackerAdapter(Protocol):
    name: str

    def check(self) -> list[str]:
        """Human-readable readiness lines. Sends nothing, writes nothing."""
        ...

    def problems(self) -> list[str]:
        """Why create_task would fail right now; empty means ready."""
        ...

    def create_task(self, proposal: Any, edits: Mapping[str, Any] | None) -> TrackerResult: ...


class InvalidProposal(Exception):
    """The proposal (after edits) cannot become a task. str() is the field name."""


@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    project: str
    rationale: str
    evidence: tuple[str, ...]
    due: date | datetime | None


# --- the proposal, normalized -----------------------------------------------------------


def _field(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _one_line(value: Any, limit: int) -> str:
    return strip_dashes(_SPACES.sub(" ", str(value))).strip()[:limit]


def _parse_due(value: Any) -> date | datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                return date.fromisoformat(text)
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    raise InvalidProposal("due")


def normalize_edits(edits: Any) -> Mapping[str, Any]:
    """Edits as a plain mapping: None, a dict, or a pydantic model such as ProposalEdits."""
    if edits is None:
        return {}
    dump = getattr(edits, "model_dump", None)
    if callable(dump):
        return {k: v for k, v in dump().items() if v is not None}
    return edits


def resolve_spec(proposal: Any, edits: Mapping[str, Any] | None) -> TaskSpec:
    """The proposal with the human's edits applied on top. Raises InvalidProposal(field).

    The due date is the edit's `due`, else the proposal's `due`, else its `due_hint`.
    """
    edits = normalize_edits(edits)

    def pick(key: str) -> Any:
        return edits[key] if key in edits and edits[key] is not None else _field(proposal, key)

    pid = str(_field(proposal, "id") or "")
    if not _ID.fullmatch(pid):
        raise InvalidProposal("id")
    due_raw = pick("due")
    if due_raw is None:  # an explicit empty string in the edits clears the due date instead
        due_raw = _field(proposal, "due_hint")
    title = _one_line(pick("title") or "", TITLE_CHARS)
    if not title:
        raise InvalidProposal("title")
    evidence_raw = pick("evidence") or ()
    if isinstance(evidence_raw, str) or not hasattr(evidence_raw, "__iter__"):
        raise InvalidProposal("evidence")
    evidence = tuple(str(e) for e in evidence_raw)[:MAX_EVIDENCE]
    if not all(_EVIDENCE_ID.fullmatch(e) for e in evidence):
        # Evidence is ids only. Free text here could smuggle a held item's content outward.
        raise InvalidProposal("evidence")
    rationale = strip_dashes(str(pick("rationale") or "").replace("\r\n", "\n").replace("\r", "\n")).strip()
    return TaskSpec(
        id=pid, title=title, project=_one_line(pick("project") or "", 100),
        rationale=rationale[:RATIONALE_CHARS], evidence=evidence, due=_parse_due(due_raw),
    )


def _due_text(due: date | datetime | None) -> str:
    if due is None:
        return "none"
    return due.isoformat(timespec="minutes") if isinstance(due, datetime) else due.isoformat()


def _due_ms(due: date | datetime) -> tuple[int, bool]:
    """ClickUp due_date in epoch ms, and whether it carries a time of day.

    A bare date goes in at 12:00 UTC so it lands on the same calendar day in any workspace
    timezone from UTC-12 to UTC+11; due_date_time=false then shows it as a date only.
    """
    if isinstance(due, datetime):
        stamp = due if due.tzinfo is not None else due.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000), True
    noon = datetime.combine(due, time(12, 0), tzinfo=timezone.utc)
    return int(noon.timestamp() * 1000), False


def _description(spec: TaskSpec) -> str:
    parts = []
    if spec.rationale:
        parts.append(spec.rationale)
    if spec.evidence:
        parts.append("Evidence: " + ", ".join(spec.evidence))
    parts.append(f"Created by JARVIS from proposal {spec.id}")
    return "\n\n".join(parts)


# --- markdown ---------------------------------------------------------------------------


class MarkdownTracker:
    """Appends a task block to confirmed-tasks.md through the vault writer."""

    name = "markdown"

    def __init__(self, cfg: Config, vault: VaultWriter, audit: AuditLog,
                 clock: Callable[[], datetime] | None = None) -> None:
        self._cfg = cfg
        self._vault = vault
        self._audit = audit
        self._clock = clock or local_now

    def _status(self) -> tuple[bool, str]:
        """(writable, detail). Asks the vault writer, so every guard it has applies; writes nothing."""
        try:
            mode = self._vault.check_append(CONFIRMED_NAME)
        except VaultWriteDenied as exc:
            return False, exc.reason
        probe = self._vault.raw_path(CONFIRMED_NAME).parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not os.access(probe, os.W_OK):
            return False, "directory_not_writable"
        return True, "will be created" if mode == "create" else "exists, will append"

    def problems(self) -> list[str]:
        writable, detail = self._status()
        return [] if writable else [f"the confirmed-tasks file cannot be written: {detail}"]

    def check(self) -> list[str]:
        writable, detail = self._status()
        return [f"markdown path writable: {'yes' if writable else 'no'}",
                f"markdown file: raw/jarvis/{CONFIRMED_NAME} ({detail})"]

    def _block(self, spec: TaskSpec) -> str:
        lines = ["", f"## {spec.title}", "",
                 f"- proposal: {spec.id}",
                 f"- project: {spec.project or 'none'}",
                 f"- due: {_due_text(spec.due)}",
                 f"- confirmed: {iso(self._clock())}",
                 f"- evidence: {', '.join(spec.evidence) if spec.evidence else 'none'}"]
        if spec.rationale:
            lines += ["", spec.rationale]
        return "\n".join(lines) + "\n"

    def create_task(self, proposal: Any, edits: Mapping[str, Any] | None) -> TrackerResult:
        try:
            spec = resolve_spec(proposal, edits)
        except InvalidProposal as exc:
            return _finish(self._audit, self.name, str(_field(proposal, "id") or ""),
                           TrackerResult(ok=False, error=f"invalid_proposal:{exc}"))
        try:
            written = self._vault.append_raw(CONFIRMED_NAME, self._block(spec), spec.id, header=HEADER)
        except VaultWriteDenied as exc:
            return _finish(self._audit, self.name, spec.id, TrackerResult(ok=False, error=f"vault:{exc.reason}"))
        except OSError as exc:
            return _finish(self._audit, self.name, spec.id,
                           TrackerResult(ok=False, error=f"vault:{type(exc).__name__}"))
        return _finish(self._audit, self.name, spec.id,
                       TrackerResult(ok=True, url=Path(written.path).as_uri(), external_id=f"md-{spec.id}"))


def _finish(audit: AuditLog, adapter: str, proposal_id: str, result: TrackerResult,
            **extra: Any) -> TrackerResult:
    """Audit the outcome and hand it back. The error string is already scrubbed by the caller."""
    audit.emit("tracker_result", adapter=adapter, proposal_id=proposal_id, ok=result.ok,
               external_id=result.external_id, error=result.error, **extra)
    return result


# --- ClickUp ----------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the Authorization header must only ever reach the configured host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        return None


def _opener(api_base: str) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_NoRedirect()]
    if _LOOPBACK.match(api_base):
        handlers.append(urllib.request.ProxyHandler({}))  # a local stand-in must not go through a proxy
    return urllib.request.build_opener(*handlers)


class ClickUpTracker:
    """Creates a task with POST {api_base}/list/{list_id}/task (ClickUp API v2)."""

    name = "clickup"

    def __init__(self, cfg: Config, audit: AuditLog, *, environ: Mapping[str, str] | None = None,
                 opener: Any = None) -> None:
        self._cfg = cfg
        self._audit = audit
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        # Bound once under another name: tests/test_write_locations.py reads every `.open(x)` as a file open.
        self._urlopen = (opener if opener is not None else _opener(cfg.tracker.clickup.api_base)).open

    # --- readiness ---

    def _token(self) -> str:
        # Read at call time, so a token set after the hub started is picked up.
        return self._environ.get(self._cfg.tracker.clickup_token_env, "").strip()

    def problems(self) -> list[str]:
        found = []
        c = self._cfg.tracker.clickup
        if not self._token():
            found.append(f"no ClickUp token in the environment variable {self._cfg.tracker.clickup_token_env}")
        if not c.lists and not c.default_list_id:
            found.append("no list configured: set [tracker.clickup].default_list_id or [tracker.clickup.lists]")
        return found

    def check(self) -> list[str]:
        c = self._cfg.tracker.clickup
        return [
            f"clickup token present: {'yes' if self._token() else 'no'} ({self._cfg.tracker.clickup_token_env})",
            f"clickup list map: {len(c.lists)} project(s), default list {'set' if c.default_list_id else 'not set'}",
            f"clickup dry_run: {'on' if c.dry_run else 'off'}",
            f"clickup api base: {c.api_base}",
        ]

    # --- create ---

    def _list_id(self, spec: TaskSpec, edits: Mapping[str, Any]) -> str:
        c = self._cfg.tracker.clickup
        chosen = edits.get("list_id")
        if chosen is not None and chosen != "":
            if not _LIST_ID.fullmatch(str(chosen)):
                raise InvalidProposal("bad_list_id")
            return str(chosen)
        wanted = spec.project.casefold()
        for project, list_id in c.lists.items():
            if project.casefold() == wanted and wanted:
                return list_id
        if c.default_list_id:
            return c.default_list_id
        raise InvalidProposal("no_list")

    def _body(self, spec: TaskSpec, edits: Mapping[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"name": spec.title, "description": _description(spec)}
        if spec.due is not None:
            body["due_date"], body["due_date_time"] = _due_ms(spec.due)
        status = _one_line(edits.get("status") or self._cfg.tracker.clickup.status, 100)
        if status:
            body["status"] = status
        return body

    def _scrub(self, text: str, token: str) -> str:
        if token:
            text = text.replace(token, "<token>")
        return _one_line(text, ERROR_BODY_CHARS)

    def create_task(self, proposal: Any, edits: Mapping[str, Any] | None) -> TrackerResult:
        edits = normalize_edits(edits)
        pid = str(_field(proposal, "id") or "")
        try:
            spec = resolve_spec(proposal, edits)
            list_id = self._list_id(spec, edits)
        except InvalidProposal as exc:
            code = str(exc) if str(exc) in ("bad_list_id", "no_list") else f"invalid_proposal:{exc}"
            return _finish(self._audit, self.name, pid, TrackerResult(ok=False, error=code))
        c = self._cfg.tracker.clickup
        url = f"{c.api_base}/list/{list_id}/task"
        data = json.dumps(self._body(spec, edits), ensure_ascii=False).encode("utf-8")
        if c.dry_run:
            self._audit.emit("tracker_dry_run", adapter=self.name, proposal_id=spec.id, method="POST", url=url,
                             authorization="<redacted>", request_json=data.decode("utf-8"))
            return TrackerResult(ok=True, dry_run=True)
        token = self._token()
        if not token:
            return _finish(self._audit, self.name, spec.id, TrackerResult(ok=False, error="token_absent"))
        self._audit.emit("tracker_intent", adapter=self.name, proposal_id=spec.id, list_id=list_id,
                         bytes=len(data), sha256=sha256_hex(data))
        return self._send(spec.id, url, data, token)

    def _send(self, proposal_id: str, url: str, data: bytes, token: str) -> TrackerResult:
        request = urllib.request.Request(  # noqa: S310  the scheme is checked by the config model
            url, data=data, method="POST",
            headers={"Authorization": token, "Content-Type": "application/json", "Accept": "application/json"})
        status: int | None = None
        try:
            response = self._urlopen(request, timeout=self._cfg.tracker.clickup.timeout_s)
            try:
                status = int(getattr(response, "status", 200))
                raw = response.read(RESPONSE_BYTES)
            finally:
                response.close()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                excerpt = exc.read(2048).decode("utf-8", "replace")
            except (OSError, ValueError):
                excerpt = ""
            finally:
                exc.close()
            error = f"http_{status}" + (f": {self._scrub(excerpt, token)}" if excerpt.strip() else "")
            return _finish(self._audit, self.name, proposal_id, TrackerResult(ok=False, error=error),
                           http_status=status)
        except TimeoutError:
            return _finish(self._audit, self.name, proposal_id, TrackerResult(ok=False, error="timeout"))
        except urllib.error.URLError as exc:
            code = "timeout" if isinstance(exc.reason, TimeoutError) else "network"
            return _finish(self._audit, self.name, proposal_id, TrackerResult(ok=False, error=code))
        except OSError:
            return _finish(self._audit, self.name, proposal_id, TrackerResult(ok=False, error="network"))
        except Exception as exc:  # noqa: BLE001  a click must end in a message, never a traceback
            return _finish(self._audit, self.name, proposal_id,
                           TrackerResult(ok=False, error=f"error:{type(exc).__name__}"))
        return self._parse(proposal_id, status, raw)

    def _parse(self, proposal_id: str, status: int | None, raw: bytes) -> TrackerResult:
        if status is None or not 200 <= status < 300:
            return _finish(self._audit, self.name, proposal_id,
                           TrackerResult(ok=False, error=f"http_{status}"), http_status=status)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        task_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(task_id, str) or not _EXTERNAL_ID.fullmatch(task_id):
            return _finish(self._audit, self.name, proposal_id,
                           TrackerResult(ok=False, error="bad_response"), http_status=status)
        link = payload.get("url")
        if not (isinstance(link, str) and link.startswith("https://") and link.isprintable()):
            link = f"https://app.clickup.com/t/{task_id}"
        return _finish(self._audit, self.name, proposal_id,
                       TrackerResult(ok=True, url=link, external_id=task_id), http_status=status)


# --- factory and readiness report -------------------------------------------------------


def build_tracker(cfg: Config, audit: AuditLog, *, vault: VaultWriter | None = None,
                  environ: Mapping[str, str] | None = None, opener: Any = None,
                  clock: Callable[[], datetime] | None = None) -> MarkdownTracker | ClickUpTracker:
    """The adapter named in [tracker].adapter."""
    if cfg.tracker.adapter == "clickup":
        return ClickUpTracker(cfg, audit, environ=environ, opener=opener)
    return MarkdownTracker(cfg, vault or VaultWriter(cfg, audit), audit, clock=clock)


def readiness(cfg: Config, audit: AuditLog, *, environ: Mapping[str, str] | None = None,
              vault: VaultWriter | None = None) -> tuple[bool, list[str]]:
    """(active adapter ready, report lines). Sends and writes nothing, so it is safe to run anywhere.

    Both adapters are described whichever one is active, so switching [tracker].adapter holds
    no surprise. Token and list ids are never printed, only whether they exist and how many.
    """
    markdown = MarkdownTracker(cfg, vault or VaultWriter(cfg, audit), audit)
    clickup = ClickUpTracker(cfg, audit, environ=environ)
    active = markdown if cfg.tracker.adapter == "markdown" else clickup
    problems = active.problems()
    lines = [f"adapter: {cfg.tracker.adapter} ({'ready' if not problems else 'not ready'})"]
    lines += markdown.check() + clickup.check()
    lines += [f"problem: {p}" for p in problems]
    return not problems, lines
