"""The Inbox decisions (plan Q4): confirm, edit and confirm, reject. One implementation, two doors.

The hub's Inbox forms (jarvisd/hub/app.py) and `jarvis proposals confirm|reject` call the same
functions, so the terminal path and the click path cannot drift apart. A human decision is the
only thing that creates a task in a tracker; that click is the `always_confirm` of [trust].

Rules, all tested in tests/test_inbox.py:
- A proposal is decided once. Anything but `proposed` answers "decided" and changes nothing,
  not the file, not the tracker, not the audit chain.
- The whole decision runs under one lock (in-process and cross-process), so two clicks, or a
  click and a terminal command, cannot both create the task.
- The tracker is asked first and the file is written second. A tracker failure leaves the
  proposal `proposed` with its edits unsaved, and is audited as `proposal_confirm_failed`.
  A dry-run send is not a confirmation either, because no task exists.
- The text is gated again at confirm time: the title, project and rationale as they will be sent
  (edits applied) go through the tier gate, and an evidence id that is held now stops the
  confirm. A term added to the config since the proposal was made, or typed into an edit
  field, therefore cannot reach a tracker. The outcome says "sensitive term" and never echoes it.
- An `<id>.attempt` marker is written before the outward call and removed once the outcome is
  certain. A timeout, a 5xx, an answer that is not a task, an exception or a crash between the
  call and the save all leave it behind, and a plain second confirm is then refused (the task may
  exist; a retry would duplicate it) until the human confirms with the explicit override.
- If the task was created but the file cannot be saved, the outcome says so loudly (the
  proposal still reads `proposed`; confirming again would create a duplicate, and the marker
  refuses it).
- Every state change is one atomic write of the proposal file (propose.save_proposal), and
  audits ids, the tracker name and the url, never a title or the free text of a rejection.
- Rejections keep their reason in the same JSON file the proposals job reads its negative
  examples from, so a rejected idea teaches the next run without any extra plumbing.

Layer L3. Imports propose, tracker, audit, fsio, models, common and config; the hub imports it.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jarvisd import daemon
from jarvisd.audit import AuditLog
from jarvisd.common import iso, local_now, strip_dashes
from jarvisd.config import Config
from jarvisd.fsio import FileBusy, FileLock, lock_path_for, path_lock
from jarvisd.models import PROPOSAL_ID_PATTERN, Proposal, ProposalEdits
from jarvisd.propose import clear_attempt, proposals_dir, read_attempt, save_proposal, write_attempt
from jarvisd.tracker import InvalidProposal, TrackerAdapter, build_tracker, resolve_spec

LOCK_WAIT_S = 20.0  # longer than a tracker's own timeout, so a second click waits instead of failing at once
REASON_MAX = 500
TITLE_MAX = 120
PROJECT_MAX = 80
RESULT_REQUEST_CHARS = 1500
_ID = re.compile(PROPOSAL_ID_PATTERN)
# The held store names its files by item id under the queue's held/ folder; this is the id rule of jobstore.py.
_HELD_FILE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9_-])?$")
_SPACES = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TRACKER_REF = re.compile(r"(https?://|file:///)[^\s]+")


@dataclass(frozen=True)
class Outcome:
    """What a decision did. `code` is one of: confirmed, edited_confirmed, rejected, not_found, decided,
    invalid, busy, not_ready, flagged, held, tracker_failed, maybe_created (the call ended without
    confirming a task that may exist), unresolved (refused: an earlier attempt is still unresolved),
    save_failed. `message` is plain text for a page or a terminal."""

    ok: bool
    code: str
    message: str
    proposal: Proposal | None = None
    url: str | None = None


class _Stop(Exception):
    """Internal: leave the locked section with this outcome."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.code)
        self.outcome = outcome


def audit_for(cfg: Config, clock: Callable[[], datetime] | None = None) -> AuditLog:
    """The audit log a decision writes to: the daemon's own chain, opened like the CLI opens it. Built per click,
    so a GET never touches it."""
    return AuditLog(daemon.audit_path(cfg), cfg.retention.audit_max_bytes, cfg.retention.audit_keep_days,
                    clock=clock, mirror_stdout=False)


def valid_id(proposal_id: str) -> bool:
    return bool(_ID.fullmatch(proposal_id or ""))


def _one_line(value: str | None) -> str:
    return _SPACES.sub(" ", _CONTROL.sub(" ", strip_dashes(value or ""))).strip()


