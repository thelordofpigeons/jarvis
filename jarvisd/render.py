"""Deterministic digest markdown (design section 9, grammar 2 in docs/hub-rework-contract.md).

Pure: no I/O, no clock, no randomness. `render_digest(ctx)` turns a `DigestContext` into
the note text. Every section is a small class in the `SECTIONS` registry, so a new section
is one class and one registry entry, with no edit to `render_digest`. The order comes from
`ctx.section_order` (the config table has no `sections` key yet, so the caller may pass
one) and otherwise from `DEFAULT_SECTIONS`. Registered sections missing from the order are
slotted in before the footer sections.

The grammar the hub parses is pinned here and nowhere else: `GRAMMAR`, `ID_TAIL` and
`HEADINGS` are imported by the hub, so a rename fails at import time, not in a browser.
Every item line ends in exactly one id tail ` [xxxxxxxx]` and nothing follows it; status
sentences, System, Held, Source status and Flag a mistake carry none. A line never glues a
Claude one-liner or a second language to the item text.

Rules enforced here, on every string, whatever its origin:
- `strip_dashes` (no U+2014 or U+2013), control characters and newlines collapse to one
  space, so a hostile line can never open a heading or a front matter block;
- `|` becomes `/`, so no table can form; square brackets inside item text become
  parentheses, so the id tail is the only bracket on an item line;
- the heading `## Open threads` is never emitted: it would be read as a session section by
  brain-nightly.py. A section class asking for it raises `RenderError`.

What the reader sees about held items: sensitive-held items are never printed, only counted
and listed by opaque id with a reason code. Policy-held items (work metadata) are printed
from collector data. Claude's attention ids are accepted only for items that were actually
cleared for Claude; anything else it cites is dropped.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from jarvisd import __version__
from jarvisd.common import STALE_AFTER_DAYS, noise_line, norm_key, strip_dashes
from jarvisd.models import CollectResult, DigestSummary, Item, WithheldItem

# --- the grammar the hub depends on ----------------------------------------------------

GRAMMAR = 2  # front matter key `grammar`; absent means 1 (the notes written before this)
ID_TAIL = re.compile(r" \[(?P<id>[0-9a-f]{8})\]$")
HEADINGS: dict[str, str] = {
    "start_here": "Start here",
    "attention": "Attention",
    "active_task": "Active task",
    "still_open": "Still open",
    "decided": "Decided yesterday",
    "repos": "Repos",
    "system": "System",
    "held": "Held back and not summarized",
    "source_status": "Source status",
    "flag_mistake": "Flag a mistake",
}
DEFAULT_SECTIONS: tuple[str, ...] = (
    "start_here",
    "attention",
    "active_task",
    "still_open",
    "decided",
    "repos",
    "system",
    "held",
    "source_status",
    "flag_mistake",
)
FOOTER_SECTIONS = ("source_status", "flag_mistake")
SOURCE_ORDER = ("brain", "task", "git", "github", "system")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_ATTENTION = 5
MAX_STILL_OPEN = 10
MAX_DECIDED = 10
MAX_HELD_IDS = 20
HELD_BACKLOG_ALERT = 20
ORPHAN_ALERT = 5
AGE_SHOWN_FROM_DAYS = 3
FORBIDDEN_HEADING = re.compile(r"^open threads\s*$", re.IGNORECASE)
GENESIS = "0" * 64
CI_FAILING = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
GITHUB_NOT_COLLECTED = "GitHub PRs and CI: not collected (the github collector did not run)."
GITHUB_DISABLED = "GitHub PRs and CI: collector disabled ([digest.github] enabled = false)."
# Fixed phrases per unreadable state, so gh's own error text never reaches the note.
GITHUB_STATES = (
    ("no_access", "no access (the active gh account cannot read the remote)"),
    ("no_remote", "no remote configured"),
    ("no_auth", "gh is not signed in (run gh auth login)"),
    ("ambiguous_remote", "several remotes (run gh repo set-default)"),
    ("gh_missing", "gh CLI not found on PATH"),
    ("timeout", "timed out"),
    ("unreadable", "could not be read"),
)

# Still open exclusions and the fallback ranking, matched on `norm_key` so case and punctuation
# do not matter (contract section 1.2).
RESOLVED = re.compile(
    r"^(done|delivered|shipped|resolved|merged|closed)\b"
    r"|\b(is|was|now) (done|delivered|shipped|resolved|merged|closed)\b"
    r"|\b(done|delivered|shipped|resolved)$"
)
COSMETIC = re.compile(r"\b(cosmetic|optional|nice to have|low priority)\b")
RATIONALE = re.compile(r"\s*[,;:]?\s*\b(because|parce que|car|rationale)\b.*$", re.IGNORECASE)
# A rationale after a dash ("Keep X <dash> the client asked") is cut before `clean` turns the dash into a comma.
# The dashes are written as escapes so this file never contains them.
DASH_RATIONALE = re.compile("\\s*[\u2014\u2013]\\s.*$|\\s+-\\s+.*$")
# "before" and "by" are not here on purpose: "before any client use" and "by hand" are not deadlines.
DEADLINE_WORDS = (
    "deadline", "due", "overdue", "today", "tomorrow", "blocked", "blocker", "blocks",
    "waiting on", "expires", "rotate", "urgent", "avant", "bloque", "echeance",
)
_DEADLINE_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in DEADLINE_WORDS) + r")\b")
# Openers that make a why a statement, not an order ("The key was pasted", "Pending answer"), and the auxiliaries
# that give a declarative away in second position ("Key was pasted").
NOT_IMPERATIVE = frozenset({"the", "a", "an", "this", "that", "these", "those", "there", "it", "its", "your", "you",
                            "no", "not", "both", "all", "still", "pending", "yesterday", "today", "tomorrow"})
_AUXILIARY = frozenset({"is", "was", "are", "were", "has", "have", "had", "remains", "needs"})
_SESSION_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}(?:-\d{2})?-?")


class RenderError(Exception):
    """A section tried to emit something the layout forbids. A programming error, not data."""


@dataclass
class DigestContext:
    """Everything the renderer needs, supplied by the digest pipeline.

    `held` merges collector-withheld references and gate-held items (hold_kind says which
    kind). Gate-held items stay in `results`; the renderer hides the sensitive ones.
    """

    job_id: str
    day: date
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    results: dict[str, CollectResult]
    status: str = "complete"  # complete | partial | degraded_no_llm
    late: bool = False
    claude_status: str = "ok"  # ok | disabled | no_items | a short reason token
    local_tier: str = "not_installed"
    degraded: bool = False
    cost_usd: float = 0.0
    summary: DigestSummary | None = None
    held: list[WithheldItem] = field(default_factory=list)
    over_cap: list[str] = field(default_factory=list)
    tier_violation_seq: int | None = None
    audit_seq: int = 0
    audit_head: str = GENESIS
    section_order: tuple[str, ...] | None = None
    generator_version: str = __version__


# --- string hygiene --------------------------------------------------------------------


def clean(value: object) -> str:
    """One line, no dashes, no pipes, no control characters. Applied to every printed string."""
    text = strip_dashes(str(value)).replace("﻿", "")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).replace("|", "/")
    return " ".join(text.split())


def clip(value: object, limit: int) -> str:
    text = clean(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "."


def item_text(value: object, limit: int) -> str:
    """`clip` for text that sits before an id tail: wikilinks lose their brackets and any other
    bracket becomes a parenthesis, so the tail is the only `[` on the line."""
    text = re.sub(r"\[\[([^\]]*)\]\]", r"\1", clean(value))
    return clip(text.replace("[", "(").replace("]", ")"), limit)


def sentence(value: object, limit: int) -> str:
    """`item_text` starting with a capital letter (the Start here and Attention grammars)."""
    text = item_text(value, limit)
    return text[:1].upper() + text[1:] if text else text


def tail(item_id: str) -> str:
    return f" [{clean(item_id)}]"


def token(value: object) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_") or "unknown"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# --- derived view ----------------------------------------------------------------------


def is_deterministic(item: Item) -> bool:
    return item.meta.get("render") == "deterministic"


def item_day(item: Item) -> str:
    """The item's date as YYYY-MM-DD: `meta.date` for brain items, else the day of `ts`."""
    day = str(item.meta.get("date") or "")
    if day:
        return day
    return str(item.ts or "")[:10]


