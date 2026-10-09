"""The digest note, parsed into the blocks the Today, Projects and Inbox views show.

Pure functions over the note's text: no I/O, no clock, no state. The grammar is the one in
docs/hub-rework-contract.md section 1 (grammar 2); a note without a `grammar` front matter key is
read with the grammar 1 patterns (today's notes). Every regex here is the contract's, so the hub
and the digest writer agree on exactly these lines and nothing else. A line that matches no
pattern is kept as text under `other`, and the views print it as it is, so a wording change in the
writer degrades to raw lines instead of an empty page.

Ids: every item line ends in one ` [xxxxxxxx]` tail. `strip_id` returns the visible text and the
id, so a view can keep the id in a `data-id` attribute and never print it.

Layer L3 (hub). Imports jarvisd.render for the shared constants and hub.mdhtml for `section`.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from jarvisd import render as _render
from jarvisd.common import noise_line, norm_key
from jarvisd.hub.mdhtml import section

# Shared with the digest writer. Read through getattr so the hub keeps rendering while the writer
# side of the contract lands; once jarvisd/render.py exports them these fall through to its values.
GRAMMAR: int = int(getattr(_render, "GRAMMAR", 2))
ID_TAIL: re.Pattern[str] = getattr(_render, "ID_TAIL", re.compile(r" \[(?P<id>[0-9a-f]{8})\]$"))
HEADINGS: dict[str, str] = dict(getattr(_render, "HEADINGS", {
    "start_here": "Start here", "attention": "Attention", "active_task": "Active task",
    "still_open": "Still open", "decided": "Decided yesterday", "repos": "Repos",
    "system": "System", "held": "Held back and not summarized",
    "source_status": "Source status", "flag_mistake": "Flag a mistake"}))
# Grammar 1 headings for the blocks that were renamed.
LEGACY_HEADINGS = {"still_open": "Brain: open threads and decisions", "system": "What JARVIS did while you slept"}

ATTENTION_KINDS = ("CI failing", "Job failed", "Daemon crashed", "Unclean exit", "Task overdue", "Backlog held",
                   "Breaker open", "Config invalid")
_ANY_ID = re.compile(r"\s*\[[0-9a-f]{8}\]")
_START = re.compile(r"^(?P<n>[1-5])\. (?P<text>[A-Z][^\[]{9,158}) \[(?P<id>[0-9a-f]{8})\]$")
_START_V1 = re.compile(r"^(?P<n>\d+)\. \[(?P<id>[0-9a-f]{8})\] (?P<text>.+)$")
_ATTENTION = re.compile(r"^- (?P<kind>" + "|".join(re.escape(k) for k in ATTENTION_KINDS)
                        + r"): (?P<text>[^\[]+?)(?: \[(?P<id>[0-9a-f]{8})\])?$")
_ACTIVE_TASK = re.compile(
    r"^- (?P<task_id>\S+) (?P<title>.+?), status (?P<status>[^,]+), "
    r"(?P<due>due \d{4}-\d{2}-\d{2}(?: \((?:OVERDUE|TODAY)\))?|no due date)\."
    r"(?: No ClickUp call was made \(v1\)\.)?(?: \[(?P<id>[0-9a-f]{8})\])?$")
_STILL_OPEN = re.compile(r"^- (?P<group>[^:\[\]]{1,60}): (?P<text>[^\[]+?)(?: \((?P<age>\d+)d\))? \[(?P<id>[0-9a-f]{8})\]$")
_HIDDEN = re.compile(r"^- (?P<hidden>\d+) more open threads? not shown \(cap 10\)\.$")
_DECIDED = re.compile(r"^- (?P<text>[^\[]{3,200}) \[(?P<id>[0-9a-f]{8})\]$")
_THREAD_V1 = re.compile(r"^- \[(?P<date>\d{4}-\d{2}-\d{2})\](?P<stale> \(stale\))? (?P<text>.+?) \[(?P<id>[0-9a-f]{8})\](?P<gloss>.*)$")
_DECISION_V1 = re.compile(r"^- Decision \[(?P<date>\d{4}-\d{2}-\d{2})\] (?P<text>.+?) \[(?P<id>[0-9a-f]{8})\](?P<gloss>.*)$")
_SESSION_V1 = re.compile(r"^- Session (?P<session>\S+), (?P<label>entry point|open thread): (?P<text>.+?) \[(?P<id>[0-9a-f]{8})\](?P<gloss>.*)$")
_SESSION_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}-?")
RATIONALE = re.compile(r"\s*[,;:]?\s*\b(because|parce que|car|rationale)\b.*$", re.IGNORECASE)
# The Still open exclusions the writer applies in grammar 2; the grammar 1 fallback applies them here instead,
# so a legacy note never prints a resolved or cosmetic thread, or the same thread from two sources.
RESOLVED: re.Pattern[str] = getattr(_render, "RESOLVED", re.compile(
    r"^(done|delivered|shipped|resolved|merged|closed)\b|\b(is|was|now) (done|delivered|shipped|resolved|merged|closed)\b"
    r"|\b(done|delivered|shipped|resolved)$"))
COSMETIC: re.Pattern[str] = getattr(_render, "COSMETIC", re.compile(r"\b(cosmetic|optional|nice to have|low priority)\b"))
_SYSTEM_GREEN = re.compile(r"^- All green: (?P<jobs>\d+) jobs? done, 0 failed, \$(?P<usd>\d+\.\d{2}) Claude, "
                           r"breaker closed, disk ok, tasks ok\.$")
_SYSTEM_LINE = re.compile(r"^- (?P<what>[A-Z][^:]{2,40}): (?P<detail>.+)\.$")
_HELD = re.compile(r"^- Held: (?P<sens>\d+) sensitive(?: \(ids (?P<ids>[^;]+); reasons: (?P<reasons>[^)]+)\))?, "
                   r"(?P<pol>\d+) policy, (?P<cap>\d+) over cap\. Claude: (?P<claude>[a-z_ ]+)\. "
                   r"Run `jarvis held` in a terminal\.$")
_HELD_V1 = re.compile(r"^- Sensitive, never read or sent: (?P<sens>\d+) items?(?: \(ids? (?P<ids>[^;)]+)(?:; reasons: (?P<reasons>[^)]+))?\))?")
_HELD_POLICY_V1 = re.compile(r"^- Policy[^:]*: (?P<pol>\d+) items?")
_ITEMS = re.compile(r"(\w+): (\d+)")

# Repos. `_REPO_LINE` is the writer's grammar; in grammar 2 nothing follows the id.
REPO_LINE = re.compile(
    r"^- (?P<name>\S+?)(?: \((?P<tag>[^)]+)\))?:? (?:branch (?P<branch>.+?), )?"
    r"(?P<commits>\d+) commits? since window, (?P<modified>\d+) modified, (?P<untracked>\d+) untracked"
    r"(?P<rest>.*)$")
_UNCOMMITTED = re.compile(r"^- Uncommitted only: (?P<list>(?:\S+ \d+/\d+)(?:, \S+ \d+/\d+)*)\.$")
_QUIET_N = re.compile(r"^- Quiet: (?P<n>\d+) repos?\.$")
_GH_QUIET_N = re.compile(r"^- GitHub quiet: (?P<n>\d+) repos?, CI green or none\.$")
_GH_NOT_READ = re.compile(r"^- GitHub not read: (?P<n>\d+) repos? \((?P<states>[a-z_]+ \d+(?:, [a-z_]+ \d+)*)\)\.$")
_GH_LINE = re.compile(r"^- GitHub (?P<name>\S+?)(?: \(work\))?: (?P<rest>.*)$")
_GH_QUIET = re.compile(r"(?P<name>[^\s,()]+) \((?P<bits>[^)]*)\)")
_PRS = re.compile(r"^(\d+) open PRs?\b")
_COMMA_OUTSIDE_PARENS = re.compile(r",\s*(?![^()]*\))")
FAILING_CI_TOKENS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})


def strip_id(line: str) -> tuple[str, str | None]:
    """(visible text, id) for one item line: the tail is removed, and any other `[id]` token too."""
    text = line.strip()
    m = ID_TAIL.search(text)
    item_id = m.group("id") if m else None
    if m:
        text = text[: m.start()]
    if item_id is None:
        inner = re.search(r"\[([0-9a-f]{8})\]", text)
        item_id = inner.group(1) if inner else None
    return " ".join(_ANY_ID.sub("", text).split()), item_id


def grammar_of(meta: dict[str, Any]) -> int:
    try:
        return int(str(meta.get("grammar", "1")).strip())
    except ValueError:
        return 1


def counts_of(meta: dict[str, Any]) -> dict[str, int]:
    """collected, cleared, held from the scalar keys when present, else from the `items` dict."""
    items = {k: int(v) for k, v in _ITEMS.findall(str(meta.get("items", "")))}
    out = {"collected": items.get("collected", 0), "cleared": items.get("cleared", 0),
           "held": items.get("held_sensitive", 0) + items.get("held_policy", 0)}
    for key, name in (("n_collected", "collected"), ("n_cleared", "cleared"), ("n_held", "held")):
        try:
            out[name] = int(str(meta[key]).strip())
        except (KeyError, ValueError):
            pass
    return out


def _lines(body: str, heading: str) -> tuple[list[str], bool]:
    """Non-empty lines of the section, and whether its heading exists at all."""
    lines = section(body, heading)
    if not lines:
        return [], _has_heading(body, heading)
    return [ln.strip() for ln in lines if ln.strip()], True


def _has_heading(body: str, heading: str) -> bool:
    want = heading.strip().lower()
    for line in body.splitlines():
        m = re.match(r"^## +(.*\S)\s*$", line)
        if m and m.group(1).lower() == want:
            return True
    return False


def _group_of(session: str) -> str:
    slug = _SESSION_PREFIX.sub("", session.strip())
    return slug.replace("-", " ").strip() or "notes"


def _ci_token(parts: list[str]) -> str:
    for part in parts:
        part = part.strip()
        if part == "no CI runs":
            return "none"
        if part.startswith("CI "):
            return re.sub(r"[^a-z0-9_]+", "_", part[3:].split(" on ")[0].lower()).strip("_") or "unknown"
    return ""


def parse_repo_lines(lines: list[str]) -> dict[str, Any]:
    """{git: facts by repo, github: facts by repo, quiet_n, other}. Grammar 1 Quiet lines name the repos (zero
    counts); grammar 2 gives a count only, so a configured repo named nowhere is quiet with zero counts
    (`quiet_n` is set) and the caller fills it in. Nothing is invented for a repo in neither."""
    git: dict[str, dict[str, Any]] = {}
    gh: dict[str, dict[str, Any]] = {}
    other: list[str] = []
    quiet_n: int | None = None
    gh_quiet_n: int | None = None
    for raw in lines:
        text = raw.strip()
        if not text:
            continue
        if (m := _QUIET_N.match(text)):
            quiet_n = int(m.group("n"))
        elif text.startswith("- Quiet:"):
            for name in text[len("- Quiet:"):].rstrip(". ").split(","):
                if name.strip():
                    git.setdefault(name.strip(), {"branch": "", "commits": 0, "modified": 0, "untracked": 0})
        elif (m := _UNCOMMITTED.match(text)):
            for entry in m.group("list").split(", "):
                name, _, counts = entry.rpartition(" ")
                mod, _, unt = counts.partition("/")
                git[name] = {"branch": "", "commits": 0, "modified": int(mod), "untracked": int(unt)}
        elif (m := _GH_QUIET_N.match(text)):
            gh_quiet_n = int(m.group("n"))
        elif text.startswith("- GitHub quiet:"):
            for m in _GH_QUIET.finditer(text[len("- GitHub quiet:"):]):
                gh[m.group("name")] = {"prs": 0, "ci": _ci_token(m.group("bits").split(","))}
        elif _GH_NOT_READ.match(text):
            other.append(text[2:])
        elif text.startswith("- GitHub "):
            m = _GH_LINE.match(_ANY_ID.sub("", text).strip())
            if m:
                parts = _COMMA_OUTSIDE_PARENS.split(m.group("rest").split(": ", 1)[0])
                prs = _PRS.match(parts[0].strip())
                gh[m.group("name")] = {"prs": int(prs.group(1)) if prs else 0, "ci": _ci_token(parts[1:])}
            else:
                other.append(text[2:])
        elif (m := REPO_LINE.match(text)):
            git[m.group("name")] = {"branch": m.group("branch") or "", "commits": int(m.group("commits")),
                                    "modified": int(m.group("modified")), "untracked": int(m.group("untracked"))}
        else:
            other.append(text[2:] if text.startswith("- ") else text)
    return {"git": git, "github": gh, "quiet_n": quiet_n, "github_quiet_n": gh_quiet_n, "other": other}


def repo_rows(lines: list[str]) -> list[dict[str, str]]:
    """The old Repos table: one row per `_REPO_LINE`, with the id and the trailing note separated."""
    rows: list[dict[str, str]] = []
    for line in lines:
        m = REPO_LINE.match(line.strip())
        if not m:
            continue
        rest = m.group("rest")
        ident = re.search(r"\[([0-9a-f]{8})\]", rest)
        note = _ANY_ID.sub("", rest).strip().lstrip(",:; ").strip()
        rows.append({"name": m.group("name"), "tag": m.group("tag") or "", "branch": m.group("branch") or "",
                     "commits": m.group("commits"), "modified": m.group("modified"),
                     "untracked": m.group("untracked"), "id": ident.group(1) if ident else "", "note": note})
    return rows


def _age(day: str, today: date | None) -> int | None:
    if today is None:
        return None
    try:
        return (today - date.fromisoformat(day)).days
    except ValueError:
        return None


def parse_note(body: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Every block of the note as data. Missing sections are empty lists with `missing` naming them."""
    grammar = grammar_of(meta)
    note_day: date | None = None
    try:
        note_day = date.fromisoformat(str(meta.get("date", "")).strip())
    except ValueError:
        pass
    other: dict[str, list[str]] = {}
    missing: list[str] = []

    def take(key: str, legacy: bool = False) -> list[str]:
        heading = LEGACY_HEADINGS.get(key, HEADINGS[key]) if legacy else HEADINGS[key]
        lines, present = _lines(body, heading)
        if not present and not legacy and key in LEGACY_HEADINGS:
            return take(key, legacy=True)
        if not present:
            missing.append(key)
        return lines

    # Start here: the headline, then the ranked lines.
    headline = ""
    start_here: list[dict[str, str]] = []
    rest: list[str] = []
    for line in take("start_here"):
        m = _START.match(line) or _START_V1.match(line)
        if m:
            start_here.append({"id": m.group("id"), "text": strip_id(m.group("text"))[0]})
        elif not headline and not re.match(r"^(?:[-*]|\d+\.) ", line):
            headline = line
        else:
            rest.append(line)
    other["start_here"] = rest

    # Attention: deterministic lines, or the one sentence.
    attention: list[dict[str, Any]] = []
    nothing_broken = False
    rest = []
    for line in take("attention"):
        if line == "- Nothing broken.":
            nothing_broken = True
            continue
        m = _ATTENTION.match(line)
        if m:
            attention.append({"kind": m.group("kind"), "text": m.group("text").strip(), "id": m.group("id")})
        else:
            rest.append(line)
    other["attention"] = rest

    # Active task: one parsed line or one fixed sentence.
    active_task: dict[str, Any] | None = None
    task_text = ""
    rest = []
    for line in take("active_task"):
        m = _ACTIVE_TASK.match(line)
        if m and active_task is None:
            active_task = {k: m.group(k) for k in ("task_id", "title", "status", "due", "id")}
            active_task["overdue"] = "(OVERDUE)" in m.group("due")
        elif not task_text:
            task_text = strip_id(line.lstrip("- "))[0]
        else:
            rest.append(line)
    other["active_task"] = rest

    # Still open and Decided yesterday. Grammar 1 reads the Brain section for both.
    still_open: list[dict[str, Any]] = []
    decided: list[dict[str, Any]] = []
    hidden = 0
    rest = []
    if grammar >= 2:
        for line in take("still_open"):
            if (m := _STILL_OPEN.match(line)):
                still_open.append({"group": m.group("group"), "text": m.group("text").strip(),
                                   "age": int(m.group("age")) if m.group("age") else None, "id": m.group("id")})
            elif (m := _HIDDEN.match(line)):
                hidden = int(m.group("hidden"))
            else:
                rest.append(line)
        other["still_open"] = rest
        rest = []
        for line in take("decided"):
            if (m := _DECIDED.match(line)):
                decided.append({"text": m.group("text").strip(), "id": m.group("id")})
            else:
                rest.append(line)
        other["decided"] = rest
    else:
        # Grammar 1 lists every thread twice (the RECENT.md bullet and the session line it came from) and screens
        # nothing. Collapse on `norm_key` (the bullet wins, the session line lends its group), and drop what the
        # writer's content rules exclude: already in Start here, resolved, cosmetic, reference-only, no active work.
        start_ids = {s["id"] for s in start_here}
        by_key: dict[str, dict[str, Any]] = {}
        for line in take("still_open", legacy=True):
            if (m := _DECISION_V1.match(line)):
                decided.append({"text": RATIONALE.sub("", m.group("text")).strip() or m.group("text").strip(),
                                "id": m.group("id")})
                continue
            thread = _THREAD_V1.match(line)
            sess = None if thread else _SESSION_V1.match(line)
            if not thread and not sess:
                rest.append(line)
                continue
            m = thread or sess
            assert m is not None
            text = m.group("text").strip()
            key = norm_key(text)
            if m.group("id") in start_ids or RESOLVED.search(key) or COSMETIC.search(key) or noise_line(text):
                continue
            if thread:
                age = _age(m.group("date"), note_day)
                row = {"group": "notes", "text": text, "age": age if (age is not None and age >= 3) else None, "id": m.group("id")}
                prev = by_key.get(key)
                if prev is None:
                    by_key[key] = row
                    still_open.append(row)
                elif prev.get("session"):
                    prev.update(text=row["text"], age=row["age"], id=row["id"])  # the bullet wins, the group stays
            else:
                group = _group_of(m.group("session"))
                prev = by_key.get(key)
                if prev is None:
                    row = {"group": group, "text": text, "age": None, "id": m.group("id"), "session": True}
                    by_key[key] = row
                    still_open.append(row)
                elif prev["group"] == "notes":
                    prev["group"] = group
        for row in still_open:
            row.pop("session", None)
        other["still_open"] = rest
        other["decided"] = []
        if "still_open" in missing:
            missing.append("decided")

    # Repos.
    repo_lines = take("repos")
    repos = parse_repo_lines(repo_lines)
    repos["rows"] = repo_rows(repo_lines)
    repos["lines"] = repo_lines
    ci_failing = [name for name, g in repos["github"].items() if g.get("ci") in FAILING_CI_TOKENS]

    # System: one green line, or one line per anomaly.
    system: list[dict[str, str]] = []
    all_green: str | None = None
    rest = []
    for line in take("system"):
        if (m := _SYSTEM_GREEN.match(line)):
            all_green = line[2:]
        elif grammar >= 2 and (m := _SYSTEM_LINE.match(line)):
            system.append({"what": m.group("what"), "detail": m.group("detail")})
        else:
            rest.append(line[2:] if line.startswith("- ") else line)
    other["system"] = rest

    # Held: one line in grammar 2, up to three in grammar 1.
    held: dict[str, Any] | None = None
    rest = []
    for line in take("held"):
        if (m := _HELD.match(line)):
            held = {"sens": int(m.group("sens")), "pol": int(m.group("pol")), "cap": int(m.group("cap")),
                    "claude": m.group("claude"), "ids": [i.strip() for i in (m.group("ids") or "").split(",") if i.strip()],
                    "reasons": m.group("reasons") or ""}
        elif (m := _HELD_V1.match(line)):
            held = held or {"sens": 0, "pol": 0, "cap": 0, "claude": "", "ids": [], "reasons": ""}
            held["sens"] = int(m.group("sens"))
            held["ids"] = [i.strip() for i in (m.group("ids") or "").split(",") if i.strip()]
            held["reasons"] = m.group("reasons") or ""
        elif (m := _HELD_POLICY_V1.match(line)):
            held = held or {"sens": 0, "pol": 0, "cap": 0, "claude": "", "ids": [], "reasons": ""}
            held["pol"] = int(m.group("pol"))
        else:
            rest.append(line[2:] if line.startswith("- ") else line)
    other["held"] = rest

    # Grammar 1 has no Attention section: derive the two facts the note does carry.
    if "attention" in missing and grammar < 2:
        if active_task and active_task["overdue"]:
            attention.append({"kind": "Task overdue", "text": f"{active_task['title']}, {active_task['due']}",
                              "id": active_task["id"]})
        for name in ci_failing:
            attention.append({"kind": "CI failing", "text": name, "id": None})
        nothing_broken = not attention

    return {
        "grammar": grammar, "date": meta.get("date", ""), "headline": headline, "start_here": start_here,
        "attention": attention, "nothing_broken": nothing_broken or (not attention and "attention" not in missing),
        "active_task": active_task, "active_task_text": task_text, "still_open": still_open,
        "still_open_hidden": hidden, "decided": decided, "repos": repos, "system": system, "all_green": all_green,
        "held": held, "counts": counts_of(meta), "other": other, "missing": missing,
    }


def still_open_groups(parsed: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Still open lines grouped by `group`, in first-seen order (the writer orders groups by newest item)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in parsed["still_open"]:
        groups.setdefault(row["group"], []).append(row)
    return list(groups.items())
