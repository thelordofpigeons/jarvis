"""ntfy adapter (plan P2): a real HTTP round trip against a local http.server fake.

Nothing leaves the machine: the fake listens on 127.0.0.1 with an OS-chosen port. The token,
topic and address in these tests are synthetic.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from jarvisd.audit import AuditLog
from jarvisd.config import Config, ConfigError
from jarvisd.notify import (
    FallbackNotifier,
    MultiNotifier,
    NotifyResult,
    NtfyNotifier,
    ToastNotifier,
    build_notifier,
)

REL = "raw/jarvis/digest-2026-10-06.md"
MESSAGE = "Digest ready: 3 to look at, 2 held. brain/" + REL
EM, EN = chr(0x2014), chr(0x2013)


class FakeNtfy:
    """A one-thread-per-request ntfy stand-in that records what it was sent."""

    def __init__(self, status: int = 200, delay: float = 0.0, location: str | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": b"", "method": "GET"})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ntfy():  # type: ignore[no-untyped-def]
    servers: list[FakeNtfy] = []

    def make(**kw: Any) -> FakeNtfy:
        server = FakeNtfy(**kw)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


def notifier(url: str, **kw: Any) -> NtfyNotifier:
    kw.setdefault("click_template", "obsidian://open?vault={vault}&file={path}")
    kw.setdefault("vault", "brain")
    return NtfyNotifier(url, "jarvis-digest", **kw)


class Recorder:
    """An audit sink that keeps (event, fields)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        self.events.append((event, fields))
        return {}


class Runner:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> int:
        self.calls.append(list(argv))
        return self.code


def toast(tmp_path: Path, runner: Runner) -> ToastNotifier:
    ps1 = tmp_path / "notify-jarvis.ps1"
    ps1.write_text("param($Title,$Message)\n", encoding="utf-8")
    return ToastNotifier(ps1, runner=runner)


# --- the request ----------------------------------------------------------------------------


def test_posts_title_message_priority_and_click_to_the_topic(ntfy) -> None:  # type: ignore[no-untyped-def]
    server = ntfy()
    result = notifier(server.url, priority=4).send(MESSAGE)

    assert result == NotifyResult(ok=True, detail="sent")
    (req,) = server.requests
    assert req["path"] == "/jarvis-digest"
    assert req["body"] == MESSAGE.encode("utf-8")
    h = {k.lower(): v for k, v in req["headers"].items()}
    assert h["title"] == "JARVIS" and h["priority"] == "4"
    assert h["click"] == "obsidian://open?vault=brain&file=raw%2Fjarvis%2Fdigest-2026-10-06"
    assert h["content-type"].startswith("text/plain") and "utf-8" in h["content-type"].lower()
    assert "authorization" not in h


def test_a_trailing_slash_on_the_base_url_is_harmless(ntfy) -> None:  # type: ignore[no-untyped-def]
    server = ntfy()
    assert notifier(server.url + "/").send(MESSAGE).ok
    assert server.requests[0]["path"] == "/jarvis-digest"


def test_token_comes_from_the_named_environment_variable(ntfy) -> None:  # type: ignore[no-untyped-def]
    server = ntfy()
    n = notifier(server.url, token_env="JARVIS_NTFY_TOKEN", environ={"JARVIS_NTFY_TOKEN": "tk_synthetic0123"})
    result = n.send(MESSAGE)
    h = {k.lower(): v for k, v in server.requests[0]["headers"].items()}
    assert h["authorization"] == "Bearer tk_synthetic0123"
    assert "tk_synthetic" not in repr(result)


