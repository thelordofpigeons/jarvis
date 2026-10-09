"""Small pure helpers shared by every layer.

Written in the style of bin/watchdog.py but never imported from it: bin/ is Phase 0 and
stays stdlib-only and venv-free.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

# Escapes, not literals, so this file itself stays free of the characters it removes.
_EM_DASH = "\u2014"
_EN_DASH = "\u2013"

_EM_RE = re.compile(r"\s*" + _EM_DASH + r"\s*")
_EN_SPACED_RE = re.compile(r"\s+" + _EN_DASH + r"\s+")
_UNIT_SEP = "\x1f"

# An open thread older than this (days) is stale: flagged by the brain collector, left out of
# the digest's Still open section (docs/hub-rework-contract.md, section 1.2).
STALE_AFTER_DAYS = 7

_WIKILINK_RE = re.compile(r"\[\[[^\]]*\]\]")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_ID_RE = re.compile(r"\[[0-9a-f]{8}\]|\bw-[0-9a-f]{6}\b")
_MARKER_RE = re.compile(r"\((?:stale|see entry point)\)")
_PUNCT_RE = re.compile(r"[^\w\s]")
# A session line that says there is nothing to do, and the words a line may be made of once
# its wikilinks are gone and still say nothing ("Voir [[x]] et [[y]]").
NO_ACTIVE_WORK = re.compile(r"^no active work\b")
WIKILINK_FILLER = frozenset({"see", "voir", "cf", "and", "et", "the", "related", "ref", "refs", "aussi", "also"})
# A line that only points elsewhere ("Related: note-a, note-b"), whatever follows the colon.
REFERENCE_PREFIX = re.compile(r"^\s*(?:[-*]\s+)?(?:related|see also|voir aussi|refs?|references?)\s*:", re.IGNORECASE)


def now_utc() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    """Current time in the machine's local zone, timezone-aware.

    Uses the stdlib only. The digest date is the local date, so a
    change of time zone moves the date but can never double-fire one date (design section 5).
    """
    return datetime.now().astimezone()


def iso(dt: datetime, timespec: str = "seconds") -> str:
    """UTC ISO-8601 string. Refuses naive datetimes: a naive time has no meaning here."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("iso() needs a timezone-aware datetime")
    return dt.astimezone(timezone.utc).isoformat(timespec=timespec)


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string into an aware datetime. Accepts a trailing Z."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return dt


def sha256_hex(data: bytes | str) -> str:
    """Hex SHA-256 of bytes, or of a str encoded as UTF-8."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def canonical_json(obj: Any) -> str:
    """Deterministic compact JSON for hashing: sorted keys, no spaces, NaN rejected.

    The audit hash chain hashes this form, so it must never change shape between runs.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def strip_dashes(text: str) -> str:
    """Remove U+2014 and U+2013 (house rule: no em or en dashes in any output).

    An em dash becomes a comma break, a spaced en dash too, an unspaced en dash (a range
    such as 10-12) becomes a hyphen. Leading and trailing breaks left behind are dropped.
    """
    if _EM_DASH not in text and _EN_DASH not in text:
        return text
    out = _EM_RE.sub(", ", text)
    out = _EN_SPACED_RE.sub(", ", out)
    out = out.replace(_EN_DASH, "-")
    if out.startswith(", "):
        out = out[2:]
    if out.endswith(", "):
        out = out[:-2]
    return out


def norm_key(text: str) -> str:
    """The normalised key of an item line: what makes two bullets "the same thread".

    Casefolded, without wikilinks, dates, item and held ids, the `(stale)` and `(see entry
    point)` markers and punctuation, whitespace collapsed. The brain collector collapses
    RECENT.md bullets and session lines on it; the hub will key item state on it.
    """
    t = text.casefold()
    t = _WIKILINK_RE.sub(" ", t)
    t = _DATE_RE.sub(" ", t)
    t = _ID_RE.sub(" ", t)
    t = _MARKER_RE.sub(" ", t)
    t = _PUNCT_RE.sub(" ", t)
    return " ".join(t.split())


def wikilink_only(key: str) -> bool:
    """True when a normalised key is empty or made only of filler words around wikilinks."""
    words = key.split()
    return not words or all(w in WIKILINK_FILLER for w in words)


def noise_line(text: str) -> bool:
    """A thread or session line that carries nothing to act on: "No active work ...", wikilinks only, or a
    reference line ("Related: ..."). One rule for the collector, the writer and the hub's fallback parser."""
    key = norm_key(text)
    return bool(NO_ACTIVE_WORK.match(key)) or wikilink_only(key) or bool(REFERENCE_PREFIX.match(text))


def short_id(*parts: object) -> str:
    """Stable 8 hex character id from its parts.

    Parts are joined with a unit separator so ("ab", "c") and ("a", "bc") differ. Used for
    item ids, so the same source line gets the same id on every run.
    """
    joined = _UNIT_SEP.join(str(p) for p in parts)
    return sha256_hex(joined)[:8]