class View:
    """Facts derived from a context once per section call (the context may be mutated between renders)."""

    def __init__(self, ctx: DigestContext) -> None:
        self.ctx = ctx
        self.items: list[Item] = [it for r in ctx.results.values() for it in r.items if not is_deterministic(it)]
        self.item_ids = {it.id for it in self.items}
        self.sensitive_ids = {w.id for w in ctx.held if w.hold_kind == "sensitive"}
        self.policy_ids = {w.id for w in ctx.held if w.hold_kind == "policy"}
        self.over_cap = set(ctx.over_cap)
        # Items Claude was allowed to see, hence the only ones it may comment on.
        self.cleared_ids = self.item_ids - self.sensitive_ids - self.policy_ids - self.over_cap

    def visible(self, source: str, kind: str | None = None) -> list[Item]:
        res = self.ctx.results.get(source)
        if res is None:
            return []
        return [
            it for it in res.items
            if it.id not in self.sensitive_ids and not is_deterministic(it) and (kind is None or it.kind == kind)
        ]

    def facts(self, source: str) -> dict[str, Any]:
        res = self.ctx.results.get(source)
        return res.facts if res is not None else {}

    def source_ok(self, source: str) -> bool:
        res = self.ctx.results.get(source)
        return res is not None and res.ok

    def counts(self) -> dict[str, int]:
        """The front matter `items` counts.

        collected = distinct collected items plus references that never became items;
        cleared = collected minus held and over-cap. Deterministic system lines are not items.
        """
        held_ids = {w.id for w in self.ctx.held}
        collected = len(self.item_ids) + len(held_ids - self.item_ids)
        sens = sum(1 for w in self.ctx.held if w.hold_kind == "sensitive")
        pol = sum(1 for w in self.ctx.held if w.hold_kind == "policy")
        cap = len(self.over_cap)
        return {
            "collected": collected,
            "cleared": max(0, collected - sens - pol - cap),
            "held_sensitive": sens,
            "held_policy": pol,
            "over_cap": cap,
        }


# --- shared plans (pure, recomputed by the sections and by the front matter) -----------


def _due_text(meta: dict[str, Any]) -> str:
    state, day = meta.get("due_state", "none"), meta.get("due_date", "")
    if state == "overdue":
        return f"due {day} (OVERDUE)"
    if state == "today":
        return f"due {day} (TODAY)"
    return f"due {day}" if state == "later" and day else "no due date"


def _threads(v: View) -> list[Item]:
    """Open threads and session lines, the raw material of Still open and the fallback."""
    return v.visible("brain", "brain_thread") + v.visible("brain", "brain_session")


