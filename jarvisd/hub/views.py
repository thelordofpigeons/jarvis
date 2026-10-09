"""HTML for the four views (Today, Projects, Activity and the digest page; the Inbox is hub/inbox.py).

Pure functions from data to a string; no I/O, no state, and nothing here calls back into the data
layer: each route hands a view the one dict its HubData method returned. Every value that did not
come from this file goes through `esc`, including values read from JARVIS's own files, so a note or
a log line can never become markup. There is no inline script, style attribute or external
reference: the Content-Security-Policy in app.py allows only same-origin files.

Jargon is translated once, in `label` and the small maps around it (docs/hub.md, "Label map").
Item ids never appear in visible text: a line keeps its id in a `data-id` attribute.

Layer L3 (hub). Imports hub.mdhtml and hub.digestparse only.
"""
from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta
from typing import Any

from jarvisd.common import parse_iso
from jarvisd.hub.digestparse import counts_of, still_open_groups
from jarvisd.hub.mdhtml import render_markdown

NAV = (("/", "Today"), ("/inbox", "Inbox"), ("/projects", "Projects"), ("/activity", "Activity"))
WRONG_CHOICES = "escalate, hold, skip or other"
# Old routes, kept as 301 redirects so bookmarks survive (docs/hub.md).
REDIRECTS = {"/runs": "/activity#runs", "/ledger": "/activity#delivered", "/held": "/activity#held",
             "/audit": "/activity#audit", "/status": "/activity#status", "/repos": "/projects",
             "/reminders": "/inbox?sort=due"}

# The label map: what the daemon calls a thing, and what the page says.
LABELS = {
    "not_installed": "not installed", "unavailable": "unavailable", "up": "up",
    "held_policy": "kept back (work metadata)", "held_sensitive": "kept back (sensitive, never read)",
    "tag_frontmatter": "tagged sensitive", "over_cap": "too long to send", "counts_only": "counts only, names kept back",
    "degraded_no_llm": "written without Claude", "partial": "written with a source missing", "complete": "written",
    "written": "written", "failed": "failed", "noop": "nothing to do",
    "proposed": "waiting for you", "confirmed": "confirmed", "edited_confirmed": "confirmed with edits",
    "rejected": "rejected", "open": "Claude calls paused", "closed": "Claude calls allowed",
    "work_policy": "kept back (work metadata)", "half_open": "Claude calls on probation",
}
# GitHub CI tokens as the Projects table says them.
CI_LABELS = {"failure": "CI failing", "timed_out": "CI timed out", "startup_failure": "CI failing", "action_required": "CI failing",
             "success": "CI green", "none": "no CI runs"}
# Daemon status keys, as the Activity disclosure labels them (the raw CLI block stays behind a nested disclosure).
STATUS_LABELS = (("running", "Daemon"), ("heartbeat_age_s", "Last sign of life"), ("version", "Version"),
                 ("claude_cli_version", "Claude CLI"), ("local_tier", "Local model"), ("breaker", "Claude calls"),
                 ("watermark", "Digest window start"), ("next_due", "Next digest"), ("kill", "Kill switch"),
                 ("pause", "Paused"), ("held_count", "Kept back references"), ("audit", "Audit head"))
# System lines of a grammar 1 note: the raw counters the page translates (task result code, notes index age).
_RESULT_CODE = re.compile(r"\bresult (\d+)\b")
_RECENT_AGE = re.compile(r"\bRECENT\.md age ([\d.]+) h\b")
# Windows scheduled task result codes, decoded the way collectors/system.py does.
TASK_RESULTS = {0: "ok", 1: "script error", 267009: "still running", 267011: "never ran", 267014: "stopped by the user",
                2147750687: "an instance was already running", 2147943623: "cancelled",
                2147946720: "refused by the operator or administrator"}
