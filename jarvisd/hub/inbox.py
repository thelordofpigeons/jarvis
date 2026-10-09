"""The Inbox view (plan Q4): proposals waiting for a decision, as slim cards with confirm, edit and reject forms.

This module only reads and renders. The decisions themselves are jarvisd/inbox.py, which the
routes in app.py call; the hub never writes from here. A card is: title, why (the rationale, clipped),
pills (project, due, outcome unknown), the three buttons; the identifiers and the evidence sit in a
`<details>`. Evidence ids are resolved against the digest note of the run the proposal came from: an id
that appears there outside the held section shows its line, an id that is held (the held store or the
note's held section) shows the id only and never anything else, and the ids the note does not mention
collapse to one line. Nothing is summarized and nothing is looked up in the vault.

`?sort=due` orders by due date, undated last; the default is newest first (the audit's "due pills"
replace the old Reminders view).

Layer L3 (hub).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from jarvisd.hub.data import HubData
from jarvisd.hub.digestparse import strip_id
from jarvisd.hub.mdhtml import section
from jarvisd.hub.views import _safe_href, clip, esc, fmt_ts, pill
from jarvisd.models import Proposal
from jarvisd.propose import load_proposals, proposals_dir, read_attempt

HELD_HEADING = "Held back and not summarized"
_LEAD = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")
FLASH = {"confirmed": "Confirmed", "edited_confirmed": "Confirmed with your edits", "rejected": "Rejected"}
SORTS = ("newest", "due")
WHY_CHARS = 160


def all_proposals(data: HubData) -> list[Proposal]:
    """Every readable proposal, oldest first. Reads only: a missing folder is an empty list."""
    return load_proposals(proposals_dir(data.state_dir))


def _due_of(p: Proposal) -> date | None:
    return p.edits.due or p.due_hint


def open_proposals(data: HubData, sort: str = "newest") -> list[Proposal]:
    found = [p for p in all_proposals(data) if p.status == "proposed"]
    if sort == "due":
        return sorted(found, key=lambda p: (_due_of(p) is None, _due_of(p) or date.max, p.created_at, p.id))
    return sorted(found, key=lambda p: (p.created_at, p.id), reverse=True)


def _boundary(item_id: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(item_id) + r"(?![\w-])")


def evidence_rows(data: HubData, proposal: Proposal, held_ids: set[str],
                  notes: dict[str, dict[str, Any] | None]) -> list[dict[str, str]]:
    """One row per evidence id: {id, state, text} with state cleared, held or unknown."""
    if proposal.run_id not in notes:
        notes[proposal.run_id] = data.digest_by_id(proposal.run_id) or data.latest_digest()
    note = notes[proposal.run_id]
    held_lines = section(note["body"], HELD_HEADING) if note else []
    held_text = "\n".join(held_lines)
    skip = set(held_lines)
    other = [line for line in note["body"].splitlines() if line not in skip] if note else []
    rows = []
    for item_id in proposal.evidence:
        if item_id in held_ids or _boundary(item_id).search(held_text):
            rows.append({"id": item_id, "state": "held", "text": ""})
            continue
        token = f"[{item_id}]"
        line = next((x for x in other if token in x), None)
        if line is None:
            rows.append({"id": item_id, "state": "unknown", "text": ""})
        else:
            rows.append({"id": item_id, "state": "cleared", "text": clip(strip_id(_LEAD.sub("", line))[0], 160)})
    return rows


def _evidence_html(rows: list[dict[str, str]]) -> str:
    out = []
    unknown = [row["id"] for row in rows if row["state"] == "unknown"]
    for row in rows:
        code = f"<code>{esc(row['id'])}</code>"
        if row["state"] == "cleared":
            out.append(f"<li>{code} {esc(row['text'])}</li>")
        elif row["state"] == "held":
            out.append(f"<li>{code} {pill('held', 'warn')} <span class=\"empty\">id only, never summarized</span></li>")
    if unknown:
        codes = ", ".join(f"<code>{esc(i)}</code>" for i in unknown)
        out.append(f'<li><span class="empty">{len(unknown)} id{"" if len(unknown) == 1 else "s"} not in that run\'s digest '
                   f"note:</span> {codes}</li>")
    return "<ul>" + "".join(out) + "</ul>"


def _csrf(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(token)}">'


def _warning(attempt: dict[str, str | None]) -> str:
    where = attempt["tracker"] or "the tracker"
    when = f" at {esc(fmt_ts(attempt['ts']))}" if attempt["ts"] else ""
    why = f" ({esc(attempt['error'])})" if attempt["error"] else ""
    return ('<div class="banner bad" role="alert">An earlier attempt to create this task in '
            f"{esc(where)}{when} ended and the outcome is unknown{why}. The task may already exist: look in {esc(where)} "
            "before using Confirm anyway, which can create a duplicate. If it is there, reject this proposal.</div>")


def due_pill(due: date | None, today: date) -> str:
    """`due in N days`, `due today` or `N days late`; nothing when the proposal carries no date."""
    if due is None:
        return ""
    days = (due - today).days
    if days < 0:
        return pill(f"{-days} day{'' if days == -1 else 's'} late", "bad")
    if days == 0:
        return pill("due today", "warn")
    return pill(f"due in {days} day{'' if days == 1 else 's'}")


def _card(p: Proposal, rows: list[dict[str, str]], token: str, today: date,
          attempt: dict[str, str | None] | None = None) -> str:
    base = f"/inbox/{esc(p.id)}"
    # With an unresolved attempt both forms carry the explicit override, and the button says so.
    override = '<input type="hidden" name="override" value="1">' if attempt else ""
    label = "Confirm anyway" if attempt else "Confirm"
    edit_label = "Edit and confirm anyway" if attempt else "Edit and confirm"
    facts = [("Suggested status", p.suggested_status), ("Due hint", p.due_hint.isoformat() if p.due_hint else "none"),
             ("Run", p.run_id), ("Created", fmt_ts(p.created_at)), ("Id", p.id), ("Kind", p.kind)]
    dl = "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in facts)
    pills = pill(p.project) + due_pill(_due_of(p), today) + (pill("outcome unknown", "bad") if attempt else "")
    return (
        f'<section class="card proposal" id="{esc(p.id)}">'
        f"<h2>{esc(p.title)}</h2>"
        f'<p class="why">{esc(clip(p.rationale, WHY_CHARS))}</p>'
        f'<p class="pills">{pills}</p>'
        f"{_warning(attempt) if attempt else ''}"
        '<div class="actions">'
        f'<form method="post" action="{base}/confirm" class="inline">{_csrf(token)}{override}'
        f'<button type="submit" class="primary">{label}</button></form>'
        '<details><summary>Edit</summary>'
        f'<form method="post" action="{base}/edit" class="stack">{_csrf(token)}{override}'
        f'<label>Title <input type="text" name="title" value="{esc(p.title)}" maxlength="120"></label>'
        f'<label>Project <input type="text" name="project" value="{esc(p.project)}" maxlength="80"></label>'
        f'<label>Due <input type="date" name="due" value="{esc(p.due_hint.isoformat() if p.due_hint else "")}"></label>'
        f'<button type="submit" class="primary">{edit_label}</button></form></details>'
        '<details><summary>Reject</summary>'
        f'<form method="post" action="{base}/reject" class="stack">{_csrf(token)}'
        '<label>Reason, required (it teaches the next proposals run) '
        '<input type="text" name="reason" required maxlength="500"></label>'
        '<button type="submit">Reject</button></form></details>'
        "</div>"
        f'<details class="ids"><summary>Evidence and ids</summary><dl class="facts">{dl}</dl>'
        f"<h3>Evidence</h3>{_evidence_html(rows)}</details>"
        "</section>")


def flash_html(proposal: Proposal | None, code: str) -> str:
    """The banner after a redirect. Built from the stored proposal, never from the query text."""
    if proposal is None or code not in FLASH:
        return ""
    want = ("rejected",) if code == "rejected" else ("confirmed", "edited_confirmed")
    if proposal.status not in want:
        return ""
    text = f"{FLASH[code]}: {esc(proposal.title)}."
    if proposal.status == "rejected":
        text += " The reason is kept and teaches the next proposals run."
    elif proposal.tracker_ref:
        href = _safe_href(proposal.tracker_ref)
        link = (f'<a href="{esc(href)}" rel="noreferrer">{esc(clip(proposal.tracker_ref, 80))}</a>' if href
                else f"<code>{esc(clip(proposal.tracker_ref, 120))}</code>")
        text += f" Task: {link}"
    return f'<div class="banner ok" role="status">{text}</div>'


def view(data: HubData, token: str, *, flash: str = "", error: str = "", sort: str = "newest") -> str:
    sort = sort if sort in SORTS else "newest"
    proposals = open_proposals(data, sort)
    held_ids = {r["id"] for r in data.held()}
    notes: dict[str, dict[str, Any] | None] = {}
    today = data.now().date()
    n = len(proposals)
    order = "by due date, undated last" if sort == "due" else "newest first"
    lede = f"{n} waiting, {order}." if proposals else "Nothing waiting."
    head = (f'<h1>Inbox</h1><p class="lede">{lede}</p>'
            '<details class="help" id="inbox-help"><summary>How this works</summary>'
            "<p>Task proposals waiting for a decision. Confirm creates the task in the configured tracker, and that "
            "click is the only thing that ever does. Edit changes the title, project or due date first. Reject needs a "
            "reason, which the next proposals run reads as a negative example. Only the ids of held items are shown, "
            "never their content.</p></details>")
    if proposals:
        other = ('<a class="tap" href="/inbox">newest first</a>' if sort == "due" else '<a class="tap" href="/inbox?sort=due">by due date</a>')
        head += f'<p class="sort">Sort {other}.</p>'
    banner = f'<div class="banner bad" role="alert">{esc(error)}</div>' if error else ""
    if not proposals:
        body = '<p class="empty">Nothing is waiting for a decision. <code>jarvis propose</code> makes new proposals.</p>'
    else:
        folder = proposals_dir(data.state_dir)
        body = "".join(_card(p, evidence_rows(data, p, held_ids, notes), token, today, read_attempt(folder, p.id))
                       for p in proposals)
    return head + flash + banner + body
