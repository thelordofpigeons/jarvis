"""Vault writer (design section 9): the only code allowed to write under the brain tree.

Every denied target must raise VaultWriteDenied, leave the vault byte for byte unchanged and
leave a vault_violation record in the audit chain.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

import pytest

from conftest import CANARY, FakeClock
from jarvisd import fsio
from jarvisd.audit import AuditLog
from jarvisd.config import Config
from jarvisd.vault import VaultWriteDenied, VaultWriter, WriteResult

MARKED = "---\ntype: jarvis-digest\ngenerator: jarvisd\njob_id: j1\n---\n# body\n- one\n"


@pytest.fixture
def audit(tmp_cfg: Config, clock: FakeClock) -> AuditLog:
    return AuditLog(tmp_cfg.paths.logs / "jarvisd-audit.jsonl", clock=clock, mirror_stdout=False)


@pytest.fixture
def writer(tmp_cfg: Config, audit: AuditLog) -> VaultWriter:
    return VaultWriter(tmp_cfg, audit)


@pytest.fixture
def sessions_on(tmp_cfg: Config) -> Config:
    """The shared cfg with the (default off) session note switch turned on."""
    tmp_cfg.digest.write_session_note = True
    return tmp_cfg


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every file and directory under root, with file bytes, for before/after comparison."""
    out: dict[str, bytes | None] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            out[str(Path(dirpath, name).relative_to(root))] = None
        for name in filenames:
            out[str(Path(dirpath, name).relative_to(root))] = Path(dirpath, name).read_bytes()
    return out


def _make_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are a Windows feature")
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip(f"mklink /J unavailable: {proc.stderr.strip()}")


def _violations(audit: AuditLog) -> list[dict]:
    return audit.records(events=["vault_violation"])


# --- the named first test ---------------------------------------------------------------

DENIED_NAMES = [
    "telos",
    "notes",
    "Documents/Work",
    "raw/x.md",
    "raw/jarvis/../x.md",
    "sessions/x.md",
    "jarvis.toml",
    "../notes/x.md",
    "..\\notes\\x.md",
    "candidates/../../notes/x.md",
    "telos/sensitive/canary.md",
    "x.md::$DATA",
    "x.md:stream",
    "CON.md",
    "a/b/c.md",
    "candidates/deep/x.md",
    "Candidates/x.md",
    "x.txt",
    ".hidden.md",
    "x.md.",
    "x.md ",
    "",
    "/x.md",
    "C:/x.md",
    "//server/share/x.md",
    "nul\x00.md",
    "x\n.md",
]


@pytest.mark.parametrize("name", DENIED_NAMES)
def test_denied_targets(name: str, writer: VaultWriter, audit: AuditLog, sessions_on: Config,
                        tmp_vault: Path) -> None:
    before = _snapshot(tmp_vault)
    # A bare word such as "telos" is a legal session slug (sessions/jarvis-telos.md is
    # allowed), so the session writer is only expected to refuse names that are not slugs.
    calls = [writer.write_raw]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        calls.append(writer.write_session)
    for call in calls:
        with pytest.raises(VaultWriteDenied):
            call(name, MARKED, "job-1")
    assert _snapshot(tmp_vault) == before
    assert len(_violations(audit)) == len(calls)
    assert audit.verify() == (True, None)