def _fallback_attention(v: View) -> list[tuple[str, str]]:
    """Deterministic Start here picks, scored by action needed (contract section 1.2).

    4 an overdue or due-today task, 3 a thread whose key names a deadline or a blocker, 3 a
    repo with CI failing, 2 a repo with PRs awaiting review, 1 a repo with commits. Items that
    score 0 are left out: a fallback line must carry a consequence, and "newest thread" has
    none. Sort: score desc, date desc, id asc.
    """
    scored: list[tuple[int, str, str, str]] = []
    for it in v.visible("task", "active_task"):
        state = it.meta.get("due_state")
        if state == "overdue":
            scored.append((4, item_day(it), it.id, f"Finish {item_text(it.title, 100)}, overdue since {clean(it.meta.get('due_date', ''))}"))
        elif state == "today":
            scored.append((4, item_day(it), it.id, f"Finish {item_text(it.title, 100)}, it is due today"))
    for it in _threads(v):
        key = str(it.meta.get("key") or norm_key(it.text))
        # The same screen Still open applies: a resolved, cosmetic or reference line is never a Start here pick.
        if _DEADLINE_RE.search(key) and not _excluded(it, set()):
            scored.append((3, item_day(it), it.id, f"Act on this thread, it names a deadline or a blocker: {item_text(it.text, 100)}"))
    for it in v.visible("github", "github_repo"):
        branch = item_text(it.meta.get("ci_branch") or "the default branch", 80)
        if str(it.meta.get("ci", "")) in CI_FAILING:
            scored.append((3, item_day(it), it.id, f"Fix {_name(it.title)}: CI is failing on {branch}, merges are blocked"))
        elif _int(it.meta.get("review_requested")):
            n = _int(it.meta.get("review_requested"))
            scored.append((2, item_day(it), it.id, f"Review {plural(n, 'PR')} in {_name(it.title)}, they wait on you"))
    for it in v.visible("git", "git_repo"):
        n = _int(it.meta.get("commits"))
        if n:
            scored.append((1, item_day(it), it.id, f"Check {_name(it.title)}: {plural(n, 'commit')} since the window start, not yet digested"))
    scored.sort(key=lambda s: (-s[0], _desc(s[1]), s[2]))
    return [(item_id, sentence(why, 159)) for _, _, item_id, why in scored[:MAX_ATTENTION]]


def start_here_picks(ctx: DigestContext, v: View) -> list[tuple[str, str]]:
    """(id, why) for the numbered Start here lines: Claude's ranked picks, or the fallback."""
    picks: list[tuple[str, str]] = []
    if ctx.summary is not None:
        for a in ctx.summary.attention:
            why = sentence(a.why, 159)
            if a.id in v.cleared_ids and len(why) >= 10 and imperative(why):
                picks.append((a.id, why))
        picks = picks[:MAX_ATTENTION]
    return picks or _fallback_attention(v)


def imperative(text: str) -> bool:
    """True when a why reads as an order: the first word is not a determiner, pronoun or time word, and no
    auxiliary verb follows it ("Rotate the key before the demo", not "The key was pasted"). A lexicon-free
    check, so it rejects the common declarative shapes and lets the rest through."""
    words = [w.strip(",.:;").casefold() for w in text.split()]
    if not words:
        return False
    # An auxiliary in second or third position gives a subject away ("Key was pasted", "Pilot password is printed").
    return words[0] not in NOT_IMPERATIVE and not any(w in _AUXILIARY for w in words[1:3])


def attention_lines(ctx: DigestContext, v: View) -> list[str]:
    """The Attention anomalies, deterministic, in the fixed kind order. Empty means nothing broken."""
    out: list[str] = []
    for it in v.visible("github", "github_repo"):
        if str(it.meta.get("ci", "")) in CI_FAILING:
            where = f" on {clean(it.meta['ci_branch'])}" if it.meta.get("ci_branch") else ""
            out.append(f"- CI failing: {item_text(it.title, 80)}{where}{tail(it.id)}")
    sysf = v.facts("system") if v.source_ok("system") else {}
    if _int(sysf.get("jobs_failed")):
        out.append(f"- Job failed: {plural(_int(sysf['jobs_failed']), 'job')} failed since the last digest")
    if _int(sysf.get("daemon_crashes")):
        out.append(f"- Daemon crashed: {plural(_int(sysf['daemon_crashes']), 'crash')} recorded since the last digest")
    if _int(sysf.get("unclean_exits")):
        out.append(f"- Unclean exit: {plural(_int(sysf['unclean_exits']), 'unclean exit')} since the last digest")
    for it in v.visible("task", "active_task"):
        if it.meta.get("due_state") == "overdue":
            out.append(f"- Task overdue: {item_text(it.title, 100)} was due {clean(it.meta.get('due_date', ''))}{tail(it.id)}")
    held_count = _int((sysf.get("queue") or {}).get("held")) if isinstance(sysf.get("queue"), dict) else 0
    if held_count >= HELD_BACKLOG_ALERT:
        out.append(f"- Backlog held: {held_count} held references in the queue, review them with jarvis held")
    breaker = str(sysf.get("breaker_state") or "closed")
    if breaker != "closed":
        out.append(f"- Breaker open: Claude calls are paused ({clean(breaker)}), run jarvis breaker status")
    if _int(sysf.get("config_invalid")):
        out.append(f"- Config invalid: {plural(_int(sysf['config_invalid']), 'config error')} audited, the daemon idles until it parses")
    return out


