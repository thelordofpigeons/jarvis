"""Gate 1 tests (design section 6): adversarial paths, text scan, safe_read_text.

Everything is synthetic. The canary lives in tmp_vault/telos/sensitive/canary.md.
"""
from __future__ import annotations

import builtins
import ctypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from conftest import CANARY
from jarvisd import tier
from jarvisd.config import Config
from jarvisd.models import Item, TierHit, WithheldItem

IS_WINDOWS = sys.platform == "win32"
needs_windows = pytest.mark.skipif(not IS_WINDOWS, reason="Windows path semantics")


def _cfg_with(cfg: Config, **gates: object) -> Config:
    """A copy of cfg with some [gates] fields replaced (the original stays untouched)."""
    copy = cfg.model_copy(deep=True)
    for key, value in gates.items():
        setattr(copy.gates, key, value)
    return copy


def _short_name(path: Path) -> str | None:
    """The 8.3 name of an existing path, or None when the volume does not make one."""
    if not IS_WINDOWS:
        return None
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 32768)  # type: ignore[attr-defined]
    return buf.value if n else None


def _mk_junction(link: Path, target: Path) -> bool:
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and link.exists()


# --- adversarial path table ------------------------------------------------------------

Row = Callable[[Path], str]


def _w(v: Path, tail: str) -> str:
    """vault path joined with a tail, tail written verbatim (mixed slashes allowed)."""
    return str(v) + tail


PATH_ROWS: list[tuple[str, Row]] = [
    # Under telos: the tree floor catches these even if a component rule were broken.
    ("telos-upper-case", lambda v: _w(v, "\\TELOS\\Sensitive\\x.md")),
    ("telos-backslashes", lambda v: _w(v, "\\telos\\sensitive\\x.md")),
    ("telos-whole-tree", lambda v: _w(v, "\\telos\\identity.md")),
    ("notes-tree", lambda v: _w(v, "\\notes\\anything.md")),
    # Outside telos and notes: only the component rule can catch these.
    ("component-plain", lambda v: _w(v, "/sessions/sensitive/x.md")),
    ("component-upper", lambda v: _w(v, "/sessions/SENSITIVE/x.md")),
    ("component-trailing-dot", lambda v: _w(v, "/sessions/sensitive./x.md")),
    ("component-trailing-space", lambda v: _w(v, "/sessions/sensitive /x.md")),
    ("component-dot-space-mix", lambda v: _w(v, "/sessions/sensitive. ./x.md")),
    ("dotdot-from-sessions", lambda v: _w(v, "/sessions/../telos/sensitive/x.md")),
    ("dotdot-to-component", lambda v: _w(v, "/sessions/../raw/sensitive/x.md")),
    ("relative-dotdot", lambda v: "..\\telos\\sensitive\\x.md"),  # cwd is sessions/
    ("relative-telos", lambda v: "telos/sensitive./x.md"),
    ("extended-prefix", lambda v: "\\\\?\\" + _w(v, "\\telos\\sensitive\\x.md")),
    ("extended-prefix-component", lambda v: "\\\\?\\" + _w(v, "\\sessions\\sensitive\\x.md")),
    ("ads-suffix", lambda v: _w(v, "/telos/sensitive/x.md::$DATA")),
    ("ads-lowercase", lambda v: _w(v, "/sessions/ok.md::$data")),
    ("ads-named-stream", lambda v: _w(v, "/sessions/ok.md:hidden")),
    ("nul-byte", lambda v: _w(v, "/sessions/ok.md\x00.txt")),
    ("unresolvable-chars", lambda v: _w(v, "/sessions/<bad>|name?.md")),
    ("device-name", lambda v: _w(v, "/sessions/NUL.md")),
    ("control-char", lambda v: _w(v, "/sessions/a\x07b.md")),
]


