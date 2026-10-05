"""Git collector on real temp repos (design section 8, decision D6).

Setup uses the real git binary. The collector under test is driven through a recording
runner so the argv and environment of every call can be asserted.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvisd.collectors import CollectContext
from jarvisd.collectors.git import GitCollector, GitNotAllowed, GitResult, run_git
from jarvisd.config import Config, RepoCfg
from jarvisd.models import CollectResult
from jarvisd.tier import item_hit

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

NOW = datetime.now(timezone.utc)
ALLOWED_VERBS = {"status", "log", "rev-parse", "rev-list"}


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    proc = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=repo, env=env, capture_output=True, text=True, check=True,
    )
    return proc.stdout


def make_repo(path: Path, *, commits: int = 1, subject: str = "feat: synthetic commit") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    for n in range(commits):
        (path / f"file{n}.txt").write_text(f"line {n}\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", subject if n == commits - 1 else f"chore: synthetic {n}")
    return path


def cfg_with(base: Config, *repos: RepoCfg) -> Config:
    cfg = base.model_copy(deep=True)
    cfg.digest.repos = list(repos)
    return cfg


def ctx_for(cfg: Config, hours: float = 36.0) -> CollectContext:
    return CollectContext(cfg=cfg, window_start=NOW - timedelta(hours=hours), window_end=NOW, now=NOW)


class Recorder:
    """Wraps the real runner and remembers every (argv, env) pair."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> GitResult:
        self.calls.append((list(argv), dict(env)))
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)
        return GitResult(proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"))


def collect(cfg: Config, recorder: Recorder | None = None) -> CollectResult:
    return GitCollector(runner=recorder or Recorder()).collect(ctx_for(cfg))


def one_item(result: CollectResult):  # type: ignore[no-untyped-def]
    assert result.ok, result.error
    assert len(result.items) == 1, result.facts
    return result.items[0]


def test_commits_dirty_and_untracked_counts(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repos" / "alpha", commits=2)
    (repo / "file0.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new1.txt").write_text("x", encoding="utf-8")
    (repo / "new2.txt").write_text("x", encoding="utf-8")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="alpha", path=repo, work=True))

    item = one_item(collect(cfg))

    assert item.kind == "git_repo" and item.source == "git" and item.work is True
    assert item.title == "alpha"
    assert item.meta["branch"] == "main"
    assert (item.meta["commits"], item.meta["modified"], item.meta["untracked"]) == (2, 1, 2)
    assert (item.meta["ahead"], item.meta["behind"]) == (0, 0)
    assert "feat: synthetic commit" in item.text and "chore: synthetic 0" in item.text
    # File names are used for noise filtering only and never kept.
    assert "file0.txt" not in item.model_dump_json() and "new1.txt" not in item.model_dump_json()


def test_ahead_and_behind_are_parsed(tmp_cfg: Config, tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    bare.mkdir()
    git(bare, "init", "-q", "--bare")
    first = make_repo(tmp_path / "repos" / "one", commits=1)
    git(first, "remote", "add", "origin", str(bare))
    git(first, "push", "-q", "-u", "origin", "main")
    second = tmp_path / "repos" / "two"
    git(tmp_path, "clone", "-q", str(bare), str(second))
    (second / "other.txt").write_text("remote work\n", encoding="utf-8")
    git(second, "add", "-A")
    git(second, "commit", "-q", "-m", "feat: from the other clone")
    git(second, "push", "-q", "origin", "main")
    (first / "local.txt").write_text("local work\n", encoding="utf-8")
    git(first, "add", "-A")
    git(first, "commit", "-q", "-m", "feat: local only")
    git(first, "fetch", "-q", "origin")

    item = one_item(collect(cfg_with(tmp_cfg, RepoCfg(name="one", path=first))))
    assert (item.meta["ahead"], item.meta["behind"]) == (1, 1)


def test_noise_is_filtered_and_a_clean_repo_is_quiet(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repos" / "quiet")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="quiet", path=repo))
    # Commit is brand new, so use a tiny window to make the repo quiet.
    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / "index.db").write_text("x", encoding="utf-8")
    (repo / ".clever.json").write_text("{}", encoding="utf-8")
    (repo / "zap-report-1.html").write_text("x", encoding="utf-8")
    (repo / "ZAP-Report-2.html").write_text("x", encoding="utf-8")
    ctx = CollectContext(cfg=cfg, window_start=NOW + timedelta(hours=1), window_end=NOW + timedelta(hours=2), now=NOW)
    result = GitCollector(runner=Recorder()).collect(ctx)
    assert result.ok
    assert result.items == [], "noise untracked files must not make the repo active"
    assert result.facts["quiet"] == ["quiet"]

    (repo / "real-untracked.txt").write_text("x", encoding="utf-8")
    again = GitCollector(runner=Recorder()).collect(ctx)
    assert again.items[0].meta["untracked"] == 1


def test_counts_only_emits_no_subjects_or_file_names(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repos" / "journal", commits=2, subject="feat: a very specific subject")
    (repo / "secret-looking-name.txt").write_text("x", encoding="utf-8")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="journal", path=repo, counts_only=True))

    result = collect(cfg)
    item = one_item(result)

    blob = result.model_dump_json()
    assert "very specific subject" not in blob and "secret-looking-name" not in blob
    assert item.text == "" and item.meta["branch"] == "" and item.meta["counts_only"] is True
    assert (item.meta["commits"], item.meta["untracked"]) == (2, 1)