def _group_of(item: Item) -> str:
    session = str(item.meta.get("session") or "")
    slug = _SESSION_PREFIX.sub("", session).replace("-", " ").strip() if session else ""
    text = clean(slug).replace(":", " ").replace("[", " ").replace("]", " ")
    text = " ".join(text.split())
    return clip(text, 60) if text else "notes"


def _excluded(item: Item, start_ids: set[str]) -> bool:
    if item.id in start_ids:
        return True
    if _int(item.meta.get("age_days")) > STALE_AFTER_DAYS or item.meta.get("stale"):
        return True
    key = str(item.meta.get("key") or norm_key(item.text))
    return bool(RESOLVED.search(key) or COSMETIC.search(key) or noise_line(item.text))


def _active_task_ids(v: View) -> set[str]:
    """The tracker ids of the active task: a thread that only repeats it is already printed under Active task."""
    return {str(it.meta.get("task_id") or "") for it in v.visible("task", "active_task") if it.meta.get("task_id")}


def still_open_lines(ctx: DigestContext, v: View) -> tuple[list[str], int]:
    """Still open: grouped, de-duplicated and capped open threads, plus how many were hidden."""
    start_ids = {i for i, _ in start_here_picks(ctx, v)}
    task_ids = _active_task_ids(v)
    seen: set[str] = set()
    kept: list[Item] = []
    for it in _threads(v):
        key = str(it.meta.get("key") or norm_key(it.text))
        if _excluded(it, start_ids) or key in seen or any(t in it.text for t in task_ids):
            continue
        seen.add(key)
        kept.append(it)
    by_group: dict[str, list[Item]] = {}
    for it in kept:
        by_group.setdefault(_group_of(it), []).append(it)
    for members in by_group.values():
        members.sort(key=lambda it: (_desc(item_day(it)), it.id))
    groups = sorted(by_group.items(), key=lambda kv: (_desc(item_day(kv[1][0])), kv[0]))
    ordered = [it for _, members in groups for it in members]
    lines: list[str] = []
    for it in ordered[:MAX_STILL_OPEN]:
        age = _int(it.meta.get("age_days"))
        age_text = f" ({age}d)" if age >= AGE_SHOWN_FROM_DAYS else ""
        lines.append(f"- {_group_of(it)}: {item_text(it.text, 200)}{age_text}{tail(it.id)}")
    return lines, max(0, len(ordered) - MAX_STILL_OPEN)


def _desc(day: str) -> str:
    """A sort key that puts the newest YYYY-MM-DD first when sorted ascending."""
    return "".join(chr(0x10FFFF - ord(c)) for c in day) if day else "\U0010FFFF"


def decided_lines(ctx: DigestContext, v: View) -> list[str]:
    """Decided yesterday: window dates only, newest first, the rationale cut, cap 10."""
    start, end = ctx.window_start.date().isoformat(), ctx.window_end.date().isoformat()
    picked = [it for it in v.visible("brain", "brain_decision") if start <= item_day(it) <= end]
    picked.sort(key=lambda it: (_desc(item_day(it)), it.id))
    out: list[str] = []
    for it in picked[:MAX_DECIDED]:
        cut = RATIONALE.sub("", DASH_RATIONALE.sub("", it.text)).strip()
        text = item_text(cut if len(cut) >= 3 else it.text, 200)
        out.append(f"- {text}{tail(it.id)}")
    return out


def _repo_line(it: Item) -> str:
    m = it.meta
    counts = [
        f"{plural(_int(m.get('commits')), 'commit')} since window",
        f"{_int(m.get('modified'))} modified",
        f"{_int(m.get('untracked'))} untracked",
    ]
    if m.get("ahead"):
        counts.append(f"ahead {_int(m['ahead'])}")
    if m.get("behind"):
        counts.append(f"behind {_int(m['behind'])}")
    if m.get("commits_withheld"):
        counts.append(f"{plural(_int(m['commits_withheld']), 'subject')} withheld")
    name = _name(it.title) + (" (work)" if it.work else "")
    if m.get("counts_only"):
        return f"- {name}: {', '.join(counts)}{tail(it.id)}"
    branch = f"branch {item_text(m['branch'], 80)}, " if m.get("branch") else ""
    subjects = "; ".join(item_text(s, 160) for s in it.text.split("\n") if s.strip())
    rest = f": {subjects}" if subjects else ""
    return f"- {name} {branch}{', '.join(counts)}{rest}{tail(it.id)}"


def _name(value: object) -> str:
    """A repo name as one token: the grammar reads it as `\\S+`."""
    return re.sub(r"\s+", "_", item_text(value, 80)) or "unnamed"


def _repo_active(it: Item) -> bool:
    m = it.meta
    return any(_int(m.get(k)) > 0 for k in ("commits", "ahead", "behind", "commits_withheld"))


