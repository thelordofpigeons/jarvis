"""Documentation for strangers (task P8): the README, architecture, contributing and cross-links.

These tests keep the public face honest: every command in the CLI is in the README table,
every path the docs name exists, every phase of the build order has a status row, and the
Phase 0 record that moved out of the README is still byte for byte what it was.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from jarvisd import ROOT
from jarvisd.cli import build_parser

README = ROOT / "README.md"
DOCS = ROOT / "docs"
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

# The Phase 0 narrative, moved from the README into docs/phase0.md. Pinned so a later edit has to
# be deliberate. Rebaseline only when the record itself is meant to change.
PHASE0_MARKER = "<!-- phase0-record:begin -->\n"
PHASE0_RECORD_BYTES = 5902
PHASE0_RECORD_SHA256 = "44344ce44a3fb283bf03830bb6191e944d6c68dd80601b392cd2d8ee0627feeb"

# Files whose every path mention must resolve: they are written for a reader who will click.
STRICT_FILES = ("README.md", "CONTRIBUTING.md", "docs/architecture.md", "docs/phase0.md")

PHASE_STATES = {"done", "adapter only", "not built", "in progress tonight"}

_BACKTICK = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\]\(([^)\s]+)\)")
_ROOT_FILES = (
    r"README\.md|CONTRIBUTING\.md|CHANGELOG\.md|CITATION\.cff|jarvis\.toml|jarvis\.cmd|jarvis\.local\.toml\.example"
    r"|srt-settings\.json|pyproject\.toml|requirements\.lock"
)
_REPO_PATH = re.compile(rf"^(?:bin|deploy|docs|jarvisd|tests|\.github)/[\w./\-]*\w$|^(?:{_ROOT_FILES})$")


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Body of a level 2 section, up to the next level 2 heading."""
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.MULTILINE)
    assert match, f"missing section: {heading}"
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _table_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if line.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", line):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows[1:]  # drop the header row


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text))


# --- the Phase 0 record -------------------------------------------------------------------------


def test_phase0_record_moved_to_docs_unchanged() -> None:
    raw = (DOCS / "phase0.md").read_bytes()
    assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert text.count(PHASE0_MARKER) == 1
    body = text.split(PHASE0_MARKER, 1)[1].encode("utf-8")
    assert len(body) == PHASE0_RECORD_BYTES
    assert hashlib.sha256(body).hexdigest() == PHASE0_RECORD_SHA256, (
        "docs/phase0.md record changed; it is pinned on purpose, rebaseline only on a deliberate edit"
    )


def test_phase0_pin_detects_an_edit() -> None:
    # Guards the guard: a one-character change must change the hash.
    body = (DOCS / "phase0.md").read_text(encoding="utf-8").split(PHASE0_MARKER, 1)[1]
    assert hashlib.sha256(body.replace("Phase 0", "Phase Zero", 1).encode()).hexdigest() != PHASE0_RECORD_SHA256


# --- README --------------------------------------------------------------------------------------


def test_readme_is_a_ten_minute_read() -> None:
    # About 230 words a minute: 2300 words is ten minutes, tables and code included.
    assert _words(_text("README.md")) <= 2300


def test_readme_has_the_required_sections_in_order() -> None:
    headings = re.findall(r"^## (.+)$", _text("README.md"), re.MULTILINE)
    expected = [
        "At a glance", "How a morning run works", "Quick start", "Status by phase", "Privacy model",
        "CLI", "What this is not", "Cost", "Docs", "License",
    ]
    positions = [headings.index(h) for h in expected]
    assert positions == sorted(positions), f"sections out of order: {headings}"


def test_readme_opens_with_a_tagline_and_an_at_a_glance_block() -> None:
    text = _text("README.md")
    lines = text.splitlines()
    assert lines[0] == "# jarvis" and lines[2].startswith("*") and "windows" in lines[2].casefold()
    assert len(re.findall(r"img\.shields\.io|badge\.svg", text.split("## At a glance")[0])) <= 5
    folded = _section(text, "At a glance").casefold()
    for needle in ("daemon", "observe-only", "claude", "digest", "not built"):
        assert needle in folded, f"the at-a-glance block does not mention {needle}"


def test_status_table_covers_phases_zero_to_nine_with_honest_states() -> None:
    rows = _table_rows(_section(_text("README.md"), "Status by phase"))
    assert [r[0].split()[0] for r in rows] == [str(n) for n in range(10)], rows
    for row in rows:
        assert len(row) == 4, row
        assert row[2].casefold() in PHASE_STATES, f"unknown state {row[2]!r} in {row[0]!r}"
        assert row[3], "every row says what that means in practice"


