"""System collector: numbers from a synthetic audit log, watchdog logs and a schtasks fixture."""
from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import CANARY, FakeClock
from jarvisd.audit import AuditLog
from jarvisd.collectors import CollectContext
from jarvisd.collectors.system import (
    TASK_RESULTS,
    SystemCollector,
    parse_task_time,
    task_info_argv,
    task_result_ok,
    task_result_text,
)
from jarvisd.config import Config
from jarvisd.jobstore import JobStore
from jarvisd.models import CollectResult
from jarvisd.state import StateStore

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 10, 6, 6, 31, 0, tzinfo=TZ)  # equals FakeClock's default 05:31 UTC
FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "schtasks_info_nightly.json"
WATCH_LINE = (
    "Kill switch: 2 trips (last: manual-stop), 2 dry runs. "
    "Watchdog: 1 crashloop, 2 restart attempts, 2 kill switch requests."
)


@pytest.fixture(autouse=True)
def _watched(tmp_cfg: Config) -> None:
    # The collector reports the tasks named in [digest].watched_tasks; the tracked default is the daemon's own.
    tmp_cfg.digest.watched_tasks = ["ExampleNightly", "ExampleWeekly"]


def ctx_for(cfg: Config) -> CollectContext:
    return CollectContext(cfg=cfg, window_start=NOW - timedelta(hours=24), window_end=NOW, now=NOW)


def jl(path: Path, *records: dict[str, object] | str) -> None:
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


class FakeRunner:
    def __init__(self, weekly: tuple[int, str] | None = (1, "")) -> None:
        self.calls: list[list[str]] = []
        self.nightly = (0, FIXTURE.read_text(encoding="utf-8"))
        self.weekly = weekly

    def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        self.calls.append(list(argv))
        if "ExampleNightly" in argv[-1]:
            return self.nightly
        if self.weekly is None:
            raise OSError("powershell is gone")
        return self.weekly


@pytest.fixture
def audit(tmp_cfg: Config, clock: FakeClock) -> AuditLog:
    log = AuditLog(tmp_cfg.paths.logs / "jarvisd-audit.jsonl", clock=clock, mirror_stdout=False)
    clock.advance(hours=-48)
    log.emit("job_done", job_id="digest-2026-10-04")  # outside the window
    log.emit("daemon_start", pid=1)
    clock.advance(hours=48)
    log.emit("job_done", job_id="digest-2026-10-06")
    log.emit("job_failed", job_id="digest-x", error="boom")
    log.emit("claude_call", call_id="c1", ok=True, total_cost_usd=0.0412,
             usage={"input_tokens": 2000, "output_tokens": 300, "cache_creation_input_tokens": 900,
                    "cache_read_input_tokens": 0})
    log.emit("vault_write", ok=True, rel="raw/jarvis/digest-2026-10-06.md")
    log.emit("vault_write", ok=False, rel="raw/jarvis/other.md")
    log.emit("daemon_start", pid=2)
    log.emit("daemon_start", pid=3)
    log.emit("unclean_previous_exit")
    log.emit("breaker", state="open")
    log.emit("correction", item_id="a1b2c3d4")
    log.emit("disk_check", ok=True, free_gb=123)
    return log


def make(cfg: Config, audit: AuditLog, clock: FakeClock, runner: FakeRunner | None = None,
         store: JobStore | None = None) -> SystemCollector:
    return SystemCollector(audit, StateStore.from_config(cfg, clock=clock), store=store, runner=runner or FakeRunner())


def lines_of(result: CollectResult) -> list[str]:
    return [it.title for it in result.items]


def seed_logs(logs: Path) -> None:
    jl(
        logs / "killswitch.jsonl",
        {"ts": "2026-09-22T03:14:04.8598310Z", "event": "killswitch", "dry_run": False},  # before the window
        {"ts": "2026-10-06T01:00:00.1234567Z", "event": "killswitch", "dry_run": False,
         "reason": "manual-stop"},
        {"ts": "2026-10-06T01:05:00Z", "event": "killswitch", "dry_run": True},
        "not json at all",
    )
    jl(
        logs / "watchdog.jsonl",
        {"ts": "2026-09-01T00:00:00+00:00", "event": "health_crashloop"},
        {"ts": "2026-10-06T00:10:00+00:00", "event": "killswitch_trip", "dry_run": False},
        {"ts": "2026-10-06T00:20:00+00:00", "event": "health_crashloop"},
        {"ts": "2026-10-06T00:30:00+00:00", "event": "health_crashloop"},
        {"ts": "2026-10-06T00:40:00+00:00", "event": "restart_attempt"},
        *({"ts": f"2026-10-06T02:0{n}:00+00:00", "event": "gpu_yield"} for n in range(5)),
        {"ts": "2026-10-06T03:00:00+00:00", "event": "probe"},
    )