@needs_windows
@pytest.mark.parametrize("make", [pytest.param(m, id=i) for i, m in PATH_ROWS])
def test_adversarial_paths_all_hit(
    make: Row, tmp_cfg: Config, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_vault / "sessions")
    hit = tier.path_hit(make(tmp_vault), tmp_cfg)
    assert isinstance(hit, TierHit)
    assert hit.kind == "sensitive"
    assert hit.where == "path"


@needs_windows
def test_adversarial_junction_hits(tmp_cfg: Config, tmp_vault: Path) -> None:
    target = tmp_vault / "telos" / "sensitive"
    (target / "x.md").write_text("synthetic\n", encoding="utf-8")
    link = tmp_vault / "sessions" / "link"
    if not _mk_junction(link, target):
        pytest.skip("mklink /J failed on this volume")
    hit = tier.path_hit(str(link / "x.md"), tmp_cfg)
    assert hit is not None and hit.kind == "sensitive"
    # A junction into a directory that is merely named something else must resolve first.
    plain = tmp_vault / "raw" / "plain"
    plain.mkdir()
    jl = tmp_vault / "sessions" / "jl"
    if not _mk_junction(jl, plain):
        pytest.skip("mklink /J failed on this volume")
    assert tier.path_hit(str(jl / "y.md"), tmp_cfg) is None


def test_adversarial_symlink_hits(tmp_cfg: Config, tmp_vault: Path) -> None:
    target = tmp_vault / "telos" / "sensitive"
    link = tmp_vault / "sessions" / "symlink"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs a privilege this account lacks")
    hit = tier.path_hit(str(link / "x.md"), tmp_cfg)
    assert hit is not None and hit.kind == "sensitive"


@needs_windows
def test_adversarial_short_name_hits(tmp_cfg: Config, tmp_vault: Path) -> None:
    long_dir = tmp_vault / "sessions" / "sensitive"
    long_dir.mkdir()
    short = _short_name(long_dir)
    if not short or "~" not in short:
        pytest.skip("8.3 short names are disabled on this volume (fsutil 8dot3name)")
    # The leaf does not exist: expansion must work on the existing prefix only.
    hit = tier.path_hit(short + "\\not-created-yet.md", tmp_cfg)
    assert hit is not None and hit.kind == "sensitive"
    assert tier.canonical(short + "\\not-created-yet.md").endswith("/sensitive/not-created-yet.md")


@needs_windows
def test_short_name_of_parent_is_expanded(tmp_vault: Path) -> None:
    short = _short_name(tmp_vault)
    if not short or "~" not in short:
        pytest.skip("8.3 short names are disabled on this volume")
    assert tier.canonical(short + "\\sessions") == tier.canonical(str(tmp_vault / "sessions"))


def test_unresolvable_path_is_a_fail_closed_hit(
    tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_p: str) -> str:
        raise OSError("simulated resolver failure")

    monkeypatch.setattr(os.path, "realpath", boom)
    hit = tier.path_hit("anything/at/all.md", tmp_cfg)
    assert hit == TierHit(kind="sensitive", code="tier_error_fail_closed", where="path")


def test_canonical_is_forward_slash_lowercase_and_has_no_dotdot(tmp_vault: Path) -> None:
    out = tier.canonical(str(tmp_vault) + "\\Sessions\\..\\RAW\\x.MD")
    assert "\\" not in out and ".." not in out.split("/")
    if IS_WINDOWS:
        assert out == out.lower()
    assert out.endswith("/raw/x.md")


def test_canonical_raises_on_nul_and_ads() -> None:
    for bad in ("a\x00b", "C:/x/y.md::$DATA", "C:/x/y.md:stream"):
        with pytest.raises(tier.CanonicalError):
            tier.canonical(bad)


@pytest.mark.parametrize(
    "tail",
    ["/raw/jarvis/2026-10-06-digest.md", "/sessions/2026-10-06-01.md", "/RECENT.md"],
)
def test_negative_paths_do_not_hit(tmp_cfg: Config, tmp_vault: Path, tail: str) -> None:
    assert tier.path_hit(str(tmp_vault) + tail, tmp_cfg) is None


