"""GitHub collector (plan P2): read-only PR, CI and stale-branch facts through the `gh` CLI.

For each repo in `[digest].repos` that has a GitHub remote the active `gh` account can read:
open pull requests authored by the user or waiting for the user's review, the latest
workflow run on the default branch, and how many branches have not moved in 30 days.

Only four command shapes can run (see `validate_args`); `pr merge`, `repo delete`, `api -X`,
`auth token` and every other verb raise `GhNotAllowed` before a process is started. The one
`api` call is a fixed GraphQL query, never a mutation. The child environment is an
allowlist, not the inherited one (the git collector drops `GIT_*`; this one keeps only what
`gh` needs to find its own login), and prompts and update checks are switched off.

Access is a fact, not an error. A repo the active account cannot read, a folder with no
GitHub remote, a missing `gh` binary and a timeout each become a state in `facts["states"]`
and the collector still returns `ok=True`. `gh`'s stderr is classified and dropped: it can
carry the owner and repository name, and neither is ever printed (only the configured
`name`, as in the git section).

What it keeps: counts, a CI status token, PR numbers and titles. Never PR bodies, diffs,
branch names, workflow names or author names. `counts_only` repos emit numbers and nothing
else. PR titles pass `text_hit`; a hit withholds that title (it still counts) and leaves a
content-free reference. A repo under a sensitive path is never listed, and one under a
`[paths].vault_forbidden` root is `work` by location, exactly as for git items.

Limits:
- Stale means "last commit older than 30 days", read from the first 100 branches only. With
  more than 100 branches the count is a floor and `stale_is_floor` says so.
- The active `gh` account is whatever `gh auth status` marks active. A repo that belongs to
  another logged-in account shows as no access; this collector never switches accounts and
  never reads another account's token.
- A `gh` process that outlives its timeout is killed; a grandchild it spawned (a credential
  helper) is not. The four allowed commands do not run hooks.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any, NamedTuple

from jarvisd.collectors import CollectContext, withheld_ref
from jarvisd.common import parse_iso, short_id
from jarvisd.config import RepoCfg
from jarvisd.models import CollectResult, Item, WithheldItem
from jarvisd.tier import forbidden_hit, path_hit, text_hit

DEFAULT_TIMEOUT_S = 20.0
# The runner around every collector gives up at 60 s. Repos not started by then are reported
# as timed out instead of letting the whole source fail.
DEFAULT_BUDGET_S = 45.0
MAX_WORKERS = 4
STALE_DAYS = 30
TITLE_CHARS = 160
BRANCH_PAGE = 100
FAILING_CI = frozenset({"failure", "timed_out", "startup_failure", "action_required"})

# States that mean "the repo could not be read here", each with a fixed reason in the renderer.
UNREADABLE_STATES = ("no_access", "no_remote", "no_auth", "ambiguous_remote", "gh_missing", "timeout", "unreadable")

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$")
_TOKEN = re.compile(r"[^a-z0-9_]+")

_PR_FIELDS = frozenset({"number", "title", "isDraft", "updatedAt", "reviewDecision"})
_RUN_FIELDS = frozenset({"status", "conclusion", "workflowName", "createdAt", "event", "updatedAt"})
_VIEW = ("repo", "view", "--json", "nameWithOwner,defaultBranchRef")
PR_JSON = "number,title,isDraft,updatedAt"
RUN_JSON = "status,conclusion,createdAt"
# One line on purpose: it is an argv item. A query, not a mutation, and not configurable.
BRANCH_QUERY = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
    f'refs(first:{BRANCH_PAGE},refPrefix:"refs/heads/")'
    "{totalCount pageInfo{hasNextPage} nodes{name target{... on Commit{committedDate}}}}}}"
)

# Environment names `gh` may need. Anything else, an API key included, stays out of the child.
_ENV_KEEP = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "TEMP", "TMP", "USERNAME", "XDG_CONFIG_HOME",
    "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
})


class GhNotAllowed(Exception):
    """The requested gh invocation is not on the read-only allowlist."""


class GhResult(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


# argv (starting with "gh"), cwd, env, timeout.
Runner = Callable[[list[str], Path, dict[str, str], float], GhResult]


def _fields_ok(value: str, allowed: frozenset[str]) -> bool:
    parts = value.split(",")
    return bool(parts) and all(p in allowed for p in parts)


def _limit_ok(value: str, top: int) -> bool:
    return value.isdigit() and 0 < int(value) <= top


def validate_args(args: Sequence[str]) -> None:
    """Raise GhNotAllowed unless args (without the leading "gh") is exactly one allowed shape."""
    shape = tuple(args)
    if shape == _VIEW:
        return
    if len(shape) == 12 and shape[:2] == ("pr", "list") and shape[2] == "-R" and _SLUG.match(shape[3]):
        if (
            shape[4:6] == ("--state", "open")
            and shape[6:8] in (("--author", "@me"), ("--search", "review-requested:@me"))
            and shape[8] == "--limit"
            and _limit_ok(shape[9], 50)
            and shape[10] == "--json"
            and _fields_ok(shape[11], _PR_FIELDS)
        ):
            return
    if (
        len(shape) == 10
        and shape[:2] == ("run", "list")
        and shape[2] == "-R"
        and _SLUG.match(shape[3])
        and shape[4] == "--branch"
        and _BRANCH.match(shape[5])
        and shape[6] == "--limit"
        and _limit_ok(shape[7], 20)
        and shape[8] == "--json"
        and _fields_ok(shape[9], _RUN_FIELDS)
    ):
        return
    if (
        len(shape) == 8
        and shape[:4] == ("api", "graphql", "-f", "query=" + BRANCH_QUERY)
        and shape[4] == "-f"
        and shape[5].startswith("owner=")
        and _OWNER.match(shape[5][6:])
        and shape[6] == "-f"
        and shape[7].startswith("name=")
        and _NAME.match(shape[7][5:])
    ):
        return
    verb = " ".join(shape[:2])
    raise GhNotAllowed(f"gh {verb!r} with these arguments is not on the read-only allowlist")


def gh_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Allowlisted slice of the environment, plus switches that keep `gh` non-interactive."""
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items() if k.upper() in _ENV_KEEP}
    env.update(
        {
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_SPINNER_DISABLED": "1",
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
            "NO_COLOR": "1",
            "CLICOLOR": "0",
        }
    )
    return env