def _load(folder: Path, proposal_id: str) -> Proposal:
    """The proposal, or _Stop(not_found). A file that is not a valid proposal counts as not found."""
    if not valid_id(proposal_id) or not folder.is_dir():
        raise _Stop(Outcome(False, "not_found", f"No proposal with id {proposal_id[:80]!r}."))
    try:
        proposal = Proposal.model_validate_json((folder / f"{proposal_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _Stop(Outcome(False, "not_found", f"No proposal with id {proposal_id!r}.")) from None
    if proposal.id != proposal_id:
        raise _Stop(Outcome(False, "not_found", f"No proposal with id {proposal_id!r}."))
    return proposal


def _open(folder: Path, proposal_id: str) -> Proposal:
    proposal = _load(folder, proposal_id)
    if proposal.status != "proposed":
        raise _Stop(Outcome(False, "decided", f"Proposal {proposal.id} is already {proposal.status}; nothing was changed.",
                            proposal))
    return proposal


def make_edits(proposal: Proposal, title: str | None, project: str | None, due: str | None) -> ProposalEdits:
    """Only the fields that differ from the proposal as made. Blank means "as proposed". Raises ValueError(text)."""
    fields: dict[str, Any] = {}
    new_title = _one_line(title)
    if new_title and new_title != proposal.title:
        if len(new_title) > TITLE_MAX:
            raise ValueError(f"title must be at most {TITLE_MAX} characters")
        fields["title"] = new_title
    new_project = _one_line(project)
    if new_project and new_project != proposal.project:
        if len(new_project) > PROJECT_MAX:
            raise ValueError(f"project must be at most {PROJECT_MAX} characters")
        fields["project"] = new_project
    text = (due or "").strip()
    if text:
        try:
            day = date.fromisoformat(text) if _DATE.fullmatch(text) else None
        except ValueError:
            day = None
        if day is None:
            raise ValueError("due must be a date like 2026-10-31")
        if day != proposal.due_hint:
            fields["due"] = day
    try:
        return ProposalEdits(**fields)
    except ValidationError as exc:  # unreachable after the checks above, but a model change must not become a 500
        raise ValueError(f"the edits are not valid: {exc.errors()[0]['loc'][-1]}") from None


class _Locked:
    """In-process and cross-process exclusion for one decision. Not reentrant, by design."""

    def __init__(self, folder: Path) -> None:
        self._thread = path_lock(folder / "inbox")
        self._file = FileLock(lock_path_for(folder / "inbox"), timeout=LOCK_WAIT_S)
        self._have_thread = False

    def __enter__(self) -> "_Locked":
        if not self._thread.acquire(timeout=LOCK_WAIT_S):
            raise _Stop(Outcome(False, "busy", "Another decision is being processed right now. Try again in a moment."))
        self._have_thread = True
        try:
            if not self._file.acquire():
                raise _Stop(Outcome(False, "busy", "Another decision is being processed right now. Try again in a moment."))
        except OSError as exc:
            self._thread.release()
            self._have_thread = False
            raise _Stop(Outcome(False, "busy", f"The proposals folder could not be locked ({type(exc).__name__}).")) from exc
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


def _saved(proposal: Proposal, **changes: Any) -> Proposal:
    """A new, validated proposal with `changes` applied (validation also re-strips dashes)."""
    return Proposal.model_validate({**proposal.model_dump(mode="json"), **changes})


def confirm(cfg: Config, audit: AuditLog, tracker: TrackerAdapter | None, proposal_id: str, *,
            title: str | None = None, project: str | None = None, due: str | None = None,
            clock: Callable[[], datetime] | None = None, override: bool = False) -> Outcome:
    """Create the task through the tracker adapter, then mark the proposal confirmed (or edited_confirmed).

    `title`, `project` and `due` are the Edit form's raw strings; blank or unchanged means as proposed.
    `tracker` None builds the adapter named in [tracker].adapter at the moment of the click.
    `override` is the human's explicit "confirm anyway" after an attempt whose outcome is unknown.
    """
    folder = proposals_dir(cfg.daemon.state_dir)
    try:
        _load(folder, proposal_id)  # a missing proposal must not create the folder or its lock file
        with _Locked(folder):
            return _confirm_locked(cfg, audit, tracker, folder, proposal_id, title, project, due, clock, override)
    except _Stop as stop:
        return stop.outcome


def _failed(audit: AuditLog, name: str, proposal: Proposal, error: str, code: str, message: str, **extra: Any) -> Outcome:
    # `run_id` is the audit envelope's own key (the process run); the digest run goes under its own name.
    audit.emit("proposal_confirm_failed", proposal_id=proposal.id, digest_run_id=proposal.run_id, tracker=name,
               error=error, **extra)
    return Outcome(False, code, message, proposal)


def _held_evidence(cfg: Config, proposal: Proposal) -> list[str]:
    """Evidence ids that sit in the held store now. Reads file names only, never a held file's content."""
    held = Path(cfg.paths.queue) / "held"
    return [e for e in proposal.evidence if _HELD_FILE_ID.match(e) and (held / f"{e}.json").is_file()]


def _unresolved(proposal: Proposal, attempt: dict[str, str | None]) -> Outcome:
    where = attempt["tracker"] or "the tracker"
    when = f" at {attempt['ts']}" if attempt["ts"] else ""
    why = f" ({attempt['error']})" if attempt["error"] else ""
    return Outcome(False, "unresolved",
                   f"Not confirmed: an earlier attempt to create this task in {where}{when} ended and the outcome is "
                   f"unknown{why}. The task may already exist, so confirming again could create a duplicate. Look in "
                   f"{where} for a task from proposal {proposal.id}. If it is not there, use Confirm anyway (or "
                   "--confirm-anyway on the command line). If it is, reject this proposal. Nothing was changed.", proposal)


def _confirm_locked(cfg: Config, audit: AuditLog, tracker: TrackerAdapter | None, folder: Path, proposal_id: str,
                    title: str | None, project: str | None, due: str | None,
                    clock: Callable[[], datetime] | None, override: bool = False) -> Outcome:
    proposal = _open(folder, proposal_id)
    attempt = read_attempt(folder, proposal.id)
    if attempt is not None and not override:
        return _unresolved(proposal, attempt)
    try:
        edits = make_edits(proposal, title, project, due)
    except ValueError as exc:
        return Outcome(False, "invalid", str(exc).capitalize() + ". Nothing was changed.", proposal)
    adapter = tracker if tracker is not None else build_tracker(cfg, audit, clock=clock)
    name = str(getattr(adapter, "name", "tracker"))
    # The gate again, on what will actually be sent: the config may have changed since the proposal was made,
    # and the Edit fields are free text the proposals job never saw.
    try:
        resolve_spec(proposal, edits, cfg)
    except InvalidProposal as exc:
        if str(exc) == "sensitive":
            return _failed(audit, name, proposal, "flagged:sensitive", "flagged",
                           "Not confirmed: the title, project or rationale of this proposal contains a sensitive term, so "
                           "nothing was sent anywhere. Reject it and write the task by hand if it is still needed.")
    held = _held_evidence(cfg, proposal)
    if held:
        return _failed(audit, name, proposal, "held_evidence", "held",
                       f"Not confirmed: evidence item(s) {', '.join(held)} are held back now, and this proposal's text may "
                       "summarize them. Nothing was sent. Reject the proposal if the work is still needed.")
    try:
        problems = adapter.problems()
    except Exception as exc:  # noqa: BLE001  a click must end in a message, never a traceback
        problems = [f"readiness check failed ({type(exc).__name__})"]
    if problems:
        text = "; ".join(problems)
        return _failed(audit, name, proposal, f"not_ready: {text}"[:300], "not_ready",
                       f"Not confirmed: the {name} tracker is not ready: {text}. The proposal is still open.")
    stamp = iso((clock or local_now)())
    if attempt is not None:
        audit.emit("proposal_confirm_override", proposal_id=proposal.id, digest_run_id=proposal.run_id, tracker=name,
                   earlier_attempt=attempt["ts"] or None)
    try:
        write_attempt(folder, proposal.id, tracker=name, ts=stamp)  # before the call: a crash must leave a trace
    except (OSError, FileBusy) as exc:
        return _failed(audit, name, proposal, f"attempt_marker_failed:{type(exc).__name__}", "save_failed",
                       f"Not confirmed: the proposals folder could not be written ({type(exc).__name__}), so nothing was "
                       "sent. The proposal is still open.")

    def unknown(error: str) -> Outcome:
        try:
            write_attempt(folder, proposal.id, tracker=name, ts=stamp, error=error)
        except (OSError, FileBusy):
            pass  # the marker from before the call is already there
        return _failed(audit, name, proposal, error, "maybe_created",
                       f"The {name} tracker did not confirm the create ({error}), but the task may already exist. "
                       f"Nothing was saved. Look in {name} for a task from proposal {proposal.id} before doing anything "
                       "else: confirming again needs an explicit override, because it could create a duplicate.",
                       outcome_unknown=True)

    try:
        result = adapter.create_task(proposal, edits)
    except Exception as exc:  # noqa: BLE001
        return unknown(f"error:{type(exc).__name__}")
    if result.dry_run:
        clear_attempt(folder, proposal.id)  # nothing was sent, so there is nothing to be unsure about
        shown = f" The request that would have been sent: {result.request[:RESULT_REQUEST_CHARS]}" if result.request else ""
        return _failed(audit, name, proposal, "dry_run", "tracker_failed",
                       f"Not confirmed: the {name} tracker is in dry_run, so the request was built and nothing was sent. "
                       f"The proposal is still open.{shown}", dry_run=True)
    if not result.ok:
        error = (result.error or "failed")[:300]
        if result.unknown:
            return unknown(error)
        clear_attempt(folder, proposal.id)
        return _failed(audit, name, proposal, error, "tracker_failed",
                       f"Not confirmed: the {name} tracker answered {error}. The proposal is still open.")
    ref = result.url if result.url and _TRACKER_REF.fullmatch(result.url) and len(result.url) <= 500 else None
    edited = bool(edits.title or edits.project or edits.due)
    status = "edited_confirmed" if edited else "confirmed"
    try:
        done = _saved(proposal, status=status, tracker_ref=ref, edits=edits.model_dump(mode="json"))
        save_proposal(folder, done)
    except (OSError, FileBusy, ValidationError) as exc:
        where = f" at {result.url}" if result.url else ""
        return _failed(audit, name, proposal, f"save_failed:{type(exc).__name__}", "save_failed",
                       f"The task was already created{where}, but the proposal file could not be saved "
                       f"({type(exc).__name__}), so it still reads proposed. Do not confirm it again: that would create "
                       "a duplicate.", external_id=result.external_id, url=result.url)
    clear_attempt(folder, proposal.id)
    audit.emit("proposal_confirmed", proposal_id=done.id, digest_run_id=done.run_id, tracker=name, url=ref,
               external_id=result.external_id, edited=edited, status=status)
    where = f": {ref}" if ref else ""
    return Outcome(True, status, f"Confirmed {done.id}; the task was created in {name}{where}.", done, ref)


def reject(cfg: Config, audit: AuditLog, proposal_id: str, reason: str | None) -> Outcome:
    """Mark the proposal rejected with a required reason (the next proposals run reads it as a negative example)."""
    folder = proposals_dir(cfg.daemon.state_dir)
    try:
        _load(folder, proposal_id)
        with _Locked(folder):
            proposal = _open(folder, proposal_id)
            text = _one_line(reason)
            if not text:
                return Outcome(False, "invalid", "A reason is required to reject a proposal. Nothing was changed.", proposal)
            if len(text) > REASON_MAX:
                return Outcome(False, "invalid", f"The reason must be at most {REASON_MAX} characters. Nothing was changed.",
                               proposal)
            try:
                done = _saved(proposal, status="rejected", rejected_reason=text)
                save_proposal(folder, done)
            except (OSError, FileBusy, ValidationError) as exc:
                audit.emit("proposal_reject_failed", proposal_id=proposal.id, error=f"save_failed:{type(exc).__name__}")
                return Outcome(False, "save_failed", f"The proposal could not be saved ({type(exc).__name__}); "
                               "it is still open.", proposal)
            clear_attempt(folder, done.id)  # a rejected proposal is never confirmed, so a marker would only be noise
            audit.emit("proposal_rejected", proposal_id=done.id, digest_run_id=done.run_id,
                       reason_chars=len(done.rejected_reason or ""))
            return Outcome(True, "rejected", f"Rejected {done.id}. The reason is kept and teaches the next proposals run.", done)
    except _Stop as stop:
        return stop.outcome


# --- the terminal door ------------------------------------------------------------------------------------------------


def cmd_decide(ctx: Any, args: Any) -> int:
    """`jarvis proposals confirm <id> [--title T --project P --due D]` and `... reject <id> --reason TEXT`.

    `ctx` is the CLI's Ctx; this module never imports the CLI. Exit 0 only when the decision was made.
    """
    audit = ctx.audit()
    if args.proposals_command == "confirm":
        out = confirm(ctx.cfg, audit, None, args.id, title=args.title, project=args.project, due=args.due,
                      clock=ctx.clock, override=bool(getattr(args, "confirm_anyway", False)))
    else:
        out = reject(ctx.cfg, audit, args.id, args.reason)
    print(out.message)
    return 0 if out.ok else 1