def test_floor_holds_when_toml_globs_are_removed(tmp_cfg: Config, tmp_vault: Path) -> None:
    bare = _cfg_with(tmp_cfg, sensitive_path_globs=[])
    assert tier.path_hit(str(tmp_vault / "telos" / "deep" / "x.md"), bare) is not None
    assert tier.path_hit(str(tmp_vault / "notes" / "x.md"), bare) is not None
    assert tier.path_hit(str(tmp_vault / "sessions" / "sensitive" / "x.md"), bare) is not None
    assert tier.path_hit(str(tmp_vault / "sessions" / "ok.md"), bare) is None


def test_config_can_only_add_globs(tmp_cfg: Config, tmp_vault: Path) -> None:
    target = str(tmp_vault / "raw" / "jarvis" / "ledger" / "x.md")
    assert tier.path_hit(target, tmp_cfg) is None
    extra = _cfg_with(tmp_cfg, sensitive_path_globs=["**/brain/raw/jarvis/ledger/**"])
    assert tier.path_hit(target, extra) is not None


# --- text table ------------------------------------------------------------------------

TEXT_ROWS: list[tuple[str, str]] = [
    ("frontmatter-tags-list", "---\ntitle: a\ntags: [x, medical]\n---\nbody\n"),
    ("frontmatter-tags-block", "---\ntags:\n  - x\n  - private\n---\nbody\n"),
    ("frontmatter-tags-csv", "---\ntags: x, financial-personal\n---\n"),
    ("frontmatter-tags-hash", "---\ntags: ['#sensitive']\n---\n"),
    ("inline-hashtag", "Notes for later #private and more"),
    ("inline-hashtag-start", "#medical follow up"),
    ("sensitive-true", "---\nsensitive: true\n---\nbody"),
    ("sensitive-true-bare", "something\nsensitive: true\n"),
    ("sensitive-true-upper", "SENSITIVE: TRUE"),
    ("wikilink", "see [[telos/sensitive/health]] for details"),
    ("wikilink-backslash", "see telos\\sensitive\\health.md"),
    ("commit-subject", "fix #sensitive"),
    ("mixed-case-tags-key", "TAGS: [x, Medical]"),
    ("zero-width-evasion", "#sen\u200bsitive"),
    ("term-accents-case", "Rendez-vous avec le Caf\u00c9 Z\u00e9phyr demain"),
    ("term-decomposed", "cafe\u0301 zephyr"),
]


@pytest.mark.parametrize("text", [pytest.param(t, id=i) for i, t in TEXT_ROWS])
def test_text_rows_hit(text: str, tmp_cfg: Config) -> None:
    cfg = _cfg_with(tmp_cfg, sensitive_terms=["cafe zephyr"])
    hit = tier.text_hit(text, cfg)
    assert isinstance(hit, TierHit)
    assert hit.kind == "sensitive"
    assert hit.where == "text"


@pytest.mark.parametrize(
    "text",
    [
        "I keep a private notebook and a private opinion.",
        "# Heading\n\nPlain prose, issue #12 fixed, tags are useful.",
        "---\ntags: [x, y]\nsensitive: false\n---\nbody\n",
        "C# and F# are languages",
        "",
    ],
)
def test_negative_text_rows(text: str, tmp_cfg: Config) -> None:
    assert tier.text_hit(text, _cfg_with(tmp_cfg, sensitive_terms=["cafe zephyr"])) is None


def test_term_hit_code_never_echoes_the_term(tmp_cfg: Config) -> None:
    cfg = _cfg_with(tmp_cfg, sensitive_terms=["unrelated", "Zephyr Secret"])
    hit = tier.text_hit("the zephyr secret again", cfg)
    assert hit is not None
    assert hit.code == "term:1"
    assert "zephyr" not in (hit.code + hit.where).casefold()


def test_empty_term_never_matches_everything(tmp_cfg: Config) -> None:
    assert tier.text_hit("plain prose", _cfg_with(tmp_cfg, sensitive_terms=["", "   "])) is None


