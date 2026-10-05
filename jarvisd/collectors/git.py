"""Git collector (design section 8, decision D6): read-only metadata for an explicit repo list.

Only four command shapes can run (see `validate_args`); `fetch`, `pull`, `checkout`,
`config`, `gc` and every other verb raise `GitNotAllowed` before a process is started. Every
run carries `--no-optional-locks` and `GIT_OPTIONAL_LOCKS=0`, so `git status` never rewrites
`.git/index` (a test pins the index mtime). The child environment drops every inherited
`GIT_*` variable (a stray `GIT_DIR` would point git at another repository) and sets
`core.fsmonitor=false` through `GIT_CONFIG_COUNT`, so a repo's own config cannot make
`git status` run a program of its choosing.

What it keeps: branch, counts and commit subjects. Never diffs, file contents, or file
names (names are used only to filter untracked noise and are then dropped). `counts_only`
repos emit numbers and nothing else. Commit subjects and branch names pass `text_hit`; a
hit withholds that subject (it still counts) and leaves a content-free reference.

A repo under a `[paths].vault_forbidden` root (Documents/Work) is marked `work` by its
location, whatever its `work` flag says, so the D6 opt-in (`work_metadata_to_claude`) is the
only way its subjects reach Claude and a forgotten flag fails closed.

Limits:
- A git process that outlives its timeout is killed, but a grandchild it spawned (a hook or
  credential helper) is not. The four allowed commands do not run hooks.
- "dubious ownership" and similar refusals show up as an `error` state for that repo, never
  as a failure of the whole source.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from jarvisd.collectors import CollectContext, withheld_ref
from jarvisd.common import iso, short_id
from jarvisd.config import RepoCfg
from jarvisd.models import CollectResult, Item, WithheldItem
from jarvisd.tier import forbidden_hit, path_hit, text_hit

DEFAULT_TIMEOUT_S = 20.0
MAX_SUBJECTS_SHOWN = 8
SUBJECT_CHARS = 160
LOG_LIMIT = 30


class GitNotAllowed(Exception):
    """The requested git invocation is not on the read-only allowlist."""


class GitResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


# argv (without the leading "git" and "--no-optional-locks"), cwd, env, timeout.
Runner = Callable[[list[str], Path, dict[str, str], float], GitResult]

_SINCE = re.compile(r"^--since=[0-9TZ:+.\-]+$")
_LOG_FORMAT = "--format=%h%x1f%aI%x1f%s"
_EXACT: tuple[tuple[str, ...], ...] = (
    ("status", "--porcelain=v1", "-b", "--untracked-files=normal"),
    ("rev-parse", "--abbrev-ref", "HEAD"),
    ("rev-list", "--left-right", "--count", "@{u}...HEAD"),
)


def validate_args(args: Sequence[str]) -> None:
    """Raise GitNotAllowed unless args is exactly one of the four allowed shapes."""
    shape = tuple(args)
    if shape in _EXACT:
        return
    if (
        len(shape) == 7
        and shape[:3] == ("log", "--all", "--no-merges")
        and _SINCE.match(shape[3])
        and shape[4] == _LOG_FORMAT
        and shape[5] == "-n"
        and shape[6].isdigit()
        and 0 < int(shape[6]) <= 100
    ):
        return
    verb = shape[0] if shape else ""
    raise GitNotAllowed(f"git {verb!r} with these arguments is not on the read-only allowlist")


def git_env() -> dict[str, str]:
    """Inherited environment minus every GIT_*, plus the read-only settings."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
            "LC_ALL": "C",
        }
    )
    return env