def test_numbers_from_audit_logs_and_schtasks(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    seed_logs(tmp_cfg.paths.logs)
    runner = FakeRunner()
    result = make(tmp_cfg, audit, clock, runner).collect(ctx_for(tmp_cfg))

    assert result.ok and result.source == "system"
    lines = lines_of(result)
    assert lines[0] == "Jobs: 1 done, 1 failed. Claude calls: 1 ($0.04, 3.2k tokens). Vault writes: 1. Breaker: closed."
    assert "Daemon starts since last digest: 2. Unclean exits: 1. Breaker events: 1." in lines
    assert "ExampleNightly: last run 2026-10-06 02:30, ok (0x00000000)." in lines
    assert "ExampleWeekly: unavailable (could not query the scheduled task)." in lines
    assert (
        "Kill switch: 1 trip (last: manual-stop), 1 dry run. "
        "Watchdog: 2 crashloops, 1 restart attempt, 1 kill switch request. Skipped 1 unreadable log line."
    ) in lines
    assert "Disk check: ok, 123 GB free." in lines
    assert "Corrections logged: 1." in lines

    f = result.facts
    assert (f["jobs_done"], f["jobs_failed"], f["claude_calls"], f["vault_writes"]) == (1, 1, 1, 1)
    assert f["claude_cost_usd"] == 0.0412 and f["claude_tokens"] == 3200
    assert (f["killswitch_trips"], f["killswitch_dry_runs"], f["watchdog_crashloops"], f["restart_attempts"]) == (
        1, 1, 2, 1)
    assert (f["watchdog_trip_requests"], f["log_malformed_lines"]) == (1, 1)
    assert f["gpu_yield_noise"] == 5
    nightly = f["scheduled_tasks"]["ExampleNightly"]
    assert nightly["last_result"] == 0 and nightly["ok"] is True and nightly["last_result_text"] == "ok (0x00000000)"
    assert nightly["last_run_text"] == "2026-10-06 02:30"
    assert f["scheduled_tasks"]["ExampleWeekly"]["available"] is False
    assert (f["daemon_crashes"], f["config_invalid"]) == (0, 0)
    assert (f["disk_checked"], f["disk_ok"], f["disk_free_gb"]) == (True, True, 123)
    assert not any("gpu" in ln.lower() for ln in lines), "gpu_yield is noise and is not shown"


def test_crashes_and_invalid_config_are_counted_as_facts(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    audit.emit("daemon_crash", error_type="RuntimeError", where="daemon.py:1", thread="MainThread")
    audit.emit("daemon_crash", error_type="OSError", where="daemon.py:2", thread="MainThread")
    audit.emit("config_invalid", error="toml")
    f = make(tmp_cfg, audit, clock).collect(ctx_for(tmp_cfg)).facts
    assert (f["daemon_crashes"], f["config_invalid"]) == (2, 1)


def test_task_results_are_decoded_to_words_plus_the_hex_code() -> None:
    assert task_result_text(0) == "ok (0x00000000)"
    assert task_result_text(1) == "script error (0x00000001)"
    assert task_result_text(267009) == "still running (0x00041301)"
    assert task_result_text(267011) == "never ran (0x00041303)"
    assert task_result_text(267014) == "stopped by the user (0x00041306)"
    assert task_result_text(2147750687) == "an instance was already running (0x8004131F)"
    assert task_result_text(2147943623) == "cancelled (0x800704C7)"
    assert task_result_text(2147946720) == "refused by the operator or administrator (0x800710E0)"
    assert task_result_text(4660) == "0x00001234 (unknown)"
    assert task_result_text("4660") == "0x00001234 (unknown)"
    assert task_result_text(None) == "unknown result" and task_result_text("garbage") == "unknown result"
    assert task_result_text(True) == "unknown result" and task_result_text(-1) == "unknown result"
    assert TASK_RESULTS[2147946720] == "refused by the operator or administrator"
    assert task_result_ok(0) and task_result_ok(267009)
    assert not task_result_ok(1) and not task_result_ok(2147946720) and not task_result_ok(None) and not task_result_ok(True)


def test_a_refused_task_is_reported_in_words(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    runner = FakeRunner(weekly=(0, json.dumps({"LastRunTime": "2026-10-06T06:00:00+01:00", "LastTaskResult": 2147946720})))
    result = make(tmp_cfg, audit, clock, runner).collect(ctx_for(tmp_cfg))
    assert "ExampleWeekly: last run 2026-10-06 06:00, refused by the operator or administrator (0x800710E0)." in lines_of(result)
    weekly = result.facts["scheduled_tasks"]["ExampleWeekly"]
    assert weekly["ok"] is False and weekly["last_result"] == 2147946720
    assert weekly["last_result_text"] == "refused by the operator or administrator (0x800710E0)"
    assert "2147946720" not in "\n".join(lines_of(result)), "the raw code never reaches a line"


def test_items_are_deterministic_and_carry_no_free_text(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    result = make(tmp_cfg, audit, clock).collect(ctx_for(tmp_cfg))
    assert result.items
    for item in result.items:
        assert item.meta["render"] == "deterministic"
        assert item.kind == "system_line" and item.source == "system"
        assert item.text == "" and item.paths == [] and item.work is False
    assert len({it.id for it in result.items}) == len(result.items)
    assert CANARY not in result.model_dump_json()


def test_schtasks_runner_gets_exact_argv_for_the_two_known_tasks(
    tmp_cfg: Config, audit: AuditLog, clock: FakeClock
) -> None:
    runner = FakeRunner()
    make(tmp_cfg, audit, clock, runner).collect(ctx_for(tmp_cfg))
    assert [c[-1] for c in runner.calls] == [
        "Get-ScheduledTaskInfo -TaskName ExampleNightly | ConvertTo-Json -Compress",
        "Get-ScheduledTaskInfo -TaskName ExampleWeekly | ConvertTo-Json -Compress",
    ]
    assert all(c[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"] for c in runner.calls)
    allowed = ["ExampleNightly", "ExampleWeekly"]
    with pytest.raises(ValueError):
        task_info_argv("SomethingElse; Remove-Item", allowed)
    with pytest.raises(ValueError):
        task_info_argv("ExampleNightly; Remove-Item", ["ExampleNightly; Remove-Item"])  # the shape is checked too
    assert task_info_argv("ExampleNightly", allowed)[-1].endswith("ExampleNightly | ConvertTo-Json -Compress")


def test_the_tracked_default_watches_only_the_daemons_own_task(tmp_cfg: Config) -> None:
    from jarvisd.config import DigestCfg

    assert DigestCfg().watched_tasks == ["JarvisDaemon"]


@pytest.mark.parametrize("bad", ["a b", "x;y", "$(calc)", "", "t" * 65, "a|b"])
def test_watched_task_names_are_restricted_to_safe_characters(bad: str) -> None:
    from pydantic import ValidationError

    from jarvisd.config import DigestCfg

    with pytest.raises(ValidationError):
        DigestCfg(watched_tasks=[bad])


@pytest.mark.parametrize(
    "weekly",
    [None, (0, "not json"), (0, ""), (1, '{"LastTaskResult": 0}'), (0, "[1, 2]")],
)
def test_schtasks_failures_degrade_to_unavailable(
    tmp_cfg: Config, audit: AuditLog, clock: FakeClock, weekly: tuple[int, str] | None
) -> None:
    result = make(tmp_cfg, audit, clock, FakeRunner(weekly=weekly)).collect(ctx_for(tmp_cfg))
    assert result.ok
    assert "ExampleWeekly: unavailable (could not query the scheduled task)." in lines_of(result)
    assert any(ln.startswith("ExampleNightly: last run") for ln in lines_of(result))


def test_powershell_seven_iso_times_and_never_ran(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    runner = FakeRunner(weekly=(0, json.dumps({"LastRunTime": "2026-10-05T02:30:00+01:00", "LastTaskResult": 267011})))
    runner.nightly = (0, json.dumps({"LastRunTime": "/Date(-62135596800000)/", "LastTaskResult": 267011}))
    lines = lines_of(make(tmp_cfg, audit, clock, runner).collect(ctx_for(tmp_cfg)))
    assert "ExampleNightly: never ran (0x00041303)." in lines
    assert "ExampleWeekly: last run 2026-10-05 02:30, never ran (0x00041303)." in lines
    runner.nightly = (0, json.dumps({"LastRunTime": "/Date(-62135596800000)/", "LastTaskResult": 1}))
    lines = lines_of(make(tmp_cfg, audit, clock, runner).collect(ctx_for(tmp_cfg)))
    assert "ExampleNightly: never ran, last result script error (0x00000001)." in lines


def test_parse_task_time() -> None:
    assert parse_task_time("/Date(1791250200000)/") == datetime(2026, 10, 6, 1, 30, tzinfo=timezone.utc)
    assert parse_task_time("/Date(1791250200000+0100)/") == datetime(2026, 10, 6, 1, 30, tzinfo=timezone.utc)
    assert parse_task_time("2026-10-06T02:30:00+01:00") == datetime(2026, 10, 6, 1, 30, tzinfo=timezone.utc)
    assert parse_task_time(1791250200000) == datetime(2026, 10, 6, 1, 30, tzinfo=timezone.utc)
    assert parse_task_time("2026-10-06T02:30:00") is None  # naive
    assert parse_task_time(None) is None and parse_task_time("garbage") is None


def test_missing_logs_count_as_zero_and_unreadable_logs_degrade(
    tmp_cfg: Config, audit: AuditLog, clock: FakeClock
) -> None:
    result = make(tmp_cfg, audit, clock).collect(ctx_for(tmp_cfg))
    assert (
        "Kill switch: 0 trips, 0 dry runs. Watchdog: 0 crashloops, 0 restart attempts, 0 kill switch requests."
        in lines_of(result)
    )


def test_a_log_over_the_size_cap_is_unavailable(
    tmp_cfg: Config, audit: AuditLog, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import jarvisd.collectors.system as system

    seed_logs(tmp_cfg.paths.logs)
    monkeypatch.setattr(system, "MAX_LOG_BYTES", 10)
    degraded = make(tmp_cfg, audit, clock).collect(ctx_for(tmp_cfg))
    assert degraded.ok and degraded.facts["logs_available"] is False
    assert any("unavailable" in ln and "Kill switch" in ln for ln in lines_of(degraded))


def test_queue_counts_and_breaker_state(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    store = JobStore.from_config(tmp_cfg, clock=clock)
    state = StateStore.from_config(tmp_cfg, clock=clock)
    state.breaker.trip("synthetic")
    collector = SystemCollector(audit, state, store=store, runner=FakeRunner())
    result = collector.collect(ctx_for(tmp_cfg))
    assert "Queue: pending 0, running 0, done 0, failed 0, held 0." in lines_of(result)
    assert result.facts["breaker_state"] == "open"
    assert lines_of(result)[0].endswith("Breaker: open.")


def test_a_broken_audit_log_degrades_only_its_own_lines(tmp_cfg: Config, clock: FakeClock) -> None:
    class Broken:
        def records(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            raise RuntimeError("audit is on fire")

    seed_logs(tmp_cfg.paths.logs)
    state = StateStore.from_config(tmp_cfg, clock=clock)
    result = SystemCollector(Broken(), state, runner=FakeRunner()).collect(ctx_for(tmp_cfg))  # type: ignore[arg-type]
    lines = lines_of(result)
    assert result.ok
    assert "Jobs: unavailable (RuntimeError)." in lines
    assert any(ln.startswith("Kill switch: 1 trip") for ln in lines)
    assert "audit is on fire" not in result.model_dump_json()


# --- real record shapes, encodings and the sensitive-text gate ---------------------------------


def put(logs: Path, name: str, data: bytes) -> None:
    logs.mkdir(parents=True, exist_ok=True)
    (logs / name).write_bytes(data)


def real(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def watch_line(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> tuple[str, CollectResult]:
    result = make(tmp_cfg, audit, clock).collect(ctx_for(tmp_cfg))
    return next(ln for ln in lines_of(result) if ln.startswith("Kill switch")), result


ENCODINGS = {
    "utf8": lambda t: t.encode("utf-8"),
    "utf8_bom": lambda t: b"\xef\xbb\xbf" + t.encode("utf-8"),
    "utf8_crlf": lambda t: t.replace("\n", "\r\n").encode("utf-8"),
    "utf16_le_no_bom": lambda t: t.encode("utf-16-le"),
}


@pytest.mark.parametrize("enc", ENCODINGS)
def test_real_record_shapes_in_every_encoding(tmp_cfg: Config, audit: AuditLog, clock: FakeClock, enc: str) -> None:
    put(tmp_cfg.paths.logs, "killswitch.jsonl", ENCODINGS[enc](real("killswitch_real.jsonl")))
    put(tmp_cfg.paths.logs, "watchdog.jsonl", ENCODINGS[enc](real("watchdog_real.jsonl")))
    line, result = watch_line(tmp_cfg, audit, clock)
    assert line == WATCH_LINE
    f = result.facts
    assert f["logs_available"] is True
    assert (f["killswitch_trips"], f["killswitch_dry_runs"], f["killswitch_last_reason"]) == (2, 2, "manual-stop")
    assert (f["watchdog_crashloops"], f["restart_attempts"], f["watchdog_trip_requests"]) == (1, 2, 2)
    assert (f["restarts_skipped"], f["gpu_yield_noise"], f["log_malformed_lines"]) == (1, 1, 0)


def test_dry_runs_never_count_as_trips(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    jl(tmp_cfg.paths.logs / "killswitch.jsonl",
       {"ts": "2026-10-06T01:00:00Z", "event": "killswitch", "dry_run": True, "reason": "sim"},
       {"ts": "2026-10-06T01:01:00Z", "event": "killswitch", "dry_run": True, "reason": "sim"})
    line, result = watch_line(tmp_cfg, audit, clock)
    assert line.startswith("Kill switch: 0 trips, 2 dry runs.")
    assert result.facts["killswitch_last_reason"] is None


def test_a_record_without_dry_run_is_a_real_trip(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    jl(tmp_cfg.paths.logs / "killswitch.jsonl", {"ts": "2026-10-06T01:00:00Z", "event": "killswitch"})
    line, _ = watch_line(tmp_cfg, audit, clock)
    assert line.startswith("Kill switch: 1 trip, 0 dry runs.")


def test_last_reason_is_the_latest_real_trip_and_is_clipped(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    jl(tmp_cfg.paths.logs / "killswitch.jsonl",
       {"ts": "2026-10-06T02:00:00Z", "event": "killswitch", "dry_run": False, "reason": "later"},
       {"ts": "2026-10-06T01:00:00Z", "event": "killswitch", "dry_run": False, "reason": "earlier"},
       {"ts": "2026-10-06T03:00:00Z", "event": "killswitch", "dry_run": True, "reason": "dry-is-ignored"})
    _, result = watch_line(tmp_cfg, audit, clock)
    assert result.facts["killswitch_last_reason"] == "later"
    jl(tmp_cfg.paths.logs / "killswitch.jsonl",
       {"ts": "2026-10-06T03:30:00Z", "event": "killswitch", "dry_run": False, "reason": "x" * 500})
    _, result = watch_line(tmp_cfg, audit, clock)
    assert result.facts["killswitch_last_reason"] == "x" * 80 + "..."


def test_malformed_lines_are_skipped_and_counted(tmp_cfg: Config, audit: AuditLog, clock: FakeClock) -> None:
    jl(tmp_cfg.paths.logs / "killswitch.jsonl",
       "{truncated",
       "[1, 2, 3]",
       '{"event": "killswitch", "dry_run": false}',  # no ts
       '{"ts": "yesterday", "event": "killswitch", "dry_run": false}',  # bad ts
       "",
       {"ts": "2026-10-06T01:00:00Z", "event": "killswitch", "dry_run": False, "reason": "ok",
        "unknown": [1, {"a": 2}]})
    line, result = watch_line(tmp_cfg, audit, clock)
    assert line.startswith("Kill switch: 1 trip (last: ok), 0 dry runs.")
    assert line.endswith("Skipped 4 unreadable log lines.")
    assert result.facts["log_malformed_lines"] == 4


def test_a_log_withheld_by_the_text_gate_is_unavailable_with_the_reason(
    tmp_cfg: Config, audit: AuditLog, clock: FakeClock
) -> None:
    """The first real run: one watchdog line matched a sensitive term, so safe_read_text withheld the
    whole file and the digest said only "log could not be read"."""
    jl(tmp_cfg.paths.logs / "watchdog.jsonl",
       {"ts": "2026-10-06T00:10:00+00:00", "event": "health_crashloop"},
       {"ts": "2026-10-06T00:20:00+00:00", "event": "probe", "detail": "note #sensitive here"})
    shutil.copy(FIXTURES / "killswitch_real.jsonl", tmp_cfg.paths.logs / "killswitch.jsonl")
    line, result = watch_line(tmp_cfg, audit, clock)
    assert line == "Kill switch and watchdog logs: unavailable (watchdog.jsonl was withheld by the sensitive text gate)."
    assert result.facts["logs_available"] is False
    assert result.facts["logs_unavailable"] == {"watchdog.jsonl": "tag_inline"}
    assert "#sensitive" not in result.model_dump_json()


@pytest.mark.parametrize("bom", [b"\xff\xfe", b"\xfe\xff"])
def test_utf16_with_a_bom_is_named_as_not_utf8(tmp_cfg: Config, audit: AuditLog, clock: FakeClock, bom: bytes) -> None:
    """safe_read_text decodes UTF-8 only; the BOM bytes make it refuse the file."""
    codec = "utf-16-le" if bom == b"\xff\xfe" else "utf-16-be"
    put(tmp_cfg.paths.logs, "killswitch.jsonl", bom + real("killswitch_real.jsonl").encode(codec))
    line, _ = watch_line(tmp_cfg, audit, clock)
    assert line == "Kill switch and watchdog logs: unavailable (killswitch.jsonl is not UTF-8 text)."


def test_bomless_utf16_is_gated_line_by_line_and_the_reason_is_dropped(
    tmp_cfg: Config, audit: AuditLog, clock: FakeClock
) -> None:
    text = real("killswitch_real.jsonl") + (
        '{"ts": "2026-10-06T01:00:00Z", "event": "killswitch", "dry_run": false, "reason": "about #sensitive",'
        ' "x": "#sensitive"}\n'
    )
    put(tmp_cfg.paths.logs, "killswitch.jsonl", text.encode("utf-16-le"))
    line, result = watch_line(tmp_cfg, audit, clock)
    assert line.startswith("Kill switch: 3 trips, 2 dry runs.")  # the newest trip lost its reason to the gate
    assert result.facts["log_withheld_lines"] == 1
    assert "#sensitive" not in result.model_dump_json()


def test_undecodable_bytes_degrade_to_unavailable_and_name_the_log(
    tmp_cfg: Config, audit: AuditLog, clock: FakeClock
) -> None:
    put(tmp_cfg.paths.logs, "watchdog.jsonl", b"\xc3\x28\xc3\x28\xc3\x28")
    result = make(tmp_cfg, audit, clock).collect(ctx_for(tmp_cfg))
    assert result.ok and result.facts["logs_available"] is False
    assert "Kill switch and watchdog logs: unavailable (watchdog.jsonl is not UTF-8 text)." in lines_of(result)


def test_the_resident_daemon_skipping_its_own_trigger_is_healthy(
        tmp_cfg: Config, audit: AuditLog, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    # The daemon runs under IgnoreNew: its 06:00 trigger meets the running instance and Windows records
    # 0x800710E0. For the resident task that is a skip, not a refusal; any other task still reads as refused.
    import jarvisd.collectors.system as system

    assert "JarvisDaemon" in system.RESIDENT_TASKS
    monkeypatch.setattr(system, "RESIDENT_TASKS", frozenset({"ExampleWeekly"}))
    runner = FakeRunner(weekly=(0, json.dumps({"LastRunTime": "2026-10-06T06:00:00+01:00", "LastTaskResult": 2147946720})))
    result = make(tmp_cfg, audit, clock, runner).collect(ctx_for(tmp_cfg))
    weekly = result.facts["scheduled_tasks"]["ExampleWeekly"]
    assert weekly["ok"] is True and weekly["last_result_text"] == system.RESIDENT_SKIP_TEXT
    assert "ExampleWeekly: last run 2026-10-06 06:00, skipped, the daemon was already running (0x800710E0)." in lines_of(result)