def repo_lines(ctx: DigestContext, v: View) -> tuple[list[str], int, int]:
    """The git part of Repos: (lines, repos printed on their own line, quiet repos)."""
    res = ctx.results.get("git")
    if res is not None and not res.ok:
        return ["- Repo data unavailable, see Source status."], 0, 0
    facts = v.facts("git")
    repos = v.visible("git", "git_repo")
    active = [it for it in repos if _repo_active(it)]
    dirty = [it for it in repos if not _repo_active(it) and (_int(it.meta.get("modified")) or _int(it.meta.get("untracked")))]
    out = [_repo_line(it) for it in active]
    if dirty:
        parts = [f"{_name(it.title)} {_int(it.meta.get('modified'))}/{_int(it.meta.get('untracked'))}" for it in dirty]
        out.append("- Uncommitted only: " + ", ".join(parts) + ".")
    quiet = len(facts.get("quiet") or [])
    if quiet:
        out.append(f"- Quiet: {plural(quiet, 'repo')}.")
    if facts.get("not_repos"):
        out.append("- Not a git repo or missing: " + ", ".join(clean(n) for n in facts["not_repos"]) + ".")
    if facts.get("errors"):
        out.append("- Could not be read: " + ", ".join(clean(n) for n in facts["errors"]) + ".")
    if not out:
        out.append("- No repositories configured.")
    return out, len(active), quiet


def _github_line(it: Item) -> str:
    m = it.meta
    mine, theirs = _int(m.get("authored")), _int(m.get("review_requested"))
    if mine or theirs:
        prs = f"{plural(mine + theirs, 'open PR')} ({mine} yours, {theirs} awaiting your review)"
    else:
        prs = "no open PRs"
    ci = token(m.get("ci", "unknown"))
    ci_text = "no CI runs" if ci == "none" else f"CI {ci.replace('_', ' ')}"
    if m.get("ci_branch") and ci not in ("none", "unknown"):
        ci_text += f" on {item_text(m['ci_branch'], 80)}"
    parts = [prs, ci_text]
    stale = _int(m.get("stale_branches"))
    if stale:
        parts.append(f"{'at least ' if m.get('stale_is_floor') else ''}{stale} stale branch{'' if stale == 1 else 'es'}")
    if m.get("prs_withheld"):
        parts.append(f"{plural(_int(m['prs_withheld']), 'title')} withheld")
    name = _name(it.title) + (" (work)" if it.work else "")
    titles = "; ".join(item_text(s, 160) for s in it.text.splitlines() if s.strip())
    rest = f": {titles}" if titles and not m.get("counts_only") else ""
    return f"- GitHub {name}: {', '.join(parts)}{rest}{tail(it.id)}"


def _github_worth_a_line(it: Item) -> bool:
    m = it.meta
    return bool(_int(m.get("authored")) or _int(m.get("review_requested")) or str(m.get("ci", "")) in CI_FAILING)


def github_lines(ctx: DigestContext, v: View) -> list[str]:
    """The GitHub block of Repos: repos with PRs or failing CI, then two count lines."""
    res = ctx.results.get("github")
    if res is None:
        return [f"- {GITHUB_NOT_COLLECTED}"]
    if not res.ok:
        return ["- GitHub PRs and CI: unavailable, see Source status."]
    facts = res.facts
    if facts.get("disabled"):
        return [f"- {GITHUB_DISABLED}"]
    repos = v.visible("github", "github_repo")
    shown = [it for it in repos if _github_worth_a_line(it)]
    out = [_github_line(it) for it in shown]
    quiet = len(facts.get("quiet") or []) + len(repos) - len(shown)
    if quiet:
        out.append(f"- GitHub quiet: {plural(quiet, 'repo')}, CI green or none.")
    states = Counter(str(s) for s in (facts.get("states") or {}).values())
    not_read = [(state, states[state]) for state, _ in GITHUB_STATES if states.get(state)]
    if not_read:
        detail = ", ".join(f"{state} {n}" for state, n in not_read)
        out.append(f"- GitHub not read: {plural(sum(n for _, n in not_read), 'repo')} ({detail}).")
    if not out:
        out.append("- GitHub PRs and CI: no repositories configured.")
    return out