@pytest.mark.parametrize("environ", [{}, {"JARVIS_NTFY_TOKEN": ""}, {"JARVIS_NTFY_TOKEN": "  "}])
def test_an_absent_or_empty_token_sends_without_authorization(ntfy, environ: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    server = ntfy()
    assert notifier(server.url, token_env="JARVIS_NTFY_TOKEN", environ=environ).send(MESSAGE).ok
    assert "authorization" not in {k.lower() for k in server.requests[0]["headers"]}


def test_no_click_header_without_a_note_path_or_template(ntfy) -> None:  # type: ignore[no-untyped-def]
    server = ntfy()
    notifier(server.url).send("Claude login expired. Run claude /login, then jarvis run-digest --claude --force.")
    notifier(server.url, click_template="").send(MESSAGE)
    for req in server.requests:
        assert "click" not in {k.lower() for k in req["headers"]}


def test_a_hostile_path_in_the_message_cannot_forge_the_click_url(ntfy) -> None:  # type: ignore[no-untyped-def]
    server = ntfy()
    notifier(server.url).send("x brain/raw/../../evil&file=x.md\r\nX-Injected: 1")
    h = {k.lower(): v for k, v in server.requests[0]["headers"].items()}
    assert "x-injected" not in h
    click = h.get("click", "")
    assert "\n" not in click and "&file=x" not in click.split("file=", 1)[-1]


def test_message_is_cleaned_like_the_toast(ntfy) -> None:  # type: ignore[no-untyped-def]
    server = ntfy()
    notifier(server.url).send(f"a {EM} b {EN} c\x00\n" + "x" * 400)
    body = server.requests[0]["body"].decode("utf-8")
    assert EM not in body and EN not in body and "\x00" not in body and "\n" not in body
    assert len(body) <= 180


def test_proxy_environment_is_ignored(ntfy, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    # The server is on a private network; a corporate proxy variable must not capture the push.
    server = ntfy()
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    assert notifier(server.url).send(MESSAGE).ok
    assert len(server.requests) == 1


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_http_errors_are_a_code_not_a_body(ntfy, status: int) -> None:  # type: ignore[no-untyped-def]
    server = ntfy(status=status)
    result = notifier(server.url).send(MESSAGE)
    assert result == NotifyResult(ok=False, detail=f"http_{status}")


def test_redirects_are_not_followed_so_a_token_cannot_travel(ntfy) -> None:  # type: ignore[no-untyped-def]
    elsewhere = ntfy()
    server = ntfy(status=302, location=elsewhere.url + "/stolen")
    n = notifier(server.url, token_env="T", environ={"T": "tk_synthetic"})
    result = n.send(MESSAGE)
    assert result == NotifyResult(ok=False, detail="http_302")
    assert elsewhere.requests == []


def test_connection_refused_is_network() -> None:
    import socket

    with socket.socket() as probe:  # a port that was free a moment ago and has no listener
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    result = notifier(f"http://127.0.0.1:{port}", timeout=3).send(MESSAGE)
    assert result.ok is False and result.detail in {"network", "timeout"}


def test_slow_server_is_a_timeout(ntfy) -> None:  # type: ignore[no-untyped-def]
    server = ntfy(delay=1.5)
    started = time.monotonic()
    result = notifier(server.url, timeout=0.3).send(MESSAGE)
    assert result == NotifyResult(ok=False, detail="timeout")
    assert time.monotonic() - started < 1.4


def test_unexpected_exception_never_escapes(ntfy) -> None:  # type: ignore[no-untyped-def]
    class Boom:
        def open(self, *a: object, **k: object) -> None:
            raise ValueError("synthetic")

    result = notifier(ntfy().url, opener=Boom()).send(MESSAGE)
    assert result == NotifyResult(ok=False, detail="error")


@pytest.mark.parametrize("url", ["", "ftp://example.invalid", "file:///c:/x", "http://user:pw@example.invalid"])
def test_constructor_refuses_a_bad_address(url: str) -> None:
    with pytest.raises(ConfigError):
        NtfyNotifier(url, "jarvis-digest")


@pytest.mark.parametrize("url", [
    "http://192.168.1.20", "http://ntfy.example.invalid:2586", "http://10.0.0.5:2586/base", "http://localhost.example.invalid",
])
def test_plain_http_to_a_non_loopback_host_is_refused(url: str) -> None:
    # A bearer token and the digest line must not cross a network unencrypted.
    with pytest.raises(ConfigError, match="https"):
        NtfyNotifier(url, "jarvis-digest", token_env="X")


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:2586", "http://localhost:2586", "http://[::1]:2586", "http://127.5.6.7",
    "https://ntfy.example.invalid", "https://ntfy.example.invalid:8443/base",
])
def test_https_or_loopback_http_is_accepted(url: str) -> None:
    assert NtfyNotifier(url, "jarvis-digest").endpoint.endswith("/jarvis-digest")


