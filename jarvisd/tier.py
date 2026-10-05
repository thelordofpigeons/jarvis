"""Gate 1, the tier gate (design section 6): canonical paths, hardcoded floor, text scan.

Everything here is deterministic and fails closed: any exception inside a check becomes a
"sensitive" hit, never a pass. This module also holds the only file-read primitive that
collectors may use (`safe_read_text`), so a path is judged before the file is opened.

Layer L2. It imports only common, models and config.

Known limits, stated so nobody trusts this for more than it does:
- A time-of-check/time-of-use race between resolving a path and opening it is narrowed by
  opening the resolved path, not closed. The daemon account is the only writer of these trees.
- The text scan is a keyword and tag scan. It does not understand meaning; it over-holds on
  purpose. A sensitive note with no tag, term or path signal is not caught by this gate.
- 8.3 short names only expand when the volume created them. A volume with short names
  disabled has none to defeat.
"""
from __future__ import annotations

import ctypes
import os
import re
import unicodedata
from collections.abc import Iterable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from jarvisd.common import short_id
from jarvisd.config import SENSITIVE_FLOOR_GLOBS, Config
from jarvisd.models import Item, TierHit, WithheldItem

FAIL_CLOSED = "tier_error_fail_closed"
DERIVED_TAG = "derived_from_sensitive_session"

# Characters Windows refuses in a name. A path holding one cannot name a real file, so
# treating it as unresolvable (a hit) costs nothing and closes parser-differential tricks.
_INVALID_CHARS = set('<>"|*?')
# Device names open hardware or block forever when passed to open(); never a valid input here.
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class CanonicalError(ValueError):
    """The path cannot be given one safe canonical form. Callers treat this as a hit."""


class TierViolation(Exception):
    """The final prompt failed the last scan. Carries the hit; the message is the code only."""

    def __init__(self, hit: TierHit) -> None:
        super().__init__(hit.code)
        self.hit = hit


# --- canonical paths -------------------------------------------------------------------


def _long_name(path: str) -> str:
    """Expand 8.3 short names in an existing path with GetLongPathNameW (Windows only)."""
    if os.name != "nt":
        return path
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetLongPathNameW(path, buf, 32768)  # type: ignore[attr-defined]
    if n == 0 or n >= 32768:
        # Exists but cannot be expanded: do not guess, fail closed through the caller.
        raise OSError(f"GetLongPathNameW failed for an existing path (code {ctypes.GetLastError()})")  # type: ignore[attr-defined]
    return buf.value


def _expand_short(path: str) -> str:
    """Expand short names on the longest existing prefix; keep the not-yet-existing tail.

    A leaf that does not exist yet (a file we are about to write or probe) has no short name,
    but its parent directory might: USERNA~1 or SENSIT~1.
    """
    tail: list[str] = []
    cur = path
    while not os.path.lexists(cur):
        parent, name = os.path.split(cur)
        if not name or parent == cur:
            return path
        tail.append(name)
        cur = parent
    expanded = _long_name(cur)
    return os.path.join(expanded, *reversed(tail)) if tail else expanded


def _strip_extended_prefix(raw: str) -> str:
    """Drop the \\\\?\\ and \\\\.\\ prefixes, keeping UNC shares as plain \\\\server\\share."""
    for unc in ("\\\\?\\UNC\\", "//?/UNC/", "\\\\.\\UNC\\", "//./UNC/"):
        if raw.upper().startswith(unc.upper()):
            return "\\\\" + raw[len(unc):]
    for pre in ("\\\\?\\", "//?/", "\\\\.\\", "//./"):
        if raw.startswith(pre):
            return raw[len(pre):]
    return raw


def _clean_components(path: str) -> str:
    """Strip trailing dots and spaces per component (Windows ignores them) and refuse devices."""
    drive, rest = os.path.splitdrive(path)
    rooted = rest[:1] in ("\\", "/")
    parts: list[str] = []
    for part in re.split(r"[\\/]+", rest):
        if part in ("", ".", ".."):
            if part:
                parts.append(part)
            continue
        cleaned = part.rstrip(". ")
        if not cleaned:
            raise CanonicalError("component made only of dots or spaces")
        if cleaned.split(".")[0].rstrip(" ").casefold() in _RESERVED:
            raise CanonicalError("reserved device name in path")
        parts.append(cleaned)
    return drive + (os.sep if rooted else "") + os.sep.join(parts)


