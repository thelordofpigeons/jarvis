"""The work hub: a read-only cockpit over state, queue, audit and the digest notes (P4).

Every test drives the real app through FastAPI's TestClient against a throwaway tree that is
filled by the daemon's own writers, so the hub is checked against the formats it will meet.
The tree is synthetic: no brain content, no real repository names (design D10).
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvisd import ROOT, cli, daemon  # noqa: E402
from jarvisd.audit import AuditLog  # noqa: E402
from jarvisd.config import Config, HubCfg  # noqa: E402
from jarvisd.hub import app as hub_app  # noqa: E402
from jarvisd.hub import check as hub_check  # noqa: E402
from jarvisd.hub.mdhtml import render_markdown  # noqa: E402
from jarvisd.jobstore import JobStore  # noqa: E402
from jarvisd.models import Job, RunManifest, WithheldItem  # noqa: E402
from jarvisd.state import StateStore  # noqa: E402
from jarvisd.vault import VaultWriter  # noqa: E402
from conftest import FakeClock  # noqa: E402

HOST = "http://127.0.0.1:8765"
SECRET_PATH = "C:/secret-place/private-notes.md"
VIEWS = ["/", "/runs", "/held", "/repos", "/audit", "/status"]

DIGEST = """---
type: jarvis-digest
generator: jarvisd
generator_version: 1.1.0
job_id: digest-2026-10-06
date: 2026-10-06
generated_at: 2026-10-06T05:31:00+00:00
status: complete
claude: ok
local_tier: not_installed
degraded: false
cost_usd: 0.0285
items: {collected: 9, cleared: 6, held_sensitive: 2, held_policy: 1, over_cap: 0}
sources: {brain: ok, task: ok, git: ok, system: ok, clickup: disabled, github: not_collected}
audit_seq: 3
audit_head: abc
tags: [jarvis, digest]
---
# JARVIS morning digest, Tuesday 2026-10-06

## Start here
Open threads: **one** synthetic thread and `code`. <script>alert(1)</script>
1. [5c4d04ed] First numbered point.
2. [8d94ef0d] Second numbered point.

## Repos
- alpha-repo (flagged) branch main, 2 commits since window, 1 modified, 3 untracked: fix a thing [58d5ed8d]
- beta-repo: 0 commits since window, 4 modified, 0 untracked [72e2fd1c]
- Quiet: gamma-repo, delta-repo.
- GitHub PRs and CI: not collected in v1.