def test_scan_terms_reports_only_term_hits(tmp_cfg: Config) -> None:
    cfg = _cfg_with(tmp_cfg, sensitive_terms=["unrelated", "Cafe Zephyr"])
    hit = tier.scan_terms("rendez-vous au Café Zéphyr", cfg)
    assert hit == TierHit(kind="sensitive", code="term:1", where="text")
    # Tags and flags are the file-level rules, not this primitive.
    assert tier.scan_terms("note #private\nsensitive: true", cfg) is None
    assert tier.scan_terms("plain prose", cfg) is None
    assert tier.scan_terms("anything", _cfg_with(tmp_cfg, sensitive_terms=["", "  "])) is None


def test_scan_terms_fails_closed(tmp_cfg: Config) -> None:
    hit = tier.scan_terms("fine", object())  # type: ignore[arg-type]
    assert hit == TierHit(kind="sensitive", code="tier_error_fail_closed", where="text")


def test_safe_read_text_can_defer_terms_but_never_tags(tmp_cfg: Config, tmp_vault: Path) -> None:
    cfg = _cfg_with(tmp_cfg, sensitive_terms=["cafe zephyr"])
    termed = tmp_vault / "sessions" / "termed.md"
    termed.write_bytes(b"- fine\n- cafe zephyr\n")
    assert isinstance(tier.safe_read_text(termed, cfg, roots=[tmp_vault]), WithheldItem)
    out = tier.safe_read_text(termed, cfg, roots=[tmp_vault], terms=False)
    assert out == "- fine\n- cafe zephyr\n"
    tagged = tmp_vault / "sessions" / "tagged2.md"
    tagged.write_text("---\ntags: [medical]\n---\ncafe zephyr\n", encoding="utf-8")
    held = tier.safe_read_text(tagged, cfg, roots=[tmp_vault], terms=False)
    assert isinstance(held, WithheldItem) and held.reason == "tag_frontmatter"
    flagged = tmp_vault / "sessions" / "flagged.md"
    flagged.write_text("sensitive: true\n", encoding="utf-8")
    assert isinstance(tier.safe_read_text(flagged, cfg, roots=[tmp_vault], terms=False), WithheldItem)


def test_exception_inside_text_check_is_a_fail_closed_hit(tmp_cfg: Config) -> None:
    hit = tier.text_hit("fine", object())  # type: ignore[arg-type]
    assert hit == TierHit(kind="sensitive", code="tier_error_fail_closed", where="text")
    hit_none = tier.text_hit(None, tmp_cfg)  # type: ignore[arg-type]
    assert hit_none is not None and hit_none.code == "tier_error_fail_closed"


def test_exception_inside_item_check_is_a_fail_closed_hit(tmp_cfg: Config) -> None:
    hit = tier.item_hit(object(), tmp_cfg)  # type: ignore[arg-type]
    assert hit is not None and hit.kind == "sensitive" and hit.code == "tier_error_fail_closed"


# --- item_hit --------------------------------------------------------------------------


def _item(**over: object) -> Item:
    base: dict[str, object] = {
        "id": "abcd1234",
        "source": "brain",
        "kind": "brain_thread",
        "title": "Synthetic thread",
        "text": "Waiting on a reviewer",
    }
    base.update(over)
    return Item.model_validate(base)


def test_item_hit_clean_item(tmp_cfg: Config, tmp_vault: Path) -> None:
    item = _item(paths=[str(tmp_vault / "sessions" / "a.md")], origin="RECENT.md")
    assert tier.item_hit(item, tmp_cfg) is None


