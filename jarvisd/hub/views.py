"""HTML for the six views. Pure functions from data to a string; no I/O, no state.

Every value that did not come from this file goes through `esc`, including values read from
JARVIS's own files, so a note or a log line can never become markup. There is no inline
script, style attribute or external reference: the Content-Security-Policy in app.py allows
only same-origin files.

Layer L3 (hub). Imports hub.mdhtml only.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any

from jarvisd.common import parse_iso
from jarvisd.hub.mdhtml import render_markdown

NAV = (("/", "Today"), ("/inbox", "Inbox"), ("/runs", "Runs"), ("/held", "Held"), ("/repos", "Repos"), ("/projects", "Projects"),
       ("/ledger", "Ledger"), ("/audit", "Audit"), ("/status", "Status"))
WRONG_CHOICES = "escalate, hold, skip or other"


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


def pill(text: str, kind: str = "") -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


def table(headers: list[tuple[str, str]], rows: list[list[str]], row_class: str = "") -> str:
    """headers are (label, css class); cells are already-escaped HTML."""
    head = "".join(f'<th class="{c}">{esc(h)}</th>' for h, c in headers)
    cls = f' class="{row_class}"' if row_class else ""
    body = "".join(
        f"<tr{cls}>" + "".join(f'<td class="{headers[i][1]}">{cell}</td>' for i, cell in enumerate(r)) + "</tr>"
        for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def page(title: str, active: str, content: str, *, flags: dict[str, Any], refresh_s: int) -> str:
    nav = "".join(
        f'<a href="{href}"' + (' aria-current="page"' if label == active else "") + f">{label}</a>"
        for href, label in NAV)
    if flags.get("kill"):
        live = pill("kill file present", "bad")
    elif flags.get("running"):
        live = pill("daemon running", "ok")
    else:
        live = pill("daemon not running", "warn")
    banners = ""
    if flags.get("kill"):
        banners += ('<div class="banner bad" role="alert">state/KILL is present: the daemon stops and refuses '
                    "to start until a human removes it.</div>")
    if flags.get("pause"):
        reason = esc(flags["pause"].get("reason") or "no reason given")
        banners += f'<div class="banner warn" role="status">Paused: {reason}. Run <code>jarvis resume</code>.</div>'
    script = '<script src="/static/hub.js"></script>' if refresh_s else ""
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{esc(title)} - JARVIS hub</title>"
        '<link rel="stylesheet" href="/static/hub.css"></head>'
        f'<body data-refresh="{int(refresh_s)}">'
        '<header class="top"><div class="bar"><span class="brand">JARVIS hub</span>'
        f'<nav aria-label="Views">{nav}</nav>{live}</div></header>'
        f'<main id="main">{banners}{content}</main>'
        '<footer>Read-only cockpit: it reads state, queue, audit and the digest notes and never calls Claude. '
        'The one thing it writes is a decision you click in the Inbox. <span id="stamp"></span></footer>'
        f"{script}</body></html>\n")


def counts_text(counts: dict[str, Any]) -> str:
    if not counts:
        return ""
    held = int(counts.get("held_sensitive", 0)) + int(counts.get("held_policy", 0))
    return f"{counts.get('collected', 0)} collected, {counts.get('cleared', 0)} cleared, {held} held"


def held_table(refs: list[dict[str, Any]]) -> str:
    rows = [[f"<code>{esc(r['id'])}</code>", esc(r["kind"]), esc(r["reason"]), esc(fmt_ts(r["last_seen"])),
             esc(fmt_ts(r["expires_at"]))] for r in refs]
    return table([("Id", ""), ("Kind", ""), ("Reason", ""), ("Last seen", ""), ("Expires", "")], rows)


# --- Today -----------------------------------------------------------------------------------------


def digest_article(note: dict[str, Any]) -> str:
    meta = note["meta"]
    shown = [("Date", meta.get("date")), ("Status", meta.get("status")), ("Generated", fmt_ts(meta.get("generated_at"))),
             ("Claude", meta.get("claude")), ("Local tier", meta.get("local_tier")), ("Cost (USD)", meta.get("cost_usd")),
             ("Items", meta.get("items")), ("Audit seq", meta.get("audit_seq"))]
    facts = "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in shown if v not in (None, ""))
    return (f'<div class="card"><dl class="facts">{facts}</dl></div>'
            f'<article class="digest">{render_markdown(note["body"])}</article>')


def today(note: dict[str, Any] | None, held: list[dict[str, Any]], held_scope: str) -> str:
    if note is None:
        return ('<h1>Today</h1><p class="empty">No digest note found yet. The daemon writes one each morning; '
                "<code>jarvis run-digest --dry-run</code> shows what it would contain without writing.</p>")
    out = [f"<h1>Today</h1><p class=\"lede\">Latest digest: <code>{esc(note['job_id'])}</code>.</p>",
           digest_article(note), "<h2>Held items</h2>"]
    if held:
        out.append(f'<p class="lede">{esc(held_scope)} Ids and reasons only; see <a href="/held">Held</a> for what to run.</p>')
        out.append(held_table(held))
    else:
        out.append('<p class="empty">No held references.</p>')
    return "".join(out)


def digest_page(note: dict[str, Any]) -> str:
    return f"<h1>Digest <code>{esc(note['job_id'])}</code></h1>" + digest_article(note)


# --- Runs ------------------------------------------------------------------------------------------


def runs(rows: list[dict[str, Any]]) -> str:
    head = "<h1>Runs</h1><p class=\"lede\">One row per digest job, newest first. The witness is the audit record the run pointed at when it finished.</p>"
    if not rows:
        return head + '<p class="empty">No digest runs recorded yet.</p>'
    cells = []
    for r in rows:
        w = r["witness"]
        if w["seq"] is None:
            witness = '<span class="empty">none recorded</span>'
        elif w["found"]:
            witness = f"seq {w['seq']} <code>{esc(w['hash'])}</code>"
        else:
            witness = f"seq {w['seq']}, not found in the audit log"
        link = (f'<a href="/digest/{esc(r["job_id"])}">note</a>' if r["has_note"] else '<span class="empty">no note</span>')
        kind = "ok" if r["status"] in ("complete", "done") else ("bad" if r["status"] in ("failed", "error") else "warn")
        cells.append([esc(r["date"]), f"<code>{esc(r['job_id'])}</code>", pill(r["status"], kind),
                      esc(counts_text(r["counts"])), esc(usd(r["cost_usd"])), witness, link])
    return head + table([("Date", ""), ("Job", ""), ("Status", ""), ("Items", ""), ("USD", "num"),
                         ("Audit witness", ""), ("Note", "")], cells)


# --- Held ------------------------------------------------------------------------------------------


def held(refs: list[dict[str, Any]]) -> str:
    out = ["<h1>Held</h1>",
           '<p class="lede">References to things JARVIS did not read or did not send. The hub shows the id, the kind '
           "and the reason code, never the content and never the source path.</p>"]
    if refs:
        rows = [[f"<code>{esc(r['id'])}</code>", esc(r["kind"]), esc(r["reason"]), esc(fmt_ts(r["first_seen"])),
                 esc(fmt_ts(r["last_seen"])), esc(fmt_ts(r["expires_at"])),
                 f"<code>jarvis wrong {esc(r['id'])} --should hold</code>"] for r in refs]
        out.append(table([("Id", ""), ("Kind", ""), ("Reason", ""), ("First seen", ""), ("Last seen", ""),
                          ("Expires", ""), ("If the gate was wrong", "")], rows))
    else:
        out.append('<p class="empty">No held references.</p>')
    out.append(
        "<h2>What to run, in a terminal</h2>"
        "<ul>"
        "<li><code>jarvis held</code> or <code>jarvis held &lt;id&gt;</code> resolves an id to its source. "
        "That stays on the terminal on purpose.</li>"
        f"<li><code>jarvis wrong &lt;id&gt; --should {{escalate,hold,skip,other}} --note \"...\"</code> records a mistake "
        f"in a gate decision ({WRONG_CHOICES}).</li>"
        "<li><code>jarvis wrong &lt;id&gt; --leak</code> if something sensitive was shown or sent. It opens the breaker.</li>"
        "</ul>")
    return "".join(out)


# --- Repos -----------------------------------------------------------------------------------------


def repos(data: dict[str, Any]) -> str:
    digest = data["digest"]
    if digest is None:
        return '<h1>Repos</h1><p class="empty">No digest note found yet, so there are no repo facts to show.</p>'
    out = [f"<h1>Repos</h1><p class=\"lede\">Facts as collected for digest <code>{esc(digest['job_id'])}</code>"
           f" (generated {esc(fmt_ts(digest['generated_at']))}). The hub does not run git itself.</p>"]
    if data["rows"]:
        rows = [[esc(r["name"]) + (f" {pill(r['tag'])}" if r["tag"] else ""), esc(r["branch"]),
                 esc(r["commits"]), esc(r["modified"]), esc(r["untracked"]),
                 (f"<code>{esc(r['id'])}</code> " if r["id"] else "") + esc(clip(r["note"], 200))] for r in data["rows"]]
        out.append(table([("Repo", ""), ("Branch", ""), ("Commits", "num"), ("Modified", "num"),
                          ("Untracked", "num"), ("Id and note", "")], rows))
    else:
        out.append('<p class="empty">No repo lines in that digest.</p>')
    if data["other"]:
        out.append("<h2>Other lines</h2><ul>" + "".join(f"<li>{esc(line)}</li>" for line in data["other"]) + "</ul>")
    return "".join(out)


# --- Projects and Ledger -----------------------------------------------------------------------------


def _safe_href(url: str) -> str | None:
    """Only http(s) tracker links become anchors; anything else (javascript:, data:) stays plain text."""
    return url if url.lower().startswith(("https://", "http://")) else None


def projects(rows: list[dict[str, Any]], stale_days: int) -> str:
    head = ('<h1>Projects</h1><p class="lede">One row per configured repo, from the latest digest notes and the '
            "proposals folder. Nothing is run live and no model is asked: the risks are fixed rules "
            f"(stale after {int(stale_days)} days without a commit or uncommitted change in a digest, uncommitted work "
            "for 2 days or more, failing CI, overdue active task). A number with a plus sign means no activity at all "
            "in the digests on file, so the real idle time is at least that long.</p>")
    if not rows:
        return head + '<p class="empty">No repositories configured under [digest].repos.</p>'
    cells = []
    for r in rows:
        name = esc(r["name"]) + (f" {pill('work')}" if r["work"] else "") + (f" {pill('stale', 'warn')}" if r["stale"] else "")
        if not r["known"]:
            cells.append([name, '<span class="empty">no digest data</span>', "", "", "", "", "", "",
                          esc(r["open_proposals"])])
            continue
        prs = "" if r["prs"] is None else esc(r["prs"])
        ci = pill(r["ci"], "bad" if r["ci"] in ("failure", "timed_out", "startup_failure", "action_required")
                  else ("ok" if r["ci"] == "success" else "")) if r["ci"] else ""
        days = "" if r["days_since"] is None else esc(f"{r['days_since']}+" if r.get("idle_floor") else r["days_since"])
        extra = f"<br>{esc(clip(r['task'], 160))}" if r["task"] else ""
        risks = "".join(pill(x, "bad") + " " for x in r["risks"]) or '<span class="empty">none</span>'
        cells.append([name, esc(r["branch"]), esc(r["commits"]), f"{r['modified']} / {r['untracked']}", prs, ci, days,
                      risks + extra, esc(r["open_proposals"])])
    return head + table([("Repo", ""), ("Branch", ""), ("Commits", "num"), ("Modified / untracked", "num"),
                         ("Open PRs", "num"), ("CI", ""), ("Days idle", "num"), ("Risks and active task", ""),
                         ("Open proposals", "num")], cells)


def ledger(data: dict[str, Any]) -> str:
    head = ('<h1>Ledger</h1><p class="lede">What was delivered, newest first: confirmed proposals with their tracker '
            "link, digests written, consolidation notes and proposals runs. Cost is the run's recorded cost; a "
            "proposal has none.</p>")
    entries = data["entries"]
    if not entries:
        return head + '<p class="empty">Nothing delivered yet.</p>'
    kinds = {"proposal": "Proposal", "digest": "Digest", "consolidation": "Consolidation",
             "proposal_run": "Proposals run"}
    rows = []
    for e in entries:
        links = []
        for url in e["links"]:
            href = _safe_href(url)
            links.append(f'<a href="{esc(href)}" rel="noreferrer">{esc(clip(url, 60))}</a>' if href else esc(clip(url, 60)))
        for ev in e["evidence"]:
            links.append(f'<a href="{esc(ev)}">note</a>' if ev.startswith("/digest/") else
                         (f"<code>{esc(ev)}</code>" if e["kind"] == "consolidation" else f"<code>{esc(clip(ev, 24))}</code>"))
        title = esc(clip(e["title"], 100)) + (f" {pill(e['project'])}" if e["project"] else "")
        rows.append([esc(fmt_ts(e["stamp"])), esc(kinds[e["kind"]]), title, esc(usd(e["cost"]) if e["cost"] is not None else ""),
                     " ".join(links)])
    roll = [[esc(r["month"]), esc(r["count"]), esc(r["proposals"]), esc(r["digests"]), esc(r["notes"]),
             esc(r["runs"]), esc(usd(r["cost"]))] for r in data["rollup"]]
    return (head + "<h2>Per month</h2>" + table([("Month", ""), ("Delivered", "num"), ("Proposals", "num"), ("Digests", "num"),
                                                 ("Notes", "num"), ("Proposal runs", "num"), ("USD", "num")], roll)
            + "<h2>Delivered</h2>" + table([("When", ""), ("Kind", ""), ("What", ""), ("USD", "num"),
                                            ("Evidence", "")], rows))


# --- Audit -----------------------------------------------------------------------------------------


def _detail(details: dict[str, Any]) -> str:
    parts = []
    for key, value in details.items():
        shown = value if isinstance(value, (str, int, float, bool)) or value is None else json.dumps(value, ensure_ascii=False)
        parts.append(f"{key}={clip(shown, 80)}")
    return clip(", ".join(parts), 300)


def audit(snapshot: Any, rows: list[dict[str, Any]], budget: dict[str, Any], cost_today: float, limit: int) -> str:
    seq, head = snapshot.head
    if snapshot.files == 0:
        chain = pill("no audit records yet", "warn")
    elif snapshot.verified:
        chain = pill("Chain verified", "ok") + f" {snapshot.files} file(s), head seq {seq}, hash <code>{esc(head[:12])}</code>."
    else:
        chain = (pill("Chain BROKEN", "bad") + f" at seq {snapshot.broken_at}. Treat records from that seq on as "
                 "untrusted; the incident runbook is in docs/v1-design.md, section 16.")
    out = ["<h1>Audit</h1>", f'<div class="card"><p>{chain}</p>',
           f"<p>Budget today ({esc(budget['date'])}): {esc(usd(budget['spent_usd']))} of {esc(budget['daily_budget_usd'])} USD "
           f"spent, {esc(budget['calls'])} of {esc(budget['daily_calls'])} calls, {esc(usd(budget['reserved_usd']))} reserved. "
           f"The audit log sums {esc(usd(cost_today))} USD of Claude calls today.</p></div>",
           f"<h2>Last {limit} events</h2>"]
    if rows:
        cells = [[esc(r["seq"]), esc(fmt_ts(r["ts"])), f"<code>{esc(r['event'])}</code>", esc(_detail(r["details"])),
                  f"<code>{esc(r['hash'])}</code>"] for r in rows]
        out.append(table([("Seq", "num"), ("Time", ""), ("Event", ""), ("Details", "detail"), ("Hash", "")],
                         cells, "ev"))
    else:
        out.append('<p class="empty">The audit log is empty.</p>')
    return "".join(out)


# --- Status ----------------------------------------------------------------------------------------


def status(lines: list[str], data: dict[str, Any]) -> str:
    q = data["queue"]
    queue = "".join(f"<div><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in q.items())
    return ("<h1>Status</h1>"
            '<p class="lede">The same lines <code>jarvis status</code> prints. The daemon counts as running when '
            "its heartbeat is fresh and its process exists; the CLI asks the single-instance lock, which a viewer "
            "must not create.</p>"
            f"<pre>{esc(chr(10).join(lines))}</pre>"
            f'<h2>Queue</h2><div class="card"><dl class="facts">{queue}</dl></div>')


def not_found(code: int, message: str) -> str:
    return f"<h1>{int(code)}</h1><p>{esc(message)}</p><p><a href=\"/\">Back to Today</a></p>"
