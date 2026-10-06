"""The only code that writes under the brain tree (design section 9).

It takes a relative name, never a path, and allows exactly two places:
- files directly under <brain>/raw/jarvis/ and under <brain>/raw/jarvis/candidates/
- <brain>/sessions/jarvis-<slug>.md, and only while [digest].write_session_note is true

One file may also be appended to (APPENDABLE, plan Q2): the human-confirmed task list. The append
is audited like a write, creates the file from a generator-marked header, and refuses a file
that lacks the marker, so a hand-made file of the same name is never touched.

Everything else raises VaultWriteDenied and leaves a vault_violation record: telos, notes,
Documents/Work, '..', absolute paths, drive letters, alternate data streams, junctions and
symlinks that redirect a directory, and any existing file that is not ours. The vault is
synced by Syncthing and read by Obsidian and Basic Memory, so a write is hidden-temp then
os.replace with a retry on sharing violations, then a "-r2" sibling name, never a silent drop.

Layer L1. Imports common, config, fsio, tier and audit.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from jarvisd import fsio
from jarvisd.audit import AuditLog
from jarvisd.common import sha256_hex, strip_dashes
from jarvisd.config import Config
from jarvisd.tier import canonical, path_hit

RAW_SUBPATH = "raw/jarvis"
CANDIDATES_DIR = "candidates"
SESSION_PREFIX = "jarvis-"
MARKER_BYTES = 400
MAX_BYTES = 2_000_000
# The only raw/jarvis files append_raw accepts. A generator-owned log that grows block by block;
# every other file the daemon writes is regenerated whole and goes through write_raw.
APPENDABLE = frozenset({"confirmed-tasks.md"})

# One name component: starts alphanumeric, no spaces, no ':' or '~' or backslash, so a
# trailing dot or space, a stream suffix and an 8.3 alias cannot even be spelled.
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,80}")
_MARKER = re.compile(rb"(?m)^generator: jarvisd[ \t]*\r?$")
_DEVICES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT: symlinks and junctions


class VaultWriteDenied(Exception):
    """The target is outside what the daemon may write. str() is the reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class WriteResult:
    """What was written. rel is vault-relative with forward slashes (safe to log and to show)."""

    path: Path
    rel: str
    sha256: str
    size: int
    kind: str  # "raw" or "session"
    fallback_used: bool = False
    replaced: bool = False


@dataclass(frozen=True)
class _Plan:
    kind: str
    base: Path  # the configured directory for this kind
    expected_base: str  # canonical form it must resolve to
    subdir: str | None
    filename: str
    label: str  # vault-relative directory, e.g. "raw/jarvis/candidates"

    @property
    def parent(self) -> Path:
        return self.base / self.subdir if self.subdir else self.base

    @property
    def expected_parent(self) -> str:
        return f"{self.expected_base}/{self.subdir}" if self.subdir else self.expected_base


def _check_component(part: str) -> None:
    if not _COMPONENT.fullmatch(part):
        raise VaultWriteDenied("bad_name")
    if part.split(".")[0].casefold() in _DEVICES:
        raise VaultWriteDenied("bad_name")


def _split_raw_name(name: object) -> tuple[str | None, str]:
    """A raw name is 'file.md' or 'candidates/file.md'. Anything else is refused."""
    if not isinstance(name, str) or not name:
        raise VaultWriteDenied("bad_name")
    parts = name.split("/")
    if len(parts) > 2 or (len(parts) == 2 and parts[0] != CANDIDATES_DIR):
        raise VaultWriteDenied("bad_name")
    for part in parts:
        _check_component(part)
    filename = parts[-1]
    if not filename.endswith(".md") or len(filename) == 3:
        raise VaultWriteDenied("bad_name")
    return (parts[0] if len(parts) == 2 else None), filename


def _session_filename(slug: object) -> str:
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise VaultWriteDenied("bad_name")
    return slug + ".md" if slug.startswith(SESSION_PREFIX) else f"{SESSION_PREFIX}{slug}.md"


