"""Deterministic digest markdown (design section 9). Pure: no I/O, no clock, no randomness.

`render_digest(ctx)` turns a `DigestContext` into the note text. Every section is a small
class in the `SECTIONS` registry, so a new section is one class and one registry entry, with
no edit to `render_digest`. The order comes from `ctx.section_order` (the config table has no
`sections` key yet, so the caller may pass one) and otherwise from `DEFAULT_SECTIONS`.
Registered sections missing from the order are slotted in before the footer sections.

Rules enforced here, on every string, whatever its origin:
- `strip_dashes` (no U+2014 or U+2013), control characters and newlines collapse to one
  space, so a hostile line can never open a heading or a front matter block;
- `|` becomes `/`, so no table can form;
- the heading `## Open threads` is never emitted: it would be read as a session section by
  brain-nightly.py. A section class asking for it raises `RenderError`.

What the reader sees about held items: sensitive-held items are never printed, only counted
and listed by opaque id with a reason code. Policy-held items (work metadata) are printed
from collector data but never carry a Claude one-liner, because Claude never saw them.
Claude one-liners and attention ids are accepted only for items that were actually cleared
for Claude; anything else it cites is dropped.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from jarvisd import __version__
from jarvisd.common import strip_dashes
from jarvisd.models import CollectResult, DigestSummary, Item, WithheldItem

DEFAULT_SECTIONS: tuple[str, ...] = (
    "start_here",
    "active_task",
    "brain",
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
MAX_HELD_IDS = 20
FORBIDDEN_HEADING = re.compile(r"^open threads\s*$", re.IGNORECASE)
GENESIS = "0" * 64
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


def token(value: object) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_") or "unknown"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --- derived view ----------------------------------------------------------------------


def is_deterministic(item: Item) -> bool:
    return item.meta.get("render") == "deterministic"


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

    def oneliner(self, item_id: str) -> str:
        summary = self.ctx.summary
        if summary is None or item_id not in self.cleared_ids:
            return ""
        text = summary.summaries.get(item_id, "")
        return " " + clip(text, 140) if text else ""

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


# --- sections --------------------------------------------------------------------------


class Section:
    """A named block of the note. Subclass, set `name` and `heading`, implement `body`."""

    name: str = ""
    heading: str = ""

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


def _due_text(meta: dict[str, Any]) -> str:
    state, day = meta.get("due_state", "none"), meta.get("due_date", "")
    if state == "overdue":
        return f"due {day} (OVERDUE)"
    if state == "today":
        return f"due {day} (TODAY)"
    return f"due {day}" if state == "later" and day else "no due date"


def _fallback_attention(v: View) -> list[tuple[str, str]]:
    """Deterministic 'Start here' picks: due task, repos with commits, newest open threads."""
    picks: list[tuple[str, str]] = []
    for it in v.visible("task", "active_task"):
        state = it.meta.get("due_state")
        if state in ("overdue", "today"):
            picks.append((it.id, f"{clip(it.title, 100)} is " + ("overdue" if state == "overdue" else "due today")))
    repos = [it for it in v.visible("git", "git_repo") if int(it.meta.get("commits", 0)) > 0]
    for it in sorted(repos, key=lambda r: (-int(r.meta.get("commits", 0)), r.title)):
        picks.append((it.id, f"{clip(it.title, 80)}: {plural(int(it.meta['commits']), 'commit')} since the window start"))
    for it in v.visible("github", "github_repo"):
        if int(it.meta.get("review_requested", 0)):
            picks.append((it.id, f"{clip(it.title, 80)}: {plural(int(it.meta['review_requested']), 'PR')} waiting for your review"))
        elif str(it.meta.get("ci", "")) in ("failure", "timed_out", "startup_failure", "action_required"):
            picks.append((it.id, f"{clip(it.title, 80)}: CI is failing on the default branch"))
    threads = sorted(v.visible("brain", "brain_thread"), key=lambda t: str(t.meta.get("date", "")), reverse=True)
    for it in threads:
        picks.append((it.id, clip(it.text, 160)))
    return picks[:MAX_ATTENTION]


class StartHere(Section):
    name = "start_here"
    heading = "Start here"

    def body(self, ctx: DigestContext) -> list[str]:
        v = View(ctx)
        lines: list[str] = []
        picks: list[tuple[str, str]] = []
        if ctx.summary is not None:
            lines.append(clip(ctx.summary.headline, 200))
            picks = [(a.id, clip(a.why, 160)) for a in ctx.summary.attention if a.id in v.cleared_ids][:MAX_ATTENTION]
        elif ctx.tier_violation_seq is not None:
            lines.append(
                f"TIER VIOLATION, Claude call aborted, see audit seq {int(ctx.tier_violation_seq)}. "
                "Deterministic sections below are complete."
            )
        else:
            reason = clean(ctx.claude_status.replace("_", " "))
            lines.append(f"Claude summary unavailable ({reason}). Deterministic sections below are complete.")
        if not picks:
            picks = _fallback_attention(v)
        # Held items did change something, so only a truly empty window gets this line.
        changed = bool(ctx.held) or any(it.kind in ("git_repo", "brain_session") for it in v.items)
        if not changed:
            lines.append("Nothing changed overnight: no commits, no new sessions.")
        lines.extend(f"{n}. [{clean(i)}] {why}" for n, (i, why) in enumerate(picks, start=1))
        return lines


class ActiveTask(Section):
    name = "active_task"
    heading = "Active task"

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
        no_clickup = "clickup" not in ctx.results
        out = []
        for it in tasks:
            m = it.meta
            status = clean(m.get("status") or "unknown")
            tail = " No ClickUp call was made (v1)." if no_clickup else ""
            out.append(f"- {clean(m.get('task_id', ''))} {clip(it.title, 120)}, status {status}, {_due_text(m)}.{tail}")
        return out


def _bullet_date(item: Item) -> str:
    return str(item.meta.get("date", ""))


class Brain(Section):
    name = "brain"
    heading = "Brain: open threads and decisions"

    def body(self, ctx: DigestContext) -> list[str]:
        v = View(ctx)
        res = ctx.results.get("brain")
        if res is not None and not res.ok:
            return ["- Brain data unavailable, see Source status."]
        out: list[str] = []
        facts = v.facts("brain")
        if facts.get("recent_missing"):
            out.append("- RECENT.md was not found.")
        if facts.get("recent_withheld"):
            out.append("- RECENT.md was withheld whole, see Held back and not summarized.")
        if facts.get("recent_stale"):
            out.append(f"- RECENT.md is {facts.get('recent_age_hours')} h old (over 30 h), the nightly job may have failed.")
        threads = sorted(v.visible("brain", "brain_thread"), key=_bullet_date, reverse=True)
        decisions = sorted(v.visible("brain", "brain_decision"), key=_bullet_date, reverse=True)
        sessions = v.visible("brain", "brain_session")
        for it in threads:
            stale = " (stale)" if it.meta.get("stale") else ""
            out.append(f"- [{clean(_bullet_date(it))}]{stale} {clip(it.text, 300)} [{it.id}]{v.oneliner(it.id)}")
        for it in decisions:
            out.append(f"- Decision [{clean(_bullet_date(it))}] {clip(it.text, 300)} [{it.id}]{v.oneliner(it.id)}")
        for it in sessions[:10]:
            label = "entry point" if it.meta.get("section") == "entry_point" else "open thread"
            out.append(f"- Session {clean(it.meta.get('session', ''))}, {label}: {clip(it.text, 300)} [{it.id}]{v.oneliner(it.id)}")
        if not (threads or decisions or sessions):
            out.append("- No open threads, decisions or new sessions.")
        return out


def _repo_line(it: Item, v: View) -> str:
    m = it.meta
    counts = [
        f"{plural(int(m.get('commits', 0)), 'commit')} since window",
        f"{int(m.get('modified', 0))} modified",
        f"{int(m.get('untracked', 0))} untracked",
    ]
    if m.get("ahead"):
        counts.append(f"ahead {int(m['ahead'])}")
    if m.get("behind"):
        counts.append(f"behind {int(m['behind'])}")
    if m.get("commits_withheld"):
        counts.append(f"{plural(int(m['commits_withheld']), 'subject')} withheld")
    name = clean(it.title) + (" (work)" if it.work else "")
    if m.get("counts_only"):
        return f"- {name}: {', '.join(counts)} [{it.id}]"
    branch = f"branch {clean(m['branch'])}, " if m.get("branch") else ""
    subjects = "; ".join(clip(s, 160) for s in it.text.split("\n") if s.strip())
    tail = f": {subjects}" if subjects else ""
    return f"- {name} {branch}{', '.join(counts)}{tail} [{it.id}]{v.oneliner(it.id)}"


def _github_line(it: Item, v: View) -> str:
    m = it.meta
    mine, theirs = int(m.get("authored", 0)), int(m.get("review_requested", 0))
    if mine or theirs:
        prs = f"{plural(mine + theirs, 'open PR')} ({mine} yours, {theirs} awaiting your review)"
    else:
        prs = "no open PRs"
    ci = token(m.get("ci", "unknown"))
    ci_text = "no CI runs" if ci == "none" else f"CI {ci.replace('_', ' ')}"
    if m.get("ci_branch") and ci not in ("none", "unknown"):
        ci_text += f" on {clean(m['ci_branch'])}"
    parts = [prs, ci_text]
    stale = int(m.get("stale_branches", 0))
    if stale:
        parts.append(f"{'at least ' if m.get('stale_is_floor') else ''}{stale} stale branch{'' if stale == 1 else 'es'}")
    if m.get("prs_withheld"):
        parts.append(f"{plural(int(m['prs_withheld']), 'title')} withheld")
    name = clean(it.title) + (" (work)" if it.work else "")
    titles = "; ".join(clip(s, 160) for s in it.text.splitlines() if s.strip())
    tail = f": {titles}" if titles and not m.get("counts_only") else ""
    return f"- GitHub {name}: {', '.join(parts)}{tail} [{it.id}]{v.oneliner(it.id)}"


def _github_lines(ctx: DigestContext, v: View) -> list[str]:
    """The GitHub block of the Repos section: per-repo lines, quiet repos, then why some were not read."""
    res = ctx.results.get("github")
    if res is None:
        return [f"- {GITHUB_NOT_COLLECTED}"]
    if not res.ok:
        return ["- GitHub PRs and CI: unavailable, see Source status."]
    facts = res.facts
    if facts.get("disabled"):
        return [f"- {GITHUB_DISABLED}"]
    out = [_github_line(it, v) for it in v.visible("github", "github_repo")]
    summary = facts.get("summary", {})
    states = facts.get("states", {})
    quiet = []
    for name in facts.get("quiet", []):
        ci = token(summary.get(name, {}).get("ci", "unknown"))
        facts_of = summary.get(name, {})
        bits = ["no CI runs" if ci == "none" else "CI " + ci.replace("_", " ")]
        stale = int(facts_of.get("stale_branches") or 0)
        if stale:
            bits.append(f"{'at least ' if facts_of.get('stale_is_floor') else ''}{stale} stale branch{'' if stale == 1 else 'es'}")
        quiet.append(f"{clean(name)} ({', '.join(bits)})")
    if quiet:
        out.append("- GitHub quiet: " + ", ".join(quiet) + ".")
    for state, phrase in GITHUB_STATES:
        names = [clean(n) for n, s in states.items() if s == state]
        if names:
            out.append(f"- GitHub, {phrase}: {', '.join(names)}.")
    if not out:
        out.append("- GitHub PRs and CI: no repositories configured.")
    return out


class Repos(Section):
    name = "repos"
    heading = "Repos"

    def body(self, ctx: DigestContext) -> list[str]:
        v = View(ctx)
        res = ctx.results.get("git")
        out: list[str] = []
        if res is not None and not res.ok:
            out.append("- Repo data unavailable, see Source status.")
        else:
            facts = v.facts("git")
            out.extend(_repo_line(it, v) for it in v.visible("git", "git_repo"))
            if facts.get("quiet"):
                out.append("- Quiet: " + ", ".join(clean(n) for n in facts["quiet"]) + ".")
            if facts.get("not_repos"):
                out.append("- Not a git repo or missing: " + ", ".join(clean(n) for n in facts["not_repos"]) + ".")
            if facts.get("errors"):
                out.append("- Could not be read: " + ", ".join(clean(n) for n in facts["errors"]) + ".")
            if not out:
                out.append("- No repositories configured.")
        out.extend(_github_lines(ctx, v))
        return out


class System(Section):
    name = "system"
    heading = "What JARVIS did while you slept"

    def body(self, ctx: DigestContext) -> list[str]:
        res = ctx.results.get("system")
        if res is not None and not res.ok:
            return ["- System data unavailable, see Source status."]
        out = [f"- {clip(it.title, 300)}" for it in (res.items if res else []) if is_deterministic(it)]
        brain = ctx.results.get("brain")
        if brain is not None and brain.ok:
            facts = brain.facts
            if "recent_age_hours" in facts:
                out.append(f"- RECENT.md age {facts['recent_age_hours']} h.")
            if "orphan_checkpoints" in facts:
                out.append(f"- Checkpoints waiting for /promote-sessions: {int(facts['orphan_checkpoints'])}.")
        return out or ["- No system data."]


class Held(Section):
    name = "held"
    heading = "Held back and not summarized"

    def body(self, ctx: DigestContext) -> list[str]:
        sens = sorted((w for w in ctx.held if w.hold_kind == "sensitive"), key=lambda w: w.id)
        pol = [w for w in ctx.held if w.hold_kind == "policy"]
        out = []
        if sens:
            ids = [clean(w.id) for w in sens]
            shown = ", ".join(ids[:MAX_HELD_IDS]) + (f", and {len(ids) - MAX_HELD_IDS} more" if len(ids) > MAX_HELD_IDS else "")
            reasons = Counter(clean(w.reason) for w in sens)
            why = ", ".join(f"{r} x{n}" for r, n in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])))
            out.append(
                f"- Sensitive, never read or sent: {plural(len(sens), 'item')} (ids {shown}; reasons: {why}). "
                "Run `jarvis held` in a terminal."
            )
        else:
            out.append("- Sensitive, never read or sent: 0 items.")
        out.append(
            f"- Policy (work metadata to Claude disabled): {plural(len(pol), 'item')}"
            + (", rendered above without summary." if pol else ".")
        )
        status = ctx.claude_status
        if status == "ok" or ctx.summary is not None:
            unavailable = "none"
        elif status == "no_items":
            unavailable = "not called, nothing was cleared to send"
        else:
            unavailable = clean(status.replace("_", " "))
        out.append(f"- Over size cap: {plural(len(set(ctx.over_cap)), 'item')}. Claude unavailable: {unavailable}.")
        return out


class SourceStatus(Section):
    name = "source_status"
    heading = "Source status"

    def body(self, ctx: DigestContext) -> list[str]:
        parts = []
        for name, label in source_labels(ctx):
            parts.append(f"{clean(name)} {label}")
        return ["- " + ", ".join(parts) + "."]


class FlagMistake(Section):
    name = "flag_mistake"
    heading = "Flag a mistake"

    def body(self, ctx: DigestContext) -> list[str]:
        return [
            '- `jarvis wrong <id> --should {escalate,hold,skip,other} --note "..."`; '
            "`--leak` if something sensitive was shown or sent."
        ]


class ClickUp(Section):
    """Open ClickUp tasks (plan T13). Absent unless the collector ran; not in DEFAULT_SECTIONS, so
    it slots in before the footer. Links are built from the validated task id, never from model text."""

    name = "clickup"
    heading = "ClickUp: open tasks"

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
            out.append(f"- [{task_id}](https://app.clickup.com/t/{task_id}) {clip(it.title, 160)}, status {status}, "
                       f"{_due_text(m)} [{it.id}]{v.oneliner(it.id)}")
        if res.facts.get("partial"):
            out.append(f"- ClickUp list is partial ({clean(res.facts.get('partial_reason') or 'unknown')}).")
        if not out:
            out.append("- No open ClickUp tasks in the window.")
        return out


for _cls in (StartHere, ActiveTask, Brain, Repos, System, Held, SourceStatus, FlagMistake, ClickUp):
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