def _default_runner(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> GhResult:
    exe = shutil.which("gh", path=env.get("PATH") or env.get("Path"))
    if exe is None:
        raise FileNotFoundError("gh")
    proc = subprocess.run(  # noqa: S603  list argv, no shell, allowlisted shapes only
        [exe, *argv[1:]],
        cwd=cwd,
        env=env,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return GhResult(
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def run_gh(
    cwd: Path | str,
    args: Sequence[str],
    timeout: float = DEFAULT_TIMEOUT_S,
    *,
    runner: Runner | None = None,
) -> GhResult:
    """Run one allowlisted gh command from `cwd`. May raise GhNotAllowed, TimeoutExpired, OSError."""
    validate_args(args)
    return (runner or _default_runner)(["gh", *args], Path(cwd), gh_env(), timeout)


# --- parsing ---------------------------------------------------------------------------


def classify_failure(stderr: str) -> str:
    """Map gh's stderr to a state. The text itself is dropped: it can name the owner and repo."""
    text = stderr.lower()
    if "no git remotes found" in text or "none of the git remotes" in text:
        return "no_remote"
    if "set-default" in text or "multiple remotes" in text:
        return "ambiguous_remote"
    if "gh auth login" in text or "http 401" in text or "bad credentials" in text or "not logged in" in text:
        return "no_auth"
    if any(s in text for s in ("could not resolve to a repository", "http 404", "not found", "http 403",
                               "saml", "forbidden", "resource not accessible", "must have admin", "no access")):
        return "no_access"
    return "unreadable"


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def parse_view(stdout: str) -> tuple[str, str] | None:
    """(owner/name, default branch) from `gh repo view`, or None for anything unexpected."""
    data = _json(stdout)
    if not isinstance(data, dict):
        return None
    slug = data.get("nameWithOwner")
    ref = data.get("defaultBranchRef")
    branch = ref.get("name") if isinstance(ref, dict) else None
    if not (isinstance(slug, str) and _SLUG.match(slug) and isinstance(branch, str) and _BRANCH.match(branch)):
        return None
    return slug, branch


class _Pr(NamedTuple):
    number: int
    title: str
    draft: bool


def parse_prs(stdout: str) -> list[_Pr] | None:
    """Valid pull requests from a `gh pr list` reply. None means the reply was not a list."""
    data = _json(stdout)
    if not isinstance(data, list):
        return None
    out: list[_Pr] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        number, title = entry.get("number"), entry.get("title")
        if isinstance(number, bool) or not isinstance(number, int) or not isinstance(title, str):
            continue
        out.append(_Pr(number, title, entry.get("isDraft") is True))
    return out


def parse_ci(stdout: str) -> str | None:
    """A lowercase status token for the latest run: success, failure, in_progress, none, ..."""
    data = _json(stdout)
    if not isinstance(data, list):
        return None
    if not data:
        return "none"
    run = data[0]
    if not isinstance(run, dict):
        return None
    status = str(run.get("status") or "").lower()
    conclusion = str(run.get("conclusion") or "").lower()
    token = conclusion if status == "completed" and conclusion else status
    return _TOKEN.sub("_", token).strip("_") or "unknown"


class _Stale(NamedTuple):
    count: int
    is_floor: bool


def parse_stale(stdout: str, default_branch: str, now: Any) -> _Stale | None:
    """Branches (other than the default) whose last commit is older than STALE_DAYS."""
    data = _json(stdout)
    try:
        refs = data["data"]["repository"]["refs"]
        nodes = refs["nodes"]
        more = bool(refs["pageInfo"]["hasNextPage"])
    except (TypeError, KeyError):
        return None
    if not isinstance(nodes, list):
        return None
    cutoff = now - timedelta(days=STALE_DAYS)
    stale = 0
    for node in nodes:
        if not isinstance(node, dict) or node.get("name") == default_branch:
            continue
        target = node.get("target")
        stamp = target.get("committedDate") if isinstance(target, dict) else None
        try:
            if isinstance(stamp, str) and parse_iso(stamp) < cutoff:
                stale += 1
        except ValueError:
            continue
    return _Stale(stale, more)


# --- the collector ---------------------------------------------------------------------


class _Repo(NamedTuple):
    state: str
    item: Item | None
    withheld: list[WithheldItem]
    summary: dict[str, Any]
    quiet: bool


def _outcome(state: str, withheld: list[WithheldItem] | None = None) -> _Repo:
    return _Repo(state, None, withheld or [], {"state": state}, False)


class GitHubCollector:
    """Collector named `github`. `runner` is injectable so tests never start `gh`."""

    name = "github"

    def __init__(self, runner: Runner | None = None, timeout: float | None = None,
                 budget_s: float = DEFAULT_BUDGET_S) -> None:
        self.runner = runner
        self.timeout = timeout
        self.budget_s = budget_s

    def collect(self, ctx: CollectContext) -> CollectResult:
        cfg = ctx.cfg.digest.github
        if not cfg.enabled:
            return CollectResult(source=self.name, ok=True, facts={"disabled": True})
        repos = list(ctx.cfg.digest.repos)
        deadline = time.monotonic() + self.budget_s
        if repos:
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(repos)), thread_name_prefix="gh") as pool:
                outcomes = list(pool.map(lambda r: self._one(ctx, r, deadline), repos))
        else:
            outcomes = []
        items: list[Item] = []
        withheld: list[WithheldItem] = []
        states: dict[str, str] = {}
        summary: dict[str, dict[str, Any]] = {}
        quiet: list[str] = []
        for repo, out in zip(repos, outcomes):
            states[repo.name] = out.state
            withheld.extend(out.withheld)
            if out.item is not None:
                items.append(out.item)
            if out.state == "ok":
                summary[repo.name] = out.summary
            if out.quiet:
                quiet.append(repo.name)
        facts = {
            "repos_total": len(repos),
            "repos_read": sum(1 for s in states.values() if s == "ok"),
            "no_access": [n for n, s in states.items() if s == "no_access"],
            "quiet": quiet,
            "states": states,
            "summary": summary,
        }
        return CollectResult(source=self.name, ok=True, items=items, withheld=withheld, facts=facts)

    def _gh(self, ctx: CollectContext, cwd: Path, args: Sequence[str]) -> GhResult:
        timeout = self.timeout if self.timeout is not None else ctx.cfg.digest.github.timeout_s
        return run_gh(cwd, args, timeout, runner=self.runner)

    def _one(self, ctx: CollectContext, repo: RepoCfg, deadline: float) -> _Repo:
        path = Path(repo.path)
        if not path.is_dir():
            return _outcome("missing")
        hit = path_hit(path, ctx.cfg)
        if hit is not None:
            # Gate 1 before any read: not even a gh call is made for a sensitive location.
            return _outcome("withheld_path", [withheld_ref("github_repo", str(path), hit.code)])
        if not (path / ".git").exists():
            return _outcome("not_a_repo")
        if time.monotonic() >= deadline:
            return _outcome("timeout")
        work = repo.work or forbidden_hit(path, ctx.cfg) is not None
        try:
            return self._inspect(ctx, repo, path, work)
        except subprocess.TimeoutExpired:
            return _outcome("timeout")
        except FileNotFoundError:
            return _outcome("gh_missing")
        except (OSError, GhNotAllowed):
            return _outcome("unreadable")

    def _inspect(self, ctx: CollectContext, repo: RepoCfg, path: Path, work: bool) -> _Repo:
        cfg = ctx.cfg
        limit = str(cfg.digest.github.max_prs)
        view = self._gh(ctx, path, _VIEW)
        if view.returncode != 0:
            return _outcome(classify_failure(view.stderr))
        parsed = parse_view(view.stdout)
        if parsed is None:
            return _outcome("unreadable")
        slug, branch = parsed
        unknown: list[str] = []

        mine_run = self._gh(ctx, path, ("pr", "list", "-R", slug, "--state", "open", "--author", "@me",
                                        "--limit", limit, "--json", PR_JSON))
        theirs_run = self._gh(ctx, path, ("pr", "list", "-R", slug, "--state", "open",
                                          "--search", "review-requested:@me", "--limit", limit, "--json", PR_JSON))
        mine = parse_prs(mine_run.stdout) if mine_run.returncode == 0 else None
        theirs = parse_prs(theirs_run.stdout) if theirs_run.returncode == 0 else None
        if mine is None or theirs is None:
            unknown.append("prs")
        mine, theirs = mine or [], theirs or []
        seen = {p.number for p in mine}
        theirs = [p for p in theirs if p.number not in seen]

        ci_run = self._gh(ctx, path, ("run", "list", "-R", slug, "--branch", branch, "--limit", "1",
                                      "--json", RUN_JSON))
        ci = parse_ci(ci_run.stdout) if ci_run.returncode == 0 else None
        if ci is None:
            ci = "unknown"
            unknown.append("ci")

        owner, _, name = slug.partition("/")
        stale_run = self._gh(ctx, path, ("api", "graphql", "-f", "query=" + BRANCH_QUERY,
                                         "-f", f"owner={owner}", "-f", f"name={name}"))
        stale = parse_stale(stale_run.stdout, branch, ctx.now) if stale_run.returncode == 0 else None
        if stale is None:
            unknown.append("stale")

        withheld: list[WithheldItem] = []
        lines: list[str] = []
        held = 0
        for label, prs in (("review requested", theirs), ("yours", mine)):
            for pr in prs:
                hit = text_hit(pr.title, cfg)
                if hit is not None:
                    held += 1
                    withheld.append(withheld_ref("github_pr", f"{repo.name}:{short_id(pr.number, pr.title)}", hit.code))
                    continue
                tag = f"{label}, draft" if pr.draft and label == "yours" else label
                title = " ".join(pr.title.split())[:TITLE_CHARS]
                lines.append(f"PR #{pr.number} ({tag}): {title}")

        summary: dict[str, Any] = {
            "state": "ok",
            "authored": len(mine),
            "review_requested": len(theirs),
            "ci": ci,
            "stale_branches": stale.count if stale is not None else None,
            "stale_is_floor": stale.is_floor if stale is not None else False,
        }
        news = bool(mine or theirs) or ci in FAILING_CI
        if not news:
            return _Repo("ok", None, withheld, summary, True)
        item = Item(
            id=short_id("github", repo.name, *(p.number for p in mine), "r", *(p.number for p in theirs), ci),
            source="github",
            kind="github_repo",
            title=repo.name,
            text="" if repo.counts_only else "\n".join(lines),
            work=work,
            priority=1 if (theirs or ci in FAILING_CI) else 2,
            meta={
                "repo": repo.name,
                "authored": len(mine),
                "review_requested": len(theirs),
                "prs_withheld": 0 if repo.counts_only else held,
                "ci": ci,
                "ci_branch": "" if repo.counts_only else branch,
                "stale_branches": summary["stale_branches"] or 0,
                "stale_is_floor": summary["stale_is_floor"],
                "unknown": unknown,
                "counts_only": repo.counts_only,
            },
        )
        return _Repo("ok", item, withheld, summary, False)