def test_non_repo_and_missing_paths_are_facts_not_failures(tmp_cfg: Config, tmp_path: Path) -> None:
    plain = tmp_path / "repos" / "plain"
    plain.mkdir(parents=True)
    # A plain folder inside a real repo must not report the parent repo.
    parent = make_repo(tmp_path / "outer")
    inner = parent / "inner"
    inner.mkdir()
    cfg = cfg_with(
        tmp_cfg,
        RepoCfg(name="plain", path=plain),
        RepoCfg(name="missing", path=tmp_path / "does" / "not" / "exist"),
        RepoCfg(name="inner", path=inner),
    )
    rec = Recorder()
    result = collect(cfg, rec)
    assert result.ok and result.items == []
    assert result.facts["not_repos"] == ["plain", "missing", "inner"]
    assert result.facts["states"] == {"plain": "not_a_repo", "missing": "missing", "inner": "not_a_repo"}
    assert rec.calls == [], "no git process for a path that is not a repo"


def test_empty_repository_without_commits_does_not_fail(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "fresh"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="fresh", path=repo)))
    item = one_item(result)
    assert item.meta["commits"] == 0 and item.meta["untracked"] == 1


def test_recorded_argv_is_read_only_and_env_is_locked_down(
    tmp_cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repos" / "alpha")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "somewhere-else"))  # must not be inherited
    rec = Recorder()
    collect(cfg_with(tmp_cfg, RepoCfg(name="alpha", path=repo)), rec)

    assert rec.calls
    for argv, env in rec.calls:
        assert argv[0] == "git" and argv[1] == "--no-optional-locks"
        assert argv[2] in ALLOWED_VERBS
        assert env["GIT_OPTIONAL_LOCKS"] == "0"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert "GIT_DIR" not in env
        assert env["GIT_CONFIG_KEY_0"] == "core.fsmonitor" and env["GIT_CONFIG_VALUE_0"] == "false"
    verbs = [argv[2] for argv, _ in rec.calls]
    assert verbs[0] == "status" and "log" in verbs and "rev-parse" in verbs
    log = next(argv for argv, _ in rec.calls if argv[2] == "log")
    assert "--all" in log and "--no-merges" in log and "-n" in log
    assert any(a.startswith("--since=") for a in log)


def test_git_index_mtime_is_unchanged_by_collection(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repos" / "alpha")
    # Touch a tracked file so that a normal `git status` would refresh and rewrite the index.
    later = NOW.timestamp() + 5
    os.utime(repo / "file0.txt", (later, later))
    index = repo / ".git" / "index"
    before = index.stat().st_mtime_ns
    collect(cfg_with(tmp_cfg, RepoCfg(name="alpha", path=repo)))
    assert index.stat().st_mtime_ns == before


@pytest.mark.parametrize(
    "args",
    [
        ["fetch"], ["pull"], ["checkout", "main"], ["config", "user.name", "x"], ["gc"], ["push"],
        ["status"], ["status", "--porcelain=v1", "-b"], ["log", "-p"], ["log", "--all", "--no-merges"],
        ["log", "--all", "--no-merges", "--since=$(evil)", "--format=%h%x1f%aI%x1f%s", "-n", "30"],
        ["log", "--all", "--no-merges", "--since=2026-01-01", "--format=%H", "-n", "30"],
        ["log", "--all", "--no-merges", "--since=2026-01-01", "--format=%h%x1f%aI%x1f%s", "-n", "100000"],
        ["rev-parse", "--git-dir"], ["rev-list", "--all"], [],
    ],
)
def test_run_git_refuses_anything_off_the_allowlist(tmp_path: Path, args: list[str]) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> GitResult:
        calls.append(argv)
        return GitResult(0, "", "")

    with pytest.raises(GitNotAllowed):
        run_git(tmp_path, args, runner=runner)
    assert calls == [], "a refused command must never reach the runner"


def test_run_git_accepts_the_four_allowed_shapes(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> GitResult:
        seen.append(argv)
        return GitResult(0, "", "")

    for args in (
        ["status", "--porcelain=v1", "-b", "--untracked-files=normal"],
        ["log", "--all", "--no-merges", "--since=2026-10-05T04:30:11+00:00", "--format=%h%x1f%aI%x1f%s", "-n", "30"],
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["rev-list", "--left-right", "--count", "@{u}...HEAD"],
    ):
        run_git(tmp_path, args, runner=runner)
    assert [a[:2] for a in seen] == [["git", "--no-optional-locks"]] * 4


def test_tagged_commit_subject_is_withheld(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repos" / "alpha", commits=2, subject="wip notes #private details")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="alpha", path=repo))
    result = collect(cfg)
    item = one_item(result)
    assert "private" not in result.model_dump_json().lower()
    assert item.meta["commits"] == 2 and item.meta["commits_withheld"] == 1
    assert "chore: synthetic 0" in item.text
    assert [w.kind for w in result.withheld] == ["git_commit"]
    assert result.withheld[0].reason == "tag_inline" and result.withheld[0].id.startswith("w-")


def test_sensitive_term_in_a_subject_is_withheld_without_echoing_the_term(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repos" / "alpha", subject="fix: handle frobnicator edge case")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="alpha", path=repo))
    cfg.gates.sensitive_terms = ["frobnicator"]
    result = collect(cfg)
    assert "frobnicator" not in result.model_dump_json()
    assert result.withheld[0].reason == "term:0"


def test_sensitive_branch_name_is_withheld(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repos" / "alpha")
    git(repo, "checkout", "-q", "-b", "feature/#private")
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="alpha", path=repo)))
    item = one_item(result)
    assert item.meta["branch"] == "(withheld)"
    assert "feature/#private" not in result.model_dump_json()
    assert [w.kind for w in result.withheld] == ["git_branch"]


def test_repo_under_a_sensitive_path_is_never_touched(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "sensitive" / "stuff")
    rec = Recorder()
    result = collect(cfg_with(tmp_cfg, RepoCfg(name="hidden", path=repo)), rec)
    assert result.ok and result.items == []
    assert rec.calls == []
    assert [w.kind for w in result.withheld] == ["git_repo"]
    assert result.facts["states"] == {"hidden": "withheld_path"}


def test_timeout_and_git_errors_are_per_repo_facts(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repos" / "alpha")

    def slow(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> GitResult:
        raise subprocess.TimeoutExpired(argv, timeout)

    def broken(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> GitResult:
        return GitResult(128, "", "fatal: detected dubious ownership")

    cfg = cfg_with(tmp_cfg, RepoCfg(name="alpha", path=repo))
    timed_out = GitCollector(runner=slow).collect(ctx_for(cfg))
    assert timed_out.ok and timed_out.facts["errors"] == ["alpha"]
    assert timed_out.facts["states"]["alpha"] == "error:timeout"
    refused = GitCollector(runner=broken).collect(ctx_for(cfg))
    assert refused.ok and refused.facts["states"]["alpha"] == "error:status_rc128"


def test_no_repos_configured_is_fine(tmp_cfg: Config) -> None:
    result = GitCollector(runner=Recorder()).collect(ctx_for(cfg_with(tmp_cfg)))
    assert result.ok and result.items == [] and result.facts["repos_total"] == 0


def test_repo_under_a_forbidden_root_is_work_without_the_flag(tmp_cfg: Config, tmp_path: Path) -> None:
    # [paths].vault_forbidden holds Documents/Work. A forgotten `work = true` must not open it.
    repo = make_repo(tmp_path / "Documents" / "Work" / "Dev" / "client-x", subject="fix: synthetic export")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="client-x", path=repo))  # work defaults to False
    item = one_item(collect(cfg))
    assert item.work is True
    hit = item_hit(item, cfg)
    assert hit is not None and hit.code == "work_policy"


def test_repo_under_a_forbidden_root_reaches_claude_only_with_the_opt_in(tmp_cfg: Config, tmp_path: Path) -> None:
    from jarvisd.dispatch import clear_for_claude, run_gates
    from jarvisd.router import build_router

    class Sink:
        def emit(self, event: str, **fields: object) -> None:
            return None

    repo = make_repo(tmp_path / "Documents" / "Work" / "Dev" / "client-x", subject="fix: synthetic export")
    cfg = cfg_with(tmp_cfg, RepoCfg(name="client-x", path=repo))
    item = one_item(collect(cfg))

    closed = run_gates([item], build_router(cfg), cfg, "not_installed", Sink())
    assert closed[0].route == "held"
    assert "client-x" not in clear_for_claude([item], closed, cfg).text

    cfg.digest.work_metadata_to_claude = True
    opened = run_gates([item], build_router(cfg), cfg, "not_installed", Sink())
    assert opened[0].route == "claude"
    assert '"work":true' in clear_for_claude([item], opened, cfg).text


def test_a_repo_outside_every_forbidden_root_keeps_its_own_flag(tmp_cfg: Config, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repos" / "plain")
    item = one_item(collect(cfg_with(tmp_cfg, RepoCfg(name="plain", path=repo))))
    assert item.work is False