def system_lines(ctx: DigestContext, v: View) -> tuple[list[str], int]:
    """System: one All green line or one line per anomaly, then result lines. Returns (lines, anomalies)."""
    res = ctx.results.get("system")
    if res is None:
        return ["- No system data."], 0
    if not res.ok:
        return ["- System data unavailable, see Source status."], 0
    f = res.facts
    anomalies: list[str] = []
    for key in ("jobs", "tasks", "logs", "queue"):
        if f.get(f"{key}_error"):
            anomalies.append(f"- {key.capitalize()}: unavailable ({clean(f[f'{key}_error'])}).")
    if _int(f.get("jobs_failed")):
        anomalies.append(f"- Jobs failed: {plural(_int(f['jobs_failed']), 'job')} failed since the last digest.")
    if _int(f.get("daemon_crashes")):
        anomalies.append(f"- Daemon crashed: {plural(_int(f['daemon_crashes']), 'crash')} recorded since the last digest.")
    if _int(f.get("unclean_exits")):
        anomalies.append(f"- Unclean exits: {_int(f['unclean_exits'])} since the last digest.")
    breaker = str(f.get("breaker_state") or "closed")
    if breaker != "closed":
        anomalies.append(f"- Breaker: {clean(breaker)}, Claude calls are paused, run jarvis breaker status.")
    if _int(f.get("killswitch_trips")):
        last = f" (last: {clean(f['killswitch_last_reason'])})" if f.get("killswitch_last_reason") else ""
        anomalies.append(f"- Kill switch: {plural(_int(f['killswitch_trips']), 'trip')} since the last digest{last}.")
    if f.get("logs_available") is False:
        why = "; ".join(f"{clean(n)} {clean(r)}" for n, r in (f.get("logs_unavailable") or {}).items()) or "unknown"
        anomalies.append(f"- Watchdog: logs unavailable ({why}).")
    elif _int(f.get("watchdog_crashloops")) or _int(f.get("restart_attempts")):
        anomalies.append(f"- Watchdog: {plural(_int(f.get('watchdog_crashloops')), 'crashloop')}, "
                         f"{plural(_int(f.get('restart_attempts')), 'restart attempt')} since the last digest.")
    if f.get("disk_checked") and f.get("disk_ok") is False:
        free = f.get("disk_free_gb")
        anomalies.append("- Disk: WARNING" + (f", {free} GB free." if free is not None else ", free space low."))
    tasks = f.get("scheduled_tasks") if isinstance(f.get("scheduled_tasks"), dict) else {}
    for name, info in tasks.items():
        if not isinstance(info, dict):
            continue
        if not info.get("available"):
            anomalies.append(f"- Task: {clean(name)} could not be queried.")
        elif not info.get("ok"):
            ran = f"last ran {clean(info['last_run_text'])}" if info.get("last_run_text") else "never ran"
            anomalies.append(f"- Task: {clean(name)} {ran}, {clean(info.get('last_result_text') or 'unknown result')}.")
    if _int(f.get("config_invalid")):
        anomalies.append(f"- Config invalid: {plural(_int(f['config_invalid']), 'error')} audited since the last digest, "
                         "the daemon idles until jarvis.toml parses.")
    brain = v.facts("brain") if v.source_ok("brain") else {}
    if brain.get("recent_missing"):
        anomalies.append("- Notes index missing: RECENT.md was not found.")
    if brain.get("recent_stale"):
        anomalies.append(f"- Notes index stale: RECENT.md is {brain.get('recent_age_hours')} h old (over 30 h), "
                         "the nightly job may have failed.")
    orphans = _int(brain.get("orphan_checkpoints"))
    if orphans >= ORPHAN_ALERT:
        anomalies.append(f"- Sessions not filed: {orphans} checkpoints waiting for /promote-sessions.")
    held = _int((f.get("queue") or {}).get("held")) if isinstance(f.get("queue"), dict) else 0
    if held >= HELD_BACKLOG_ALERT:
        anomalies.append(f"- Held backlog: {held} held references in the queue, run jarvis held.")
    out = list(anomalies)
    if not anomalies:
        cost = float(f.get("claude_cost_usd") or 0.0)
        out.append(f"- All green: {plural(_int(f.get('jobs_done')), 'job')} done, 0 failed, ${cost:.2f} Claude, "
                   "breaker closed, disk ok, tasks ok.")
    # Results, not anomalies: what the night produced, and the checkpoint count under the alert threshold (a
    # result line by contract section 1.2, so the owner sees the backlog before it becomes an anomaly).
    if f.get("consolidation"):
        out.append(f"- {clip(f['consolidation'], 300)}")
    if 0 < orphans < ORPHAN_ALERT:
        out.append(f"- Checkpoints waiting for /promote-sessions: {orphans}.")
    return out, len(anomalies)


# --- sections --------------------------------------------------------------------------


class Section:
    """A named block of the note. Subclass, set `name`, implement `body`; the heading comes from HEADINGS."""

    name: str = ""

    @property
    def heading(self) -> str:
        return HEADINGS.get(self.name, self.name)

    def body(self, ctx: DigestContext) -> list[str]:
        raise NotImplementedError

    def render(self, ctx: DigestContext) -> list[str]:
        if FORBIDDEN_HEADING.match(self.heading.strip()):
            raise RenderError("the heading 'Open threads' is reserved for session notes")
        lines = self.body(ctx)
        return [f"## {clean(self.heading)}", *lines] if lines else []


SECTIONS: dict[str, Section] = {}


def register(section: Section) -> Section:
    SECTIONS[section.name] = section
    return section


class StartHere(Section):
    name = "start_here"

    def body(self, ctx: DigestContext) -> list[str]:
        v = View(ctx)
        lines: list[str] = []
        if ctx.summary is not None:
            lines.append(clip(ctx.summary.headline, 200))
        elif ctx.tier_violation_seq is not None:
            lines.append(
                f"TIER VIOLATION, Claude call aborted, see audit seq {int(ctx.tier_violation_seq)}. "
                "Deterministic sections below are complete."
            )
        else:
            reason = clean(ctx.claude_status.replace("_", " "))
            lines.append(f"Claude summary unavailable ({reason}). Deterministic sections below are complete.")
        # Held items did change something, so only a truly empty window gets this line.
        changed = bool(ctx.held) or any(it.kind in ("git_repo", "brain_session") for it in v.items)
        if not changed:
            lines.append("Nothing changed overnight: no commits, no new sessions.")
        lines.extend(f"{n}. {why}{tail(i)}" for n, (i, why) in enumerate(start_here_picks(ctx, v), start=1))
        return lines


class Attention(Section):
    name = "attention"

    def body(self, ctx: DigestContext) -> list[str]:
        return attention_lines(ctx, View(ctx)) or ["- Nothing broken."]


class ActiveTask(Section):
    name = "active_task"

    def body(self, ctx: DigestContext) -> list[str]:
        v = View(ctx)
        res = ctx.results.get("task")
        if res is not None and not res.ok:
            return ["- Task data unavailable, see Source status."]
        tasks = v.visible("task", "active_task")
        if not tasks:
            hidden = [it for it in (res.items if res else []) if it.id in v.sensitive_ids]
            if hidden or v.facts("task").get("task_withheld"):
                return ["- Active task withheld, see Held back and not summarized."]
            return ["- No active task recorded."]
        out = []
        for it in tasks:
            m = it.meta
            status = clean(m.get("status") or "unknown").replace(",", " ")
            task_id = re.sub(r"\s+", "_", clean(m.get("task_id", ""))) or "unknown"
            out.append(f"- {task_id} {item_text(it.title, 120)}, status {status}, {_due_text(m)}.{tail(it.id)}")
        return out