def _default_runner(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> GitResult:
    proc = subprocess.run(  # noqa: S603  list argv, no shell, allowlisted shapes only
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return GitResult(
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def run_git(
    repo: Path | str,
    args: Sequence[str],
    timeout: float = DEFAULT_TIMEOUT_S,
    *,
    runner: Runner | None = None,
) -> GitResult:
    """Run one allowlisted git command in `repo`. May raise GitNotAllowed, TimeoutExpired, OSError."""
    validate_args(args)
    argv = ["git", "--no-optional-locks", *args]
    return (runner or _default_runner)(argv, Path(repo), git_env(), timeout)


# --- parsing ---------------------------------------------------------------------------


class _Status(NamedTuple):
    branch: str
    has_upstream: bool
    ahead: int
    behind: int
    modified: int
    untracked: int


_HEADER_COUNTS = re.compile(r"\[(?:ahead (\d+))?(?:, )?(?:behind (\d+))?\]")


def _is_noise(path: str, patterns: Sequence[str]) -> bool:
    parts = path.strip().strip('"').replace("\\", "/").rstrip("/").split("/")
    folded = [p.casefold() for p in patterns if p]
    return any(fnmatch.fnmatchcase(part.casefold(), pat) for part in parts for pat in folded)


def parse_status(stdout: str, ignore: Sequence[str]) -> _Status:
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    has_header = bool(lines) and lines[0].startswith("## ")
    header = lines[0][3:] if has_header else ""
    body = lines[1:] if has_header else lines
    branch = header
    has_upstream = False
    ahead = behind = 0
    if header.startswith("No commits yet on "):
        branch = header[len("No commits yet on "):]
    elif header.startswith("Initial commit on "):
        branch = header[len("Initial commit on "):]
    elif "..." in header:
        branch, _, rest = header.partition("...")
        has_upstream = "[gone]" not in rest
        counts = _HEADER_COUNTS.search(rest)
        if counts:
            ahead, behind = int(counts.group(1) or 0), int(counts.group(2) or 0)
    elif header.startswith("HEAD (no branch)"):
        branch = "HEAD"
    modified = untracked = 0
    for line in body:
        code, path = line[:2], line[3:]
        if code == "??":
            if not _is_noise(path, ignore):
                untracked += 1
        elif code != "!!":
            modified += 1
    return _Status(branch.strip(), has_upstream, ahead, behind, modified, untracked)


def parse_log(stdout: str) -> list[str]:
    subjects = []
    for line in stdout.splitlines():
        parts = line.split("\x1f", 2)
        if len(parts) == 3:
            subjects.append(parts[2].strip())
    return subjects


def parse_left_right(stdout: str) -> tuple[int, int] | None:
    """`rev-list --left-right --count @{u}...HEAD` prints 'behind<TAB>ahead'."""
    fields = stdout.split()
    if len(fields) == 2 and all(f.isdigit() for f in fields):
        return int(fields[1]), int(fields[0])
    return None


# --- the collector ---------------------------------------------------------------------


class _RepoOutcome(NamedTuple):
    state: str
    item: Item | None
    withheld: list[WithheldItem]
    quiet: bool


class GitCollector:
    """Collector named `git`. `runner` is injectable so tests can record argv and env."""

    name = "git"

    def __init__(self, runner: Runner | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.runner = runner
        self.timeout = timeout

    def collect(self, ctx: CollectContext) -> CollectResult:
        items: list[Item] = []
        withheld: list[WithheldItem] = []
        states: dict[str, str] = {}
        quiet: list[str] = []
        not_repos: list[str] = []
        errors: list[str] = []
        for repo in ctx.cfg.digest.repos:
            outcome = self._one(ctx, repo)
            states[repo.name] = outcome.state
            withheld.extend(outcome.withheld)
            if outcome.item is not None:
                items.append(outcome.item)
            if outcome.quiet:
                quiet.append(repo.name)
            if outcome.state in ("missing", "not_a_repo"):
                not_repos.append(repo.name)
            elif outcome.state != "ok":
                errors.append(repo.name)
        facts = {
            "repos_total": len(ctx.cfg.digest.repos),
            "repos_active": len(items),
            "quiet": quiet,
            "not_repos": not_repos,
            "errors": errors,
            "states": states,
        }
        return CollectResult(source=self.name, ok=True, items=items, withheld=withheld, facts=facts)

    def _git(self, repo: Path, args: Sequence[str]) -> GitResult:
        return run_git(repo, args, self.timeout, runner=self.runner)

    def _one(self, ctx: CollectContext, repo: RepoCfg) -> _RepoOutcome:
        path = Path(repo.path)
        if not path.is_dir():
            return _RepoOutcome("missing", None, [], False)
        hit = path_hit(path, ctx.cfg)
        if hit is not None:
            ref = withheld_ref("git_repo", str(path), hit.code)
            return _RepoOutcome("withheld_path", None, [ref], False)
        if not (path / ".git").exists():
            # A plain folder inside another repo would otherwise report the parent repo.
            return _RepoOutcome("not_a_repo", None, [], False)
        # D6: a repo under a forbidden root (Documents/Work) is work by location, so a missing
        # or mistyped `work = true` can only make it stricter, never let it past the policy.
        work = repo.work or forbidden_hit(path, ctx.cfg) is not None
        try:
            return self._inspect(ctx, repo, path, work)
        except subprocess.TimeoutExpired:
            return _RepoOutcome("error:timeout", None, [], False)
        except OSError:
            return _RepoOutcome("error:oserror", None, [], False)

    def _inspect(self, ctx: CollectContext, repo: RepoCfg, path: Path, work: bool) -> _RepoOutcome:
        cfg = ctx.cfg
        status_run = self._git(path, ("status", "--porcelain=v1", "-b", "--untracked-files=normal"))
        if status_run.returncode != 0:
            return _RepoOutcome(f"error:status_rc{status_run.returncode}", None, [], False)
        status = parse_status(status_run.stdout, cfg.digest.dirty_ignore)

        branch = status.branch
        head = self._git(path, ("rev-parse", "--abbrev-ref", "HEAD"))
        if head.returncode == 0 and head.stdout.strip():
            branch = head.stdout.strip()

        log_run = self._git(
            path,
            ("log", "--all", "--no-merges", f"--since={iso(ctx.window_start)}", _LOG_FORMAT, "-n", str(LOG_LIMIT)),
        )
        subjects = parse_log(log_run.stdout) if log_run.returncode == 0 else []

        ahead, behind = status.ahead, status.behind
        if status.has_upstream:
            counted = self._git(path, ("rev-list", "--left-right", "--count", "@{u}...HEAD"))
            parsed = parse_left_right(counted.stdout) if counted.returncode == 0 else None
            if parsed is not None:
                ahead, behind = parsed

        withheld: list[WithheldItem] = []
        shown: list[str] = []
        held_subjects = 0
        for subject in subjects:
            hit = text_hit(subject, cfg)
            if hit is not None:
                held_subjects += 1
                withheld.append(withheld_ref("git_commit", f"{repo.name}:{short_id(subject)}", hit.code))
            elif len(shown) < MAX_SUBJECTS_SHOWN:
                shown.append(subject[:SUBJECT_CHARS])
        branch_hit = text_hit(branch, cfg)
        if branch_hit is not None:
            withheld.append(withheld_ref("git_branch", f"{repo.name}:{short_id(branch)}", branch_hit.code))
            branch = "(withheld)"

        commits = len(subjects)
        is_quiet = not (commits or status.modified or status.untracked or ahead or behind)
        if is_quiet:
            return _RepoOutcome("ok", None, withheld, True)
        return _RepoOutcome("ok", self._item(repo, work, branch, commits, held_subjects, shown, status, ahead, behind), withheld, False)

    @staticmethod
    def _item(
        repo: RepoCfg,
        work: bool,
        branch: str,
        commits: int,
        held_subjects: int,
        shown: list[str],
        status: _Status,
        ahead: int,
        behind: int,
    ) -> Item:
        counts_only = repo.counts_only
        return Item(
            id=short_id("git", repo.name, commits, status.modified, status.untracked, ahead, behind, *shown),
            source="git",
            kind="git_repo",
            title=repo.name,
            text="" if counts_only else "\n".join(shown),
            work=work,
            priority=2 if commits else 3,
            meta={
                "repo": repo.name,
                "branch": "" if counts_only else branch,
                "commits": commits,
                "commits_withheld": 0 if counts_only else held_subjects,
                "modified": status.modified,
                "untracked": status.untracked,
                "ahead": ahead,
                "behind": behind,
                "counts_only": counts_only,
            },
        )