def _resolve(path: str | os.PathLike[str]) -> tuple[str, str]:
    """Return (real path to open, canonical form for matching). Raises on anything odd."""
    raw = os.fspath(path)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    if any(ord(ch) < 32 for ch in raw):
        raise CanonicalError("control character or NUL in path")
    if "::$" in raw.upper():
        raise CanonicalError("alternate data stream syntax")
    stripped = _strip_extended_prefix(raw)
    after_drive = stripped[2:] if re.match(r"^[A-Za-z]:", stripped) else stripped
    if ":" in after_drive:
        raise CanonicalError("colon outside the drive letter (alternate data stream)")
    if _INVALID_CHARS & set(stripped):
        raise CanonicalError("character that cannot appear in a Windows name")
    cleaned = _clean_components(stripped)
    # Win32 collapses '..' lexically before touching the filesystem, so abspath matches it.
    absolute = os.path.abspath(cleaned)
    real = os.path.realpath(_expand_short(absolute))
    real = _strip_extended_prefix(real)
    if "~" in real:
        real = _expand_short(real)
    canon = os.path.normcase(real).replace("\\", "/")
    if ".." in canon.split("/"):
        raise CanonicalError("'..' survived resolution")
    return real, canon


def canonical(path: str | os.PathLike[str]) -> str:
    """Canonical path: resolved, lowercase on Windows, forward slashes, no '..'.

    Raises CanonicalError (a ValueError) or OSError. `path_hit` converts both into a
    fail-closed hit; direct callers must treat them as sensitive too.
    """
    return _resolve(path)[1]


# --- path rules ------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _glob_regex(glob: str) -> re.Pattern[str]:
    """Translate a glob to a regex over canonical paths. '**' crosses slashes, '*' does not."""
    pat = glob.replace("\\", "/").casefold()
    suffix = ""
    if pat.endswith("/**"):
        pat, suffix = pat[:-3], "(?:/.*)?"
    out: list[str] = []
    i = 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out) + suffix)


def _under(canon: str, root_canon: str) -> bool:
    root = root_canon.rstrip("/")
    return canon == root or canon.startswith(root + "/")


def _canon_hit(canon: str, cfg: Config) -> TierHit | None:
    """Apply the floor, the config globs and the brain-tree prefixes to a canonical path."""
    folded = canon.casefold()
    for comp in folded.split("/"):
        if comp.rstrip(". ") == "sensitive":
            return TierHit(kind="sensitive", code="component_sensitive", where="path")
    for glob in cfg.sensitive_globs():
        if _glob_regex(glob).fullmatch(folded):
            code = "glob_floor" if glob in SENSITIVE_FLOOR_GLOBS else "glob_config"
            return TierHit(kind="sensitive", code=code, where="path")
    # Belt and braces for a vault that is not literally named "brain": telos and notes under
    # the configured vault root are floor whatever the globs say.
    brain = cfg.paths.brain_root
    for sub in ("telos", "notes"):
        if _under(folded, canonical(brain / sub).casefold()):
            return TierHit(kind="sensitive", code="floor_brain_tree", where="path")
    return None


def _forbidden_canon(canon: str, cfg: Config) -> TierHit | None:
    """A canonical path under a `[paths].vault_forbidden` root (telos, notes, Documents/Work).

    Not part of `path_hit`: design D6 lets git *metadata* of repos under Documents/Work reach
    Claude when `work_metadata_to_claude` is on, so such a repo is not withheld outright. But
    file content from these roots never may, and a repo under them is work whatever its
    hand-set flag says.
    """
    folded = canon.casefold()
    for root in cfg.paths.vault_forbidden:
        if _under(folded, canonical(root).casefold()):
            return TierHit(kind="sensitive", code="vault_forbidden", where="path")
    return None


