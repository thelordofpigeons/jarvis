"""`jarvis weekly`: the CLI twin of the digest's weekly review write (contract section 9)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jarvisd import cli
from test_cli import Env, env, tree  # noqa: F401  the fixture is used by name

MARKER = "---\ntype: jarvis-weekly\ngenerator: jarvisd\n"


def test_weekly_dry_run_prints_the_note_and_writes_nothing(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    before = tree(env.vault, env.cfg.paths.queue, env.cfg.daemon.state_dir)
    assert env.run("weekly", "--dry-run", "--week", "2026-W40") == 0
    out = capsys.readouterr().out
    assert out.startswith(MARKER) and "# Week 2026-W40" in out and "## Cost by day" in out
    assert "week: 2026-W40" in out and "from: 2026-09-28" in out and "to: 2026-10-04" in out
    assert "Nothing was written" in out and "nothing to review" in out
    assert tree(env.vault, env.cfg.paths.queue, env.cfg.daemon.state_dir) == before
    assert env.runner.all() == []


def test_weekly_writes_the_note_through_the_vault_writer_and_can_rewrite_its_own_file(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    assert env.run("weekly", "--week", "2026-W40") == 0
    out = capsys.readouterr().out
    path = env.raw("weekly-2026-W40.md")
    assert path.is_file() and path.as_posix() in out
    text = path.read_text(encoding="utf-8")
    assert text.startswith(MARKER) and "- 0 runs, 0 failed, $0.00 Claude." in text
    assert env.events("vault_write")[-1]["rel"] == "raw/jarvis/weekly-2026-W40.md"
    assert env.events("vault_write")[-1]["job_id"] == "weekly-2026-W40-manual"
    assert env.run("weekly", "--week", "2026-W40") == 0, "the marker rule lets it rewrite its own file"
    assert env.events("vault_write")[-1]["replaced"] is True


def test_weekly_refuses_a_foreign_file_and_a_bad_week(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.raw("weekly-2026-W39.md").write_text("someone else's review", encoding="utf-8")
    assert env.run("weekly", "--week", "2026-W39") == 1
    assert "vault refused (existing_file_not_ours)" in capsys.readouterr().out
    assert env.raw("weekly-2026-W39.md").read_text(encoding="utf-8") == "someone else's review"
    for bad in ("2026-13", "2026-W60", "week41"):
        assert env.run("weekly", "--week", bad) == 2, bad
        err = capsys.readouterr().err
        assert err.startswith("jarvis: ") and ("2026-W41" in err or "no such ISO week" in err), err


def test_weekly_defaults_to_the_week_just_ended(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    monday = datetime(2026, 10, 12, 7, 0, tzinfo=timezone.utc)  # the Monday of 2026-W42
    code = cli.main(["weekly", "--dry-run"], cfg=env.cfg, claude_runner=env.runner, notifier=env.notes,
                    command_runner=env.commands, task_base_dir=env.tmp_path / "claude-home", network_probe=lambda: True,
                    clock=lambda: monday)
    assert code == 0
    assert "week: 2026-W41" in capsys.readouterr().out


def test_weekly_reads_the_run_manifests_and_the_history(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    state = env.cfg.daemon.state_dir
    run = state / "runs" / "digest-2026-09-30"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({
        "job_id": "digest-2026-09-30", "status": "complete", "started_at": "2026-09-30T05:30:00+00:00",
        "finished_at": "2026-09-30T05:31:00+00:00", "stages": {}, "counts": {}, "cost_usd": 0.02, "paths": {}, "hashes": {},
        "config_sha256": None, "audit_seq": 3}), encoding="utf-8")
    (state / "item-history.json").write_text(json.dumps({"updated": "2026-10-01", "last_run": "digest-2026-10-01", "items": {
        "synthetic decision": {"id": "9f8e7d6c", "text": "Synthetic decision", "first_seen": "2026-10-01", "last_seen": "2026-10-01",
                               "times_shown": 1, "sections": ["decided"], "status": "done", "snoozed_until": None,
                               "resolved_at": "2026-10-01"}}}), encoding="utf-8")
    assert env.run("weekly", "--dry-run", "--week", "2026-W40") == 0
    out = capsys.readouterr().out
    assert "- 1 run, 0 failed, $0.02 Claude." in out
    assert "- 2026-10-01: Synthetic decision [9f8e7d6c]" in out
    assert "- 2026-09-30: 1 run, $0.02." in out
    assert "n_decided: 1" in out and "Nothing was written." in out and "nothing to review" not in out
