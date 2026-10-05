"""Shared fixtures: a synthetic vault, a Config pointed at tmp_path, a fake clock.

Everything here is synthetic. No brain content and no work data (design D10).
"""
from __future__ import annotations

import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvisd import ROOT
from jarvisd.config import Config, build_config

CANARY = "JARVIS-CANARY-7f3a"

SYNTHETIC_RECENT = """# Recent

## Open Threads
- [2026-10-04] Synthetic thread one, waiting on a reviewer
- [2026-09-20] Synthetic stale thread

## Recent Decisions
- [2026-10-03] Synthetic decision, because it is a fixture
"""


class FakeClock:
    """Deterministic clock. Call it, or read .now, and move it with advance()/set()."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 6, 5, 31, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now

    def set(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("FakeClock needs an aware datetime")
        self.now = value
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """A throwaway brain tree. The directory is named 'brain' so path globs match."""
    brain = tmp_path / "brain"
    for rel in (
        "telos/sensitive",
        "notes",
        "raw/jarvis",
        "sessions",
        "session-checkpoints/processed",
        "session-checkpoints/from-old-machine",
    ):
        (brain / rel).mkdir(parents=True, exist_ok=True)
    (brain / "telos" / "sensitive" / "canary.md").write_text(
        f"# canary\n{CANARY}\n", encoding="utf-8", newline="\n"
    )
    (brain / "RECENT.md").write_text(SYNTHETIC_RECENT, encoding="utf-8", newline="\n")
    return brain


def _fwd(path: Path) -> str:
    return path.as_posix()


@pytest.fixture
def tmp_cfg(tmp_path: Path, tmp_vault: Path) -> Config:
    """The real jarvis.toml (no local override) with every path moved under tmp_path."""
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    root = tmp_path / "jarvis"
    for name in ("queue", "logs", "models", "state", "deploy"):
        (root / name).mkdir(parents=True, exist_ok=True)
    raw["paths"].update(
        {
            "root": _fwd(root),
            "queue": _fwd(root / "queue"),
            "logs": _fwd(root / "logs"),
            "models": _fwd(root / "models"),
            "vault_write_raw": _fwd(tmp_vault / "raw" / "jarvis"),
            "vault_write_sessions": _fwd(tmp_vault / "sessions"),
            "vault_forbidden": [
                _fwd(tmp_vault / "telos"),
                _fwd(tmp_vault / "notes"),
                _fwd(tmp_path / "Documents" / "Work"),
            ],
        }
    )
    raw.setdefault("daemon", {})["state_dir"] = _fwd(root / "state")
    raw.setdefault("notify", {})["toast_script"] = _fwd(root / "deploy" / "notify-jarvis.ps1")
    return build_config(raw)


@pytest.fixture(autouse=True)
def _claude_check_is_not_skipped_by_the_runner_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub sets CI=true, which makes `self-test` skip a missing Claude binary (selftest.py).

    Tests that assert the FAIL behaviour must see the same thing on a laptop and on CI, so the
    skip switches are cleared here; tests/test_publishing.py sets them back where it needs them.
    """
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("JARVIS_NO_CLAUDE", raising=False)