@pytest.mark.parametrize(
    "over",
    [
        pytest.param(lambda v: {"paths": [str(v / "telos" / "x.md")]}, id="paths"),
        pytest.param(lambda v: {"origin": str(v / "notes" / "x.md")}, id="origin"),
        pytest.param(lambda v: {"meta": {"src": str(v / "telos" / "x.md")}}, id="meta-path"),
        pytest.param(lambda v: {"meta": {"deep": {"files": [str(v / "notes" / "x.md")]}}}, id="meta-nested"),
        pytest.param(lambda v: {"title": "fix #sensitive"}, id="title"),
        pytest.param(lambda v: {"text": "see [[telos/sensitive/x]]"}, id="text"),
        pytest.param(lambda v: {"tags": ["medical"]}, id="tag"),
        pytest.param(lambda v: {"tags": ["#Private"]}, id="tag-hash-case"),
        pytest.param(lambda v: {"tags": ["derived_from_sensitive_session"]}, id="derived"),
    ],
)
def test_item_hit_every_field(over: Callable[[Path], dict[str, object]], tmp_cfg: Config, tmp_vault: Path) -> None:
    hit = tier.item_hit(_item(**over(tmp_vault)), tmp_cfg)
    assert hit is not None and hit.kind == "sensitive"


def test_item_hit_work_is_a_policy_hold_unless_allowed(tmp_cfg: Config) -> None:
    item = _item(work=True)
    assert tmp_cfg.digest.work_metadata_to_claude is False
    hit = tier.item_hit(item, tmp_cfg)
    assert hit is not None and hit.kind == "policy"
    allowed = tmp_cfg.model_copy(deep=True)
    allowed.digest.work_metadata_to_claude = True
    assert tier.item_hit(item, allowed) is None
    # Sensitive outranks policy.
    both = _item(work=True, tags=["medical"])
    hit2 = tier.item_hit(both, tmp_cfg)
    assert hit2 is not None and hit2.kind == "sensitive"


# --- safe_read_text --------------------------------------------------------------------


