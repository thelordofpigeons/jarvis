"""Publishing files and CI behaviour (task P1): the repo must be installable by a stranger."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from jarvisd import ROOT, __version__
from jarvisd import selftest
from jarvisd.config import Config


def _no_claude_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def _never_called(*_a: object, **_k: object) -> None:
    raise AssertionError("no process may be spawned when the binary is missing")


def test_claude_check_fails_loudly_on_a_machine_that_should_have_claude(
    tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_claude_on_path(monkeypatch, tmp_path)
    for var in ("CI", "JARVIS_NO_CLAUDE"):
        monkeypatch.delenv(var, raising=False)
    ok, detail = selftest.check_claude(tmp_cfg, _never_called)
    assert ok is False and "binary_not_found" in detail


@pytest.mark.parametrize("var, value", [("CI", "true"), ("JARVIS_NO_CLAUDE", "1")])
def test_claude_check_skips_cleanly_on_ci_or_when_asked(
    tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, var: str, value: str
) -> None:
    _no_claude_on_path(monkeypatch, tmp_path)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("JARVIS_NO_CLAUDE", raising=False)
    monkeypatch.setenv(var, value)
    ok, detail = selftest.check_claude(tmp_cfg, _never_called)
    assert ok is None and "no claude binary" in detail


def test_ci_skip_never_hides_a_broken_installed_binary(
    tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The skip is for "not installed" only. A binary that resolves but lacks flags still FAILS on CI.
    monkeypatch.setenv("CI", "true")
    from jarvisd.claude import PreflightReport

    class _Client:
        def __init__(self, *_a: object, **_k: object) -> None: ...
        def preflight(self) -> PreflightReport:
            return PreflightReport(False, binary="claude", reason="missing_flags", missing_flags=("--tools",))

    monkeypatch.setattr(selftest, "ClaudeClient", _Client)
    monkeypatch.setattr(selftest.StateStore, "from_config", classmethod(lambda cls, cfg: None))
    ok, detail = selftest.check_claude(tmp_cfg, None)
    assert ok is False and "missing_flags" in detail


# --- repository files ---------------------------------------------------------------------------


def test_license_is_mit_with_the_owner_as_holder() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    holder = re.search(r"^Copyright \(c\) 2026 (\S+ \S+)$", text, re.MULTILINE)
    assert holder, "the copyright line is 'Copyright (c) 2026 <given> <family>'"
    given, family = holder.group(1).split()
    cff = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    assert f"family-names: {family}" in cff and f"given-names: {given}" in cff
    assert "Permission is hereby granted, free of charge" in text
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in text


def test_citation_file_is_valid_cff_and_matches_the_package() -> None:
    text = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    assert text.startswith("cff-version: 1.2.0")
    for needle in ("title:", "authors:", "family-names:", "given-names:", "license: MIT",
                   "type: software", "date-released:"):
        assert needle in text, f"CITATION.cff is missing {needle}"
    match = re.search(r"^version: (\S+)$", text, re.MULTILINE)
    assert match and match.group(1) == __version__


def test_package_metadata_matches_the_released_version() -> None:
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    licence = meta["license"]
    assert (licence if isinstance(licence, str) else licence["text"]) == "MIT"
    assert meta["version"].split("-")[0] == __version__.split("-")[0]


def test_changelog_is_newest_first_and_its_top_entry_is_the_package_version() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.startswith("# Changelog")
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    # Either work in progress under "Unreleased", or the release that is checked in: then its heading is the
    # package version and a date, so the file and the code cannot disagree about what the newest release is.
    assert headings[0] == "Unreleased" or re.fullmatch(re.escape(__version__) + r" - \d{4}-\d{2}-\d{2}", headings[0])
    assert any(h.startswith("1.0.0-opt2") for h in headings)
    assert any(h.startswith("0.1.0") for h in headings)
    assert 0 < next(i for i, h in enumerate(headings) if h.startswith("1.0.0-opt2"))
    assert next(i for i, h in enumerate(headings) if h.startswith("1.0.0-opt2")) < next(
        i for i, h in enumerate(headings) if h.startswith("0.1.0"))


def test_ci_workflow_runs_pytest_on_windows_with_python_312_and_no_claude() -> None:
    path = ROOT / ".github" / "workflows" / "ci.yml"
    raw = path.read_bytes()
    assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert "windows-latest" in text
    assert re.search(r"python-version:\s*['\"]?3\.12", text)
    assert "pytest" in text
    assert "actions/checkout" in text and "actions/setup-python" in text
    # Installs from the pinned lock file, not whatever is newest.
    assert "requirements.lock" in text
    # The Claude CLI is never installed or authenticated on CI; no secret is read either.
    assert "secrets." not in text
    assert "npm install" not in text and "claude.ai/install" not in text
    assert "permissions:" in text and "contents: read" in text
    # The self-test skips the Claude check there instead of failing.
    assert "CI" in text or "self-test" in text


# --- release 1.1.0: what a stranger meets first ---------------------------------------------------


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_every_version_marker_agrees() -> None:
    meta = tomllib.loads(_read("pyproject.toml"))["project"]
    assert meta["version"] == __version__
    cff = re.search(r"^version: (\S+)$", _read("CITATION.cff"), re.MULTILINE)
    assert cff and cff.group(1) == __version__
    assert tomllib.loads(_read("jarvis.toml"))["meta"]["version"] == __version__
    headings = re.findall(r"^## (.+)$", _read("CHANGELOG.md"), re.MULTILINE)
    released = [h for h in headings if h != "Unreleased"]
    assert released[0].startswith(__version__ + " - "), "the newest changelog entry is the packaged version"


def test_the_changelog_names_every_feature_the_readme_status_table_claims() -> None:
    section = _read("CHANGELOG.md").split("## 1.1.0", 1)[1].split("\n## ", 1)[0].casefold()
    for feature in ("hub", "ask", "consolidat", "clickup", "ntfy", "github", "local"):
        assert feature in section, f"CHANGELOG 1.1.0 does not mention {feature}"
    assert re.search(r"\d+ offline tests", section), "state the size of the offline suite"


def test_ci_runs_on_pushes_to_the_branch_that_gets_published() -> None:
    text = _read(".github/workflows/ci.yml")
    match = re.search(r"push:\s*\n\s*branches:\s*\[([^\]]*)\]", text)
    assert match and "main" in [b.strip() for b in match.group(1).split(",")]


def test_pip_install_gets_a_working_hub_through_an_extra() -> None:
    meta = tomllib.loads(_read("pyproject.toml"))["project"]
    hub = " ".join(meta["optional-dependencies"]["hub"]).casefold()
    for name in ("fastapi", "uvicorn", "httpx2"):
        assert name in hub
    # The lock file pins exactly what the extra names.
    lock = _read("requirements.lock").casefold()
    for name in ("fastapi==", "uvicorn==", "httpx2=="):
        assert name in lock
    assert "jarvisd[hub]" in _read("README.md") or ".[hub]" in _read("README.md")


def test_the_hub_import_error_names_a_way_to_install_on_any_platform() -> None:
    source = _read("jarvisd/cli.py")
    assert "requirements.lock" in source and ".[hub]" in source


def test_contributing_lists_the_dependencies_that_really_ship() -> None:
    text = _read("CONTRIBUTING.md").casefold()
    for name in ("pydantic", "apscheduler", "pytest", "fastapi", "uvicorn", "httpx2"):
        assert name in text


def test_readme_first_run_sequence_works_after_the_morning_run() -> None:
    text = _read("README.md")
    # After 06:30 a digest job for today exists, so the paid step needs --force (v1-operations).
    for line in text.splitlines():
        if "run-digest --claude" in line and not line.lstrip().startswith("#"):
            assert "--force" in line, f"README line would be refused after 06:30: {line.strip()}"
    # Activate.ps1 is blocked by the default Restricted policy, so the README calls the venv python directly.
    assert "Scripts\\activate" not in text and "Activate.ps1" not in text
    assert ".venv\\Scripts\\python.exe -m pytest" in text


def test_readme_says_which_python_it_expects_and_how_to_point_elsewhere() -> None:
    text = _read("README.md")
    assert "-Python" in text and "python.org" in text.casefold()


def test_readme_has_a_real_clone_url_and_the_citation_points_at_the_repository() -> None:
    text = _read("README.md")
    assert "<url" not in text and "git clone https://github.com/" in text
    assert re.search(r"^repository-code: https://github\.com/\S+$", _read("CITATION.cff"), re.MULTILINE)


def test_readme_states_the_clickup_exception_to_the_isolation_claim() -> None:
    privacy = _read("README.md").split("## Privacy model", 1)[1].split("\n## ", 1)[0].casefold()
    assert "clickup" in privacy and "exception" in privacy and "strict-mcp-config" in privacy
    assert "off by default" in privacy


def test_readme_says_the_scheduled_run_is_unproven_and_the_cost_claim_is_one_run() -> None:
    text = _read("README.md").casefold()
    assert "unattended" in text and "never" in text
    assert "0.007 to 0.03" not in text, "the range was backed by one recorded run"
    assert "0.0068" in text


def test_readme_defines_the_valley_nudge_or_stops_using_the_term() -> None:
    text = _read("README.md").casefold()
    if "valley nudge" in text:
        assert re.search(r"valley nudge[^|\n]*(means|is a|is an|:)", text)


def test_a_vault_layout_document_exists_and_the_readme_links_it() -> None:
    doc = _read("docs/vault-layout.md")
    for needle in ("RECENT.md", "## Open Threads", "sessions/", "raw/jarvis", "empty digest", "current-task"):
        assert needle in doc, f"vault-layout.md is missing {needle}"
    assert "docs/vault-layout.md" in _read("README.md")