def test_denied_absolute_path_elsewhere(writer: VaultWriter, audit: AuditLog, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for call in (writer.write_raw, writer.write_session):
        with pytest.raises(VaultWriteDenied):
            call(str(elsewhere / "x.md"), MARKED, "job-1")
    assert list(elsewhere.iterdir()) == []
    assert len(_violations(audit)) == 2


def test_denied_vault_relative_spellings_of_forbidden_places(
        writer: VaultWriter, audit: AuditLog, tmp_vault: Path) -> None:
    """Even the spelling of a full vault path cannot reach telos or notes."""
    for target in (tmp_vault / "telos" / "x.md", tmp_vault / "notes" / "x.md",
                   tmp_vault / "raw" / "x.md", tmp_vault / "sessions" / "x.md"):
        for spelling in (str(target), target.as_posix(), os.path.relpath(target, tmp_vault)):
            with pytest.raises(VaultWriteDenied):
                writer.write_raw(spelling, MARKED, "job-1")
            assert not target.exists()


@pytest.mark.parametrize("junction", ["candidates", "evil"])
def test_denied_junction_pointing_at_notes(junction: str, writer: VaultWriter, audit: AuditLog,
                                           tmp_vault: Path) -> None:
    link = tmp_vault / "raw" / "jarvis" / junction
    _make_junction(link, tmp_vault / "notes")
    with pytest.raises(VaultWriteDenied):
        writer.write_raw(f"{junction}/x.md", MARKED, "job-1")
    assert not (tmp_vault / "notes" / "x.md").exists()
    assert list((tmp_vault / "notes").iterdir()) == []
    assert _violations(audit)[-1]["reason"] in {"parent_realpath_mismatch", "bad_name"}


def test_denied_when_raw_dir_itself_is_a_junction_to_notes(
        tmp_cfg: Config, audit: AuditLog, tmp_vault: Path) -> None:
    """The configured base resolving somewhere else than where it is spelled is not enough
    to escape: the target must also stay out of vault_forbidden and the tier floor."""
    raw = tmp_vault / "raw" / "jarvis"
    raw.rmdir()
    _make_junction(raw, tmp_vault / "notes")
    w = VaultWriter(tmp_cfg, audit)
    with pytest.raises(VaultWriteDenied):
        w.write_raw("x.md", MARKED, "job-1")
    assert list((tmp_vault / "notes").iterdir()) == []


def test_denied_when_target_is_a_directory(writer: VaultWriter, tmp_vault: Path) -> None:
    (tmp_vault / "raw" / "jarvis" / "x.md").mkdir()
    with pytest.raises(VaultWriteDenied):
        writer.write_raw("x.md", MARKED, "job-1")


def test_denied_when_target_is_a_symlink(
        writer: VaultWriter, tmp_vault: Path) -> None:
    notes_file = tmp_vault / "notes" / "private.md"
    notes_file.write_text("generator: jarvisd\nmine\n", encoding="utf-8")
    link = tmp_vault / "raw" / "jarvis" / "alias.md"
    try:
        os.symlink(notes_file, link)
    except OSError:
        pytest.skip("symlinks need a privilege on this machine")
    with pytest.raises(VaultWriteDenied):
        writer.write_raw("alias.md", MARKED, "job-1")
    assert notes_file.read_text(encoding="utf-8") == "generator: jarvisd\nmine\n"


# --- allowed targets --------------------------------------------------------------------


def test_allowed_raw_candidates_and_session(sessions_on: Config, audit: AuditLog,
                                            tmp_vault: Path) -> None:
    w = VaultWriter(sessions_on, audit)
    r1 = w.write_raw("digest-2026-10-06.md", MARKED, "digest-2026-10-06")
    r2 = w.write_raw("candidates/x.md", MARKED, "job-2")
    r3 = w.write_session("x", MARKED, "job-3")
    assert r1.path == tmp_vault / "raw" / "jarvis" / "digest-2026-10-06.md"
    assert r2.path == tmp_vault / "raw" / "jarvis" / "candidates" / "x.md"
    assert r3.path == tmp_vault / "sessions" / "jarvis-x.md"
    assert (r1.rel, r2.rel, r3.rel) == ("raw/jarvis/digest-2026-10-06.md",
                                        "raw/jarvis/candidates/x.md", "sessions/jarvis-x.md")
    for result in (r1, r2, r3):
        assert isinstance(result, WriteResult)
        assert result.path.read_text(encoding="utf-8") == MARKED
        assert result.fallback_used is False
    assert audit.verify() == (True, None)
    assert _violations(audit) == []


def test_session_slug_may_already_carry_the_prefix(sessions_on: Config, audit: AuditLog) -> None:
    w = VaultWriter(sessions_on, audit)
    assert w.write_session("jarvis-y", MARKED, "j").rel == "sessions/jarvis-y.md"


def test_session_note_is_disabled_by_config(writer: VaultWriter, audit: AuditLog, tmp_vault: Path,
                                            tmp_cfg: Config) -> None:
    assert tmp_cfg.digest.write_session_note is False
    with pytest.raises(VaultWriteDenied):
        writer.write_session("x", MARKED, "j")
    assert not (tmp_vault / "sessions" / "jarvis-x.md").exists()
    assert _violations(audit)[-1]["reason"] == "session_notes_disabled"


# --- marker rule ------------------------------------------------------------------------


def test_existing_file_without_marker_is_never_overwritten(
        writer: VaultWriter, audit: AuditLog, tmp_vault: Path) -> None:
    target = tmp_vault / "raw" / "jarvis" / "digest-2026-10-06.md"
    target.write_text("# my own note\nhand written\n", encoding="utf-8", newline="\n")
    with pytest.raises(VaultWriteDenied):
        writer.write_raw("digest-2026-10-06.md", MARKED, "job-1")
    assert target.read_text(encoding="utf-8") == "# my own note\nhand written\n"
    assert _violations(audit)[-1]["reason"] == "existing_file_not_ours"
    assert not list(target.parent.glob("*-r2.md"))


def test_marker_beyond_first_400_bytes_does_not_count(writer: VaultWriter, tmp_vault: Path) -> None:
    target = tmp_vault / "raw" / "jarvis" / "late.md"
    target.write_text("x" * 450 + "\ngenerator: jarvisd\n", encoding="utf-8")
    with pytest.raises(VaultWriteDenied):
        writer.write_raw("late.md", MARKED, "job-1")


def test_existing_file_with_marker_is_replaced(writer: VaultWriter, tmp_vault: Path) -> None:
    writer.write_raw("d.md", MARKED, "job-1")
    newer = MARKED + "- two\n"
    result = writer.write_raw("d.md", newer, "job-2")
    assert (tmp_vault / "raw" / "jarvis" / "d.md").read_text(encoding="utf-8") == newer
    assert result.replaced is True


def test_new_content_without_marker_is_refused(writer: VaultWriter, audit: AuditLog,
                                               tmp_vault: Path) -> None:
    """A file we write without the marker could never be refreshed, so it is not written."""
    with pytest.raises(VaultWriteDenied):
        writer.write_raw("d.md", "# no frontmatter\n", "job-1")
    assert not (tmp_vault / "raw" / "jarvis" / "d.md").exists()
    assert _violations(audit)[-1]["reason"] == "missing_generator_marker"


# --- bytes on disk ----------------------------------------------------------------------


def test_bytes_sha_no_bom_lf_and_no_temp(writer: VaultWriter, tmp_vault: Path) -> None:
    text = "\ufeff" + MARKED.replace("\n", "\r\n") + "- caf\u00e9\n"
    result = writer.write_raw("d.md", text, "job-1")
    data = result.path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data
    assert data.decode("utf-8") == MARKED + "- caf\u00e9\n"
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.size == len(data)
    assert [p.name for p in result.path.parent.iterdir()] == ["d.md"]


def test_dashes_are_replaced_not_written(writer: VaultWriter) -> None:
    text = MARKED + "- a \u2014 b \u2013 c\n"
    data = writer.write_raw("d.md", text, "job-1").path.read_text(encoding="utf-8")
    assert "\u2014" not in data and "\u2013" not in data


def test_audit_has_intent_before_write_and_no_text(writer: VaultWriter, audit: AuditLog) -> None:
    secret_text = MARKED + f"- {CANARY}\n"
    result = writer.write_raw("d.md", secret_text, "job-9")
    events = [r["event"] for r in audit.records() if r["event"].startswith("vault_")]
    assert events == ["vault_intent", "vault_write"]
    intent, done = audit.records(events=["vault_intent", "vault_write"])
    assert intent["job_id"] == done["job_id"] == "job-9"
    assert intent["rel"] == done["rel"] == "raw/jarvis/d.md"
    assert intent["sha256"] == done["sha256"] == result.sha256
    assert done["ok"] is True and done["fallback_used"] is False
    assert CANARY not in audit.path.read_text(encoding="utf-8")


# --- busy destination -------------------------------------------------------------------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None


@contextmanager
def _held_open_without_sharing(path: Path) -> Iterator[None]:
    """Open path with share mode 0 so any other open or replace gets a sharing violation."""
    if _k32 is None:
        pytest.skip("Windows file handles only")
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = _k32.CreateFileW(str(path), 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error(), "CreateFileW failed in the test")
    try:
        yield
    finally:
        _k32.CloseHandle(handle)


def test_busy_destination_backs_off_then_uses_r2(writer: VaultWriter, audit: AuditLog,
                                                 tmp_vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_vault / "raw" / "jarvis" / "digest-2026-10-06.md"
    target.write_text(MARKED, encoding="utf-8", newline="\n")
    sleeps: list[float] = []
    monkeypatch.setattr(fsio.time, "sleep", lambda s: sleeps.append(s))
    with _held_open_without_sharing(target):
        result = writer.write_raw("digest-2026-10-06.md", MARKED + "- fresh\n", "job-1")
    assert sleeps == list(fsio.BACKOFF_SECONDS)
    assert result.fallback_used is True
    assert result.path == target.with_name("digest-2026-10-06-r2.md")
    assert result.rel == "raw/jarvis/digest-2026-10-06-r2.md"
    assert result.path.read_text(encoding="utf-8") == MARKED + "- fresh\n"
    assert result.sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest()
    assert target.read_text(encoding="utf-8") == MARKED
    assert sorted(p.name for p in target.parent.iterdir()) == [
        "digest-2026-10-06-r2.md", "digest-2026-10-06.md"]
    done = audit.records(events=["vault_write"])[-1]
    assert done["fallback_used"] is True and done["ok"] is True
    assert done["rel"] == "raw/jarvis/digest-2026-10-06-r2.md"


def test_busy_destination_without_a_marker_check_failure(writer: VaultWriter, tmp_vault: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """A held file that cannot even be read is busy, not foreign: no violation, just -r2."""
    target = tmp_vault / "raw" / "jarvis" / "held.md"
    target.write_text(MARKED, encoding="utf-8", newline="\n")
    monkeypatch.setattr(fsio.time, "sleep", lambda s: None)
    with _held_open_without_sharing(target):
        result = writer.write_raw("held.md", MARKED, "job-1")
    assert result.fallback_used is True


def test_both_names_busy_raises_filebusy_and_leaves_no_temp(
        writer: VaultWriter, audit: AuditLog, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_vault / "raw" / "jarvis"
    first, second = raw / "d.md", raw / "d-r2.md"
    for p in (first, second):
        p.write_text(MARKED, encoding="utf-8", newline="\n")
    monkeypatch.setattr(fsio.time, "sleep", lambda s: None)
    with _held_open_without_sharing(first), _held_open_without_sharing(second):
        with pytest.raises(fsio.FileBusy):
            writer.write_raw("d.md", MARKED + "- new\n", "job-1")
    assert sorted(p.name for p in raw.iterdir()) == ["d-r2.md", "d.md"]
    assert first.read_text(encoding="utf-8") == MARKED
    assert audit.records(events=["vault_write"])[-1]["ok"] is False


def test_r2_name_that_is_foreign_is_not_overwritten(writer: VaultWriter, tmp_vault: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_vault / "raw" / "jarvis"
    (raw / "d.md").write_text(MARKED, encoding="utf-8", newline="\n")
    (raw / "d-r2.md").write_text("hand written\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(fsio.time, "sleep", lambda s: None)
    with _held_open_without_sharing(raw / "d.md"):
        with pytest.raises(VaultWriteDenied):
            writer.write_raw("d.md", MARKED, "job-1")
    assert (raw / "d-r2.md").read_text(encoding="utf-8") == "hand written\n"