@pytest.fixture
def open_tracer(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every path handed to open() and os.open(), then pass the call through."""
    seen: list[str] = []
    real_open = builtins.open
    real_os_open = os.open

    def traced_open(file, *a, **k):  # type: ignore[no-untyped-def]
        if isinstance(file, (str, bytes, os.PathLike)):
            seen.append(os.fsdecode(file))
        return real_open(file, *a, **k)

    def traced_os_open(path, *a, **k):  # type: ignore[no-untyped-def]
        seen.append(os.fsdecode(path))
        return real_os_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", traced_open)
    monkeypatch.setattr(os, "open", traced_os_open)
    return seen


def test_safe_read_text_never_opens_withheld_file(
    tmp_cfg: Config, tmp_vault: Path, open_tracer: list[str]
) -> None:
    canary = tmp_vault / "telos" / "sensitive" / "canary.md"
    result = tier.safe_read_text(canary, tmp_cfg, roots=[tmp_vault], max_bytes=4096)
    assert isinstance(result, WithheldItem)
    assert result.hold_kind == "sensitive"
    assert CANARY not in result.model_dump_json()
    assert not [p for p in open_tracer if "canary" in p.casefold()]
    assert not [p for p in open_tracer if "sensitive" in p.casefold()]


def test_safe_read_text_outside_roots_is_never_opened(
    tmp_cfg: Config, tmp_vault: Path, tmp_path: Path, open_tracer: list[str]
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "plain.md").write_text("plain\n", encoding="utf-8")
    result = tier.safe_read_text(elsewhere / "plain.md", tmp_cfg, roots=[tmp_vault / "sessions"], max_bytes=4096)
    assert isinstance(result, WithheldItem)
    assert result.reason == "outside_roots"
    assert not [p for p in open_tracer if "plain.md" in p]
    # A sibling directory sharing a prefix with a root is still outside it.
    (tmp_vault / "sessions-evil").mkdir()
    (tmp_vault / "sessions-evil" / "x.md").write_text("x\n", encoding="utf-8")
    sneaky = tier.safe_read_text(tmp_vault / "sessions-evil" / "x.md", tmp_cfg, roots=[tmp_vault / "sessions"], max_bytes=4096)
    assert isinstance(sneaky, WithheldItem) and sneaky.reason == "outside_roots"


def test_safe_read_text_reads_clean_file_and_strips_bom(tmp_cfg: Config, tmp_vault: Path) -> None:
    note = tmp_vault / "sessions" / "2026-10-05-01.md"
    note.write_bytes(b"\xef\xbb\xbf# Session\nplain prose with the word private in it\n")
    out = tier.safe_read_text(note, tmp_cfg, roots=[tmp_vault], max_bytes=4096)
    assert out == "# Session\nplain prose with the word private in it\n"


def test_safe_read_text_whole_file_text_hit_withholds_everything(tmp_cfg: Config, tmp_vault: Path) -> None:
    note = tmp_vault / "sessions" / "tagged.md"
    note.write_text("---\ntags: [x, medical]\n---\n" + "filler\n" * 5 + f"{CANARY}\n", encoding="utf-8")
    out = tier.safe_read_text(note, tmp_cfg, roots=[tmp_vault], max_bytes=4096)
    assert isinstance(out, WithheldItem)
    assert CANARY not in out.model_dump_json()
    assert out.reason == "tag_frontmatter"


def test_safe_read_text_oversize_and_undecodable_fail_closed(tmp_cfg: Config, tmp_vault: Path) -> None:
    big = tmp_vault / "sessions" / "big.md"
    big.write_text("a" * 500, encoding="utf-8")
    over = tier.safe_read_text(big, tmp_cfg, roots=[tmp_vault], max_bytes=100)
    assert isinstance(over, WithheldItem) and over.reason == "too_large"
    bad = tmp_vault / "sessions" / "bad.md"
    bad.write_bytes(b"\xff\xfe\xfa not utf8 \x80")
    assert isinstance(tier.safe_read_text(bad, tmp_cfg, roots=[tmp_vault], max_bytes=4096), WithheldItem)


def test_safe_read_text_missing_file_fails_closed(tmp_cfg: Config, tmp_vault: Path) -> None:
    out = tier.safe_read_text(tmp_vault / "sessions" / "nope.md", tmp_cfg, roots=[tmp_vault], max_bytes=4096)
    assert isinstance(out, WithheldItem) and out.reason == "tier_error_fail_closed"


def test_withheld_ids_are_stable_and_content_free(tmp_cfg: Config, tmp_vault: Path) -> None:
    canary = tmp_vault / "telos" / "sensitive" / "canary.md"
    a = tier.safe_read_text(canary, tmp_cfg, roots=[tmp_vault], max_bytes=4096)
    b = tier.safe_read_text(canary, tmp_cfg, roots=[tmp_vault], max_bytes=4096)
    assert isinstance(a, WithheldItem) and isinstance(b, WithheldItem)
    assert a.id == b.id and len(a.id) == 8


# --- assert_clean ----------------------------------------------------------------------


def test_assert_clean_passes_plain_prompt(tmp_cfg: Config) -> None:
    tier.assert_clean("<data id=abc>Synthetic thread, waiting on a reviewer</data>", tmp_cfg)


@pytest.mark.parametrize(
    "prompt",
    ["notes #medical here", "see [[telos/sensitive/x]]", "sensitive: true", "tags: [private]"],
)
def test_assert_clean_raises_on_hit(prompt: str, tmp_cfg: Config) -> None:
    with pytest.raises(tier.TierViolation) as exc:
        tier.assert_clean(prompt, tmp_cfg)
    assert exc.value.hit.kind == "sensitive"


def test_assert_clean_rejects_vault_path_literals(tmp_cfg: Config, tmp_vault: Path) -> None:
    leaked = f"read {tmp_vault.as_posix()}/telos/identity.md please"
    with pytest.raises(tier.TierViolation):
        tier.assert_clean(leaked, tmp_cfg)
    with pytest.raises(tier.TierViolation):
        tier.assert_clean(f"read {str(tmp_vault).upper()}\\NOTES\\x.md", tmp_cfg)


def test_assert_clean_fails_closed_on_internal_error() -> None:
    with pytest.raises(tier.TierViolation):
        tier.assert_clean("fine", object())  # type: ignore[arg-type]


# --- review fixes: forbidden roots, line endings, tag layouts, item/final-scan agreement ---


def test_vault_forbidden_roots_are_a_forbidden_hit_but_not_a_path_hit(tmp_cfg: Config, tmp_path: Path) -> None:
    # D6: git metadata of Documents/Work repos may reach Claude behind the opt-in flag, so
    # path_hit (the floor) stays quiet; forbidden_hit is what marks the location.
    work = tmp_path / "Documents" / "Work" / "Dev" / "client-x"
    work.mkdir(parents=True)
    assert tier.path_hit(work, tmp_cfg) is None
    hit = tier.forbidden_hit(work, tmp_cfg)
    assert hit is not None and hit.kind == "sensitive" and hit.code == "vault_forbidden"
    assert tier.forbidden_hit(work / "new-file.txt", tmp_cfg) is not None
    assert tier.forbidden_hit(tmp_path / "Documents" / "Workish", tmp_cfg) is None  # prefix, not a child
    assert tier.forbidden_hit(tmp_path / "elsewhere", tmp_cfg) is None


def test_safe_read_text_refuses_a_forbidden_root(tmp_cfg: Config, tmp_path: Path) -> None:
    work = tmp_path / "Documents" / "Work"
    work.mkdir(parents=True)
    note = work / "plan.md"
    note.write_text("synthetic plan\n", encoding="utf-8")
    out = tier.safe_read_text(note, tmp_cfg, [tmp_path])
    assert isinstance(out, WithheldItem) and out.reason == "vault_forbidden"


def test_an_item_that_names_a_file_under_a_forbidden_root_is_held(tmp_cfg: Config, tmp_path: Path) -> None:
    target = tmp_path / "Documents" / "Work" / "Dev" / "x" / "README.md"
    for item in (
        Item(id="f-1", source="x", kind="file", title="t", text="b", paths=[str(target)]),
        Item(id="f-2", source="x", kind="file", title="t", text="b", origin=str(target)),
        Item(id="f-3", source="x", kind="file", title="t", text="b", meta={"where": str(target)}),
    ):
        hit = tier.item_hit(item, tmp_cfg)
        assert hit is not None and hit.code == "vault_forbidden", item.id


@pytest.mark.parametrize("text", [
    "---\r\nsensitive: true\r\n---\r\nbody\r\n",
    "---\rsensitive: true\r---\rbody",
    "---\ntags: [\n  sensitive,\n  other\n]\n---\n",
    "---\ntags: # a comment\n  - sensitive\n---\n",
    "---\ntags:\n\n  - sensitive\n---\n",
    "---\ntags:\n  # note\n  - other\n  - sensitive\n---\n",
    "---\r\ntags:\r\n  - sensitive\r\n---\r\n",
    "---\ntags: [other,\n  private]\n---\n",
])
def test_text_gate_sees_sensitive_in_valid_note_formats(tmp_cfg: Config, text: str) -> None:
    assert tier.text_hit(text, tmp_cfg) is not None


@pytest.mark.parametrize("text", [
    "---\ntags: [other,\n  fine]\n---\nbody that mentions sensitivity in prose\n",
    "---\ntags:\n  - work\n\nsensitive: false\n---\n",
    "---\r\nsensitive: false\r\n---\r\n",
])
def test_text_gate_does_not_over_hold_on_the_new_layouts(tmp_cfg: Config, text: str) -> None:
    assert tier.text_hit(text, tmp_cfg) is None


def test_item_gate_agrees_with_the_final_scan_on_the_vault_literal(tmp_cfg: Config) -> None:
    literal = (tmp_cfg.paths.brain_root / "telos" / "30-projects.md").as_posix()
    item = Item(id="t-1", source="brain", kind="brain_thread", title="Thread",
                text=f"Update {literal} before Friday")
    hit = tier.item_hit(item, tmp_cfg)
    assert hit is not None and hit.code == "vault_path_literal"
    with pytest.raises(tier.TierViolation):
        tier.assert_clean(item.text, tmp_cfg)