class StillOpen(Section):
    name = "still_open"

    def body(self, ctx: DigestContext) -> list[str]:
        res = ctx.results.get("brain")
        if res is not None and not res.ok:
            return ["- Brain data unavailable, see Source status."]
        lines, hidden = still_open_lines(ctx, View(ctx))
        if hidden:
            lines.append(f"- {hidden} more open thread{'' if hidden == 1 else 's'} not shown (cap {MAX_STILL_OPEN}).")
        return lines


class Decided(Section):
    name = "decided"

    def body(self, ctx: DigestContext) -> list[str]:
        if not View(ctx).source_ok("brain"):
            return []
        return decided_lines(ctx, View(ctx))


class Repos(Section):
    name = "repos"

    def body(self, ctx: DigestContext) -> list[str]:
        v = View(ctx)
        out, _, _ = repo_lines(ctx, v)
        out.extend(github_lines(ctx, v))
        return out


class System(Section):
    name = "system"

    def body(self, ctx: DigestContext) -> list[str]:
        return system_lines(ctx, View(ctx))[0]


class Held(Section):
    name = "held"

    def body(self, ctx: DigestContext) -> list[str]:
        sens = sorted((w for w in ctx.held if w.hold_kind == "sensitive"), key=lambda w: w.id)
        pol = [w for w in ctx.held if w.hold_kind == "policy"]
        sens_part = f"{len(sens)} sensitive"
        if sens:
            ids = [clean(w.id) for w in sens]
            shown = ", ".join(ids[:MAX_HELD_IDS]) + (f", and {len(ids) - MAX_HELD_IDS} more" if len(ids) > MAX_HELD_IDS else "")
            reasons = Counter(clean(w.reason).replace(")", "") for w in sens)
            why = ", ".join(f"{r} x{n}" for r, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))
            sens_part += f" (ids {shown.replace(';', ',')}; reasons: {why})"
        status = ctx.claude_status
        if status == "ok" or ctx.summary is not None:
            claude = "none"
        elif status == "no_items":
            claude = "not called"
        else:
            claude = token(status).replace("_", " ")
        return [
            f"- Held: {sens_part}, {len(pol)} policy, {len(set(ctx.over_cap))} over cap. "
            f"Claude: {claude}. Run `jarvis held` in a terminal."
        ]


class SourceStatus(Section):
    name = "source_status"

    def body(self, ctx: DigestContext) -> list[str]:
        parts = []
        for name, label in source_labels(ctx):
            parts.append(f"{clean(name)} {label}")
        return ["- " + ", ".join(parts) + "."]


class FlagMistake(Section):
    name = "flag_mistake"

    def body(self, ctx: DigestContext) -> list[str]:
        return [
            '- `jarvis wrong <id> --should {escalate,hold,skip,other} --note "..."`; '
            "`--leak` if something sensitive was shown or sent."
        ]


class ClickUp(Section):
    """Open ClickUp tasks (plan T13). Absent unless the collector ran; not in DEFAULT_SECTIONS, so
    it slots in before the footer. Links are built from the validated task id, never from model text."""

    name = "clickup"

    @property
    def heading(self) -> str:
        return "ClickUp: open tasks"

    def body(self, ctx: DigestContext) -> list[str]:
        res = ctx.results.get("clickup")
        if res is None:
            return []
        if not res.ok:
            return [f"- ClickUp: unavailable ({clean(res.error or 'unknown')})."]
        if res.facts.get("skipped"):
            return [f"- ClickUp: not queried ({clean(res.facts['skipped']).replace('_', ' ')})."]
        v = View(ctx)
        out = []
        for it in v.visible("clickup", "clickup_task"):
            m = it.meta
            task_id = clean(m.get("task_id", ""))
            status = clean(m.get("status") or "unknown")
            out.append(f"- [{task_id}](https://app.clickup.com/t/{task_id}) {item_text(it.title, 160)}, status {status}, "
                       f"{_due_text(m)}{tail(it.id)}")
        if res.facts.get("partial"):
            out.append(f"- ClickUp list is partial ({clean(res.facts.get('partial_reason') or 'unknown')}).")
        if not out:
            out.append("- No open ClickUp tasks in the window.")
        return out


for _cls in (StartHere, Attention, ActiveTask, StillOpen, Decided, Repos, System, Held, SourceStatus, FlagMistake, ClickUp):
    register(_cls())


# --- source status and front matter ----------------------------------------------------


def _ordered_sources(ctx: DigestContext) -> list[str]:
    known = [s for s in SOURCE_ORDER if s in ctx.results]
    extra = [s for s in ctx.results if s not in SOURCE_ORDER and s != "clickup"]
    return known + extra