def forbidden_hit(path: str | os.PathLike[str], cfg: Config) -> TierHit | None:
    """Hit when `path` is under a `[paths].vault_forbidden` root. Fails closed like `path_hit`."""
    try:
        return _forbidden_canon(canonical(path), cfg)
    except Exception:  # noqa: BLE001  fail closed on anything at all
        return TierHit(kind="sensitive", code=FAIL_CLOSED, where="path")


def path_hit(path: str | os.PathLike[str], cfg: Config) -> TierHit | None:
    """Gate 1 on one path. Any failure to canonicalize is a hit (fail closed)."""
    try:
        return _canon_hit(canonical(path), cfg)
    except Exception:  # noqa: BLE001  fail closed on anything at all
        return TierHit(kind="sensitive", code=FAIL_CLOSED, where="path")


# --- text rules ------------------------------------------------------------------------


# Every line break YAML or a Windows tool may write. The line-anchored rules below only know
# LF, so a CRLF note would otherwise slip past `sensitive: true` and past a tag list.
_LINE_BREAKS = re.compile("\r\n|[\r\x0b\x0c\x85" + chr(0x2028) + chr(0x2029) + "]")


def _fold(text: str) -> str:
    """Casefold, strip accents (Mn) and invisible format characters such as zero-width space.

    Line breaks are normalised to LF first, so the rules that anchor on a line end behave the
    same for LF, CRLF and bare CR text.
    """
    decomposed = unicodedata.normalize("NFKD", _LINE_BREAKS.sub("\n", text))
    kept = "".join(ch for ch in decomposed if unicodedata.category(ch) not in ("Mn", "Cf"))
    return kept.casefold()


_TAGS_LINE = re.compile(r"^[ \t]*tags[ \t]*:(.*)$", re.MULTILINE)
_LIST_ITEM = re.compile(r"^[ \t]*-[ \t]+(.+)$")
_SENSITIVE_FLAG = re.compile(r"^[ \t]*sensitive[ \t]*:[ \t]*[\"']?(?:true|yes|on|1)[\"']?[ \t]*(?:#.*)?$", re.MULTILINE)
_HASHTAG = re.compile(r"(?<![\w])#(\w[\w\-/]*)")
_TOKEN_SPLIT = re.compile(r"[\s,\[\]'\"]+")


def _tag_matches(tag: str, wanted: set[str]) -> bool:
    cleaned = tag.strip().lstrip("#").rstrip("-/")
    return bool(cleaned) and (cleaned in wanted or cleaned.split("/")[0] in wanted)


_COMMENT = re.compile(r"(?:^|[ \t])#.*$")
# A flow list that never closes must not swallow the rest of the file as tags forever.
_FLOW_LINE_LIMIT = 200


def _tokens(chunk: str) -> Iterator[str]:
    yield from (t for t in _TOKEN_SPLIT.split(chunk.strip()) if t)


def _frontmatter_tags(folded: str) -> Iterator[str]:
    """Every tag named by a 'tags:' key: inline, comma list, multi-line flow list or block list.

    Matched anywhere in the text, not only inside a --- block: a missing closing fence must
    not turn a tag into a pass. After the key, blank lines and comment lines are skipped, a
    trailing comment on the key line is ignored for the layout decision (its words are still
    scanned, which can only over-hold), and an unclosed `[` reads on until the `]`.
    """
    lines = folded.split("\n")
    for idx, line in enumerate(lines):
        m = _TAGS_LINE.match(line)
        if not m:
            continue
        inline = m.group(1)
        yield from _tokens(inline)
        rest = _COMMENT.sub("", inline).strip()
        if rest.startswith("[") and "]" not in rest:
            for follow in lines[idx + 1: idx + 1 + _FLOW_LINE_LIMIT]:
                yield from _tokens(follow)
                if "]" in follow:
                    break
            continue
        if rest:
            continue
        for follow in lines[idx + 1:]:
            if not follow.strip() or follow.lstrip().startswith("#"):
                continue
            item = _LIST_ITEM.match(follow)
            if not item:
                break
            yield from _tokens(item.group(1))