def test_status_states_match_what_the_tree_contains() -> None:
    rows = {r[0].split()[0]: r for r in _table_rows(_section(_text("README.md"), "Status by phase"))}
    # Rows that claim something exists must name files that exist; "not built" rows must not claim code.
    for phase, row in rows.items():
        for token in _BACKTICK.findall(row[3]):
            if _REPO_PATH.match(token):
                assert (ROOT / token).exists(), f"phase {phase} names {token}, which does not exist"
    assert rows["2"][2].casefold() == "done"
    # No model has been benchmarked, so phase 1 cannot say done.
    assert rows["1"][2].casefold() != "done"
    for phase in ("6", "7", "8", "9"):
        assert rows[phase][2].casefold() == "not built", f"phase {phase} has no code in the tree"
    # Each adapter row must point at an adapter that is really there.
    present = {
        "jarvisd/local.py": rows["1"], "jarvisd/consolidate.py": rows["5"], "jarvisd/hub/app.py": rows["4"],
    }
    for rel, row in present.items():
        assert (ROOT / rel).is_file()
        assert rel in row[3], f"{rel} exists but its phase row does not name it"


def test_architecture_diagram_is_a_mermaid_flowchart_inside_the_readme() -> None:
    section = _section(_text("README.md"), "How a morning run works")
    block = re.search(r"```mermaid\n(flowchart LR\n.+?)\n```", section, re.DOTALL)
    assert block, "the diagram must be a fenced mermaid flowchart LR"
    diagram = block.group(1)
    for needle in ("collect", "gate", "claude", "writer", "audit", "ledger"):
        assert needle in diagram.casefold(), f"the diagram does not show {needle}"
    assert len(diagram.splitlines()) <= 40


def test_privacy_model_is_exactly_five_bullets() -> None:
    section = _section(_text("README.md"), "Privacy model")
    bullets = re.findall(r"^- \*\*(.+?)\*\*", section, re.MULTILINE)
    assert len(bullets) == 5, bullets
    folded = " ".join(b.casefold() for b in bullets)
    for needle in ("tier gate", "gate order", "single writer", "isolated", "audit"):
        assert needle in folded, f"privacy bullets miss: {needle}"


def test_install_section_has_the_commands_a_stranger_needs() -> None:
    section = _section(_text("README.md"), "Quick start")
    for needle in (
        "setup-venv.ps1", "jarvis.local.toml.example", "jarvis.local.toml", "self-test",
        "run-digest --dry-run", "install-task", "-m pytest -q",
    ):
        assert needle in section, f"install section misses: {needle}"
    assert "Python 3.12" in section and "Windows" in section


def test_every_cli_command_is_in_the_readme_table() -> None:
    parser = build_parser()
    choices = next(a.choices for a in parser._actions if getattr(a, "choices", None))
    section = _section(_text("README.md"), "CLI")
    rows = _table_rows(section)
    listed = " ".join(r[0] for r in rows)
    for name in choices:
        assert f"jarvis {name}" in listed, f"README command table does not list `jarvis {name}`"
    for row in rows:
        assert len(row) == 2 and row[1], row


def test_readme_states_the_observed_cost_and_the_cap() -> None:
    section = _section(_text("README.md"), "Cost")
    assert "0.0068" in section and "one measurement" in section
    assert "daily_budget_usd" in section and "breaker" in section.casefold()


def test_what_this_is_not_says_the_three_hard_truths() -> None:
    folded = " ".join(_section(_text("README.md"), "What this is not").casefold().split())
    for needle in ("not sandboxed", "tamper-evident", "not tamper-proof", "no local model", "benchmark"):
        assert needle in folded, f"'What this is not' misses: {needle}"


def test_next_steps_and_license_sections() -> None:
    status = _section(_text("README.md"), "Status by phase")
    assert len(re.findall(r"^\d+\. ", status, re.MULTILINE)) >= 3
    license_text = _section(_text("README.md"), "License")
    assert "MIT" in license_text and "LICENSE" in license_text


def test_readme_links_every_doc_so_none_is_orphaned() -> None:
    readme = _text("README.md")
    missing = [p.name for p in sorted(DOCS.glob("*.md")) if f"docs/{p.name}" not in readme]
    assert not missing, f"README Documentation section does not list: {missing}"


def test_readme_names_nothing_private() -> None:
    folded = _text("README.md").casefold()
    # The employer's name and the other hashed names are checked by tests/test_repo_hygiene.py.
    for word in ("clever cloud", "clickup task", "c:/users", "c:\\users"):
        assert word not in folded, f"README mentions {word!r}"
    assert "https://github.com/thelordofpigeons/jarvis" in _text("README.md")


# --- architecture and contributing ---------------------------------------------------------------