def source_labels(ctx: DigestContext) -> list[tuple[str, str]]:
    """(name, human label) per source, in a fixed order, with the two fixed non-sources last."""
    out: list[tuple[str, str]] = []
    for name in _ordered_sources(ctx):
        res = ctx.results[name]
        if not res.ok:
            out.append((name, f"FAILED ({clip(res.error or 'unknown error', 80)})"))
        elif name == "git":
            total = int(res.facts.get("repos_total", 0))
            bad = len(res.facts.get("not_repos", []))
            out.append((name, f"ok ({plural(total, 'repo')}, {plural(bad, 'not repo')})"))
        elif name == "github":
            if res.facts.get("disabled"):
                out.append((name, "disabled"))
            else:
                read, total = int(res.facts.get("repos_read", 0)), int(res.facts.get("repos_total", 0))
                out.append((name, f"ok ({read} of {plural(total, 'repo')} read)"))
        elif name in ("brain",):
            out.append((name, f"ok ({plural(len(res.items), 'item')})"))
        else:
            out.append((name, "ok"))
    click = ctx.results.get("clickup")
    if click is None:
        label = "disabled"
    elif not click.ok:
        label = f"FAILED ({clip(click.error or '', 80)})"
    elif click.facts.get("skipped"):
        label = "skipped"
    elif click.facts.get("partial"):
        label = f"partial ({clip(click.facts.get('partial_reason') or '', 40)})"
    else:
        label = "ok"
    out.append(("clickup", label))
    if "github" not in ctx.results:
        out.append(("github", "not collected"))
    return out


def _sources_value(ctx: DigestContext) -> str:
    states = []
    for name, label in source_labels(ctx):
        word = "failed" if label.startswith("FAILED") else label.split(" (")[0].replace(" ", "_")
        states.append(f"{token(name)}: {word}")
    return "{" + ", ".join(states) + "}"


def section_counts(ctx: DigestContext) -> dict[str, int]:
    """The grammar 2 front matter counters: what each section will show, computed from the same plans."""
    v = View(ctx)
    c = v.counts()
    still, hidden = still_open_lines(ctx, v)
    _, repos_active, repos_quiet = repo_lines(ctx, v)
    return {
        "n_collected": c["collected"],
        "n_cleared": c["cleared"],
        "n_held": c["held_sensitive"] + c["held_policy"],
        "n_start_here": len(start_here_picks(ctx, v)),
        "n_attention": len(attention_lines(ctx, v)),
        "n_still_open": len(still),
        "n_still_open_hidden": hidden,
        "n_decided": len(decided_lines(ctx, v)) if v.source_ok("brain") else 0,
        "n_repos_active": repos_active,
        "n_repos_quiet": repos_quiet,
        "n_system_anomalies": system_lines(ctx, v)[1],
    }


def _front_matter(ctx: DigestContext) -> list[str]:
    for stamp in (ctx.generated_at, ctx.window_start, ctx.window_end):
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("DigestContext timestamps must be timezone-aware")
    c = View(ctx).counts()
    items = ", ".join(f"{k}: {c[k]}" for k in ("collected", "cleared", "held_sensitive", "held_policy", "over_cap"))
    pairs = [
        ("type", "jarvis-digest"),
        ("generator", "jarvisd"),
        ("generator_version", clean(ctx.generator_version)),
        ("job_id", re.sub(r"[^A-Za-z0-9_.-]+", "_", ctx.job_id)),
        ("date", ctx.day.isoformat()),
        ("generated_at", ctx.generated_at.isoformat(timespec="seconds")),
        ("window_start", ctx.window_start.isoformat(timespec="seconds")),
        ("window_end", ctx.window_end.isoformat(timespec="seconds")),
        ("status", token(ctx.status)),
        ("late", "true" if ctx.late else "false"),
        ("claude", token(ctx.claude_status)),
        ("local_tier", token(ctx.local_tier)),
        ("degraded", "true" if ctx.degraded else "false"),
        ("cost_usd", f"{float(ctx.cost_usd):.4f}"),
        ("items", "{" + items + "}"),
        ("grammar", str(GRAMMAR)),
        *((k, str(n)) for k, n in section_counts(ctx).items()),
        ("sources", _sources_value(ctx)),
        ("audit_seq", str(int(ctx.audit_seq))),
        ("audit_head", re.sub(r"[^0-9a-f]", "", ctx.audit_head.lower()) or GENESIS),
        ("tags", "[jarvis, digest]"),
    ]
    return ["---", *(f"{k}: {v}" for k, v in pairs), "---"]


# --- entry points ----------------------------------------------------------------------


def digest_filename(day: date, run: int = 1) -> str:
    """digest-YYYY-MM-DD.md, and -r2, -r3 for forced reruns (design section 5)."""
    suffix = "" if run <= 1 else f"-r{int(run)}"
    return f"digest-{day.isoformat()}{suffix}.md"


def _section_names(ctx: DigestContext) -> list[str]:
    if ctx.section_order is not None:
        return [n for n in ctx.section_order if n in SECTIONS]
    order = [n for n in DEFAULT_SECTIONS if n in SECTIONS]
    extras = [n for n in SECTIONS if n not in DEFAULT_SECTIONS]
    body = [n for n in order if n not in FOOTER_SECTIONS]
    footer = [n for n in order if n in FOOTER_SECTIONS]
    return body + extras + footer


def render_digest(ctx: DigestContext) -> str:
    """The whole note: front matter, title, sections. LF line endings, one trailing newline."""
    title = f"# JARVIS morning digest, {WEEKDAYS[ctx.day.weekday()]} {ctx.day.isoformat()}"
    blocks = ["\n".join([*_front_matter(ctx), title])]
    for name in _section_names(ctx):
        lines = SECTIONS[name].render(ctx)
        if lines:
            blocks.append("\n".join(lines))
    text = "\n\n".join(blocks) + "\n"
    if re.search(r"(?im)^##\s+open threads\b", text):
        raise RenderError("rendered text contains the reserved heading")
    return text
