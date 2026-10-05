"""Notifier: the toast argv, message hygiene, failure handling and the message variants.

No toast is ever shown here: every ToastNotifier gets a recording runner. Synthetic data only.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from jarvisd.config import Config, ConfigError
from jarvisd.notify import (
    MAX_MESSAGE_CHARS,
    NotifyResult,
    NullNotifier,
    ToastNotifier,
    build_notifier,
    digest_message,
    reason_class,
)

REL = "raw/jarvis/digest-2026-10-06.md"
# Built from code points so this file itself stays free of the characters under test.
EM, EN = chr(0x2014), chr(0x2013)


class Runner:
    """Records argv and timeout; returns or raises what the test asks for."""

    def __init__(self, code: int = 0, raises: BaseException | None = None) -> None:
        self.code, self.raises = code, raises
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: list[str], timeout: float) -> int:
        self.calls.append((list(argv), timeout))
        if self.raises is not None:
            raise self.raises
        return self.code


def script(tmp_path: Path, name: str = "notify-jarvis.ps1") -> Path:
    path = tmp_path / name
    path.write_text("param($Title,$Message)\n", encoding="utf-8")
    return path


# --- ToastNotifier -----------------------------------------------------------------------


def test_toast_builds_the_exact_argv(tmp_path: Path) -> None:
    ps1 = script(tmp_path)
    runner = Runner()
    result = ToastNotifier(ps1, runner=runner).send("Digest ready: 3 to look at, 2 held. brain/" + REL)

    assert result == NotifyResult(ok=True, detail="sent")
    argv, timeout = runner.calls[0]
    assert argv == [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", ps1.as_posix(), "-Title", "JARVIS",
        "-Message", "Digest ready: 3 to look at, 2 held. brain/" + REL,
    ]
    assert timeout == 15.0


def test_toast_never_uses_a_shell_and_message_is_one_argv_item(tmp_path: Path) -> None:
    runner = Runner()
    nasty = 'x" ; calc & echo $env:USERNAME `n'
    ToastNotifier(script(tmp_path), runner=runner).send(nasty)
    argv = runner.calls[0][0]
    assert argv[argv.index("-Message") + 1].count("\n") == 0
    assert len(argv) == 10, "the message must stay a single argument"


def test_message_is_capped_cleaned_and_dash_free(tmp_path: Path) -> None:
    runner = Runner()
    long_text = f"Digest{EM}ready\n\twith   spaces {EN} " + "x" * 400
    ToastNotifier(script(tmp_path), runner=runner).send(long_text)
    sent = runner.calls[0][0][-1]
    assert len(sent) <= MAX_MESSAGE_CHARS == 180
    assert EM not in sent and EN not in sent
    assert "\n" not in sent and "\t" not in sent and "  " not in sent


def test_a_message_that_starts_with_a_dash_cannot_become_a_parameter(tmp_path: Path) -> None:
    runner = Runner()
    ToastNotifier(script(tmp_path), runner=runner).send("-File evil.ps1")
    assert not runner.calls[0][0][-1].startswith("-")


def test_missing_script_is_ok_false_and_runs_nothing(tmp_path: Path) -> None:
    runner = Runner()
    result = ToastNotifier(tmp_path / "absent.ps1", runner=runner).send("hello")
    assert result.ok is False and result.detail == "script_missing"
    assert runner.calls == []


def test_missing_script_falls_back_to_the_given_hooks_script(tmp_path: Path) -> None:
    fallback = script(tmp_path, "hooks-notify.ps1")
    runner = Runner()
    result = ToastNotifier(tmp_path / "absent.ps1", runner=runner, fallback_script=fallback).send("hello")
    assert result.ok is True
    assert runner.calls[0][0][5] == fallback.as_posix()


def test_nonzero_exit_is_ok_false(tmp_path: Path) -> None:
    result = ToastNotifier(script(tmp_path), runner=Runner(code=1)).send("hello")
    assert result.ok is False and result.detail == "exit_1"


def test_timeout_is_ok_false_not_an_exception(tmp_path: Path) -> None:
    boom = subprocess.TimeoutExpired(["powershell"], 15)
    result = ToastNotifier(script(tmp_path), runner=Runner(raises=boom)).send("hello")
    assert result.ok is False and result.detail == "timeout"


@pytest.mark.parametrize("exc", [OSError("no powershell"), RuntimeError("weird"), PermissionError("denied")])
def test_any_runner_failure_is_swallowed(tmp_path: Path, exc: BaseException) -> None:
    result = ToastNotifier(script(tmp_path), runner=Runner(raises=exc)).send("hello")
    assert result.ok is False and result.detail in {"spawn_failed", "error"}
    assert "hello" not in result.detail


# --- NullNotifier and build_notifier ------------------------------------------------------


def test_null_notifier_is_ok_and_silent() -> None:
    assert NullNotifier().send("anything") == NotifyResult(ok=True, detail="null")


def test_build_notifier_follows_the_adapter(tmp_cfg: Config) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    notifier = build_notifier(cfg)
    assert isinstance(notifier, ToastNotifier)
    assert notifier.script == cfg.notify.toast_script
    cfg.notify.adapter = "null"
    assert isinstance(build_notifier(cfg), NullNotifier)
    cfg.notify.adapter = "carrier-pigeon"
    with pytest.raises(ConfigError):
        build_notifier(cfg)


# --- digest_message ----------------------------------------------------------------------


def test_message_variants_use_only_integers_and_fixed_phrases() -> None:
    counts = {"attention": 3, "held": 2}
    assert digest_message("ok", counts, REL) == "Digest ready: 3 to look at, 2 held. brain/" + REL
    assert digest_message("degraded", counts, REL, reason="timeout") == (
        "Digest ready, Claude was unavailable (timeout). Deterministic sections only."
    )
    assert digest_message("auth", counts, REL) == (
        "Claude login expired. Run claude /login, then jarvis run-digest --claude --force."
    )
    assert digest_message("breaker", counts, REL, reason="isolation_breach") == (
        "JARVIS paused Claude calls: isolation check. Run jarvis breaker status."
    )
    assert digest_message("fallback", counts, REL) == (
        "Digest written under a fallback name, the target was busy. brain/" + REL
    )


def test_reason_is_mapped_to_a_fixed_class_never_echoed() -> None:
    assert reason_class("rate_limit") == "quota"
    assert reason_class("budget") == "budget"
    hostile = reason_class(f"My secret project title {EM} do not leak")
    assert hostile == "unknown"
    msg = digest_message("degraded", {"attention": 0, "held": 0}, REL, reason="secret project title")
    assert "secret" not in msg


def test_path_is_sanitized_and_counts_are_coerced_to_integers() -> None:
    msg = digest_message("ok", {"attention": "7", "held": 1.9}, "raw/jarvis/we ird\n$(calc).md")
    assert "\n" not in msg and "$" not in msg and "(" not in msg
    assert msg.startswith("Digest ready: 7 to look at, 1 held.")


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ValueError):
        digest_message("party", {}, REL)


def test_every_variant_fits_the_toast_cap() -> None:
    for status in ("ok", "degraded", "auth", "breaker", "fallback"):
        msg = digest_message(status, {"attention": 5, "held": 99}, REL, reason="isolation_anomaly")
        assert len(msg) <= MAX_MESSAGE_CHARS, status
        assert EM not in msg and EN not in msg


def test_notify_result_is_plain_data() -> None:
    result: Any = NotifyResult(ok=False, detail="x")
    assert (result.ok, result.detail) == (False, "x")


def test_unwritten_toast_uses_fixed_phrases_and_fits() -> None:
    for token, phrase in (("vault_busy", "vault busy"), ("vault_error", "vault error"),
                          ("vault_denied", "vault refused"), ("anything else", "unknown")):
        message = digest_message("unwritten", {}, "", token)
        assert phrase in message and "could not be written" in message
        assert len(message) <= 180 and "jarvis status" in message


def test_the_auth_toast_names_a_command_that_works_after_the_degraded_job_is_done() -> None:
    # The degraded digest job is already done, so a bare --claude run is refused; --force is the way in.
    assert "--claude --force" in digest_message("auth", {}, "")