## Held back and not summarized
- Sensitive, never read or sent: 2 items (ids w-b33f54, w-dc2799).
"""


def _fwd(path: Path) -> str:
    return path.as_posix()


@pytest.fixture
def seeded(tmp_cfg: Config, clock: FakeClock) -> Config:
    """A tree written by the real writers: heartbeat, budget, audit chain, queue, held, note, run."""
    audit = AuditLog(daemon.audit_path(tmp_cfg), clock=clock, mirror_stdout=False)
    audit.emit("daemon_start", claude_cli_version="9.9.9 (Test)")
    audit.emit("gate_decision", item_id="58d5ed8d", route="claude", decided_by="confidence",
               importance="low", confidence=0.0, category="git_repo", reasons=["confidence_below_threshold"])
    audit.emit("claude_call", call_id="c1", ok=True, total_cost_usd=0.03, cost_usd=0.03,
               job_id="digest-2026-10-06")
    state = StateStore.from_config(tmp_cfg, clock=clock)
    state.heartbeat(None, mode="task")
    res = state.budget.reserve("digest", 0.05)
    state.budget.settle(res, 0.03)
    store = JobStore.from_config(tmp_cfg, clock=clock, audit=audit)
    job = Job(id="digest-2026-10-06", kind="morning_digest", key="2026-10-06", job_class="observe_only",
              latency_class="background_batch", created_at="2026-10-06T04:31:00+00:00",
              not_before="2026-10-06T04:31:00+00:00")
    store.enqueue(job)
    claimed = store.claim_next(clock())
    assert claimed is not None
    store.complete(claimed, {"status": "complete", "note_path": "raw/jarvis/digest-2026-10-06.md",
                             "cost_usd": 0.03})
    for ref_id, reason in (("w-b33f54", "term:3"), ("w-dc2799", "term:3")):
        store.hold(WithheldItem(id=ref_id, kind="brain_note", source_ref=SECRET_PATH, reason=reason),
                   "digest-2026-10-06")
    VaultWriter(tmp_cfg, audit).write_raw("digest-2026-10-06.md", DIGEST, "digest-2026-10-06")
    manifest = RunManifest(
        job_id="digest-2026-10-06", status="complete", started_at="2026-10-06T05:30:00+00:00",
        finished_at="2026-10-06T05:31:00+00:00", counts={"collected": 9, "cleared": 6, "held_sensitive": 2,
                                                         "held_policy": 1}, cost_usd=0.0285,
        paths={"note": "raw/jarvis/digest-2026-10-06.md"}, audit_seq=3)
    run_dir = tmp_cfg.daemon.state_dir / "runs" / "digest-2026-10-06"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8", newline="\n")
    return tmp_cfg


def _client(cfg: Config, clock: FakeClock | None = None) -> TestClient:
    return TestClient(hub_app.create_app(cfg, clock=clock), base_url=HOST)


def _snapshot(*roots: Path) -> dict[str, tuple[int, int, str]]:
    out: dict[str, tuple[int, int, str]] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                out[path.as_posix() + "/"] = (0, 0, "")
            else:
                data = path.read_bytes()
                out[path.as_posix()] = (len(data), path.stat().st_mtime_ns, hashlib.sha256(data).hexdigest())
    return out


# --- every view renders --------------------------------------------------------------------------


@pytest.mark.parametrize("path", VIEWS)
def test_every_view_renders(seeded: Config, clock: FakeClock, path: str) -> None:
    resp = _client(seeded, clock).get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    for label in ("Today", "Runs", "Held", "Repos", "Audit", "Status"):
        assert f">{label}</a>" in resp.text
    assert "read-only" in resp.text.lower()


def test_the_hub_never_writes(seeded: Config, clock: FakeClock, tmp_path: Path) -> None:
    before = _snapshot(tmp_path)
    client = _client(seeded, clock)
    for path in [*VIEWS, "/digest/digest-2026-10-06", "/api/status", "/static/hub.css", "/static/hub.js", "/nope"]:
        client.get(path)
    assert _snapshot(tmp_path) == before


def test_a_tree_that_does_not_exist_renders_and_is_not_created(tmp_cfg: Config, tmp_path: Path) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    ghost = tmp_path / "ghost"
    cfg.paths.queue = ghost / "queue"
    cfg.paths.logs = ghost / "logs"
    cfg.daemon.state_dir = ghost / "state"
    cfg.paths.vault_write_raw = ghost / "brain-raw"
    client = _client(cfg)
    for path in [*VIEWS, "/api/status"]:
        assert client.get(path).status_code == 200, path
    assert not ghost.exists()
    assert "No digest" in client.get("/").text


def test_mutating_methods_are_refused(seeded: Config) -> None:
    client = _client(seeded)
    for method in ("post", "put", "delete", "patch"):
        for path in ("/", "/held", "/api/status"):
            assert getattr(client, method)(path).status_code == 405, (method, path)


# --- the loopback boundary -------------------------------------------------------------------------


def test_bind_address_is_not_configurable() -> None:
    assert "host" not in HubCfg.model_fields
    assert HubCfg().port == 8765 and HubCfg().allowed_hosts == []


def test_host_header_must_be_loopback_or_listed(seeded: Config) -> None:
    app = hub_app.create_app(seeded)
    assert TestClient(app, base_url="http://127.0.0.1:8765").get("/status").status_code == 200
    assert TestClient(app, base_url="http://localhost:8765").get("/status").status_code == 200
    assert TestClient(app, base_url="http://evil.example:8765").get("/status").status_code == 403
    cfg = seeded.model_copy(deep=True)
    cfg.hub.allowed_hosts = ["phone.example.test"]
    listed = TestClient(hub_app.create_app(cfg), base_url="https://phone.example.test")
    assert listed.get("/status").status_code == 200


def test_allowed_hosts_are_bare_names() -> None:
    with pytest.raises(ValueError):
        HubCfg(allowed_hosts=["https://x.example.test"])
    with pytest.raises(ValueError):
        HubCfg(port=80)


def test_security_headers_and_no_external_references(seeded: Config, clock: FakeClock) -> None:
    client = _client(seeded, clock)
    resp = client.get("/")
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in resp.headers["cache-control"]
    for path in [*VIEWS, "/static/hub.css", "/static/hub.js"]:
        body = client.get(path).text
        assert not re.search(r"https?://", body), path
        assert not re.search(r'(?:src|href)="(?:https?:)?//', body), path


def test_the_api_docs_that_pull_from_a_cdn_are_off(seeded: Config) -> None:
    client = _client(seeded)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- Today ---------------------------------------------------------------------------------------------


def test_today_renders_the_digest_and_escapes_html(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/").text
    assert "<h2>Start here</h2>" in body
    assert "<strong>one</strong>" in body and "<code>code</code>" in body
    assert "<ol>" in body and "First numbered point" in body
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "generator: jarvisd" not in body  # the front matter is shown as facts, not raw


def test_today_lists_held_items_by_id_and_reason(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/").text
    assert "w-b33f54" in body and "w-dc2799" in body and "term:3" in body


def test_a_digest_page_by_job_id(seeded: Config, clock: FakeClock) -> None:
    client = _client(seeded, clock)
    assert "First numbered point" in client.get("/digest/digest-2026-10-06").text
    assert client.get("/digest/digest-2026-10-09").status_code == 404
    for bad in ("..%2f..%2fetc", "digest-2026-10-06.md", "digest-x", "digest-2026-10-06-r2x"):
        assert client.get(f"/digest/{bad}").status_code == 404, bad


# --- Runs ----------------------------------------------------------------------------------------------


def test_runs_ledger_row(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/runs").text
    assert "2026-10-06" in body and "complete" in body
    assert "0.0285" in body
    assert "6 cleared" in body or ">6<" in body
    assert 'href="/digest/digest-2026-10-06"' in body
    assert "seq 3" in body  # the audit witness
    assert re.search(r"seq 3[^<]*<code>[0-9a-f]{12}</code>", body)


def test_a_run_whose_audit_record_is_gone_says_so(seeded: Config, clock: FakeClock) -> None:
    manifest = seeded.daemon.state_dir / "runs" / "digest-2026-10-06" / "run.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["audit_seq"] = 9999
    manifest.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert "not found in the audit log" in _client(seeded, clock).get("/runs").text


# --- Held ----------------------------------------------------------------------------------------------


def test_held_shows_references_and_instructions_never_content(seeded: Config, clock: FakeClock) -> None:
    client = _client(seeded, clock)
    body = client.get("/held").text
    assert "w-b33f54" in body and "term:3" in body and "brain_note" in body
    assert "jarvis wrong w-b33f54" in body
    assert "--leak" in body and "jarvis held" in body
    for path in [*VIEWS, "/api/status", "/digest/digest-2026-10-06"]:
        text = client.get(path).text
        assert SECRET_PATH not in text and "secret-place" not in text, path


# --- Repos ---------------------------------------------------------------------------------------------


def test_repos_view_reads_the_last_digest(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/repos").text
    assert "alpha-repo" in body and "beta-repo" in body
    assert re.search(r"alpha-repo.*?main.*?2.*?1.*?3", body, re.S)
    assert "gamma-repo" in body  # the Quiet line
    assert "58d5ed8d" in body
    assert "not collected" in body  # the GitHub line, shown rather than dropped
    assert "digest-2026-10-06" in body  # says which digest the facts come from


# --- Audit ---------------------------------------------------------------------------------------------


def test_audit_view_verifies_the_chain_and_shows_budget(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/audit").text
    assert "Chain verified" in body
    assert "claude_call" in body and "daemon_start" in body
    assert "0.03" in body  # spent today
    assert "calls" in body.lower()


def test_audit_view_reports_a_broken_chain(seeded: Config, clock: FakeClock) -> None:
    path = daemon.audit_path(seeded)
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["confidence"] = 0.9  # content changed, hash not
    lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    body = _client(seeded, clock).get("/audit").text
    assert "BROKEN" in body and "seq 2" in body


def test_audit_view_caps_rows(seeded: Config, clock: FakeClock) -> None:
    audit = AuditLog(daemon.audit_path(seeded), clock=clock, mirror_stdout=False)
    for n in range(70):
        audit.emit("housekeeping", n=n)
    client = _client(seeded, clock)
    body = client.get("/audit").text
    assert body.count('class="ev"') == 50
    cfg = seeded.model_copy(deep=True)
    cfg.hub.audit_rows = 5
    assert _client(cfg, clock).get("/audit").text.count('class="ev"') == 5


def test_audit_cache_follows_new_records(seeded: Config, clock: FakeClock) -> None:
    client = _client(seeded, clock)
    assert "zz_marker_event" not in client.get("/audit").text
    AuditLog(daemon.audit_path(seeded), clock=clock, mirror_stdout=False).emit("zz_marker_event")
    assert "zz_marker_event" in client.get("/audit").text


# --- Status --------------------------------------------------------------------------------------------


def test_status_matches_the_cli_where_the_facts_are_the_same(seeded: Config, clock: FakeClock) -> None:
    ctx = cli.Ctx(seeded, clock=clock)
    expected = cli.collect_status(ctx)
    got = _client(seeded, clock).get("/api/status").json()
    assert set(expected) <= set(got)
    for key in ("budget", "queue", "breaker", "held_count", "audit", "kill", "pause", "local_tier",
                "last_digest", "watermark", "next_due", "claude_cli_version"):
        assert got[key] == json.loads(json.dumps(expected[key], default=str)), key
    assert got["running"] is True  # a fresh heartbeat from a live pid (this process)


def test_status_page_prints_the_cli_lines(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/status").text
    assert "JARVIS daemon: running" in body
    assert "Budget today" in body and "Held references: 2" in body


def test_a_stale_heartbeat_reads_as_stopped(seeded: Config, clock: FakeClock) -> None:
    later = FakeClock(clock() + timedelta(hours=2))
    data = _client(seeded, later).get("/api/status").json()
    assert data["running"] is False


def test_a_kill_file_shows_on_every_page(seeded: Config, clock: FakeClock) -> None:
    (seeded.daemon.state_dir / "KILL").write_text("", encoding="utf-8")
    assert "KILL" in _client(seeded, clock).get("/").text


# --- assets, refresh -------------------------------------------------------------------------------------------


def test_static_assets(seeded: Config) -> None:
    client = _client(seeded)
    css = client.get("/static/hub.css")
    js = client.get("/static/hub.js")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert 100 <= len(css.text.splitlines()) <= 400  # a few hundred lines, no framework
    assert "prefers-color-scheme" in css.text and "@media" in css.text
    assert "fetch(" in js.text
    assert client.get("/static/other.js").status_code == 404


def test_refresh_script_follows_config(seeded: Config) -> None:
    assert 'data-refresh="30"' in _client(seeded).get("/status").text
    cfg = seeded.model_copy(deep=True)
    cfg.hub.refresh_s = 0
    off = _client(cfg).get("/status").text
    assert "hub.js" not in off


# --- markdown -------------------------------------------------------------------------------------------


def test_markdown_subset() -> None:
    html = render_markdown("# T\n\n## H\ntext **b** `c` <i>\n- a\n- b\n\n1. x\n2. y\n\npara one\nstill one\n")
    assert "<h1>T</h1>" in html and "<h2>H</h2>" in html
    assert "<strong>b</strong>" in html and "<code>c</code>" in html and "&lt;i&gt;" in html
    assert "<ul><li>a</li><li>b</li></ul>" in html
    assert "<ol><li>x</li><li>y</li></ol>" in html
    assert "<p>para one still one</p>" in html


def test_markdown_never_emits_a_link_or_raw_html() -> None:
    html = render_markdown("[x](javascript:alert(1)) <a href=\"http://e\">e</a> ![i](http://e/i.png)")
    assert "<a " not in html and "<img" not in html and "<a href" not in html
    assert "&lt;a href" in html


# --- the hub never reaches Claude -----------------------------------------------------------------------


def test_hub_modules_import_neither_claude_nor_subprocess() -> None:
    forbidden = {"jarvisd.claude", "subprocess", "socket", "urllib", "urllib.request", "http.client", "requests"}
    offenders: dict[str, list[str]] = {}
    for path in sorted((ROOT / "jarvisd" / "hub").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found += [a.name for a in node.names if a.name in forbidden]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in forbidden or any(f"{node.module}.{a.name}" in forbidden for a in node.names):
                    found.append(node.module)
        if found:
            offenders[path.name] = found
    assert not offenders, offenders


def test_hub_modules_use_no_job_or_state_mutators() -> None:
    """A name-level guard on top of tests/test_write_locations.py: nothing here calls a writer."""
    banned = {"enqueue", "claim_next", "complete", "fail", "retry", "hold", "expire_held", "prune", "emit",
              "reserve", "settle", "release", "heartbeat", "set_pause", "clear_pause", "reset", "trip",
              "advance", "acquire_daemon_lock", "write_raw", "write_session"}
    offenders: dict[str, set[str]] = {}
    for path in sorted((ROOT / "jarvisd" / "hub").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in banned}
        if hits:
            offenders[path.name] = hits
    assert not offenders, offenders


# --- the command ----------------------------------------------------------------------------------------


def test_hub_check_passes_on_a_seeded_tree(seeded: Config, clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["hub", "--check"], cfg=seeded, clock=clock) == 0
    out = capsys.readouterr().out
    for name in ("Today", "Runs", "Held", "Repos", "Audit", "Status"):
        assert f"PASS  {name}" in out
    assert "FAIL" not in out


def test_hub_check_passes_on_an_empty_tree(tmp_cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["hub", "--check"], cfg=tmp_cfg) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_hub_check_fails_loudly_when_a_view_breaks(seeded: Config, clock: FakeClock, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    def boom(self: Any, *a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("jarvisd.hub.data.HubData.runs", boom)
    assert cli.main(["hub", "--check"], cfg=seeded, clock=clock) == 1
    assert "FAIL  Runs" in capsys.readouterr().out


def test_hub_serves_on_loopback_only(seeded: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(app: Any, **kw: Any) -> None:
        seen.update(kw)

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert cli.main(["hub"], cfg=seeded) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
    seen.clear()
    assert cli.main(["hub", "--port", "9123"], cfg=seeded) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 9123


def test_hub_port_must_be_unprivileged(seeded: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["hub", "--port", "80"], cfg=seeded) == cli.EXIT_USAGE
    assert cli.main(["hub", "--port", "x"], cfg=seeded) == cli.EXIT_USAGE


def test_hub_reports_a_port_that_is_taken(seeded: Config, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    def refuse(app: Any, **kw: Any) -> None:
        raise SystemExit(1)

    monkeypatch.setattr("uvicorn.run", refuse)
    assert cli.main(["hub"], cfg=seeded) == cli.EXIT_FAIL
    assert "8765" in capsys.readouterr().err


# --- packaging ---------------------------------------------------------------------------------------------


def test_dependencies_are_pinned_where_the_rules_say() -> None:
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    setup = (ROOT / "deploy" / "setup-venv.ps1").read_text(encoding="utf-8")
    for name in ("fastapi", "uvicorn", "httpx2"):
        assert re.search(rf"(?mi)^{name}==\d", lock), name
        assert name in setup, name


def test_hub_table_is_in_the_tracked_config() -> None:
    import tomllib

    raw = tomllib.loads((ROOT / "jarvis.toml").read_text(encoding="utf-8"))
    assert raw["hub"]["port"] == 8765
    assert "host" not in raw["hub"]


def test_hub_doc_states_the_limits() -> None:
    text = (ROOT / "docs" / "hub.md").read_text(encoding="utf-8")
    for needle in ("tailscale serve", "127.0.0.1", "never writes", "jarvis hub --check", "httpx2", "Not built"):
        assert needle in text, needle


def test_gitignore_does_not_hide_the_hub_package() -> None:
    """`hub/` once ignored every folder of that name, including jarvisd/hub; only the root one is runtime data."""
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    kept = subprocess.run([git, "check-ignore", "-q", "jarvisd/hub/app.py"], cwd=ROOT, capture_output=True)
    assert kept.returncode == 1, "jarvisd/hub/app.py is ignored by .gitignore"
    runtime = subprocess.run([git, "check-ignore", "-q", "hub/hub.db"], cwd=ROOT, capture_output=True)
    assert runtime.returncode == 0, "the root hub/ runtime folder should stay ignored"