def _structural_hit(text: str, cfg: Config) -> TierHit | None:
    """The file-level rules: telos literal, sensitive flag, frontmatter and inline tags."""
    folded = _fold(text)
    if "telos/sensitive" in folded.replace("\\", "/"):
        return TierHit(kind="sensitive", code="literal_telos_sensitive", where="text")
    if _SENSITIVE_FLAG.search(folded):
        return TierHit(kind="sensitive", code="flag_sensitive_true", where="text")
    wanted = {_fold(t).strip().lstrip("#") for t in cfg.gates.sensitive_tags if t.strip()}
    if any(_tag_matches(t, wanted) for t in _frontmatter_tags(folded)):
        return TierHit(kind="sensitive", code="tag_frontmatter", where="text")
    if any(_tag_matches(m.group(1), wanted) for m in _HASHTAG.finditer(folded)):
        return TierHit(kind="sensitive", code="tag_inline", where="text")
    return None


def _term_hit(text: str, cfg: Config) -> TierHit | None:
    """The `[gates].sensitive_terms` scan, folded like every other text rule."""
    folded = _fold(text)
    for index, term in enumerate(cfg.gates.sensitive_terms):
        needle = _fold(term).strip()
        # An empty term would match every text; skipping it is the safe reading of a typo.
        if needle and needle in folded:
            return TierHit(kind="sensitive", code=f"term:{index}", where="text")
    return None


def _text_hit(text: str, cfg: Config) -> TierHit | None:
    return _structural_hit(text, cfg) or _term_hit(text, cfg)


def text_hit(text: str, cfg: Config) -> TierHit | None:
    """Gate 1 on a piece of text. The code names the rule, never the matched term."""
    try:
        return _text_hit(text, cfg)
    except Exception:  # noqa: BLE001  fail closed on anything at all
        return TierHit(kind="sensitive", code=FAIL_CLOSED, where="text")


def scan_terms(text: str, cfg: Config) -> TierHit | None:
    """Only the sensitive-terms rule, for callers that split a file into independent units.

    Tags, the sensitive flag and the telos literal stay file-level: `safe_read_text(...,
    terms=False)` still applies them to the whole file. A collector that reads this way must
    call `scan_terms` on every unit it keeps. Fails closed like `text_hit`.
    """
    try:
        return _term_hit(text, cfg)
    except Exception:  # noqa: BLE001  fail closed on anything at all
        return TierHit(kind="sensitive", code=FAIL_CLOSED, where="text")


# --- items -----------------------------------------------------------------------------


def _looks_like_path(value: str) -> bool:
    return bool(re.search(r"[\\/]", value) or re.match(r"^[A-Za-z]:", value) or value.startswith("~"))


def _meta_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for inner in value.values():
            yield from _meta_strings(inner)
    elif isinstance(value, (list, tuple, set)):
        for inner in value:
            yield from _meta_strings(inner)


def _vault_literal_hit(text: str, cfg: Config) -> TierHit | None:
    """The absolute telos or notes path written out in text. Shared by gate 1 and the last scan.

    Both must agree: an item that gate 1 lets through but the final scan rejects would abort
    the whole payload (and open the breaker) instead of being held on its own.
    """
    haystack = _fold(text).replace("\\", "/")
    brain = cfg.paths.brain_root
    for sub in ("telos", "notes"):
        if _fold((brain / sub).as_posix()) in haystack:
            return TierHit(kind="sensitive", code="vault_path_literal", where="text")
    return None


