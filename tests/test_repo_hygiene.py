"""Repo hygiene (design D10, sections 14 and 16): the repo must stay publishable.

These tests read the working tree through git, so they cover files that are tracked and files
that are new but not yet committed (the verifier commits after the tests pass). Ignored files
are out of scope on purpose: state/, queue/, logs/ and jarvis.local.toml are where private
material is allowed to live.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from jarvisd import ROOT

# Built from code points so this file never contains the characters it forbids.
EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)

DASH_SCAN_DIRS = ("jarvisd", "tests", "docs", "deploy", "bin", ".github")
# Root files are scanned too: the README is prose written by this project, and the publishing
# files (licence, citation, changelog) and the configs must be as clean as the code.
DASH_SCAN_FILES = (
    "README.md", "CONTRIBUTING.md", "CHANGELOG.md", "CITATION.cff", "LICENSE", "pyproject.toml", "jarvis.toml",
    "jarvis.local.toml.example", "jarvis.cmd", "requirements.lock", ".gitignore", ".gitattributes",
)

# Files exempt from the dash rule, each with the exact number of dashes it may keep. Empty since
# the publishing scrub removed the last two (quoted tool output in docs/sandbox-policy.md).
FROZEN_PHASE0_DASHES: dict[str, int] = {}

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pyc", ".zip", ".gz", ".whl", ".db", ".sqlite"}

# A concrete drive-letter path that ends inside the sensitive telos folder. Globs and deny
# entries such as "**/brain/telos/sensitive/**" are protective and are not absolute paths.
# The pattern is assembled so this file does not match itself.
_SENSITIVE_TAIL = "brain" + r"[\\/]" + "telos" + r"[\\/]" + "sensitive"
SENSITIVE_ABS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s'\"`<>|]*" + _SENSITIVE_TAIL, re.IGNORECASE)


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr.strip()}"
    return result.stdout


def repo_files() -> list[str]:
    """Tracked files plus new files that are not ignored, as forward slash paths."""
    out = _git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    names = sorted({n for n in out.split("\0") if n})
    return [n for n in names if (ROOT / n).is_file()]


def read_text_or_none(rel: str) -> str | None:
    path = ROOT / rel
    if path.suffix.lower() in BINARY_SUFFIXES:
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _in_dash_scope(rel: str) -> bool:
    return rel.split("/", 1)[0] in DASH_SCAN_DIRS or rel in DASH_SCAN_FILES


def test_no_em_dashes_in_tracked_files() -> None:
    offenders: list[str] = []
    for rel in repo_files():
        if not _in_dash_scope(rel):
            continue
        text = read_text_or_none(rel)
        if text is None:
            continue
        count = text.count(EM_DASH) + text.count(EN_DASH)
        allowed = FROZEN_PHASE0_DASHES.get(rel, 0)
        if count > allowed:
            first = next(i for i, ln in enumerate(text.splitlines(), 1) if EM_DASH in ln or EN_DASH in ln)
            offenders.append(f"{rel}: {count} found, {allowed} allowed, first on line {first}")
    assert not offenders, "U+2014 or U+2013 found:\n" + "\n".join(offenders)


def test_frozen_phase0_exemption_is_exact() -> None:
    # If a frozen file loses its dashes the exemption is dead weight and should be removed.
    for rel, allowed in FROZEN_PHASE0_DASHES.items():
        text = read_text_or_none(rel)
        assert text is not None, f"{rel} is listed as frozen but cannot be read"
        assert text.count(EM_DASH) + text.count(EN_DASH) == allowed, (
            f"{rel} no longer has exactly {allowed} dashes; update FROZEN_PHASE0_DASHES"
        )


@pytest.mark.parametrize("sample", [
    "state/anything.json", "queue/pending/x.json", "logs/jarvisd-audit.jsonl", ".venv/Scripts/python.exe",
    "jarvis.local.toml", "scratch.tmp", "state/hygiene-denylist.txt",
])
def test_gitignore_covers_runtime_dirs(sample: str) -> None:
    result = subprocess.run(["git", "check-ignore", "-q", "--no-index", sample], cwd=ROOT)
    assert result.returncode == 0, f".gitignore does not cover {sample}"


def test_gitignore_does_not_hide_the_example_config() -> None:
    result = subprocess.run(["git", "check-ignore", "-q", "--no-index", "jarvis.local.toml.example"], cwd=ROOT)
    assert result.returncode == 1, "jarvis.local.toml.example must stay trackable"


def test_no_absolute_sensitive_path_in_repo_files() -> None:
    offenders: list[str] = []
    for rel in repo_files():
        text = read_text_or_none(rel)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if SENSITIVE_ABS_PATH.search(line):
                offenders.append(f"{rel}:{number}")
    assert not offenders, "absolute path into the sensitive telos folder at: " + ", ".join(offenders)


def test_sensitive_path_pattern_catches_the_real_shape() -> None:
    # Guards the guard: a pattern that matches nothing would pass the test above forever.
    probe = "C:" + "/Users/someone/" + "brain" + "/telos/" + "sensitive" + "/x.md"
    assert SENSITIVE_ABS_PATH.search(probe)
    assert SENSITIVE_ABS_PATH.search(probe.replace("/", "\\"))
    assert not SENSITIVE_ABS_PATH.search("**/" + "brain" + "/telos/" + "sensitive" + "/**")


def test_repo_root_has_no_claude_config() -> None:
    assert not (ROOT / "CLAUDE.md").exists(), "CLAUDE.md must not live in this repo (D10)"
    assert not (ROOT / ".claude").exists(), ".claude/ must not live in this repo (D10)"


# Names that must never reach a tracked file, stored as sha256 of the casefolded word so this
# public file does not itself spell them (an NDA laboratory, an NDA project and a personal
# GitHub account; the account that publishes this repository is public by choice and is not
# listed). Only whole words are matched; the private substring list below is the broader net and
# lives where private material is allowed to live.
BUILTIN_DENY_SHA256 = (
    "07a0d303fd66951862f305729bf8c2e2a04fb590c5de71c3fdb3653a727936f6",
    "e43025b61c12c2a437b482bbea59c2042cae951988530a4c67053c8adb963a15",
    "5bff1319b99645ad38f517a7304cca02bb4b4e9b7b9fdf99bdd76101039e0e04",
    # The employer, two of its product names and a client-type repository, a university, the
    # private scheduled task of the owner's notes routine, and the country of the owner's travel.
    "97e3efbcb9c13ef2356e6f7699594985da3081235558670b9124e038119dd983",
    "ac7f7a58b824b54163c758f6faa89328d6b0ccd39e3dd2e3bc9d979f9c1c2f52",
    "0b34adc7590633b3c25999682a51b0279722b2a63d1b90e6494aa269760f0eda",
    "e7690f050af370f0eeef17377f6b4d9ab909960eae9e6fa073a149b3b21b99ea",
    "e5ee7668be3dd8a934e46ae58d9d52c12a360133c47028dda1be8c36b9b1789f",
    "e8f139dc509ad5004dd3b1ed5997d64c4544627d89975d86773898ff6539bd5f",
    "52118e19d4efe87556e2d64de6d32fe753ff0caee45d4c14ebbbe0a50d8a023d",
)
_WORD = re.compile(r"\w+")


def hashed_hits(text: str, hashes: tuple[str, ...] | list[str]) -> list[int]:
    """1-based indexes into `hashes` of every entry that appears as a whole word in text."""
    wanted = {digest: index for index, digest in enumerate(hashes, 1)}
    found: set[int] = set()
    for word in set(_WORD.findall(text.casefold())):
        # \w includes the underscore, so an identifier such as a_b_c is one word; its parts
        # are checked too, or a config key that embeds a name would slip through.
        for candidate in {word, *word.split("_")}:
            index = wanted.get(hashlib.sha256(candidate.encode("utf-8")).hexdigest())
            if index is not None:
                found.add(index)
    return sorted(found)


def private_terms(local_toml: Path, legacy_txt: Path) -> list[str]:
    """Terms from `[hygiene].deny_substrings` in jarvis.local.toml plus the older text file.

    A local file that cannot be parsed raises: silently scanning for nothing is the failure
    this exists to prevent.
    """
    terms: list[str] = []
    if local_toml.is_file():
        data = tomllib.loads(local_toml.read_text(encoding="utf-8-sig"))
        listed = data.get("hygiene", {}).get("deny_substrings", [])
        assert isinstance(listed, list) and all(isinstance(t, str) for t in listed), (
            "[hygiene].deny_substrings must be a list of strings"
        )
        terms.extend(listed)
    if legacy_txt.is_file():
        terms.extend(legacy_txt.read_text(encoding="utf-8").splitlines())
    folded = (t.strip().casefold() for t in terms)
    return [t for t in folded if t and not t.startswith("#")]


def substring_hits(files: dict[str, str], terms: list[str]) -> list[str]:
    """`path matches denylist line N` for every term found. Never prints the term itself."""
    hits: list[str] = []
    for rel, text in files.items():
        folded = text.casefold()
        for index, term in enumerate(terms, 1):
            if term in folded or term in rel.casefold():
                hits.append(f"{rel} matches denylist line {index}")
    return hits


def _tracked_texts() -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in repo_files():
        text = read_text_or_none(rel)
        if text is not None:
            out[rel] = text
    return out


def test_builtin_denylist_names_are_absent_from_repo_files() -> None:
    hits = [
        f"{rel} matches built-in denylist entry {index}"
        for rel, text in _tracked_texts().items()
        for index in hashed_hits(text + "\n" + rel.replace("/", " "), BUILTIN_DENY_SHA256)
    ]
    assert not hits, "\n".join(hits)


def test_hashed_denylist_matches_whole_words_only() -> None:
    # Guards the guard: a hash list that matches nothing would pass the test above forever.
    secret = "zzsynthetic-name"
    digest = hashlib.sha256(secret.casefold().encode("utf-8")).hexdigest()
    assert hashed_hits(f"see the {secret.upper()} file", [digest]) == []  # a hyphen splits words
    word = "zzsyntheticname"
    digest = hashlib.sha256(word.encode("utf-8")).hexdigest()
    assert hashed_hits(f"see the {word.upper()} file", ["0" * 64, digest]) == [2]
    assert hashed_hits("see the zzsyntheticnames file", [digest]) == []
    # Identifiers: underscore-joined parts are checked, so a key that embeds a name is caught.
    assert hashed_hits(f"{word}_metadata_to_claude = false", [digest]) == [1]
    assert hashed_hits(f"is_{word}", [digest]) == [1]
    assert hashed_hits(f"{word}x_metadata", [digest]) == []
    assert len(BUILTIN_DENY_SHA256) == len(set(BUILTIN_DENY_SHA256))
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in BUILTIN_DENY_SHA256)


def test_private_terms_are_read_from_the_local_toml_key_the_docs_name(tmp_path: Path) -> None:
    # jarvis.local.toml.example and docs/v1-operations.md tell the owner to use this key.
    local = tmp_path / "jarvis.local.toml"
    local.write_text('[hygiene]\ndeny_substrings = ["Acme-Corp", "  ", "Project X"]\n', encoding="utf-8")
    legacy = tmp_path / "hygiene-denylist.txt"
    legacy.write_text("# comment\nOld Term\n", encoding="utf-8")
    assert private_terms(local, legacy) == ["acme-corp", "project x", "old term"]
    assert private_terms(tmp_path / "missing.toml", tmp_path / "missing.txt") == []
    hits = substring_hits({"docs/a.md": "We met Acme-Corp today", "docs/b.md": "clean"}, ["acme-corp"])
    assert hits == ["docs/a.md matches denylist line 1"]
    assert "acme" not in hits[0].replace("denylist", "")


def test_a_broken_local_toml_fails_the_hygiene_check_loudly(tmp_path: Path) -> None:
    local = tmp_path / "jarvis.local.toml"
    local.write_text("[hygiene\ndeny_substrings = [", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        private_terms(local, tmp_path / "none.txt")
    local.write_text('[hygiene]\ndeny_substrings = "not a list"\n', encoding="utf-8")
    with pytest.raises(AssertionError):
        private_terms(local, tmp_path / "none.txt")


def test_private_denylist_terms_are_absent_from_repo_files() -> None:
    terms = private_terms(ROOT / "jarvis.local.toml", ROOT / "state" / "hygiene-denylist.txt")
    if not terms:
        pytest.skip("no private terms: set [hygiene].deny_substrings in jarvis.local.toml (D10)")
    hits = substring_hits(_tracked_texts(), terms)
    assert not hits, "\n".join(hits)


# The Phase 0 narrative used to be the top of the README and was pinned here by length and hash.
# The README was rewritten for a public audience and the narrative moved to docs/phase0.md, where
# tests/test_docs.py pins it the same way.


def test_register_script_matches_the_design_block() -> None:
    path = ROOT / "deploy" / "register-jarvisd-task.ps1"
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 without BOM"
    assert b"\r" not in raw, "LF line endings"
    text = raw.decode("utf-8")
    for needle in (
        "-m jarvisd serve --task", "New-ScheduledTaskTrigger -AtLogOn", "-Daily -At 06:00",
        "-StartWhenAvailable", "-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries",
        "-MultipleInstances IgnoreNew", "-ExecutionTimeLimit ([TimeSpan]::Zero)", "-RestartCount 5",
        "-RestartInterval (New-TimeSpan -Minutes 2)", "-LogonType Interactive -RunLevel Limited",
        "Register-ScheduledTask", "'JarvisDaemon'", "-Force", "Unregister-ScheduledTask",
        "[switch]$Unregister", "SupportsShouldProcess", "Get-ScheduledTaskInfo",
    ):
        assert needle in text, f"register-jarvisd-task.ps1 is missing: {needle}"
    assert "-WakeToRun" not in text, "design keeps WakeToRun off (section 10)"


def test_register_script_agrees_with_the_cli_block() -> None:
    # The CLI prints the same registration for `jarvis install-task`; they must not drift.
    from jarvisd.cli import registration_block

    script = (ROOT / "deploy" / "register-jarvisd-task.ps1").read_text(encoding="utf-8")
    for line in registration_block().splitlines():
        for token in re.findall(r"-(?:Daily -At \S+|RestartCount \d+|LogonType \w+|RunLevel \w+)", line):
            assert token in script, f"CLI block token {token!r} missing from the script"


def test_new_docs_exist_and_reference_spec_sections() -> None:
    for rel in ("docs/killswitch-v1-patch.md", "docs/v1-operations.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert len(text) > 500, f"{rel} is too short to be real"
        assert re.search(r"section \d+", text, re.IGNORECASE), f"{rel} cites no design section by number"
        assert not text.startswith(chr(0xFEFF))
        assert "\r" not in text
    patch = (ROOT / "docs" / "killswitch-v1-patch.md").read_text(encoding="utf-8")
    for needle in ("```diff", "-TargetAccount <owner>", "jarvisd serve --task", "hostile", "proposal"):
        assert needle in patch, f"killswitch-v1-patch.md is missing: {needle}"
    ops = (ROOT / "docs" / "v1-operations.md").read_text(encoding="utf-8")
    for needle in ("jarvis pause", "jarvis audit verify", "state/KILL", "Known gaps", "first-run"):
        assert needle.casefold() in ops.casefold(), f"v1-operations.md is missing: {needle}"


# --- generic identity rules (publishing, task P1) ----------------------------------------------
#
# The repo goes public, so no file may name the machine it was built on or the person's
# account on it. The rules below are generic (they work on any contributor's checkout) and
# the few legitimate hits are listed with a reason in tests/hygiene-allowlist.toml.
#
#   user-profile-path  a drive-letter path into a user profile folder. Placeholders such as
#                      <owner> or <you> are fine; a concrete name is not.
#   owner-name         the owner's account name or first or family name as a whole word, held
#                      as hashes so this public file does not spell it, plus the account,
#                      machine and host names of whoever runs the test (read from the
#                      environment and from the gitignored jarvis.local.toml [meta]).
#   tailscale          a Tailscale CGNAT address (the 100.64/10 block), a tailnet name (*.ts.net)
#                      or the Tailscale IPv6 prefix.
#   email              an e-mail address, except reserved synthetic domains.

ALLOWLIST_PATH = ROOT / "tests" / "hygiene-allowlist.toml"
RULES = ("user-profile-path", "owner-name", "tailscale", "email")

# sha256 of the casefolded whole words that identify the owner (first name, family name).
# The account name and the machine name are made of these words, so one list covers both.
OWNER_WORD_SHA256 = (
    "54c330d5fa02d666849bbf31f5b97395fe155b38a5ca09c719d279086c214e5e",
    "14bf33162f852832e80ce5cb834ca73f8465e846b55b4980aaedb299d0bbc521",
)

# <drive>:\Users\<name>, forward or back slashes, single or doubled (escaped in source).
_USER_PATH = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([^\\/\s'\"`|:*?]+)", re.IGNORECASE)
_TAILSCALE = re.compile(
    r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?![\d])"
    r"|\bfd7a:115c:a1e0:", re.IGNORECASE)
# A tailnet host name. Names that say "example" are the documented placeholders.
_TAILNET_NAME = re.compile(r"[A-Za-z0-9.-]+\.ts\.net\b", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
_SYNTHETIC_DOMAIN = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|invalid|example|test|localhost)$", re.IGNORECASE)
_ALNUM_WORD = re.compile(r"[a-z0-9]+")
# Generic account and host names that mean nothing on their own; never treated as identity.
_GENERIC_NAMES = {"user", "users", "admin", "administrator", "owner", "jarvis", "runner", "runneradmin",
                  "default", "public", "localhost", "desktop", "laptop", "windows", "github", "action"}


def environment_identity(local_toml: Path | None = None) -> list[str]:
    """Account, machine and host names of whoever runs the tests, as casefolded whole words.

    Names shorter than five characters or in a generic list are dropped: they would match
    ordinary prose. [meta] machine and owner_account of jarvis.local.toml are added when the
    file exists, since that file is where the real values live.
    """
    import getpass
    import os
    import socket

    raw: list[str] = [Path.home().name, os.environ.get("COMPUTERNAME", ""), os.environ.get("USERNAME", ""),
                      os.environ.get("USER", "")]
    for getter in (getpass.getuser, socket.gethostname):
        try:
            raw.append(getter())
        except (OSError, KeyError):
            pass
    path = local_toml if local_toml is not None else ROOT / "jarvis.local.toml"
    if path.is_file():
        meta = tomllib.loads(path.read_text(encoding="utf-8-sig")).get("meta", {})
        raw += [str(meta.get("machine", "")), str(meta.get("owner_account", ""))]
    words: set[str] = set()
    for name in raw:
        for word in _ALNUM_WORD.findall(name.casefold()):
            if len(word) >= 5 and word not in _GENERIC_NAMES:
                words.add(word)
    return sorted(words)


def scan_identity(rel: str, text: str, env_words: list[str] | tuple[str, ...] = ()) -> list[tuple[str, int]]:
    """`(rule, line number)` for every identity leak in `text`. Line 0 means the file name."""
    hits: list[tuple[str, int]] = []
    extra = set(env_words)

    def owner_hit(chunk: str) -> bool:
        words = set(_ALNUM_WORD.findall(chunk.casefold()))
        if words & extra:
            return True
        return any(hashlib.sha256(w.encode("utf-8")).hexdigest() in OWNER_WORD_SHA256 for w in words)

    if owner_hit(rel):
        hits.append(("owner-name", 0))
    for number, line in enumerate(text.splitlines(), 1):
        if any(not m.group(1).startswith("<") for m in _USER_PATH.finditer(line)):
            hits.append(("user-profile-path", number))
        if owner_hit(line):
            hits.append(("owner-name", number))
        if _TAILSCALE.search(line) or any("example" not in m.group(0).casefold()
                                          for m in _TAILNET_NAME.finditer(line)):
            hits.append(("tailscale", number))
        if any(not _SYNTHETIC_DOMAIN.search(m.group(1)) for m in _EMAIL.finditer(line)):
            hits.append(("email", number))
    return hits


def load_allowlist(path: Path = ALLOWLIST_PATH) -> list[dict[str, str]]:
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("allow", [])
    return [dict(e) for e in entries]


def identity_violations(files: dict[str, str], allow: list[dict[str, str]], env_words: list[str]) -> tuple[list[str], set[int]]:
    """Leaks that no allowlist entry covers, and the indexes of the entries that were used."""
    allowed = {(e["path"], e["rule"]): index for index, e in enumerate(allow)}
    used: set[int] = set()
    problems: list[str] = []
    for rel, text in files.items():
        for rule, number in scan_identity(rel, text, env_words):
            key = (rel, rule)
            if key in allowed:
                used.add(allowed[key])
                continue
            where = "file name" if number == 0 else f"line {number}"
            problems.append(f"{rel}: {rule} at {where}")
    return problems, used


def test_no_identity_leaks_in_repo_files() -> None:
    problems, _ = identity_violations(_tracked_texts(), load_allowlist(), environment_identity())
    assert not problems, "identity leak (fix the file, or allowlist it with a reason):\n" + "\n".join(problems)


def test_every_allowlist_entry_has_a_reason_and_is_still_needed() -> None:
    allow = load_allowlist()
    assert allow, "the allowlist is expected to name at least the licence and citation files"
    for entry in allow:
        assert set(entry) == {"path", "rule", "reason"}, f"bad allowlist entry keys: {sorted(entry)}"
        assert entry["rule"] in RULES, f"unknown rule {entry['rule']!r}"
        assert len(entry["reason"].strip()) >= 20, f"{entry['path']}: give a real reason"
        assert (ROOT / entry["path"]).is_file(), f"{entry['path']} is allowlisted but does not exist"
    pairs = [(e["path"], e["rule"]) for e in allow]
    assert len(pairs) == len(set(pairs)), "duplicate allowlist entries"
    _, used = identity_violations(_tracked_texts(), allow, environment_identity())
    stale = [f"{allow[i]['path']} [{allow[i]['rule']}]" for i in range(len(allow)) if i not in used]
    assert not stale, "allowlist entries that no longer match anything: " + ", ".join(stale)


def test_the_allowlist_file_does_not_spell_the_owner_either() -> None:
    text = ALLOWLIST_PATH.read_text(encoding="utf-8")
    assert scan_identity("tests/hygiene-allowlist.toml", text, environment_identity()) == []


def test_user_profile_path_rule_catches_concrete_names_and_allows_placeholders() -> None:
    drive = "C" + ":"
    assert scan_identity("a.md", f"cd {drive}/Users/bobsmith/repo") == [("user-profile-path", 1)]
    assert scan_identity("a.md", f"cd {drive}\\Users\\bobsmith\\repo") == [("user-profile-path", 1)]
    assert scan_identity("a.md", f'"{drive}\\\\Users\\\\bobsmith\\\\x"') == [("user-profile-path", 1)]
    assert scan_identity("a.md", f"{drive}/users/BobSmith") == [("user-profile-path", 1)]
    for placeholder in ("<owner>", "<you>", "<user>"):
        assert scan_identity("a.md", f"cd {drive}/Users/{placeholder}/repo") == []
    assert scan_identity("a.md", "cd %USERPROFILE%\\repo and ~/brain") == []


def test_tailscale_rule_catches_cgnat_addresses_names_and_the_ipv6_prefix() -> None:
    for text in ("ssh 100." "101.102.103", "host 100." "64.0.1:22", "at 100." "127.255.254", "box.tail1234." "ts.net",
                 "fd7a:115c:" "a1e0::1"):
        assert [r for r, _ in scan_identity("a.md", text)] == ["tailscale"], text
    assert scan_identity("a.md", "https://jarvis-host.example-tailnet." "ts.net") == []
    for text in ("version 100.2.3.4", "10.100.64.1", "100.128.0.1", "http://127.0.0.1:8080", "1100." "64.0.1"):
        assert scan_identity("a.md", text) == [], text


def test_email_rule_catches_real_domains_and_allows_synthetic_ones() -> None:
    assert scan_identity("a.md", "write to someone@" "gmail.com") == [("email", 1)]
    assert scan_identity("a.md", "first.last+tag@" "corp.co.uk") == [("email", 1)]
    for text in ("fixture@example.invalid", "a@example.com", "b@sub.example.org", "c@host.test", "uses: actions/checkout@v4",
                 "@pytest.fixture", "pkg@2.1"):
        assert scan_identity("a.md", text) == [], text


def test_owner_rule_matches_whole_words_from_hashes_and_from_the_environment() -> None:
    # Guards the guard: hashes that match nothing would pass the repo test forever.
    assert len(OWNER_WORD_SHA256) == len(set(OWNER_WORD_SHA256))
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in OWNER_WORD_SHA256)
    first, family = "".join(chr(c) for c in (97, 109, 105, 110, 101)), "".join(chr(c) for c in (107, 97, 114, 99, 104, 97))
    for text in (f"user {first}-{family} here", f"HOST {first.upper()}", f"the {family} machine", f"{first}~1", f"{family}\\jarvis"):
        assert [r for r, _ in scan_identity("a.md", text)] == ["owner-name"], text
    assert scan_identity("a.md", f"an {first}r and {family}s") == []
    assert scan_identity(f"docs/{family}.md", "clean text") == [("owner-name", 0)]
    assert scan_identity("a.md", "host zzbuildbox7 here", ["zzbuildbox7"]) == [("owner-name", 1)]
    assert scan_identity("a.md", "host zzbuildbox7x here", ["zzbuildbox7"]) == []


def test_environment_identity_reads_the_local_meta_and_drops_generic_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("USERNAME", "USER", "COMPUTERNAME"):
        monkeypatch.setenv(var, "Admin")
    local = tmp_path / "jarvis.local.toml"
    local.write_text('[meta]\nmachine = "Box-Seven"\nowner_account = "Some-Person"\n', encoding="utf-8")
    words = environment_identity(local)
    assert "some" not in words  # four letters: too short to be a safe whole word
    assert "person" in words and "seven" in words
    assert "admin" not in words and "box" not in words


def test_allowlist_covers_the_licence_and_citation_by_name_only() -> None:
    allowed = {(e["path"], e["rule"]) for e in load_allowlist()}
    assert ("LICENSE", "owner-name") in allowed
    assert ("CITATION.cff", "owner-name") in allowed
    assert not any(rule != "owner-name" for _, rule in allowed), "only the author's name may be allowlisted so far"


# --- plan P2: pasted credentials -----------------------------------------------------------------
# A GitHub collector and an ntfy adapter bring tokens into the vocabulary of the docs and the
# tests. Tailnet names and addresses are already covered by the identity rules above; a token
# pasted into a doc or a fixture is shaped too, so it can be caught without knowing its value
# (the real one lives in an environment variable, never in a file).
TOKEN_SHAPES = (
    re.compile(r"\btk_[a-z0-9]{29}\b"),  # ntfy access token: "tk_" plus 29 characters
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),  # GitHub classic and OAuth tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),  # GitHub fine-grained tokens
)


def token_findings(text: str) -> list[int]:
    """1-based indexes into TOKEN_SHAPES of every shape that occurs in text."""
    return [n for n, rx in enumerate(TOKEN_SHAPES, 1) if rx.search(text)]


def test_no_access_token_shape_in_repo_files() -> None:
    offenders = [f"{rel} matches token shape {n}" for rel, text in _tracked_texts().items() for n in token_findings(text)]
    assert not offenders, "token-shaped string in tracked files:\n" + "\n".join(offenders)


def test_token_scan_catches_the_shapes_and_lets_placeholders_through() -> None:
    # Guards the guard. Probes are assembled so this file does not match itself.
    assert token_findings("token " + "tk_" + "a" * 29) == [1]
    assert token_findings("token " + "tk_" + "a" * 28) == []
    assert token_findings("token " + "tk_" + "<your-token>") == []
    assert token_findings("token " + "ghp_" + "A" * 36) == [2]
    assert token_findings("token " + "github" + "_pat_" + "A" * 30) == [3]
    assert token_findings("set the variable named in ntfy_token_env") == []


# --- git history (publishing) ---------------------------------------------------------------------
#
# Everything above reads the working tree. A public push also publishes every earlier commit,
# so the same rules run over the history: author and committer addresses, commit messages and
# every line that was ever added. Publish from a history that passes this; the project did, by
# publishing from a single fresh commit authored with a GitHub noreply address.

_NOREPLY_EMAIL = re.compile(r"^[A-Za-z0-9_.+-]+@users\.noreply\.github\.com$")
# Assembled so this file does not itself contain an address on a real domain.
NOREPLY_TEST_ADDRESS = "1+fixture@" "users.noreply.github.com"
_US, _RS = "\x1f", "\x1e"
# The attribution trailer the tooling appends is the one real address a message may carry.
_TRAILER = re.compile(r"^Co-Authored-By: [^<\n]+ <noreply@anthropic\.com>$", re.IGNORECASE | re.MULTILINE)


def _git_in(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr.strip()}"
    return result.stdout


def scan_history(repo: Path, allow: list[dict[str, str]], env_words: list[str]) -> list[str]:
    """Findings for every commit reachable from the checked-out branch. Never prints a leaked value.

    HEAD, not --all: a branch kept only for the author's own reference must not fail the suite,
    and a push publishes the branch it names. Run this on the branch you are about to push.
    """
    findings: list[str] = []
    head = _git_in(repo, "log", "HEAD", f"--format=%h{_US}%ae{_US}%ce{_US}%B{_RS}")
    for record in head.split(_RS):
        if not record.strip():
            continue
        short, author, committer, message = (record.strip("\n").split(_US) + ["", "", "", ""])[:4]
        message = _TRAILER.sub("", message)
        for role, email in (("author", author), ("committer", committer)):
            if not _NOREPLY_EMAIL.match(email):
                findings.append(f"{short}: {role} address is not a GitHub noreply address")
        for rule, _ in scan_identity(f"commit {short}", message, env_words):
            findings.append(f"{short}: commit message trips {rule}")
        if hashed_hits(message, BUILTIN_DENY_SHA256):
            findings.append(f"{short}: commit message matches the built-in denylist")

    added: dict[str, list[str]] = {}
    current = ""
    patch = _git_in(repo, "log", "HEAD", "-p", "-U0", "--no-color", "--no-ext-diff", "--format=")
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = line.rsplit(" b/", 1)[-1]
        elif line.startswith("+") and not line.startswith("+++") and current:
            added.setdefault(current, []).append(line[1:])
    texts = {path: "\n".join(lines) for path, lines in added.items()}
    problems, _ = identity_violations(texts, allow, env_words)
    findings += [f"history: {p}" for p in problems]
    for path, text in texts.items():
        for index in hashed_hits(text + "\n" + path.replace("/", " "), BUILTIN_DENY_SHA256):
            findings.append(f"history: {path} matches built-in denylist entry {index}")
        for shape in token_findings(text):
            findings.append(f"history: {path} matches token shape {shape}")
    return findings


def test_git_history_has_no_identity_leaks() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    findings = scan_history(ROOT, load_allowlist(), environment_identity())
    assert not findings, (
        "the history would publish private identity (publish from a fresh commit, see docs/publishing.md):\n"
        + "\n".join(findings[:40])
    )


def _scratch_repo(path: Path, email: str, filename: str, content: str, message: str) -> Path:
    path.mkdir()
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": email}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, env=env)
    (path / filename).write_text(content, encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, env=env)
    subprocess.run(["git", "-c", "core.autocrlf=false", "commit", "-q", "-m", message], cwd=path, check=True, env=env)
    return path


def test_history_scan_passes_a_clean_noreply_history(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path / "clean", NOREPLY_TEST_ADDRESS, "a.md", "hello\n", "Add a")
    assert scan_history(repo, [], []) == []


def test_history_scan_catches_a_real_address_a_profile_path_and_a_denied_word(tmp_path: Path) -> None:
    drive = "C" + ":"
    leaked_word = "zzbuildbox7"
    repo = _scratch_repo(
        tmp_path / "dirty", "someone@" "corp.co.uk", "a.md",
        f"see {drive}/Users/bobsmith/x and host {leaked_word}\n", f"fix on {leaked_word}")
    findings = scan_history(repo, [], [leaked_word])
    text = "\n".join(findings)
    assert "author address is not a GitHub noreply address" in text
    assert "committer address is not a GitHub noreply address" in text
    assert "user-profile-path" in text and "owner-name" in text
    # The leaked values themselves never appear in a finding.
    assert "bobsmith" not in text and leaked_word not in text


def test_history_scan_catches_a_leak_that_a_later_commit_removed(tmp_path: Path) -> None:
    repo = _scratch_repo(tmp_path / "removed", NOREPLY_TEST_ADDRESS, "a.md",
                         "path " + "C" + ":/Users/bobsmith/x\n", "first")
    (repo / "a.md").write_text("clean\n", encoding="utf-8", newline="\n")
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": NOREPLY_TEST_ADDRESS,
           "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": NOREPLY_TEST_ADDRESS}
    subprocess.run(["git", "-c", "core.autocrlf=false", "commit", "-aqm", "second"], cwd=repo, check=True, env=env)
    assert (repo / "a.md").read_text(encoding="utf-8") == "clean\n"
    assert any("user-profile-path" in f for f in scan_history(repo, [], []))