def test_architecture_doc_covers_design_sections_three_to_seven() -> None:
    text = _text("docs/architecture.md")
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    for needle in ("Processes and layers", "Modules", "Jobs and state", "Gate semantics", "Calling Claude"):
        assert any(needle in h for h in headings), f"architecture.md has no section for: {needle}"
    folded = text.casefold()
    for needle in (
        "gate 1", "gate 2", "gate 3", "0.72", "gatedpayload", "clear_for_claude", "safe_read_text",
        "--setting-sources", "--strict-mcp-config", "--tools", "--max-budget-usd", "state/kill",
        "hash-chained", "not sandboxed", "daily_budget_usd", "circuit breaker",
    ):
        assert needle in folded, f"architecture.md misses: {needle}"
    assert "docs/v1-design.md" in text, "architecture.md must point to the full design for the rationale"


def test_architecture_names_every_module_that_exists() -> None:
    text = _text("docs/architecture.md")
    for path in sorted((ROOT / "jarvisd").glob("*.py")):
        if path.name in {"__init__.py", "__main__.py"}:
            continue
        assert f"jarvisd/{path.name}" in text, f"architecture.md does not describe jarvisd/{path.name}"
    for path in sorted((ROOT / "jarvisd" / "collectors").glob("*.py")):
        if path.name != "__init__.py":
            assert path.stem in text, f"architecture.md does not mention the {path.stem} collector"


def test_contributing_is_about_ten_lines_and_names_the_rules() -> None:
    text = _text("CONTRIBUTING.md")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert 8 <= len(lines) <= 14, f"{len(lines)} non-empty lines"
    folded = text.casefold()
    for needle in ("pytest", "test first", "dash", "hygiene", "stdlib", "audit", "privacy"):
        assert needle in folded, f"CONTRIBUTING.md misses: {needle}"


# --- cross-links and file hygiene ----------------------------------------------------------------


def _md_files() -> list[str]:
    return ["README.md", "CONTRIBUTING.md", "CHANGELOG.md", *sorted(f"docs/{p.name}" for p in DOCS.glob("*.md"))]


@pytest.mark.parametrize("rel", _md_files())
def test_relative_markdown_links_resolve(rel: str) -> None:
    base = (ROOT / rel).parent
    broken = []
    for target in _MD_LINK.findall(_text(rel)):
        if re.match(r"^(?:[a-z][a-z0-9+.-]*:|#)", target, re.IGNORECASE):
            continue
        if not (base / target.split("#", 1)[0]).exists():
            broken.append(target)
    assert not broken, f"{rel}: broken links {broken}"


@pytest.mark.parametrize("rel", _md_files())
def test_every_doc_page_named_in_backticks_exists(rel: str) -> None:
    named = {t for t in _BACKTICK.findall(_text(rel)) if re.fullmatch(r"docs/[\w.-]+\.md", t)}
    missing = sorted(t for t in named if not (ROOT / t).is_file())
    assert not missing, f"{rel}: names docs that do not exist: {missing}"


@pytest.mark.parametrize("rel", STRICT_FILES)
def test_strict_files_name_only_paths_that_exist(rel: str) -> None:
    missing = []
    for token in _BACKTICK.findall(_text(rel)):
        if token.startswith(("~", "state/", "queue/", "logs/", "models/")) or "<" in token or "*" in token:
            continue  # runtime folders and templates are not in a fresh clone
        if token == "jarvis.local.toml":
            continue  # the gitignored private config: the docs tell the reader to create it
        if _REPO_PATH.match(token) and not (ROOT / token).exists():
            missing.append(token)
    assert not missing, f"{rel}: names paths that do not exist: {sorted(set(missing))}"


@pytest.mark.parametrize("rel", ["README.md", "CONTRIBUTING.md", *[f"docs/{p.name}" for p in sorted(DOCS.glob("*.md"))]])
def test_docs_are_lf_utf8_without_bom_or_dashes(rel: str) -> None:
    raw = (ROOT / rel).read_bytes()
    assert b"\r" not in raw, f"{rel}: CRLF"
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{rel}: BOM"
    text = raw.decode("utf-8")
    assert EM_DASH not in text and EN_DASH not in text, f"{rel}: em or en dash"
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n"), f"{rel}: must end with exactly one newline"


def test_the_operations_manual_keeps_no_owner_run_log() -> None:
    # The first-run log is the author's diary; it moved to docs/first-run-log.md and is generic there.
    assert "## Run log" not in _text("docs/v1-operations.md")
    log = _text("docs/first-run-log.md")
    assert "first live run" in log.casefold()
    assert "docs/v1-operations.md" in log


def test_readme_documentation_section_describes_the_operator_manual() -> None:
    section = _section(_text("README.md"), "Docs")
    assert "docs/v1-operations.md" in section and "operator" in section.casefold()
    assert "docs/phase0.md" in section and "docs/architecture.md" in section