ATTENTION_PILL = {"CI failing": "bad", "Job failed": "bad", "Daemon crashed": "bad", "Unclean exit": "warn",
                  "Task overdue": "bad", "Backlog held": "warn", "Breaker open": "bad", "Config invalid": "bad"}


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def clip(value: object, limit: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "..."


def fmt_ts(value: object) -> str:
    """A UTC stamp as local 'YYYY-MM-DD HH:MM'; anything unparseable is shown as it is."""
    if not value:
        return ""
    try:
        return parse_iso(str(value)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


def usd(value: object) -> str:
    try:
        return f"{float(value):.4f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""


def label(code: object) -> str:
    """The plain words for a code the daemon writes; the code itself when there is no translation."""
    text = "" if code is None else str(code)
    if text in LABELS:
        return LABELS[text]
    if text.startswith("term:") and text[5:].isdigit():
        return f"matched private rule {text[5:]}"
    if text.isdigit() and int(text) in TASK_RESULTS:
        return f"{TASK_RESULTS[int(text)]} (0x{int(text):08X})"
    return text


def pill(text: str, kind: str = "") -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


def table(headers: list[tuple[str, str]], rows: list[list[str]], row_class: str = "", cards: bool = False,
          row_ids: list[str] | None = None) -> str:
    """headers are (label, css class); cells are already-escaped HTML. `cards` adds the labels every cell needs to
    read as a card under 40rem (hub.css .cards). `row_ids` anchors each row (one per row, empty for none)."""
    head = "".join(f'<th class="{c}">{esc(h)}</th>' for h, c in headers)
    cls = f' class="{row_class}"' if row_class else ""
    body = "".join(
        f"<tr{cls}" + (f' id="{esc(row_ids[n])}"' if row_ids and row_ids[n] else "") + ">" + "".join(
            f'<td class="{headers[i][1]}"' + (f' data-label="{esc(headers[i][0])}"' if cards else "") + f">{cell}</td>"
            for i, cell in enumerate(r)) + "</tr>"
        for n, r in enumerate(rows))
    wrap = ' class="scroll cards"' if cards else ' class="scroll"'
    return f"<div{wrap}><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def repo_anchor(name: str) -> str:
    """The id of a repo's row on Projects, so an Attention row can point at it."""
    return "repo-" + re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-").lower()


def strip_html(strip: dict[str, Any]) -> str:
    """The status strip: the health word in bold (the live region), then the full text, and the short text the
    phone stylesheet shows instead. face.js rebuilds the same shape on every poll."""
    health = str(strip.get("health", ""))
    text = str(strip.get("text", ""))
    rest = text[len(health):].strip() if text.startswith(health) else text
    short = str(strip.get("short", rest))
    return (f'<p class="strip" id="strip" role="status"><b aria-live="polite">{esc(health)}</b> '
            f'<span class="long">{esc(rest)}</span><span class="short">{esc(short)}</span></p>')


def when_due(value: object, now: datetime | None) -> str:
    """`Today 18:00`, `Tomorrow 06:30`, or the date and time, from an ISO stamp; `unknown` without one."""
    if not value:
        return "unknown"
    try:
        due = parse_iso(str(value)).astimezone(now.tzinfo if now else None)
    except ValueError:
        return str(value)
    if now is not None:
        if due.date() == now.date():
            return f"Today {due.strftime('%H:%M')}"
        if due.date() == now.date() + timedelta(days=1):
            return f"Tomorrow {due.strftime('%H:%M')}"
    return due.strftime("%Y-%m-%d %H:%M")


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def system_line_text(line: str) -> str:
    """A grammar 1 System line with its raw counters translated: the task result code and the notes index age."""
    out = _RESULT_CODE.sub(lambda m: "result: " + label(m.group(1)), line)
    return _RECENT_AGE.sub(lambda m: f"notes index updated {m.group(1)} h ago", out)


def page(title: str, active: str, content: str, *, flags: dict[str, Any], refresh_s: int,
         face: bool = False, strip: dict[str, Any] | None = None) -> str:
    """`face` adds the avatar companion and its two module scripts; the caller says whether the assets exist."""
    from jarvisd.hub import face as face_mod  # L3 sibling; imported here to keep this module's head free of it

    nav = "".join(
        f'<a href="{href}"' + (' aria-current="page"' if label_ == active else "") + f">{label_}</a>"
        for href, label_ in NAV)
    banners = ""
    if flags.get("kill"):
        banners += ('<div class="banner bad" role="alert">state/KILL is present: the daemon stops and refuses '
                    "to start until a human removes it.</div>")
    if flags.get("pause"):
        reason = esc(flags["pause"].get("reason") or "no reason given")
        banners += f'<div class="banner warn" role="status">Paused: {reason}. Run <code>jarvis resume</code>.</div>'
    script = '<script src="/static/hub.js"></script>' if refresh_s else ""
    companion = face_mod.COMPANION if face else ""
    face_attr = ' data-face="1"' if face else ""
    face_scripts = face_mod.SCRIPT_TAGS if face else ""
    # Without a strip (a page rendered before status is known) the health word still has to exist somewhere.
    strip_markup = strip_html(strip) if strip else strip_html({"health": "Stopped." if not flags.get("running") else "Running.",
                                                                "text": ""})
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{esc(title)} - JARVIS hub</title>"
        '<link rel="stylesheet" href="/static/hub.css"></head>'
        f'<body data-refresh="{int(refresh_s)}"{face_attr}>'
        '<header class="top"><div class="bar"><span class="brand">JARVIS hub</span>'
        f'<nav aria-label="Views">{nav}</nav>{strip_markup}</div></header>{companion}'
        f'<main id="main">{banners}{content}</main>'
        '<footer>Read-only cockpit: it reads state, queue, audit and the digest notes and never calls Claude. '
        'The one thing it writes is a decision you click in the Inbox. <span id="stamp"></span></footer>'
        f'<script src="/static/prefs.js"></script>{script}{face_scripts}</body></html>\n')


def counts_text(counts: dict[str, Any]) -> str:
    if not counts:
        return ""
    held = int(counts.get("held_sensitive", 0)) + int(counts.get("held_policy", 0))
    return f"{counts.get('collected', 0)} collected, {counts.get('cleared', 0)} summarised, {held} held"


def held_table(refs: list[dict[str, Any]], *, commands: bool = False) -> str:
    rows = []
    for r in refs:
        # The translation when there is one; the raw code only when the label map has nothing to say.
        reason = esc(label(r["reason"])) if label(r["reason"]) != r["reason"] else f"<code>{esc(r['reason'])}</code>"
        row = [f"<code>{esc(r['id'])}</code>", esc(r["kind"]), reason, esc(fmt_ts(r.get("first_seen"))),
               esc(fmt_ts(r["last_seen"])), esc(fmt_ts(r["expires_at"]))]
        if commands:
            row.append(f"<code>jarvis wrong {esc(r['id'])} --should hold</code>")
        rows.append(row)
    headers = [("Id", ""), ("Kind", ""), ("Reason", ""), ("First seen", ""), ("Last seen", ""), ("Expires", "")]
    if commands:
        headers.append(("If the gate was wrong", ""))
    return table(headers, rows)


def _csrf(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{esc(token)}">'


def _safe_href(url: str) -> str | None:
    """Only http(s) tracker links become anchors; anything else (javascript:, data:) stays plain text."""
    return url if url.lower().startswith(("https://", "http://")) else None


def _li(text: str, item_id: str | None = None, cls: str = "") -> str:
    attrs = (f' data-id="{esc(item_id)}"' if item_id else "") + (f' class="{cls}"' if cls else "")
    return f"<li{attrs}>{text}</li>"


def _raw_lines(lines: list[str]) -> str:
    """Lines the parser did not recognise, printed as they are (the tolerant fallback)."""
    return ("<ul class=\"raw\">" + "".join(f"<li>{esc(ln)}</li>" for ln in lines) + "</ul>") if lines else ""


# --- Today -----------------------------------------------------------------------------------------


def digest_article(note: dict[str, Any], *, drop_title: bool = False) -> str:
    meta = note["meta"]
    c = counts_of(meta)
    items = f"{c['collected']} collected, {c['cleared']} summarised, {c['held']} held" if meta.get("items") else ""
    shown = [("Date", meta.get("date")), ("Status", label(meta.get("status"))), ("Generated", fmt_ts(meta.get("generated_at"))),
             ("Claude", meta.get("claude")), ("Local model", label(meta.get("local_tier"))), ("Cost (USD)", meta.get("cost_usd")),
             ("Items", items), ("Audit record", meta.get("audit_seq"))]
    facts = "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in shown if v not in (None, ""))
    body = note["body"]
    if drop_title:
        lines = body.lstrip("\n").splitlines()
        if lines and lines[0].startswith("# "):
            body = "\n".join(lines[1:])
    return (f'<div class="card"><dl class="facts">{facts}</dl></div>'
            f'<article class="digest">{render_markdown(body)}</article>')


def _due_pill(due: object, today: Any) -> str:
    """`due in N days`, `due today` or `N days late` from an ISO date; nothing for an unusable one."""
    try:
        day = date.fromisoformat(str(due))
    except (TypeError, ValueError):
        return ""
    days = (day - today).days
    if days < 0:
        return pill(f"{-days} day{'' if days == -1 else 's'} late", "bad")
    if days == 0:
        return pill("due today", "warn")
    return pill(f"due in {days} day{'' if days == 1 else 's'}")


def _attention_text(a: dict[str, Any]) -> str:
    """The text after the pill; a CI row links its repo name to that repo's row on Projects."""
    text = str(a["text"])
    if a["kind"] != "CI failing" or not text:
        return esc(text)
    name = re.split(r"\s+on\s+|,|:", text, maxsplit=1)[0].strip()
    rest = text[len(name):] if name and text.startswith(name) else ""
    if not name:
        return esc(text)
    return f'<a class="tap" href="/projects#{esc(repo_anchor(name))}">{esc(name)}</a>{esc(rest)}'


def today(model: dict[str, Any], token: str, today_date: Any, flash: str = "") -> str:
    note, parsed = model["note"], model["parsed"]
    if note is None or parsed is None:
        return ('<h1>Today</h1>' + flash + '<p class="empty">No digest note found yet. The daemon writes one each morning; '
                "<code>jarvis run-digest --dry-run</code> shows what it would contain without writing.</p>"
                '<p class="end">That\'s all.</p>')
    counts = parsed["counts"]
    out = ["<h1>Today</h1>", flash,
           f'<p class="lede">Digest of {esc(parsed["date"] or note["job_id"])}: {counts["collected"]} collected, '
           f'{counts["cleared"]} summarised, {counts["held"]} held.</p>']
    # 1. Attention
    out.append("<h2>Attention</h2>")
    if parsed["attention"]:
        out.append('<ul class="rows">' + "".join(
            _li(pill(a["kind"], ATTENTION_PILL.get(a["kind"], "bad")) + " " + _attention_text(a), a["id"]) for a in parsed["attention"])
            + "</ul>")
    elif parsed["nothing_broken"]:
        out.append(f'<p class="calm">{pill("Nothing broken.", "ok")}</p>')
    else:
        out.append('<p class="empty">This note has no Attention section.</p>')
    out.append(_raw_lines(parsed["other"].get("attention", [])))
    # 2. Needs you
    out.append("<h2>Needs you</h2>")
    if parsed["headline"]:
        out.append(f'<p class="lede">{esc(parsed["headline"])}</p>')
    if parsed["start_here"]:
        out.append('<ol class="needs">' + "".join(_li(esc(s["text"]), s["id"]) for s in parsed["start_here"]) + "</ol>")
    elif "start_here" in parsed["missing"]:
        out.append('<p class="empty">This note has no Start here section.</p>')
    else:
        out.append('<p class="empty">Nothing to start with.</p>')
    out.append(_raw_lines(parsed["other"].get("start_here", [])))
    # 3. Waiting for you
    out.append("<h2>Waiting for you</h2>")
    n = model["waiting_count"]
    if n:
        out.append(f'<p><a class="tap" href="/inbox">{n} waiting</a> in the Inbox. The oldest first; Reject and Edit are on the Inbox.</p>')
        items = []
        for p in model["waiting"]:
            form = (f'<form method="post" action="/inbox/{esc(p["id"])}/confirm" class="inline">{_csrf(token)}'
                    '<input type="hidden" name="next" value="/"><button type="submit" class="primary">Confirm</button></form>')
            pills = (pill(p["project"]) if p["project"] else "") + _due_pill(p.get("due"), today_date)
            items.append(f'<li><span class="title">{esc(clip(p["title"], 120))}</span> {pills} {form}</li>')
        out.append('<ul class="waiting">' + "".join(items) + "</ul>")
    else:
        out.append('<p class="empty">Nothing is waiting for a decision.</p>')
    # 4. Changed since yesterday
    out.append("<h2>Changed since yesterday</h2>")
    delta = model["delta"]
    changes = [f"<li><b>{esc(name)}</b>: {esc(', '.join(bits))}</li>" for name, bits in delta["repos"].items()]
    changes += [f"<li>{esc(line)}</li>" for line in delta["lines"]]
    out.append(f'<ul class="rows">{"".join(changes)}</ul>' if changes else '<p class="empty">Nothing changed.</p>')
    # 5. Everything else
    out.append('<details id="else"><summary>Everything else</summary>')
    task = parsed["active_task"]
    out.append("<h3>Active task</h3>")
    if task:
        due = pill("overdue", "bad") if task["overdue"] else ""
        out.append(f'<p data-id="{esc(task["id"] or "")}"><code>{esc(task["task_id"])}</code> {esc(task["title"])}, '
                   f'status {esc(task["status"])}, {esc(task["due"])}. {due}</p>')
    else:
        out.append(f'<p class="empty">{esc(parsed["active_task_text"] or "No active task recorded.")}</p>')
    out.append(_raw_lines(parsed["other"].get("active_task", [])))
    out.append("<h3>Still open</h3>")
    groups = still_open_groups(parsed)
    if groups:
        for name, rows in groups:
            out.append(f'<h4>{esc(name)}</h4><ul class="rows">' + "".join(
                _li(esc(r["text"]) + (f' <span class="age">{r["age"]} days old</span>' if r.get("age") else ""), r["id"])
                for r in rows) + "</ul>")
        if parsed["still_open_hidden"]:
            out.append(f'<p class="empty">{parsed["still_open_hidden"]} more open threads not shown (cap 10).</p>')
    else:
        out.append('<p class="empty">No open threads.</p>')
    out.append(_raw_lines(parsed["other"].get("still_open", [])))
    out.append("<h3>Decided yesterday</h3>")
    if parsed["decided"]:
        out.append('<ul class="rows">' + "".join(_li(esc(d["text"]), d["id"]) for d in parsed["decided"]) + "</ul>")
    else:
        out.append('<p class="empty">No decision recorded in the window.</p>')
    out.append(_raw_lines(parsed["other"].get("decided", [])))
    out.append("<h3>System</h3>")
    if parsed["all_green"]:
        out.append(f'<p class="calm">{pill("All green", "ok")} {esc(parsed["all_green"][len("All green: "):])}</p>')
    elif parsed["system"]:
        out.append('<ul class="rows">' + "".join(
            f"<li><b>{esc(s['what'])}</b>: {esc(s['detail'])}.</li>" for s in parsed["system"]) + "</ul>")
    elif not parsed["other"].get("system"):
        out.append('<p class="empty">No system lines.</p>')
    out.append(_raw_lines([system_line_text(ln) for ln in parsed["other"].get("system", [])]))
    held = parsed["held"]
    if held:
        out.append(f'<p class="lede">Kept back: {held["sens"]} sensitive (never read), {held["pol"]} work metadata, '
                   f'{held["cap"]} too long to send. Ids and reasons are on <a class="tap" href="/activity#held">Activity</a>.</p>')
    out.append("</details>")
    # 6. Full digest
    out.append('<details id="full"><summary>Full digest</summary>' + digest_article(note, drop_title=True) + "</details>")
    # 7. Item ids
    ids = [(s["id"], s["text"]) for s in parsed["start_here"]]
    out.append('<details id="ids"><summary>Item ids</summary>')
    if ids:
        out.append('<p class="lede">To flag a ranking mistake, in a terminal:</p><ul>' + "".join(
            f"<li><code>jarvis wrong {esc(i)}</code> {esc(clip(t, 80))}</li>" for i, t in ids) + "</ul>")
    else:
        out.append('<p class="empty">No ranked items in this note.</p>')
    out.append("</details>")
    # 8. The end
    out.append('<p class="end">That\'s all.</p>')
    return "".join(out)


def digest_page(note: dict[str, Any]) -> str:
    return f"<h1>Digest <code>{esc(note['job_id'])}</code></h1>" + digest_article(note, drop_title=True)


# --- Projects ---------------------------------------------------------------------------------------


def _repo_name(r: dict[str, Any]) -> str:
    return (esc(r["name"]) + (f" {pill('work')}" if r["work"] else "") + (f" {pill('stale', 'warn')}" if r["stale"] else "")
            + (f" {pill('always dirty')}" if r.get("always_dirty") else ""))


def _risk_pill(risk: str) -> str:
    """Red for a real break (CI failing, overdue task); amber for housekeeping (uncommitted work, stale)."""
    return pill(risk, "bad" if risk.startswith(("CI failing", "active task overdue")) else "warn")


def _github_cell(r: dict[str, Any]) -> str:
    """Open PRs and the CI state in words; the CI pill is dropped when the same fact is already a risk pill."""
    prs = f"{r['prs']} open PR{'' if r['prs'] == 1 else 's'}" if r["prs"] else ""
    ci = ""
    if r["ci"] and not (r["ci"] in _FAILING and "CI failing" in r["risks"]):
        ci = pill(CI_LABELS.get(r["ci"], r["ci"]), "bad" if r["ci"] in _FAILING else ("ok" if r["ci"] == "success" else ""))
    return " ".join(x for x in (esc(prs), ci) if x)


_FAILING = ("failure", "timed_out", "startup_failure", "action_required")


def projects(model: dict[str, Any]) -> str:
    stale_days = int(model["stale_days"])
    head = ('<h1>Projects</h1><p class="lede">Active repositories first, risks are fixed rules.</p>'
            '<details class="help" id="projects-help"><summary>How this works</summary>'
            "<p>Rows come from the latest digest notes and the proposals folder. Nothing is run live and no model is "
            f"asked. The risks: stale after {stale_days} days without a commit or uncommitted change in a digest, "
            "uncommitted work for 2 days or more unless the repo is listed as always dirty, failing CI, overdue active "
            "task. A quiet repo is one that moved nothing and trips no rule; an idle count with a plus sign means no "
            "activity at all in the digests on file, so the real idle time is at least that long.</p></details>")
    rows = model["rows"]
    out = [head]
    if not rows:
        out.append('<p class="empty">No repositories configured under [digest].repos.</p>')
    else:
        out.append("<h2>Active</h2>")
    cells = []
    ids = []
    for r in model["active"]:
        extra = f"<br>{esc(clip(r['task'], 160))}" if r["task"] else ""
        risks = " ".join(_risk_pill(x) for x in r["risks"]) or '<span class="empty">none</span>'
        since = esc(", ".join(r["delta"])) if r["delta"] else '<span class="empty">no change</span>'
        counts = f"{r['commits']} commits, {r['modified']}/{r['untracked']} uncommitted"
        # A zero is an empty cell: the phone card list hides empty cells, and a column of zeros says nothing.
        cells.append([_repo_name(r), esc(r["branch"]), since, esc(counts), _github_cell(r), risks + extra,
                      esc(r["open_proposals"]) if r["open_proposals"] else ""])
        ids.append(repo_anchor(r["name"]))
    if cells:
        out.append(table([("Repo", ""), ("Branch", ""), ("Since yesterday", ""), ("Now", ""), ("GitHub", ""),
                          ("Risks and active task", ""), ("Open proposals", "num")], cells, cards=True, row_ids=ids))
    elif rows:
        out.append('<p class="empty">No active repository: nothing moved, nothing is flagged.</p>')
    quiet = model["quiet"]
    if rows:
        names = []
        for r in quiet:
            idle = "" if r["days_since"] is None else (f" <span class=\"age\">idle {r['days_since']}{'+' if r.get('idle_floor') else ''} days</span>")
            names.append(f"<li>{_repo_name(r)}{idle}" + ("" if r["known"] else ' <span class="empty">no digest data</span>') + "</li>")
        out.append(f'<p class="quiet">Quiet: {plural(len(quiet), "repo")}.</p>')
        if quiet:
            out.append(f'<details id="quiet"><summary>The {plural(len(quiet), "quiet repo")}</summary>'
                       f'<ul class="rows">{"".join(names)}</ul></details>')
    raw = model["raw"]
    out.append('<details id="raw-repos"><summary>Repos as collected</summary>')
    if raw["digest"] is None:
        out.append('<p class="empty">No digest note found yet, so there are no repo facts to show.</p>')
    else:
        out.append(f'<p class="lede">Facts as collected for digest <code>{esc(raw["digest"]["job_id"])}</code>'
                   f' (generated {esc(fmt_ts(raw["digest"]["generated_at"]))}). The hub does not run git itself.</p>')
        if raw["rows"]:
            rows_ = [[esc(r["name"]) + (f" {pill(r['tag'])}" if r["tag"] else ""), esc(r["branch"]),
                      esc(r["commits"]), esc(r["modified"]), esc(r["untracked"]), esc(clip(r["note"], 200))] for r in raw["rows"]]
            out.append(table([("Repo", ""), ("Branch", ""), ("Commits", "num"), ("Modified", "num"),
                              ("Untracked", "num"), ("Note", "")], rows_))
        else:
            out.append('<p class="empty">No repo lines in that digest.</p>')
        if raw["other"]:
            out.append("<h3>Other lines</h3>" + _raw_lines(raw["other"]))
    out.append("</details>")
    return "".join(out)


# --- Activity ----------------------------------------------------------------------------------------


def _runs_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">No digest runs recorded yet.</p>'
    cells = []
    for r in rows:
        w = r["witness"]
        if w["seq"] is None:
            witness = '<span class="empty">none recorded</span>'
        elif w["found"]:
            witness = f"Audit record {w['seq']} <code>{esc(w['hash'])}</code>"
        else:
            witness = f"Audit record {w['seq']}, not found in the audit log"
        link = (f'<a class="tap" href="/digest/{esc(r["job_id"])}">note</a>' if r["has_note"] else '<span class="empty">no note</span>')
        kind = "ok" if r["status"] in ("complete", "done", "written") else ("bad" if r["status"] in ("failed", "error") else "warn")
        cells.append([esc(r["date"]), f"<code>{esc(r['job_id'])}</code>", pill(label(r["status"]), kind),
                      esc(counts_text(r["counts"])), esc(usd(r["cost_usd"])), witness, link])
    return table([("Date", ""), ("Job", ""), ("Status", ""), ("Items", ""), ("USD", "num"), ("Audit witness", ""), ("Note", "")],
                 cells)


def _ledger_tables(data: dict[str, Any]) -> str:
    entries = data["entries"]
    if not entries:
        return '<p class="empty">Nothing delivered yet.</p>'
    kinds = {"proposal": "Proposal", "digest": "Digest", "consolidation": "Memory candidates",
             "proposal_run": "Proposals run"}
    rows = []
    for e in entries:
        links = []
        for url in e["links"]:
            href = _safe_href(url)
            links.append(f'<a class="tap" href="{esc(href)}" rel="noreferrer">{esc(clip(url, 60))}</a>' if href else esc(clip(url, 60)))
        for ev in e["evidence"]:
            links.append(f'<a class="tap" href="{esc(ev)}">note</a>' if ev.startswith("/digest/") else
                         (f"<code>{esc(ev)}</code>" if e["kind"] == "consolidation" else f"<code>{esc(clip(ev, 24))}</code>"))
        title = esc(clip(e["title"], 100)) + (f" {pill(e['project'])}" if e["project"] else "")
        rows.append([esc(fmt_ts(e["stamp"])), esc(kinds[e["kind"]]), title, esc(usd(e["cost"]) if e["cost"] is not None else ""),
                     " ".join(links)])
    roll = [[esc(r["month"]), esc(r["count"]), esc(r["proposals"]), esc(r["digests"]), esc(r["notes"]),
             esc(r["runs"]), esc(usd(r["cost"]))] for r in data["rollup"]]
    return ("<h3>Per month</h3>" + table([("Month", ""), ("Delivered", "num"), ("Proposals", "num"), ("Digests", "num"),
                                          ("Memory notes", "num"), ("Proposal runs", "num"), ("USD", "num")], roll)
            + "<h3>Delivered</h3>" + table([("When", ""), ("Kind", ""), ("What", ""), ("USD", "num"), ("Evidence", "")], rows))


def _detail(details: dict[str, Any]) -> str:
    parts = []
    for key, value in details.items():
        shown = value if isinstance(value, (str, int, float, bool)) or value is None else json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}={clip(shown, 80)}")
    return clip(", ".join(parts), 300)


def _held_block(refs: list[dict[str, Any]]) -> str:
    out = ['<p class="lede">References to things JARVIS did not read or did not send: the id, the kind and the reason, '
           "never the content and never the source path.</p>"]
    out.append(held_table(refs, commands=True) if refs else '<p class="empty">No held references.</p>')
    out.append(
        "<h3>What to run, in a terminal</h3>"
        "<ul>"
        "<li><code>jarvis held</code> or <code>jarvis held &lt;id&gt;</code> resolves an id to its source. "
        "That stays on the terminal on purpose.</li>"
        f"<li><code>jarvis wrong &lt;id&gt; --should {{escalate,hold,skip,other}} --note \"...\"</code> records a mistake "
        f"in a gate decision ({WRONG_CHOICES}).</li>"
        "<li><code>jarvis wrong &lt;id&gt; --leak</code> if something sensitive was shown or sent. It opens the breaker.</li>"
        "</ul>")
    return "".join(out)


def _tile(name: str, value: object, kind: str = "") -> str:
    return f'<div class="tile {kind}"><dt>{esc(name)}</dt><dd>{esc(value)}</dd></div>'


def _status_facts(status: dict[str, Any]) -> str:
    """The daemon status as a definition list in plain words (the label map), one entry per fact."""
    breaker = status.get("breaker") or {}
    audit = status.get("audit") or {}
    age = status.get("heartbeat_age_s")
    words = {
        "running": "running" if status.get("running") else "stopped",
        "heartbeat_age_s": "none yet" if age is None else f"{age} s ago",
        "version": status.get("version") or "",
        "claude_cli_version": status.get("claude_cli_version") or "not probed yet",
        "local_tier": label(status.get("local_tier")),
        "breaker": label(breaker.get("state")) + (f" ({breaker['reason']})" if breaker.get("reason") else ""),
        "watermark": fmt_ts(status.get("watermark")) or "none yet",
        "next_due": fmt_ts(status.get("next_due")) or "unknown",
        "kill": "stopped by the kill switch" if status.get("kill") else "off",
        "pause": "yes" if status.get("pause") else "no",
        "held_count": status.get("held_count", 0),
        "audit": f"Audit record {audit.get('seq', 0)}, hash {str(audit.get('head', ''))[:12]}",
    }
    return '<dl class="facts">' + "".join(f"<div><dt>{esc(name)}</dt><dd>{esc(words[key])}</dd></div>"
                                          for key, name in STATUS_LABELS if words.get(key) not in ("", None)) + "</dl>"


def activity(model: dict[str, Any]) -> str:
    t = model["tiles"]
    snapshot = model["audit"]
    seq, head = snapshot.head
    out = ["<h1>Activity</h1>",
           '<p class="lede">What the daemon did in the last 7 days, then the records behind it. Everything here is read '
           "from state, queue, the audit log and the digest notes.</p>", "<h2>Last 7 days</h2>"]
    next_due = when_due(t["next_due"], model.get("now"))
    verified = "yes" if t["verified"] else ("no records" if t["verified"] is None else "BROKEN")
    out.append('<dl class="tiles">' + "".join([
        _tile("Digest runs", t["runs"]), _tile("Failed", t["failed"], "bad" if t["failed"] else ""),
        _tile("Claude, USD", f"{t['cost_usd']:.2f}"), _tile("Proposals made", t["proposed"]),
        _tile("Confirmed", t["confirmed"]), _tile("Rejected", t["rejected"]), _tile("Kept back", t["held"]),
        _tile("Flagged wrong", t["flagged"]), _tile("Chain verified", verified, "" if t["verified"] else "bad"),
        _tile("Next digest", next_due),
    ]) + "</dl>")
    if snapshot.files == 0:
        chain = pill("no audit records yet", "warn")
    elif snapshot.verified:
        chain = pill("Chain verified", "ok") + f" {plural(snapshot.files, 'file')}, head is audit record {seq}, hash <code>{esc(head[:12])}</code>."
    else:
        chain = (pill("Chain BROKEN", "bad") + f" at audit record {snapshot.broken_at}. Treat records from that one on as "
                 "untrusted; the incident runbook is in docs/v1-design.md, section 16.")
    budget = model["budget"]
    out.append(f'<div class="card"><p>{chain}</p>'
               f"<p>Budget today ({esc(budget['date'])}): {esc(usd(budget['spent_usd']))} of {esc(budget['daily_budget_usd'])} USD "
               f"spent, {esc(budget['calls'])} of {esc(budget['daily_calls'])} calls, {esc(usd(budget['reserved_usd']))} reserved. "
               f"The audit log sums {esc(usd(model['cost_today']))} USD of Claude calls today.</p></div>")
    out.append("<h2>Records</h2>")
    out.append(f'<details id="runs"><summary>Digest runs ({len(model["runs"])})</summary>'
               '<p class="lede">One row per digest job, newest first. The witness is the audit record the run pointed at when '
               f'it finished.</p>{_runs_table(model["runs"])}</details>')
    out.append(f'<details id="delivered"><summary>Delivered ({len(model["ledger"]["entries"])})</summary>'
               '<p class="lede">Confirmed proposals with their tracker link, digests written, memory candidates and proposals '
               f'runs, newest first. Cost is the run\'s recorded cost; a proposal has none.</p>{_ledger_tables(model["ledger"])}</details>')
    out.append(f'<details id="held"><summary>Kept back ({len(model["held"])})</summary>{_held_block(model["held"])}</details>')
    rows = model["audit_rows"]
    out.append(f'<details id="audit"><summary>Audit records, last {model["audit_limit"]}</summary>')
    if rows:
        cells = [[esc(r["seq"]), esc(fmt_ts(r["ts"])), f"<code>{esc(r['event'])}</code>", esc(_detail(r["details"])),
                  f"<code>{esc(r['hash'])}</code>"] for r in rows]
        out.append(table([("Record", "num"), ("Time", ""), ("Event", ""), ("Details", "detail"), ("Hash", "")], cells, "ev"))
    else:
        out.append('<p class="empty">The audit log is empty.</p>')
    out.append("</details>")
    status = model["status"]
    queue = "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in status["queue"].items())
    out.append('<details id="status"><summary>Daemon status</summary>'
               '<p class="lede">What <code>jarvis status</code> knows, in words. The daemon counts as running when '
               "its heartbeat is fresh and its process exists; the CLI asks the single-instance lock, which a viewer "
               "must not create.</p>"
               f'<div class="card">{_status_facts(status)}</div>'
               f'<h3>Queue</h3><div class="card"><dl class="facts">{queue}</dl></div>'
               '<details id="status-raw"><summary>Raw CLI output</summary>'
               f"<pre>{esc(chr(10).join(model['status_lines']))}</pre></details></details>")
    return "".join(out)


def not_found(code: int, message: str) -> str:
    return f"<h1>{int(code)}</h1><p>{esc(message)}</p><p><a href=\"/\">Back to Today</a></p>"