def _prepare(text: object) -> bytes:
    """Normalize to the bytes that land on disk: no BOM, LF only, no U+2014 or U+2013."""
    if not isinstance(text, str):
        raise VaultWriteDenied("bad_content")
    body = strip_dashes(text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n"))
    data = body.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise VaultWriteDenied("too_large")
    # A file without the marker could never be refreshed by the next run.
    if not _MARKER.search(data[:MARKER_BYTES]):
        raise VaultWriteDenied("missing_generator_marker")
    return data


def _prepare_fragment(text: object) -> bytes:
    """Normalize text that will be appended to a file that already carries the marker."""
    if not isinstance(text, str):
        raise VaultWriteDenied("bad_content")
    body = strip_dashes(text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n"))
    return body.encode("utf-8")


def _read_all(target: Path) -> bytes:
    """Whole file, waiting out a sharing violation like _read_head does (FileBusy at the end)."""
    for attempt in range(len(fsio.BACKOFF_SECONDS) + 1):
        try:
            with open(target, "rb") as fh:
                return fh.read()
        except PermissionError as exc:
            if attempt >= len(fsio.BACKOFF_SECONDS):
                raise fsio.FileBusy(f"could not read {target.name}: {exc}") from exc
            fsio.time.sleep(fsio.BACKOFF_SECONDS[attempt])
    raise AssertionError("unreachable")  # pragma: no cover


def _read_head(target: Path) -> bytes:
    """First MARKER_BYTES bytes, waiting out a sharing violation like a write would.

    A file held open by Obsidian or Syncthing cannot be read either; that is "busy", not
    "foreign", so it ends in FileBusy and the caller moves to the -r2 name.
    """
    for attempt in range(len(fsio.BACKOFF_SECONDS) + 1):
        try:
            with open(target, "rb") as fh:
                return fh.read(MARKER_BYTES)
        except PermissionError as exc:
            if attempt >= len(fsio.BACKOFF_SECONDS):
                raise fsio.FileBusy(f"could not read {target.name}: {exc}") from exc
            fsio.time.sleep(fsio.BACKOFF_SECONDS[attempt])
    raise AssertionError("unreachable")  # pragma: no cover


class VaultWriter:
    """Guarded writer for brain/raw/jarvis and brain/sessions/jarvis-*.md."""

    def __init__(self, cfg: Config, audit: AuditLog) -> None:
        self._cfg = cfg
        self._audit = audit

    # --- public API ---------------------------------------------------------------------

    def write_raw(self, name: str, text: str, job_id: str | None) -> WriteResult:
        """Write raw/jarvis/<name> ('x.md' or 'candidates/x.md'). Raises VaultWriteDenied."""
        return self._write("raw", name, text, job_id)

    def write_session(self, slug: str, text: str, job_id: str | None) -> WriteResult:
        """Write sessions/jarvis-<slug>.md, only while [digest].write_session_note is true."""
        return self._write("session", slug, text, job_id)

    def append_raw(self, name: str, text: str, job_id: str | None, *, header: str) -> WriteResult:
        """Append text to raw/jarvis/<name>, creating it from `header` when absent.

        Only names in APPENDABLE. `header` must carry the generator marker (it is the file's
        first bytes), and an existing file without the marker is refused untouched. There is no
        "-r2" fallback: splitting an append-only list across two files would hide tasks.
        """
        return self._append(name, text, header, job_id)

    def check_append(self, name: str) -> str:
        """"create" or "append" if append_raw(name) would be allowed now, else VaultWriteDenied.

        Touches nothing and audits nothing: `jarvis tracker check` calls it.
        """
        if not isinstance(name, str) or name not in APPENDABLE:
            raise VaultWriteDenied("not_appendable")
        plan = self._plan("raw", name)
        target = self._authorize(plan, plan.filename)
        return "append" if self._check_marker(target) else "create"

    def raw_path(self, name: str) -> Path:
        """Where write_raw(name) would put the file, for readers (jarvis digest, status).

        Validates the name like a write does but touches nothing and audits nothing.
        """
        subdir, filename = _split_raw_name(name)
        base = self._cfg.paths.vault_write_raw
        return (base / subdir / filename) if subdir else (base / filename)

    # --- planning and authorization -----------------------------------------------------

    def _plan(self, kind: str, name: object) -> _Plan:
        brain = canonical(self._cfg.paths.brain_root)
        if kind == "raw":
            subdir, filename = _split_raw_name(name)
            label = RAW_SUBPATH + (f"/{subdir}" if subdir else "")
            return _Plan(kind, self._cfg.paths.vault_write_raw, f"{brain}/{RAW_SUBPATH}", subdir, filename, label)
        if not self._cfg.digest.write_session_note:
            raise VaultWriteDenied("session_notes_disabled")
        filename = _session_filename(name)
        return _Plan(kind, self._cfg.paths.vault_write_sessions, f"{brain}/sessions", None, filename,
                     "sessions")

    def _authorize(self, plan: _Plan, filename: str) -> Path:
        """Prove the parent and the target are where they claim to be. Returns the target."""
        if canonical(plan.base) != plan.expected_base:
            # The configured directory is not where the vault says it is (junction or typo).
            raise VaultWriteDenied("base_mismatch")
        parent = plan.parent
        if canonical(parent) != plan.expected_parent:
            # Realpath parent equality: a junction or symlink inside raw/jarvis lands here.
            raise VaultWriteDenied("parent_realpath_mismatch")
        target = parent / filename
        if path_hit(target, self._cfg) is not None:
            raise VaultWriteDenied("tier_floor")
        self._check_not_forbidden(plan.expected_parent)
        if canonical(target) != f"{plan.expected_parent}/{filename.casefold()}":
            raise VaultWriteDenied("target_realpath_mismatch")
        self._check_target_kind(target)
        return target

    def _check_not_forbidden(self, canon: str) -> None:
        for forbidden in self._cfg.paths.vault_forbidden:
            try:
                root = canonical(forbidden).rstrip("/")
            except (ValueError, OSError):
                raise VaultWriteDenied("forbidden_unresolvable") from None
            if canon == root or canon.startswith(root + "/"):
                raise VaultWriteDenied("forbidden_path")

    @staticmethod
    def _check_target_kind(target: Path) -> None:
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            return
        except OSError:
            raise VaultWriteDenied("target_unreadable") from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
            raise VaultWriteDenied("target_is_link")
        if not stat.S_ISREG(info.st_mode):
            raise VaultWriteDenied("target_not_a_file")

    def _check_marker(self, target: Path) -> bool:
        """True if the target exists and is ours, False if absent. Raises if it is foreign."""
        if not os.path.lexists(target):
            return False
        if not _MARKER.search(_read_head(target)):
            raise VaultWriteDenied("existing_file_not_ours")
        return True

    # --- the write ----------------------------------------------------------------------

    def _violation(self, kind: str, name: object, reason: str, job_id: str | None) -> None:
        self._audit.emit("vault_violation", job_id=job_id, kind=kind, reason=reason,
                         name=name if isinstance(name, str) else repr(name))

    def _write(self, kind: str, name: object, text: object, job_id: str | None) -> WriteResult:
        try:
            plan = self._plan(kind, name)
            data = _prepare(text)
        except VaultWriteDenied as exc:
            self._violation(kind, name, exc.reason, job_id)
            raise
        digest = sha256_hex(data)
        rel = f"{plan.label}/{plan.filename}"
        self._audit.emit("vault_intent", job_id=job_id, kind=kind, rel=rel, sha256=digest, bytes=len(data))
        try:
            return self._commit(plan, data, digest, rel, job_id)
        except VaultWriteDenied as exc:
            self._violation(kind, name, exc.reason, job_id)
            raise
        except OSError as exc:
            self._audit.emit("vault_write", job_id=job_id, kind=kind, rel=rel, sha256=digest, bytes=len(data),
                             ok=False, fallback_used=False, error=type(exc).__name__)
            raise

    def _append(self, name: object, text: object, header: object, job_id: str | None) -> WriteResult:
        kind = "raw"
        try:
            if not isinstance(name, str) or name not in APPENDABLE:
                raise VaultWriteDenied("not_appendable")
            plan = self._plan(kind, name)
            head = _prepare(header)
            add = _prepare_fragment(text)
        except VaultWriteDenied as exc:
            self._violation(kind, name, exc.reason, job_id)
            raise
        digest = sha256_hex(add)
        rel = f"{plan.label}/{plan.filename}"
        self._audit.emit("vault_intent", job_id=job_id, kind=kind, rel=rel, sha256=digest, bytes=len(add),
                         op="append")
        try:
            return self._commit_append(plan, head, add, digest, rel, job_id)
        except VaultWriteDenied as exc:
            self._violation(kind, name, exc.reason, job_id)
            raise
        except OSError as exc:
            self._audit.emit("vault_write", job_id=job_id, kind=kind, rel=rel, sha256=digest, bytes=len(add),
                             ok=False, fallback_used=False, op="append", error=type(exc).__name__)
            raise

    def _commit_append(self, plan: _Plan, head: bytes, add: bytes, digest: str, rel: str,
                       job_id: str | None) -> WriteResult:
        target = self._authorize(plan, plan.filename)
        with fsio.path_lock(target):
            exists = self._check_marker(target)
            if exists:
                old = _read_all(target)
                base = old if old.endswith(b"\n") else old + b"\n"
            else:
                base = head
            data = base + add
            if len(data) > MAX_BYTES:
                raise VaultWriteDenied("too_large")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                raise VaultWriteDenied("target_unreadable") from None
            fsio.atomic_write_text(target, text)
        self._audit.emit("vault_write", job_id=job_id, kind=plan.kind, rel=rel, intended_rel=rel, sha256=digest,
                         bytes=len(add), ok=True, fallback_used=False, replaced=exists, op="append")
        return WriteResult(path=target, rel=rel, sha256=digest, size=len(add), kind=plan.kind, replaced=exists)

    def _commit(self, plan: _Plan, data: bytes, digest: str, rel: str, job_id: str | None) -> WriteResult:
        names = [plan.filename, plan.filename[:-3] + "-r2.md"]
        for index, filename in enumerate(names):
            target = self._authorize(plan, filename)
            try:
                with fsio.path_lock(target):
                    replaced = self._check_marker(target)
                    fsio.atomic_write_text(target, data.decode("utf-8"))
            except fsio.FileBusy:
                if index == len(names) - 1:
                    raise
                continue
            return self._done(plan, filename, target, data, digest, index > 0, replaced, rel, job_id)
        raise AssertionError("unreachable")  # pragma: no cover

    def _done(self, plan: _Plan, filename: str, target: Path, data: bytes, digest: str,
              fallback: bool, replaced: bool, intended_rel: str, job_id: str | None) -> WriteResult:
        rel = f"{plan.label}/{filename}"
        self._audit.emit("vault_write", job_id=job_id, kind=plan.kind, rel=rel, intended_rel=intended_rel,
                         sha256=digest, bytes=len(data), ok=True, fallback_used=fallback, replaced=replaced)
        return WriteResult(path=target, rel=rel, sha256=digest, size=len(data), kind=plan.kind,
                           fallback_used=fallback, replaced=replaced)
