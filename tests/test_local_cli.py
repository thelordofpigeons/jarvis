"""`jarvis local status` and `jarvis local check` (P3). Against the fake server only."""
from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from jarvisd import ROOT, cli
from jarvisd.config import Config

_spec = importlib.util.spec_from_file_location("fake_openai_cli", ROOT / "tests" / "fakes" / "fake_openai.py")
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["fake_openai_cli"] = _mod
_spec.loader.exec_module(_mod)


@pytest.fixture
def server() -> Iterator[Any]:
    fake = _mod.FakeOpenAI().start()
    try:
        yield fake
    finally:
        fake.stop()


def _enabled(cfg: Config, fake: Any) -> Config:
    out = cfg.model_copy(deep=True)
    out.local.enabled, out.local.backend = True, "llama"
    out.local.request_timeout_s = 2.0
    out.llama.host, out.llama.port = "127.0.0.1", fake.port
    return out


def test_status_prints_not_installed_when_off(tmp_cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["local", "status"], cfg=tmp_cfg) == 0
    out = capsys.readouterr().out
    assert out.startswith("Local tier: not_installed")
    assert "StubRouter" in out and "Summaries of sensitive text: off" in out
    assert chr(0x2014) not in out and chr(0x2013) not in out


def test_status_json_is_machine_readable(tmp_cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["local", "status", "--json"], cfg=tmp_cfg) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["state"] == "not_installed" and data["probed"] is False and data["enabled"] is False


def test_status_up_against_the_fake(tmp_cfg: Config, server: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["local", "status"], cfg=_enabled(tmp_cfg, server)) == 0
    out = capsys.readouterr().out
    assert "Local tier: up" in out and "LocalRouter" in out and "probed" in out


def test_status_names_a_refused_host(tmp_cfg: Config, server: Any, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _enabled(tmp_cfg, server)
    cfg.llama.host = "198.51.100.3"
    assert cli.main(["local", "status"], cfg=cfg) == 0
    out = capsys.readouterr().out
    assert "Local tier: unavailable" in out and "not 127.0.0.1" in out


def test_status_with_the_wrong_backend_name_is_loud(tmp_cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.local.enabled, cfg.local.backend = True, ""
    assert cli.main(["local", "status"], cfg=cfg) == 0
    assert "Local tier: unavailable" in capsys.readouterr().out


def test_plain_status_command_reports_the_same_tier(tmp_cfg: Config, server: Any, capsys: pytest.CaptureFixture[str]) -> None:
    # `jarvis status` exits 1 while the daemon is stopped, which it is in a test; only the tier matters here.
    cli.main(["status", "--json"], cfg=_enabled(tmp_cfg, server))
    assert json.loads(capsys.readouterr().out)["local_tier"] == "up"
    cli.main(["status", "--json"], cfg=tmp_cfg)
    assert json.loads(capsys.readouterr().out)["local_tier"] == "not_installed"


def test_check_passes_and_is_audited(tmp_cfg: Config, server: Any, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _enabled(tmp_cfg, server)
    assert cli.main(["local", "check"], cfg=cfg) == 0
    out = capsys.readouterr().out
    assert "PASS  health" in out and "PASS  router_contract_darija" in out and "0 failed" in out
    audit = (cfg.paths.logs / "jarvisd-audit.jsonl").read_text(encoding="utf-8")
    assert '"event":"local_check"' in audit
    assert "Weekly planning notes" not in audit  # probe text never reaches the audit


def test_check_works_while_the_tier_is_off_and_says_so(tmp_cfg: Config, server: Any,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _enabled(tmp_cfg, server)
    cfg.local.enabled = False
    assert cli.main(["local", "check"], cfg=cfg) == 0
    assert "dry check" in capsys.readouterr().out


def test_check_fails_with_exit_one_on_a_broken_server(tmp_cfg: Config, server: Any,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    server.mode = "invalid_json"
    assert cli.main(["local", "check"], cfg=_enabled(tmp_cfg, server)) == 1
    out = capsys.readouterr().out
    assert "FAIL  router_contract_en" in out and "not the contract" in out


def test_check_json(tmp_cfg: Config, server: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["local", "check", "--json"], cfg=_enabled(tmp_cfg, server)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows][:2] == ["host", "health"] and all(r["ok"] for r in rows)


def test_local_requires_an_action(tmp_cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["local"], cfg=tmp_cfg) == 2
