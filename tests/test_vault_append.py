"""The vault writer's audited append (Q2): one generator-owned file, never a foreign one."""
from __future__ import annotations

import pytest

from conftest import FakeClock
from jarvisd.audit import AuditLog
from jarvisd.config import Config
from jarvisd.vault import APPENDABLE, VaultWriteDenied, VaultWriter

NAME = "confirmed-tasks.md"
HEADER = "---\ntype: jarvis-confirmed-tasks\ngenerator: jarvisd\n---\n# Confirmed tasks\n"


@pytest.fixture
def audit(tmp_cfg: Config, clock: FakeClock) -> AuditLog:
    return AuditLog(tmp_cfg.paths.logs / "jarvisd-audit.jsonl", clock=clock, mirror_stdout=False)


@pytest.fixture
def writer(tmp_cfg: Config, audit: AuditLog) -> VaultWriter:
    return VaultWriter(tmp_cfg, audit)


def test_only_the_confirmed_tasks_file_is_appendable() -> None:
    assert APPENDABLE == frozenset({NAME})


def test_append_creates_with_the_header_then_appends(writer: VaultWriter, tmp_cfg: Config) -> None:
    first = writer.append_raw(NAME, "\n## one\n", "j1", header=HEADER)
    second = writer.append_raw(NAME, "\n## two\n", "j1", header=HEADER)
    target = tmp_cfg.paths.vault_write_raw / NAME
    assert target.read_text(encoding="utf-8") == HEADER + "\n## one\n" + "\n## two\n"
    assert first.rel == "raw/jarvis/" + NAME and not first.replaced
    assert second.replaced and second.path == first.path
    assert b"\r" not in target.read_bytes()


def test_append_is_audited_as_intent_then_write(writer: VaultWriter, audit: AuditLog) -> None:
    writer.append_raw(NAME, "\n## one\n", "j1", header=HEADER)
    events = [r["event"] for r in audit.records(events=["vault_intent", "vault_write"])]
    assert events == ["vault_intent", "vault_write"]
    done = audit.records(events=["vault_write"])[0]
    assert done["ok"] is True and done["op"] == "append" and done["rel"] == "raw/jarvis/" + NAME


def test_append_refuses_a_file_without_the_generator_marker(writer: VaultWriter, tmp_cfg: Config,
                                                            audit: AuditLog) -> None:
    target = tmp_cfg.paths.vault_write_raw / NAME
    target.write_text("# my own notes\n", encoding="utf-8", newline="\n")
    with pytest.raises(VaultWriteDenied) as exc:
        writer.append_raw(NAME, "\n## one\n", "j1", header=HEADER)
    assert exc.value.reason == "existing_file_not_ours"
    assert target.read_text(encoding="utf-8") == "# my own notes\n"
    assert audit.records(events=["vault_violation"])[0]["reason"] == "existing_file_not_ours"


@pytest.mark.parametrize("name", ["digest-2026-10-06.md", "candidates/x.md", "../x.md", "notes.md", ""])
def test_append_refuses_any_other_name(writer: VaultWriter, name: str) -> None:
    with pytest.raises(VaultWriteDenied):
        writer.append_raw(name, "x\n", "j1", header=HEADER)


def test_append_header_must_carry_the_marker(writer: VaultWriter, tmp_cfg: Config) -> None:
    with pytest.raises(VaultWriteDenied) as exc:
        writer.append_raw(NAME, "x\n", "j1", header="# no marker\n")
    assert exc.value.reason == "missing_generator_marker"
    assert not (tmp_cfg.paths.vault_write_raw / NAME).exists()


def test_append_strips_dashes_and_bom(writer: VaultWriter, tmp_cfg: Config) -> None:
    writer.append_raw(NAME, "﻿\na" + chr(0x2014) + "b" + chr(0x2013) + "c\n", "j1", header=HEADER)
    text = (tmp_cfg.paths.vault_write_raw / NAME).read_text(encoding="utf-8")
    assert chr(0x2014) not in text and chr(0x2013) not in text and "﻿" not in text


def test_append_refuses_to_grow_past_the_size_cap(writer: VaultWriter, monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvisd.vault as vault_mod

    monkeypatch.setattr(vault_mod, "MAX_BYTES", len(HEADER) + 10)
    writer.append_raw(NAME, "\nshort\n", "j1", header=HEADER)
    with pytest.raises(VaultWriteDenied) as exc:
        writer.append_raw(NAME, "\n" + "x" * 50 + "\n", "j1", header=HEADER)
    assert exc.value.reason == "too_large"


def test_check_append_reports_create_append_or_the_reason(writer: VaultWriter, tmp_cfg: Config) -> None:
    assert writer.check_append(NAME) == "create"
    writer.append_raw(NAME, "\n## one\n", "j1", header=HEADER)
    assert writer.check_append(NAME) == "append"
    (tmp_cfg.paths.vault_write_raw / NAME).write_text("# foreign\n", encoding="utf-8", newline="\n")
    with pytest.raises(VaultWriteDenied):
        writer.check_append(NAME)


def test_check_append_writes_and_audits_nothing(writer: VaultWriter, tmp_cfg: Config, audit: AuditLog) -> None:
    writer.check_append(NAME)
    assert not (tmp_cfg.paths.vault_write_raw / NAME).exists()
    assert audit.records(events=["vault_intent", "vault_write", "vault_violation"]) == []


def test_append_refuses_to_grow_past_the_size_cap(writer: VaultWriter, monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvisd.vault as vault_mod

    monkeypatch.setattr(vault_mod, "MAX_BYTES", len(HEADER) + 10)
    writer.append_raw(NAME, "\nshort\n", "j1", header=HEADER)
    with pytest.raises(VaultWriteDenied) as exc:
        writer.append_raw(NAME, "\n" + "x" * 50 + "\n", "j1", header=HEADER)
    assert exc.value.reason == "too_large"
