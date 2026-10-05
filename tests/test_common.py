"""Tests for jarvisd.common helpers and repo-level hygiene of the T1 files."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvisd import common

REPO = Path(__file__).resolve().parent.parent
EM = "\u2014"
EN = "\u2013"

# Files T1 owns. Other tasks add their own files and their own hygiene checks.
T1_FILES = [
    "pyproject.toml",
    "requirements.lock",
    "jarvis.local.toml.example",
    "jarvis.cmd",
    "deploy/setup-venv.ps1",
    "jarvisd/__init__.py",
    "jarvisd/__main__.py",
    "jarvisd/common.py",
    "jarvisd/config.py",
    "jarvisd/models.py",
    "tests/conftest.py",
    "tests/test_config.py",
    "tests/test_models.py",
    "tests/test_common.py",
]


def test_strip_dashes_removes_em_and_en_dash() -> None:
    text = f"alpha {EM} beta{EM}gamma {EN} delta 10{EN}12"
    out = common.strip_dashes(text)
    assert EM not in out
    assert EN not in out
    assert "10-12" in out


def test_strip_dashes_leaves_plain_text_alone() -> None:
    assert common.strip_dashes("plain text, with-hyphen") == "plain text, with-hyphen"


def test_strip_dashes_handles_edges() -> None:
    assert common.strip_dashes(f"{EM} start") == "start"
    assert common.strip_dashes(f"end {EM}") == "end"
    assert common.strip_dashes("") == ""


def test_short_id_is_stable_and_distinct() -> None:
    a = common.short_id("brain", "RECENT.md", "line one")
    assert a == common.short_id("brain", "RECENT.md", "line one")
    assert len(a) == 8
    int(a, 16)
    assert a != common.short_id("brain", "RECENT.md", "line two")
    # Part boundaries matter: ("ab", "c") must not collide with ("a", "bc").
    assert common.short_id("ab", "c") != common.short_id("a", "bc")


def test_sha256_hex_accepts_str_and_bytes() -> None:
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert common.sha256_hex("abc") == expected
    assert common.sha256_hex(b"abc") == expected


def test_canonical_json_is_order_independent_and_compact() -> None:
    a = common.canonical_json({"b": 1, "a": [1, 2], "c": {"y": 1, "x": 2}})
    b = common.canonical_json({"c": {"x": 2, "y": 1}, "a": [1, 2], "b": 1})
    assert a == b
    assert a == '{"a":[1,2],"b":1,"c":{"x":2,"y":1}}'


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValueError):
        common.canonical_json({"x": float("nan")})


def test_iso_roundtrip_is_utc_seconds() -> None:
    dt = datetime(2026, 10, 6, 4, 31, 2, 999, tzinfo=timezone.utc)
    s = common.iso(dt)
    assert s == "2026-10-06T04:31:02+00:00"
    assert common.parse_iso(s) == dt.replace(microsecond=0)


def test_iso_converts_other_offsets_to_utc() -> None:
    dt = datetime(2026, 10, 6, 6, 30, tzinfo=timezone(timedelta(hours=1)))
    assert common.iso(dt) == "2026-10-06T05:30:00+00:00"


def test_parse_iso_accepts_z_and_rejects_naive() -> None:
    assert common.parse_iso("2026-10-06T04:31:02Z").utcoffset() == timedelta(0)
    with pytest.raises(ValueError):
        common.parse_iso("2026-10-06T04:31:02")
    with pytest.raises(ValueError):
        common.iso(datetime(2026, 10, 6, 4, 31, 2))


def test_now_utc_and_local_now_are_aware() -> None:
    assert common.now_utc().utcoffset() == timedelta(0)
    assert common.local_now().tzinfo is not None
    assert abs((common.local_now() - common.now_utc()).total_seconds()) < 5


def test_t1_files_have_no_em_or_en_dash_no_bom_and_lf() -> None:
    for rel in T1_FILES:
        path = REPO / rel
        if not path.exists():
            continue
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), f"BOM in {rel}"
        assert b"\r\n" not in raw, f"CRLF in {rel}"
        text = raw.decode("utf-8")
        assert EM not in text, f"em dash in {rel}"
        assert EN not in text, f"en dash in {rel}"


def test_all_t1_files_exist() -> None:
    missing = [rel for rel in T1_FILES if not (REPO / rel).exists()]
    assert missing == []