@pytest.mark.parametrize("topic", ["", "has space", "../x", "a/b"])
def test_constructor_refuses_a_bad_topic(topic: str) -> None:
    with pytest.raises(ConfigError):
        NtfyNotifier("http://127.0.0.1:2586", topic)


def test_constructor_refuses_a_template_that_cannot_format() -> None:
    with pytest.raises(ConfigError):
        NtfyNotifier("http://127.0.0.1:2586", "t", click_template="obsidian://{nope}")


# --- fallback and multi -------------------------------------------------------------------------


def test_failure_is_audited_and_falls_back_to_toast(ntfy, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    server = ntfy(status=500)
    runner, audit = Runner(), Recorder()
    both = FallbackNotifier(notifier(server.url), toast(tmp_path, runner), audit=audit)

    result = both.send(MESSAGE)

    assert both.name == "ntfy"
    assert result.ok is True, "the toast delivered it"
    assert result.detail == "ntfy_http_500_toast_sent"
    assert len(runner.calls) == 1
    (event, fields), = audit.events
    assert event == "notify_ntfy_failed"
    assert fields == {"mode": "fallback", "detail": "http_500", "toast": "sent"}


def test_success_does_not_touch_the_toast(ntfy, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    server = ntfy()
    runner, audit = Runner(), Recorder()
    result = FallbackNotifier(notifier(server.url), toast(tmp_path, runner), audit=audit).send(MESSAGE)
    assert result == NotifyResult(ok=True, detail="sent")
    assert runner.calls == [] and audit.events == []


def test_both_failing_is_not_ok(ntfy, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    server = ntfy(status=503)
    result = FallbackNotifier(notifier(server.url), toast(tmp_path, Runner(code=1))).send(MESSAGE)
    assert result.ok is False and result.detail == "ntfy_http_503_toast_exit_1"


def test_multi_sends_both_and_is_ok_if_either_is(ntfy, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    server = ntfy()
    runner = Runner()
    multi = MultiNotifier([notifier(server.url), toast(tmp_path, runner)])
    result = multi.send(MESSAGE)
    assert multi.name == "multi"
    assert result == NotifyResult(ok=True, detail="ntfy_sent_toast_sent")
    assert len(server.requests) == 1 and len(runner.calls) == 1


def test_multi_audits_a_failed_ntfy_and_still_toasts(ntfy, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    server = ntfy(status=500)
    runner, audit = Runner(), Recorder()
    result = MultiNotifier([notifier(server.url), toast(tmp_path, runner)], audit=audit).send(MESSAGE)
    assert result.ok is True and result.detail == "ntfy_http_500_toast_sent"
    assert audit.events == [("notify_ntfy_failed", {"mode": "multi", "detail": "http_500", "toast": "sent"})]


def test_one_member_raising_does_not_stop_the_other(tmp_path: Path) -> None:
    class Raises:
        name = "ntfy"

        def send(self, message: str) -> NotifyResult:
            raise RuntimeError("synthetic")

    runner = Runner()
    result = MultiNotifier([Raises(), toast(tmp_path, runner)]).send(MESSAGE)  # type: ignore[list-item]
    assert result.ok is True and len(runner.calls) == 1


def test_the_audit_record_carries_no_address_topic_token_or_message(ntfy, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    server = ntfy(status=500)
    log = AuditLog(tmp_path / "audit.jsonl", mirror_stdout=False)
    n = notifier(server.url, token_env="T", environ={"T": "tk_synthetic0123"})
    FallbackNotifier(n, toast(tmp_path, Runner()), audit=log).send(MESSAGE)
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    mine = [r for r in records if r["event"] == "notify_ntfy_failed"]
    assert len(mine) == 1 and mine[0]["detail"] == "http_500"
    for secret in ("tk_synthetic0123", "jarvis-digest", "127.0.0.1", "to look at"):
        assert secret not in raw


# --- build_notifier -----------------------------------------------------------------------------


def _cfg(tmp_cfg: Config, **notify: Any) -> Config:
    cfg = tmp_cfg.model_copy(deep=True)
    for key, value in notify.items():
        setattr(cfg.notify, key, value)
    return cfg


def test_build_ntfy_wraps_it_in_the_toast_fallback(tmp_cfg: Config) -> None:
    cfg = _cfg(tmp_cfg, adapter="ntfy", ntfy_url="http://127.0.0.1:2586", ntfy_topic="jarvis-digest",
               ntfy_token_env="JARVIS_NTFY_TOKEN", ntfy_priority=2)
    audit = Recorder()
    built = build_notifier(cfg, audit=audit)
    assert isinstance(built, FallbackNotifier) and built.name == "ntfy"
    assert isinstance(built.primary, NtfyNotifier) and isinstance(built.fallback, ToastNotifier)
    assert built.primary.priority == 2 and built.primary.vault == cfg.paths.brain_root.name
    assert built.audit is audit


def test_build_multi(tmp_cfg: Config) -> None:
    cfg = _cfg(tmp_cfg, adapter="multi", ntfy_url="http://127.0.0.1:2586", ntfy_topic="jarvis-digest")
    built = build_notifier(cfg)
    assert isinstance(built, MultiNotifier) and built.name == "multi"
    assert [type(m) for m in built.members] == [NtfyNotifier, ToastNotifier]


@pytest.mark.parametrize("adapter", ["ntfy", "multi"])
def test_build_refuses_ntfy_without_address_or_topic(tmp_cfg: Config, adapter: str) -> None:
    with pytest.raises(ConfigError):
        build_notifier(_cfg(tmp_cfg, adapter=adapter))
    with pytest.raises(ConfigError):
        build_notifier(_cfg(tmp_cfg, adapter=adapter, ntfy_url="http://127.0.0.1:2586"))


def test_unknown_adapter_message_lists_the_new_names(tmp_cfg: Config) -> None:
    with pytest.raises(ConfigError) as err:
        build_notifier(_cfg(tmp_cfg, adapter="carrier-pigeon"))
    assert "ntfy" in str(err.value) and "multi" in str(err.value)


def test_toast_and_null_are_unchanged(tmp_cfg: Config) -> None:
    assert isinstance(build_notifier(_cfg(tmp_cfg, adapter="toast")), ToastNotifier)
    assert build_notifier(_cfg(tmp_cfg, adapter="null")).name == "null"


def test_daemon_composition_hands_its_audit_log_to_the_notifier(tmp_cfg: Config) -> None:
    from jarvisd import daemon as daemon_mod

    cfg = _cfg(tmp_cfg, adapter="ntfy", ntfy_url="http://127.0.0.1:2586", ntfy_topic="jarvis-digest")
    deps = daemon_mod.build_deps(cfg, mirror_stdout=False)
    assert isinstance(deps.notifier, FallbackNotifier) and deps.notifier.audit is deps.audit


# --- the doc ------------------------------------------------------------------------------------


def test_the_ntfy_doc_has_six_steps_and_names_every_config_key() -> None:
    from jarvisd import ROOT
    from jarvisd.config import NotifyCfg

    text = (ROOT / "docs" / "notify-ntfy.md").read_text(encoding="utf-8")
    steps = [ln for ln in text.splitlines() if ln.startswith("### ")]
    assert [s.split(".", 1)[0] for s in steps] == [f"### {n}" for n in range(1, 7)]
    for key in NotifyCfg.model_fields:
        if key.startswith("ntfy_") and key != "ntfy_timeout_s":
            assert key in text, f"docs/notify-ntfy.md does not mention {key}"
    assert "notify_ntfy_failed" in text and "Funnel" in text
    assert EM not in text and EN not in text and "\r" not in text