def _item_hit(item: Item, cfg: Config) -> TierHit | None:
    # An item that names a file under a forbidden root is held; git items name no files.
    for path in item.paths:
        hit = path_hit(path, cfg) or forbidden_hit(path, cfg)
        if hit:
            return hit
    if item.origin:
        hit = path_hit(item.origin, cfg) or forbidden_hit(item.origin, cfg)
        if hit:
            return hit
    for value in _meta_strings(item.meta):
        if _looks_like_path(value):
            hit = path_hit(value, cfg) or forbidden_hit(value, cfg)
            if hit:
                return hit
    for text in (item.title, item.text):
        hit = text_hit(text, cfg) or _vault_literal_hit(text, cfg)
        if hit:
            return hit
    wanted = {_fold(t).strip().lstrip("#") for t in cfg.gates.sensitive_tags if t.strip()}
    for tag in item.tags:
        folded = _fold(tag).strip().lstrip("#")
        if folded == DERIVED_TAG:
            return TierHit(kind="sensitive", code=DERIVED_TAG, where="tag")
        if _tag_matches(folded, wanted):
            return TierHit(kind="sensitive", code="tag_item", where="tag")
    # Policy comes last so a sensitive reason always outranks it.
    if item.work and not cfg.digest.work_metadata_to_claude:
        return TierHit(kind="policy", code="work_policy", where="item")
    return None


def item_hit(item: Item, cfg: Config) -> TierHit | None:
    """Gate 1 on a whole item: paths, origin, path-like meta, title, text, tags, work policy."""
    try:
        return _item_hit(item, cfg)
    except Exception:  # noqa: BLE001  fail closed on anything at all
        return TierHit(kind="sensitive", code=FAIL_CLOSED, where="item")


# --- the read primitive ----------------------------------------------------------------


def _withheld(ref: str, reason: str) -> WithheldItem:
    """A content-free reference. source_ref is a local path for `jarvis held`, never Claude-bound."""
    return WithheldItem(id=short_id("file", ref), kind="file", source_ref=ref, reason=reason, hold_kind="sensitive")


def _display_ref(path: object) -> str:
    try:
        return "".join(ch if ord(ch) >= 32 else "?" for ch in os.fspath(path))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        return "<unprintable>"


def safe_read_text(
    path: str | os.PathLike[str],
    cfg: Config,
    roots: Iterable[str | os.PathLike[str]],
    max_bytes: int = 262144,
    terms: bool = True,
) -> str | WithheldItem:
    """Read a text file only if gate 1 allows it. The single read primitive for collectors.

    Order matters: canonicalize, check the roots, check the path rules, and only then open.
    A refused path is never opened, not even to look at its size. Oversize and undecodable
    files are withheld rather than truncated, because a tag past the cut would escape the
    scan. One text hit anywhere withholds the whole file.

    `terms=False` leaves out only the `sensitive_terms` rule. Tags, the sensitive flag, paths,
    size and decoding stay file-level. The caller then owns the term scan, per independent
    unit, through `scan_terms`; it must not emit a unit it has not scanned.
    """
    ref = _display_ref(path)
    try:
        real, canon = _resolve(path)
        ref = canon
        if not any(_under(canon, canonical(root)) for root in roots):
            return _withheld(ref, "outside_roots")
        hit = _canon_hit(canon, cfg) or _forbidden_canon(canon, cfg)
        if hit:
            return _withheld(ref, hit.code)
        if os.stat(real).st_size > max_bytes:
            return _withheld(ref, "too_large")
        with open(real, "rb") as fh:
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            return _withheld(ref, "too_large")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return _withheld(ref, "undecodable")
        text_found = text_hit(text, cfg) if terms else (_structural_hit(text, cfg))
        if text_found:
            return _withheld(ref, text_found.code)
        return text
    except Exception:  # noqa: BLE001  fail closed on anything at all
        return _withheld(ref, FAIL_CLOSED)


# --- the last scan ---------------------------------------------------------------------


def assert_clean(prompt: str, cfg: Config) -> None:
    """Final scan of the serialized prompt. Raises TierViolation on any hit, including errors."""
    try:
        hit = text_hit(prompt, cfg) or _vault_literal_hit(prompt, cfg)
    except Exception:  # noqa: BLE001  fail closed on anything at all
        hit = TierHit(kind="sensitive", code=FAIL_CLOSED, where="text")
    if hit is not None:
        raise TierViolation(hit)
