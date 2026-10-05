"""GitHub collector (plan P2): read-only `gh` calls, driven through a fake runner.

No network and no real `gh` process: every call goes through `FakeGh`, which answers from a
per-repo script and records argv, cwd, env and timeout. Names below are synthetic.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from jarvisd.collectors import CollectContext
from jarvisd.collectors.github import (
    GhNotAllowed,
    GhResult,
    GitHubCollector,
    gh_env,
    run_gh,
    validate_args,
)
from jarvisd.config import Config, RepoCfg
from jarvisd.models import CollectResult
from jarvisd.tier import item_hit

NOW = datetime(2026, 10, 5, 6, 30, tzinfo=timezone.utc)
SLUG = "example-org/example-api"


def cfg_with(base: Config, *repos: RepoCfg, **github: Any) -> Config:
    cfg = base.model_copy(deep=True)
    cfg.digest.repos = list(repos)
    cfg.digest.github.enabled = True
    for key, value in github.items():
        setattr(cfg.digest.github, key, value)
    return cfg


def ctx_for(cfg: Config) -> CollectContext:
    return CollectContext(cfg=cfg, window_start=NOW - timedelta(hours=36), window_end=NOW, now=NOW)


def make_dir(path: Path, *, git: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if git:
        (path / ".git").mkdir(exist_ok=True)
    return path


def graphql(*branches: tuple[str, str], more: bool = False) -> str:
    nodes = [{"name": n, "target": {"committedDate": d}} for n, d in branches]
    refs = {"totalCount": len(nodes), "pageInfo": {"hasNextPage": more}, "nodes": nodes}
    return json.dumps({"data": {"repository": {"refs": refs}}})


class FakeGh:
    """Answers `gh` calls from a script. Anything unscripted is a test failure, not a silent pass."""

    def __init__(
        self,
        *,
        view: GhResult | BaseException | None = None,
        authored: list[dict[str, Any]] | None = None,
        review: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        branches: str | None = None,
        fail: dict[str, GhResult | BaseException] | None = None,
    ) -> None:
        self.view = view if view is not None else GhResult(
            0, json.dumps({"nameWithOwner": SLUG, "defaultBranchRef": {"name": "main"}}), "")
        self.authored = authored or []
        self.review = review or []
        self.runs = runs or []
        self.branches = branches if branches is not None else graphql(("main", "2026-10-04T08:00:00Z"))
        self.fail = fail or {}
        self.calls: list[tuple[list[str], Path, dict[str, str], float]] = []

    def __call__(self, argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> GhResult:
        self.calls.append((list(argv), cwd, dict(env), timeout))
        verb = " ".join(argv[1:3])
        key = verb
        if verb == "pr list":
            key = "pr review" if "--search" in argv else "pr author"
        if key in self.fail:
            out = self.fail[key]
            if isinstance(out, BaseException):
                raise out
            return out
        if verb == "repo view":
            if isinstance(self.view, BaseException):
                raise self.view
            return self.view
        if key == "pr author":
            return GhResult(0, json.dumps(self.authored), "")
        if key == "pr review":
            return GhResult(0, json.dumps(self.review), "")
        if verb == "run list":
            return GhResult(0, json.dumps(self.runs), "")
        if verb == "api graphql":
            return GhResult(0, self.branches, "")
        raise AssertionError(f"unscripted gh call: {argv}")

    def verbs(self) -> list[str]:
        return [" ".join(c[0][1:3]) for c in self.calls]


def collect(cfg: Config, fake: FakeGh) -> CollectResult:
    return GitHubCollector(runner=fake).collect(ctx_for(cfg))


def pr(number: int, title: str, *, draft: bool = False) -> dict[str, Any]:
    return {"number": number, "title": title, "isDraft": draft, "updatedAt": "2026-10-04T12:00:00Z"}


# --- the allowlist --------------------------------------------------------------------------


GOOD_SHAPES = [
    ["repo", "view", "--json", "nameWithOwner,defaultBranchRef"],
    ["pr", "list", "-R", SLUG, "--state", "open", "--author", "@me", "--limit", "10",
     "--json", "number,title,isDraft,updatedAt"],
    ["pr", "list", "-R", SLUG, "--state", "open", "--search", "review-requested:@me", "--limit", "10",
     "--json", "number,title,isDraft,updatedAt"],
    ["run", "list", "-R", SLUG, "--branch", "main", "--limit", "1", "--json", "status,conclusion,createdAt"],
]


@pytest.mark.parametrize("args", GOOD_SHAPES)
def test_allowlisted_shapes_pass(args: list[str]) -> None:
    validate_args(args)


@pytest.mark.parametrize("args", [
    ["pr", "merge", "12"],
    ["pr", "close", "12", "-R", SLUG],
    ["repo", "delete", SLUG],
    ["repo", "view", SLUG, "--json", "nameWithOwner,defaultBranchRef"],
    ["auth", "token"],
    ["api", "repos/example-org/example-api/branches"],
    ["api", "-X", "POST", "graphql", "-f", "query=mutation{x}"],
    ["api", "graphql", "-f", "query=mutation { addStar }", "-F", "owner=o", "-F", "name=n"],
    ["pr", "list", "-R", SLUG, "--state", "open", "--author", "@me", "--limit", "10",
     "--json", "number,title,body"],
    ["pr", "list", "-R", "../evil", "--state", "open", "--author", "@me", "--limit", "10",
     "--json", "number,title,isDraft,updatedAt"],
    ["pr", "list", "-R", SLUG, "--state", "open", "--author", "someone", "--limit", "10",
     "--json", "number,title,isDraft,updatedAt"],
    ["pr", "list", "-R", SLUG, "--state", "open", "--author", "@me", "--limit", "9999",
     "--json", "number,title,isDraft,updatedAt"],
    ["run", "list", "-R", SLUG, "--branch", "--upload-pack=evil", "--limit", "1", "--json", "status"],
    [],
])
def test_everything_else_is_refused_before_a_process_starts(args: list[str]) -> None:
    with pytest.raises(GhNotAllowed):
        validate_args(args)
    ran: list[Any] = []
    with pytest.raises(GhNotAllowed):
        run_gh(Path("."), args, runner=lambda *a: ran.append(a) or GhResult(0, "", ""))
    assert ran == []


def test_env_is_an_allowlist_and_disables_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-pass")
    monkeypatch.setenv("SOME_OTHER_SECRET", "should-not-pass")
    monkeypatch.setenv("GH_TOKEN", "token-for-gh-itself")
    monkeypatch.setenv("GIT_DIR", "elsewhere")
    env = gh_env()
    assert "ANTHROPIC_API_KEY" not in env and "SOME_OTHER_SECRET" not in env and "GIT_DIR" not in env
    assert env["GH_TOKEN"] == "token-for-gh-itself"  # gh needs it when the keyring is not used
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert env["NO_COLOR"] == "1"
    assert "PATH" in env or "Path" in env


# --- happy path -----------------------------------------------------------------------------


def test_one_repo_with_prs_ci_and_stale_branches(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    old = (NOW - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake = FakeGh(
        authored=[pr(12, "feat: synthetic change", draft=True)],
        review=[pr(7, "fix: someone else's synthetic fix")],
        runs=[{"status": "completed", "conclusion": "failure", "workflowName": "CI", "createdAt": fresh}],
        branches=graphql(("main", fresh), ("old-a", old), ("old-b", old), ("topic", fresh)),
    )
    cfg = cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir))
    result = collect(cfg, fake)

    assert result.ok and result.source == "github"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.kind == "github_repo" and item.source == "github" and item.title == "example-api"
    assert item.work is False
    assert "#12" in item.text and "synthetic change" in item.text and "#7" in item.text
    assert "draft" in item.text and "review requested" in item.text
    m = item.meta
    assert (m["authored"], m["review_requested"], m["ci"], m["ci_branch"], m["stale_branches"]) == (1, 1, "failure", "main", 2)
    assert item.priority == 1  # a review request and a failing run both want attention
    assert result.facts["states"] == {"example-api": "ok"}
    assert result.facts["repos_read"] == 1 and result.facts["no_access"] == []
    assert item_hit(item, cfg) is None
    # the owner/name pair stays inside the process: neither the item nor the facts carry it
    assert "example-org" not in json.dumps(item.model_dump()) + json.dumps(result.facts)


def test_calls_are_scoped_to_the_repo_and_use_the_configured_limits(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    fake = FakeGh()
    cfg = cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir), max_prs=4, timeout_s=7)
    collect(cfg, fake)

    assert fake.verbs() == ["repo view", "pr list", "pr list", "run list", "api graphql"]
    first = fake.calls[0]
    assert first[1] == repo_dir and first[0][0] == "gh"
    for argv, cwd, env, timeout in fake.calls:
        validate_args(argv[1:])
        assert timeout == 7
        assert env["GH_PROMPT_DISABLED"] == "1"
        assert "shell" not in argv
    author = fake.calls[1][0]
    assert "-R" in author and author[author.index("-R") + 1] == SLUG
    assert author[author.index("--limit") + 1] == "4"
    run_argv = fake.calls[3][0]
    assert run_argv[run_argv.index("--branch") + 1] == "main"


def test_quiet_repo_makes_no_item(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    fake = FakeGh(runs=[{"status": "completed", "conclusion": "success", "createdAt": "2026-10-04T08:00:00Z"}])
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), fake)
    assert result.ok and result.items == []
    assert result.facts["quiet"] == ["example-api"]
    assert result.facts["summary"]["example-api"]["ci"] == "success"


def test_running_ci_alone_is_not_news(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    fake = FakeGh(runs=[{"status": "in_progress", "conclusion": "", "createdAt": "2026-10-04T08:00:00Z"}])
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), fake)
    assert result.items == [] and result.facts["summary"]["example-api"]["ci"] == "in_progress"


def test_a_repo_with_no_workflow_runs_says_none(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), FakeGh())
    assert result.facts["summary"]["example-api"]["ci"] == "none"


def test_stale_branches_exclude_the_default_branch_and_report_a_floor_when_paged(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    old = "2026-07-01T00:00:00Z"
    fake = FakeGh(authored=[pr(1, "feat: synthetic")],
                  branches=graphql(("main", old), ("a", old), ("b", "2026-10-01T00:00:00Z"), more=True))
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), fake)
    summary = result.facts["summary"]["example-api"]
    assert summary["stale_branches"] == 1 and summary["stale_is_floor"] is True
    assert result.items and result.items[0].meta["stale_is_floor"] is True


# --- access ---------------------------------------------------------------------------------


@pytest.mark.parametrize("stderr,expected", [
    ("GraphQL: Could not resolve to a Repository with the name 'example-org/example-api'. (repository)", "no_access"),
    ("HTTP 404: Not Found (https://api.github.com/repos/example-org/example-api)", "no_access"),
    ("HTTP 403: Resource protected by organization SAML enforcement", "no_access"),
    ("no git remotes found", "no_remote"),
    ("none of the git remotes configured for this repository point to a known GitHub host", "no_remote"),
    ("To get started with GitHub CLI, please run:  gh auth login", "no_auth"),
    ("HTTP 401: Bad credentials", "no_auth"),
    ("something nobody has seen before", "unreadable"),
])
def test_failures_become_facts_never_errors(tmp_cfg: Config, tmp_path: Path, stderr: str, expected: str) -> None:
    repo_dir = make_dir(tmp_path / "api")
    fake = FakeGh(view=GhResult(1, "", stderr))
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), fake)

    assert result.ok is True and result.error is None and result.items == []
    assert result.facts["states"] == {"example-api": expected}
    assert result.facts["repos_read"] == 0
    assert fake.verbs() == ["repo view"], "no further call once the repo cannot be read"
    if expected == "no_access":
        assert result.facts["no_access"] == ["example-api"]
    # stderr is never echoed: it can carry the owner and repository name
    assert "example-org" not in json.dumps(result.facts)


def test_missing_gh_binary_is_a_fact(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), FakeGh(view=FileNotFoundError("gh")))
    assert result.ok and result.facts["states"] == {"example-api": "gh_missing"}


def test_timeout_is_a_fact(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    boom = subprocess.TimeoutExpired(["gh"], 20)
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), FakeGh(view=boom))
    assert result.ok and result.facts["states"] == {"example-api": "timeout"}


def test_a_later_call_failing_marks_that_field_unknown_not_the_repo(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    fake = FakeGh(authored=[pr(3, "feat: synthetic")], fail={"run list": GhResult(1, "", "HTTP 500")})
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), fake)
    assert result.facts["states"] == {"example-api": "ok"}
    assert result.items[0].meta["ci"] == "unknown"


@pytest.mark.parametrize("view_out", [
    "not json",
    json.dumps([]),
    json.dumps({"nameWithOwner": "bad slug with spaces", "defaultBranchRef": {"name": "main"}}),
    json.dumps({"nameWithOwner": SLUG, "defaultBranchRef": {"name": "--upload-pack=evil"}}),
    json.dumps({"nameWithOwner": SLUG, "defaultBranchRef": None}),
])
def test_malformed_or_hostile_replies_are_unreadable_not_crashes(tmp_cfg: Config, tmp_path: Path, view_out: str) -> None:
    repo_dir = make_dir(tmp_path / "api")
    fake = FakeGh(view=GhResult(0, view_out, ""))
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), fake)
    assert result.ok and result.facts["states"] == {"example-api": "unreadable"}
    assert fake.verbs() == ["repo view"]


def test_garbage_in_a_list_reply_does_not_crash(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    fake = FakeGh(authored=[{"number": "x", "title": None}, "junk", pr(5, "feat: ok")])  # type: ignore[list-item]
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), fake)
    assert result.ok and result.items[0].meta["authored"] == 1


# --- the three folder cases the git collector also knows -------------------------------------


def test_missing_and_non_git_folders_make_no_call(tmp_cfg: Config, tmp_path: Path) -> None:
    plain = make_dir(tmp_path / "plain", git=False)
    fake = FakeGh()
    cfg = cfg_with(tmp_cfg, RepoCfg(name="gone", path=tmp_path / "nope"), RepoCfg(name="plain", path=plain))
    result = collect(cfg, fake)
    assert fake.calls == []
    assert result.facts["states"] == {"gone": "missing", "plain": "not_a_repo"}


def test_tier_gate_runs_before_any_gh_call(tmp_cfg: Config, tmp_path: Path) -> None:
    secret = make_dir(tmp_path / "sensitive" / "api")
    fake = FakeGh()
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=secret)), fake)
    assert fake.calls == [], "a sensitive path is never even listed"
    assert result.items == [] and len(result.withheld) == 1
    assert result.withheld[0].kind == "github_repo" and result.withheld[0].hold_kind == "sensitive"
    assert result.facts["states"] == {"example-api": "withheld_path"}


def test_pr_title_with_a_sensitive_term_is_withheld_but_still_counted(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir))
    cfg.gates.sensitive_terms = ["zebra-codename"]
    fake = FakeGh(authored=[pr(1, "feat: zebra-codename rollout"), pr(2, "fix: plain synthetic title")])
    result = collect(cfg, fake)

    item = result.items[0]
    assert "zebra-codename" not in item.text and "zebra-codename" not in json.dumps(item.model_dump())
    assert "plain synthetic title" in item.text
    assert item.meta["authored"] == 2 and item.meta["prs_withheld"] == 1
    assert [w.kind for w in result.withheld] == ["github_pr"]
    assert "zebra-codename" not in json.dumps([w.model_dump() for w in result.withheld])
    assert item_hit(item, cfg) is None


def test_work_flag_is_copied_and_location_forces_it(tmp_cfg: Config, tmp_path: Path) -> None:
    flagged = make_dir(tmp_path / "flagged")
    located = make_dir(tmp_path / "Documents" / "Work" / "located")  # a configured forbidden root
    cfg = cfg_with(
        tmp_cfg,
        RepoCfg(name="flagged", path=flagged, work=True),
        RepoCfg(name="located", path=located, work=False),
    )
    fake = FakeGh(authored=[pr(1, "feat: synthetic")])
    result = collect(cfg, fake)
    by_title = {i.title: i for i in result.items}
    assert by_title["flagged"].work is True
    assert by_title["located"].work is True, "a forgotten flag must fail closed (D6)"


def test_counts_only_repos_emit_numbers_and_nothing_else(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    fake = FakeGh(authored=[pr(1, "feat: synthetic title")], review=[pr(2, "fix: another title")])
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir, counts_only=True)), fake)
    item = result.items[0]
    assert item.text == ""
    assert "synthetic title" not in json.dumps(item.model_dump())
    assert item.meta["authored"] == 1 and item.meta["review_requested"] == 1 and item.meta["counts_only"] is True


# --- switches and robustness ------------------------------------------------------------------


def test_disabled_collector_makes_no_call_and_says_so(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir))
    cfg.digest.github.enabled = False
    fake = FakeGh()
    result = collect(cfg, fake)
    assert fake.calls == [] and result.ok and result.facts == {"disabled": True}


def test_repos_are_collected_in_config_order_and_one_failure_does_not_stop_the_rest(tmp_cfg: Config, tmp_path: Path) -> None:
    a, b, c = (make_dir(tmp_path / n) for n in ("a", "b", "c"))

    class Mixed(FakeGh):
        def __call__(self, argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> GhResult:
            if cwd.name == "b" and argv[1:3] == ["repo", "view"]:
                self.calls.append((list(argv), cwd, dict(env), timeout))
                return GhResult(1, "", "Could not resolve to a Repository")
            return super().__call__(argv, cwd, env, timeout)

    fake = Mixed(authored=[pr(1, "feat: synthetic")])
    cfg = cfg_with(tmp_cfg, RepoCfg(name="a", path=a), RepoCfg(name="b", path=b), RepoCfg(name="c", path=c))
    result = collect(cfg, fake)
    assert list(result.facts["states"]) == ["a", "b", "c"]
    assert result.facts["states"] == {"a": "ok", "b": "no_access", "c": "ok"}
    assert [i.title for i in result.items] == ["a", "c"]
    assert result.facts["repos_total"] == 3 and result.facts["repos_read"] == 2


def test_budget_exhaustion_marks_unstarted_repos_timeout(tmp_cfg: Config, tmp_path: Path) -> None:
    a, b = make_dir(tmp_path / "a"), make_dir(tmp_path / "b")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="a", path=a), RepoCfg(name="b", path=b))
    collector = GitHubCollector(runner=FakeGh(), budget_s=0.0)
    result = collector.collect(ctx_for(cfg))
    assert result.ok and set(result.facts["states"].values()) == {"timeout"}


def test_collector_only_emits_allowlisted_argv(tmp_cfg: Config, tmp_path: Path) -> None:
    repo_dir = make_dir(tmp_path / "api")
    fake = FakeGh(authored=[pr(1, "x")], review=[pr(2, "y")], runs=[{"status": "completed", "conclusion": "success"}])
    collect(cfg_with(tmp_cfg, RepoCfg(name="example-api", path=repo_dir)), fake)
    graph = [c for c in fake.calls if c[0][1:3] == ["api", "graphql"]]
    assert graph, "stale branches come from the one graphql call"
    for argv, *_ in fake.calls:
        validate_args(argv[1:])
        assert argv[0] == "gh"
        assert not any("mutation" in part.lower() for part in argv)
