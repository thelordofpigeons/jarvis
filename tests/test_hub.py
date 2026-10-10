"""The work hub: a read-only cockpit over state, queue, audit and the digest notes (P4, reworked in phase 2).

Every test drives the real app through FastAPI's TestClient against a throwaway tree that is
filled by the daemon's own writers, so the hub is checked against the formats it will meet.
The tree is synthetic: no brain content, no real repository names (design D10). The fixture note
is written in grammar 2 (docs/hub-rework-contract.md); the grammar 1 fallback is covered in
tests/test_hub_digestparse.py.
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
from jarvisd.hub import data as hub_data  # noqa: E402
from jarvisd.hub import views  # noqa: E402
from jarvisd.hub.mdhtml import render_markdown  # noqa: E402
from jarvisd.jobstore import JobStore  # noqa: E402
from jarvisd.models import Job, RunManifest, WithheldItem  # noqa: E402
from jarvisd.state import StateStore  # noqa: E402
from jarvisd.vault import VaultWriter  # noqa: E402
from conftest import FakeClock  # noqa: E402

HOST = "http://127.0.0.1:8765"
SECRET_PATH = "C:/secret-place/private-notes.md"
VIEWS = ["/", "/projects", "/activity"]
NAV_LABELS = ("Today", "Inbox", "Projects", "Activity")

DIGEST = """---
type: jarvis-digest
generator: jarvisd
generator_version: 1.3.0
grammar: 2
job_id: digest-2026-10-06
date: 2026-10-06
generated_at: 2026-10-06T05:31:00+00:00
status: complete
claude: ok
local_tier: not_installed
degraded: false
cost_usd: 0.0285
items: {collected: 9, cleared: 6, held_sensitive: 2, held_policy: 1, over_cap: 0}
n_collected: 9
n_cleared: 6
n_held: 3
n_start_here: 2
n_attention: 1
n_still_open: 3
n_still_open_hidden: 0
n_decided: 1
n_repos_active: 2
n_repos_quiet: 2
n_system_anomalies: 1
n_since_new: 1
n_since_resolved: 1
n_since_dropped: 1
n_since_returned: 1
sources: {brain: ok, task: ok, git: ok, system: ok, clickup: disabled, github: ok}
audit_seq: 3
audit_head: abc
tags: [jarvis, digest]
---
# JARVIS morning digest, Tuesday 2026-10-06

## Start here
One task is overdue and one repo moved overnight. <script>alert(1)</script>
1. Rotate the pasted sandbox key before the demo tomorrow [5c4d04ed]
2. Answer the reviewer on the parser fix, blocked since Monday [8d94ef0d]

## Attention
- CI failing: alpha-repo on main, 2 runs in a row [58d5ed8d]

## Active task
- 123synth fix(parser): synthetic task name, status IN REVIEW, due 2026-10-05 (OVERDUE). [c0ffee01]

## Still open
- parser rework: Confirm the retry budget with the reviewer (4d) [6b6b6b6b]
- notes: Synthetic thread **one** waiting on a reviewer with `code` [e5f6a7b8]
- notes: Second synthetic thread [0a1b2c3d]

## Decided yesterday
- Keep the strict sum of 100 for the scoring quotas [9f8e7d6c]

## Repos
- alpha-repo (work) branch main, 2 commits since window, 1 modified, 3 untracked: fix a thing [58d5ed8d]
- Uncommitted only: beta-repo 4/0.
- Quiet: 2 repos.
- GitHub alpha-repo (work): no open PRs, CI failure on main [58d5ed8d]
- GitHub quiet: 1 repo, CI green or none.
- GitHub not read: 2 repos (no_access 1, no_remote 1).

## System
- Task: ExampleNightly last ran 2026-10-06 02:30, refused by the operator or administrator (0x800710E0).

## Held back and not summarized
- Held: 2 sensitive (ids w-b33f54, w-dc2799; reasons: term:3 x2), 1 policy, 0 over cap. Claude: ok. Run `jarvis held` in a terminal.

## Source status
- brain ok (5 items), task ok, git ok (4 repos), system ok, clickup disabled, github ok.

## Flag a mistake
- `jarvis wrong <id> --should {escalate,hold,skip,other} --note "..."`; `--leak` if something sensitive was shown or sent.
"""

# The day before, so Today has something to diff against.
YESTERDAY = """---
type: jarvis-digest
generator: jarvisd
grammar: 2
job_id: digest-2026-10-05
date: 2026-10-05
status: complete
items: {collected: 7, cleared: 5, held_sensitive: 1, held_policy: 0, over_cap: 0}
n_held: 1
---
# JARVIS morning digest, Monday 2026-10-05

## Start here
Quiet night.

## Attention
- Nothing broken.

## Repos
- Uncommitted only: beta-repo 2/0.
- Quiet: 3 repos.
- GitHub quiet: 2 repos, CI green or none.

## System
- All green: 1 job done, 0 failed, $0.03 Claude, breaker closed, disk ok, tasks ok.
"""


# The item sidecar the writer leaves beside the run manifest (contract section 6): one record per rendered item
# line, keys normalised, no held id. The hub reads it for the stable keys and the Done and Snooze buttons.
def _sidecar_record(key: str, item_id: str, section: str, text: str, rank: int, group: str = "notes",
                    since: str = "") -> dict[str, Any]:
    return {"key": key, "id": item_id, "section": section, "group": group, "date": "2026-10-06", "text": text,
            "rank": rank, "since": since}


SIDECAR = {"job_id": "digest-2026-10-06", "date": "2026-10-06", "items": [
    _sidecar_record("rotate the pasted sandbox key before the demo tomorrow", "5c4d04ed", "start_here",
                    "Rotate the pasted sandbox key before the demo tomorrow", 1, since="new"),
    _sidecar_record("answer the reviewer on the parser fix blocked since monday", "8d94ef0d", "start_here",
                    "Answer the reviewer on the parser fix, blocked since Monday", 2),
    _sidecar_record("alpha repo on main 2 runs in a row", "58d5ed8d", "attention", "alpha-repo on main, 2 runs in a row", 1),
    _sidecar_record("123synth fix parser synthetic task name", "c0ffee01", "active_task",
                    "123synth fix(parser): synthetic task name, status IN REVIEW, due 2026-10-05 (OVERDUE).", 1),
    _sidecar_record("confirm the retry budget with the reviewer", "6b6b6b6b", "still_open",
                    "Confirm the retry budget with the reviewer", 1, group="parser rework"),
    _sidecar_record("synthetic thread one waiting on a reviewer with code", "e5f6a7b8", "still_open",
                    "Synthetic thread **one** waiting on a reviewer with `code`", 2, since="returned"),
    _sidecar_record("second synthetic thread", "0a1b2c3d", "still_open", "Second synthetic thread", 3),
    _sidecar_record("keep the strict sum of 100 for the scoring quotas", "9f8e7d6c", "decided",
                    "Keep the strict sum of 100 for the scoring quotas", 1),
]}

# The writer's history (contract section 6): the texts the "since yesterday" lines show.
HISTORY = {"updated": "2026-10-06", "last_run": "digest-2026-10-06", "items": {
    "rotate the pasted sandbox key before the demo tomorrow": {
        "id": "5c4d04ed", "text": "Rotate the pasted sandbox key before the demo tomorrow", "first_seen": "2026-10-06",
        "last_seen": "2026-10-06", "times_shown": 1, "sections": ["start_here"], "status": "open",
        "snoozed_until": None, "resolved_at": None},
    "keep the strict sum of 100 for the scoring quotas": {
        "id": "9f8e7d6c", "text": "Keep the strict sum of 100 for the scoring quotas", "first_seen": "2026-10-06",
        "last_seen": "2026-10-06", "times_shown": 1, "sections": ["decided"], "status": "done",
        "snoozed_until": None, "resolved_at": "2026-10-06"},
    "an old synthetic thread nobody mentioned again": {
        "id": "d0d0d0d0", "text": "An old synthetic thread nobody mentioned again", "first_seen": "2026-09-20",
        "last_seen": "2026-09-28", "times_shown": 4, "sections": ["still_open"] * 4, "status": "dropped",
        "snoozed_until": None, "resolved_at": "2026-10-06"},
}}


def _fwd(path: Path) -> str:
    return path.as_posix()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8", newline="\n")


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
    writer = VaultWriter(tmp_cfg, audit)
    writer.write_raw("digest-2026-10-05.md", YESTERDAY, "digest-2026-10-05")
    writer.write_raw("digest-2026-10-06.md", DIGEST, "digest-2026-10-06")
    manifest = RunManifest(
        job_id="digest-2026-10-06", status="complete", started_at="2026-10-06T05:30:00+00:00",
        finished_at="2026-10-06T05:31:00+00:00", counts={"collected": 9, "cleared": 6, "held_sensitive": 2,
                                                         "held_policy": 1}, cost_usd=0.0285,
        paths={"note": "raw/jarvis/digest-2026-10-06.md"}, audit_seq=3)
    run_dir = tmp_cfg.daemon.state_dir / "runs" / "digest-2026-10-06"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8", newline="\n")
    write_json(run_dir / "items.json", SIDECAR)
    write_json(tmp_cfg.daemon.state_dir / "item-history.json", HISTORY)
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


def _main(body: str) -> str:
    return body.split('<main id="main">', 1)[1].split("</main>", 1)[0]


# --- every view renders --------------------------------------------------------------------------


@pytest.mark.parametrize("path", VIEWS)
def test_every_view_renders(seeded: Config, clock: FakeClock, path: str) -> None:
    resp = _client(seeded, clock).get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    for label in NAV_LABELS:
        assert f">{label}</a>" in resp.text
    assert "read-only" in resp.text.lower()
    assert '<main id="main">' in resp.text


def test_the_nav_has_exactly_four_views_and_the_check_adds_the_face() -> None:
    assert [label for _, label in views.NAV] == list(NAV_LABELS)
    assert hub_check.VIEWS == (("Today", "/"), ("Inbox", "/inbox"), ("Projects", "/projects"), ("Activity", "/activity"),
                               ("Face", "/face"))


def test_the_hub_never_writes(seeded: Config, clock: FakeClock, tmp_path: Path) -> None:
    before = _snapshot(tmp_path)
    client = _client(seeded, clock)
    for path in [*VIEWS, "/inbox", "/inbox?sort=due", "/digest/digest-2026-10-06", "/api/status", "/static/hub.css",
                 "/static/hub.js", "/static/prefs.js", "/nope", *views.REDIRECTS]:
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
    for path in [*VIEWS, "/inbox", "/api/status"]:
        assert client.get(path).status_code == 200, path
    assert not ghost.exists()
    assert "No digest" in client.get("/").text


def test_mutating_methods_are_refused(seeded: Config) -> None:
    client = _client(seeded)
    for method in ("post", "put", "delete", "patch"):
        for path in ("/", "/activity", "/projects", "/api/status"):
            assert getattr(client, method)(path).status_code == 405, (method, path)


# --- redirects -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("old,target", sorted(views.REDIRECTS.items()))
def test_old_routes_answer_301_to_the_view_that_absorbed_them(seeded: Config, clock: FakeClock, old: str, target: str) -> None:
    client = _client(seeded, clock)
    resp = client.get(old, follow_redirects=False)
    assert resp.status_code == 301 and resp.headers["location"] == target, old
    # the query string is dropped, and the target renders
    assert client.get(old + "?limit=9", follow_redirects=False).headers["location"] == target
    assert client.get(old).status_code == 200


def test_redirects_cover_the_seven_old_routes() -> None:
    assert set(views.REDIRECTS) == {"/runs", "/ledger", "/held", "/audit", "/status", "/repos", "/reminders"}
    assert all(t.startswith(("/activity", "/projects", "/inbox")) for t in views.REDIRECTS.values())


# --- the loopback boundary -------------------------------------------------------------------------


def test_bind_address_is_not_configurable() -> None:
    assert "host" not in HubCfg.model_fields
    assert HubCfg().port == 8765 and HubCfg().allowed_hosts == []


def test_host_header_must_be_loopback_or_listed(seeded: Config) -> None:
    app = hub_app.create_app(seeded)
    assert TestClient(app, base_url="http://127.0.0.1:8765").get("/activity").status_code == 200
    assert TestClient(app, base_url="http://localhost:8765").get("/activity").status_code == 200
    assert TestClient(app, base_url="http://evil.example:8765").get("/activity").status_code == 403
    assert TestClient(app, base_url="http://evil.example:8765").get("/status", follow_redirects=False).status_code == 403
    cfg = seeded.model_copy(deep=True)
    cfg.hub.allowed_hosts = ["phone.example.test"]
    listed = TestClient(hub_app.create_app(cfg), base_url="https://phone.example.test")
    assert listed.get("/activity").status_code == 200


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
    assert "style-src 'self'" in csp and "connect-src 'self'" in csp and "img-src 'self'" in csp
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in resp.headers["cache-control"]
    for path in [*VIEWS, "/inbox", "/static/hub.css", "/static/hub.js", "/static/prefs.js"]:
        body = client.get(path).text
        assert not re.search(r"https?://", body), path
        assert not re.search(r'(?:src|href)="(?:https?:)?//', body), path
        assert "style=" not in body and not re.search(r"<script(?![^>]*\bsrc=)", body), path


def test_the_api_docs_that_pull_from_a_cdn_are_off(seeded: Config) -> None:
    client = _client(seeded)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# --- the status strip ------------------------------------------------------------------------------------


def test_every_page_carries_the_status_strip_before_the_nav(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/projects").text
    strip = re.search(r'<p class="strip" id="strip" role="status">(.*?)</p>', body)
    assert strip, "no status strip"
    long = re.search(r'<span class="long">(.*?)</span>', strip.group(1)).group(1)  # type: ignore[union-attr]
    short = re.search(r'<span class="short">(.*?)</span>', strip.group(1)).group(1)  # type: ignore[union-attr]
    assert "Last digest " in long and "Next " in long and long.endswith("0 waiting. 0 failed.")
    # the phone line: no last digest when it succeeded, no "0 failed"
    assert short.startswith("Next ") and short.endswith("0 waiting.") and "Last digest" not in short
    # the health word is the one live region; the old daemon pill is gone
    assert '<b aria-live="polite">Running.</b>' in strip.group(1)
    assert body.index('class="strip"') < body.index('<main id="main">')
    assert "daemon running" not in body and 'class="pill ok live"' not in body


def test_strip_words_follow_the_status(seeded: Config, clock: FakeClock) -> None:
    data = hub_data.HubData(seeded, clock)
    status = data.status()
    assert data.strip(status)["health"] == "Running."
    assert data.strip({**status, "running": False})["health"] == "Stopped."
    assert data.strip({**status, "pause": {"reason": "x"}})["health"] == "Paused."
    assert data.strip({**status, "kill": True})["health"] == "Kill switch on."
    assert data.strip({**status, "breaker": {"state": "open"}})["health"] == "Claude calls paused."
    assert "No digest yet." in data.strip({**status, "last_digest": None})["text"]
    failed = {**status, "last_digest": {**status["last_digest"], "status": "failed"}}
    assert re.search(r"Last digest \d\d:\d\d, failed\.", data.strip(failed)["text"])
    busy = {**status, "face": {"inbox_pending": 5, "last_finished": None}, "queue": {**status["queue"], "failed": 2}}
    assert data.strip(busy)["text"].endswith("5 waiting. 2 failed.")
    assert re.search(r"Next \d\d:\d\d (today|tomorrow)\.", data.strip(status)["text"])


def test_status_json_carries_the_digest_finish_stamp(seeded: Config, clock: FakeClock) -> None:
    got = _client(seeded, clock).get("/api/status").json()
    assert got["last_digest"]["job_id"] == "digest-2026-10-06"
    assert got["last_digest"]["at"].startswith("2026-10-06T")


# --- Today ---------------------------------------------------------------------------------------------


def test_today_blocks_in_the_contract_order_and_ids_out_of_the_text(seeded: Config, clock: FakeClock) -> None:
    body = _main(_client(seeded, clock).get("/").text)
    order = ["<h2>Attention</h2>", "<h2>Needs you</h2>", "<h2>Waiting for you</h2>", "<h2>Changed since yesterday</h2>",
             '<details id="else">', '<details id="full">', '<details id="ids">', '<p class="end">That\'s all.</p>']
    positions = [body.index(x) for x in order]
    assert positions == sorted(positions)
    assert body.rstrip().endswith('<p class="end">That\'s all.</p></section>')
    # Needs you: the headline, then the ranked lines with their key and id in data attributes only
    assert "One task is overdue and one repo moved overnight." in body
    assert ('<ol class="needs"><li data-key="rotate the pasted sandbox key before the demo tomorrow" data-id="5c4d04ed">'
            '<span class="t">Rotate the pasted sandbox key before the demo tomorrow</span><span class="act">') in body
    visible = re.sub(r"<[^>]+>", " ", body.split('<details id="full">')[0])
    assert not re.search(r"\[[0-9a-f]{8}\]", visible), "an id tail leaked into the visible text"
    assert "5c4d04ed" not in visible and "6b6b6b6b" not in visible
    # Attention: the CI line as a red row
    assert ('<span class="pill bad">CI failing</span> <a class="tap" href="/projects#repo-alpha-repo">alpha-repo</a> on main, 2 runs in a row'
            in body)  # the repo name leads to its Projects row
    assert "Nothing broken" not in body.split("<h2>Needs you</h2>")[0]
    # Item ids: the terminal command per Needs you line
    assert "<code>jarvis wrong 5c4d04ed</code>" in body and "<code>jarvis wrong 8d94ef0d</code>" in body
    # the lede says what was collected, in words
    assert "9 collected, 6 summarised, 3 held" in body


def test_today_everything_else_holds_still_open_decided_and_system(seeded: Config, clock: FakeClock) -> None:
    body = _main(_client(seeded, clock).get("/").text)
    else_block = body.split('<details id="else">')[1].split('<details id="full">')[0]
    assert "<h4>parser rework</h4>" in else_block and "<h4>notes</h4>" in else_block
    assert ('<li data-key="confirm the retry budget with the reviewer" data-id="6b6b6b6b"><span class="t">Confirm the retry '
            'budget with the reviewer <span class="age">4 days old</span></span><span class="act">') in else_block
    assert "Keep the strict sum of 100 for the scoring quotas" in else_block
    assert "<b>Task</b>: ExampleNightly last ran 2026-10-06 02:30, refused by the operator or administrator" in else_block
    assert "<code>123synth</code>" in else_block and 'class="pill bad">overdue</span>' in else_block
    assert "Kept back: 2 sensitive (never read), 1 work metadata, 0 too long to send." in else_block


def test_today_full_digest_renders_the_note_and_escapes_html(seeded: Config, clock: FakeClock) -> None:
    body = _main(_client(seeded, clock).get("/").text)
    full = body.split('<details id="full">')[1].split('<details id="ids">')[0]
    assert "<h2>Start here</h2>" in full and "<h2>Still open</h2>" in full
    assert "<strong>one</strong>" in full and "<code>code</code>" in full
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "generator: jarvisd" not in body  # the front matter is shown as facts, not raw
    assert body.count("<h1>") == 1, "the note's own title does not add a second h1"
    assert "9 collected, 6 summarised, 3 held" in full and "Audit record" in full


def test_today_changed_since_yesterday_diffs_the_two_notes(seeded: Config, clock: FakeClock) -> None:
    body = _main(_client(seeded, clock).get("/").text)
    changed = body.split("<h2>Changed since yesterday</h2>")[1].split('<details id="else">')[0]
    assert "<b>alpha-repo</b>: 2 commits, uncommitted 1/3, was 0/0, CI now failing" in changed
    assert "<b>beta-repo</b>: uncommitted 4/0, was 2/0" in changed
    assert "1 decision recorded" in changed and "held 3, was 1" in changed


def test_today_says_nothing_changed_with_one_note(tmp_cfg: Config, clock: FakeClock) -> None:
    raw = Path(tmp_cfg.paths.vault_write_raw)
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "digest-2026-10-05.md").write_text(YESTERDAY, encoding="utf-8", newline="\n")
    body = _main(_client(tmp_cfg, clock).get("/").text)
    assert "Nothing changed." in body and 'class="pill ok">Nothing broken.</span>' in body
    assert "Nothing to start with." in body and "Quiet night." in body
    assert 'class="pill ok">All green</span>' in body


def test_today_waiting_for_you_lists_the_oldest_three_with_an_inline_confirm(seeded: Config, clock: FakeClock) -> None:
    from jarvisd.models import Proposal
    from jarvisd.propose import proposals_dir, save_proposal

    folder = proposals_dir(seeded.daemon.state_dir)
    for n in range(5):
        save_proposal(folder, Proposal(id=f"p-{n:08x}", created_at=f"2026-10-01T08:{n:02d}:00+00:00", run_id="digest-2026-10-06",
                                       title=f"Synthetic proposal {n}", project="alpha-repo", kind="task", evidence=["5c4d04ed"],
                                       suggested_status="to do", rationale="Synthetic reason."))
    body = _main(_client(seeded, clock).get("/").text)
    waiting = body.split("<h2>Waiting for you</h2>")[1].split("<h2>Changed since yesterday</h2>")[0]
    assert '<a class="tap" href="/inbox">5 waiting</a>' in waiting
    assert waiting.count('action="/inbox/') == 3 and "Synthetic proposal 0" in waiting and "Synthetic proposal 3" not in waiting
    assert '<input type="hidden" name="next" value="/">' in waiting and 'name="csrf"' in waiting
    assert "/reject" not in waiting and "/edit" not in waiting  # those two stay on the Inbox


# --- phase 3: keys, Done and Snooze, seen marks, since yesterday (contract sections 6 to 8) ---------------------------


def test_today_lines_carry_their_key_and_the_done_and_snooze_controls(seeded: Config, clock: FakeClock) -> None:
    body = _main(_client(seeded, clock).get("/").text)
    assert body.startswith('<section id="today" data-digest="digest-2026-10-06">')  # prefs.js reads the digest id here
    needs = body.split('<ol class="needs">')[1].split("</ol>")[0]
    assert needs.count("<li ") == 2 and needs.count('data-key="') == 2 and needs.count('class="t"') == 2
    first = needs.split("</li>")[0]
    assert first.startswith('<li data-key="rotate the pasted sandbox key before the demo tomorrow" data-id="5c4d04ed">')
    # Done is a small form with the token; Snooze is a details holding the three choices and a date picker
    assert '<form method="post" action="/today/5c4d04ed/done" class="inline">' in first
    assert 'name="csrf"' in first and "<button type=\"submit\">Done</button>" in first
    snooze = first.split('<details class="snooze">')[1]
    assert snooze.startswith("<summary>Snooze</summary>") and 'action="/today/5c4d04ed/snooze"' in snooze
    for value, label in (("tomorrow", "Tomorrow"), ("3d", "3 days"), ("monday", "Monday")):
        assert f'<button name="until" value="{value}">{label}</button>' in snooze, value
    assert '<input type="date" name="until"' in snooze and "<button type=\"submit\">Pick</button>" in snooze
    assert "id=" not in snooze.split("<form")[0]  # the snooze details has no id, so prefs.js leaves it alone
    # Still open lines carry the same controls; Attention and Decided lines do not
    else_block = body.split('<details id="else">')[1].split('<details id="full">')[0]
    assert else_block.count('action="/today/') == 6  # three Still open lines, two forms each
    attention = body.split("<h2>Attention</h2>")[1].split("<h2>Needs you</h2>")[0]
    assert "/today/" not in attention
    decided = else_block.split("<h3>Decided yesterday</h3>")[1]
    assert "/today/" not in decided
    # the raw form-action ids never reach the visible text
    visible = re.sub(r"<[^>]+>", " ", body.split('<details id="full">')[0])
    assert "5c4d04ed" not in visible and "/today/" not in visible


def test_today_without_a_sidecar_keeps_the_keys_and_drops_the_buttons(seeded: Config, clock: FakeClock) -> None:
    (seeded.daemon.state_dir / "runs" / "digest-2026-10-06" / "items.json").unlink()
    body = _main(_client(seeded, clock).get("/").text)
    assert 'data-digest="digest-2026-10-06"' in body
    # the key falls back to the normalised text, so seen marks still work on a note written before the sidecar
    assert '<li data-key="rotate the pasted sandbox key before the demo tomorrow" data-id="5c4d04ed"><span class="t">' in body
    assert "/today/" not in body and 'class="act"' not in body and 'class="snooze"' not in body


def test_today_hides_a_line_with_a_decision_in_force_and_shows_an_expired_snooze(seeded: Config, clock: FakeClock) -> None:
    from jarvisd import attention

    folder = attention.attention_dir(seeded.daemon.state_dir)
    key = "rotate the pasted sandbox key before the demo tomorrow"
    write_json(attention.decision_path(seeded.daemon.state_dir, key),
               {"key": key, "id": "5c4d04ed", "action": "done", "until": None, "decided_at": "2026-10-06T06:00:00+00:00",
                "note": "digest-2026-10-06"})
    key2 = "confirm the retry budget with the reviewer"
    write_json(attention.decision_path(seeded.daemon.state_dir, key2),
               {"key": key2, "id": "6b6b6b6b", "action": "snooze", "until": "2026-10-09", "decided_at": "2026-10-05T06:00:00+00:00",
                "note": "digest-2026-10-05"})
    key3 = "second synthetic thread"
    write_json(attention.decision_path(seeded.daemon.state_dir, key3),
               {"key": key3, "id": "0a1b2c3d", "action": "snooze", "until": "2026-10-06", "decided_at": "2026-10-03T06:00:00+00:00",
                "note": "digest-2026-10-03"})  # expired today: open again
    (folder / "garbage.json").write_text("{not json", encoding="utf-8")
    body = _main(_client(seeded, clock).get("/").text)
    assert "Rotate the pasted sandbox key" not in body.split('<details id="full">')[0]
    assert 'data-id="8d94ef0d"' in body  # the other ranked line stays
    else_block = body.split('<details id="else">')[1].split('<details id="full">')[0]
    assert "Confirm the retry budget" not in else_block and "<h4>parser rework</h4>" not in else_block
    assert 'data-id="0a1b2c3d"' in else_block  # the expired snooze is back
    assert '<p class="lede decided">2 lines decided, applied at the next digest.</p>' in else_block
    # the Full digest still holds the whole note, decisions included: it is the record, not the triage
    assert "Rotate the pasted sandbox key" in body.split('<details id="full">')[1]


def test_today_changed_since_yesterday_lists_the_thread_lines_from_the_history(seeded: Config, clock: FakeClock) -> None:
    body = _main(_client(seeded, clock).get("/").text)
    changed = body.split("<h2>Changed since yesterday</h2>")[1].split('<details id="else">')[0]
    # after the repo lines and the count lines, the four thread lines with their texts
    assert changed.index("held 3, was 1") < changed.index("1 new thread")
    assert '<li class="since-new">1 new thread<ul class="since"><li>Rotate the pasted sandbox key before the demo tomorrow</li></ul></li>' in changed
    assert '<li class="since-resolved">1 thread resolved<ul class="since"><li>Keep the strict sum of 100 for the scoring quotas</li></ul></li>' in changed
    assert '<li class="since-dropped">1 thread dropped<ul class="since"><li>An old synthetic thread nobody mentioned again</li></ul></li>' in changed
    assert ('<li class="since-returned">1 thread back from snooze<ul class="since"><li>Synthetic thread **one** waiting on a '
            'reviewer with `code`</li></ul></li>') in changed
    # a note without the keys (yesterday's) adds nothing
    data = hub_data.HubData(seeded, clock)
    assert data.since({}, "2026-10-05", []) == []
    assert data.since({"n_since_new": "x"}, "2026-10-06", []) == []
    # a key with a decision in force is not listed as new or returned either
    hidden = data.since({"n_since_new": "1", "n_since_returned": "1"}, "2026-10-06", SIDECAR["items"],
                        {"rotate the pasted sandbox key before the demo tomorrow", "synthetic thread one waiting on a reviewer with code"})
    assert [s["texts"] for s in hidden] == [[], []]


def test_since_texts_are_capped_and_the_history_may_be_missing(seeded: Config, clock: FakeClock) -> None:
    history = dict(HISTORY)
    history["items"] = {f"thread {n}": {"text": f"Synthetic thread {n}", "first_seen": "2026-10-06", "status": "open"} for n in range(8)}
    write_json(seeded.daemon.state_dir / "item-history.json", history)
    data = hub_data.HubData(seeded, clock)
    since = data.since({"n_since_new": "8"}, "2026-10-06", [])
    assert since[0]["n"] == 8 and len(since[0]["texts"]) == hub_data.SINCE_TEXTS
    (seeded.daemon.state_dir / "item-history.json").unlink()
    data = hub_data.HubData(seeded, clock)
    assert data.since({"n_since_new": "2"}, "2026-10-06", []) == [{"n": 2, "word": "new thread", "kind": "new", "texts": []}]
    assert _client(seeded, clock).get("/").status_code == 200


def test_the_phase_3_reads_create_nothing(seeded: Config, clock: FakeClock, tmp_path: Path) -> None:
    (seeded.daemon.state_dir / "runs" / "digest-2026-10-06" / "items.json").unlink()
    (seeded.daemon.state_dir / "item-history.json").unlink()
    before = _snapshot(tmp_path)
    client = _client(seeded, clock)
    for path in ("/", "/?ok=done&id=5c4d04ed", "/?ok=snoozed&id=5c4d04ed", "/activity", "/today/5c4d04ed/done"):
        assert client.get(path).status_code in (200, 405), path
    assert _snapshot(tmp_path) == before
    assert not (seeded.daemon.state_dir / "attention").exists()


def test_a_digest_page_by_job_id(seeded: Config, clock: FakeClock) -> None:
    client = _client(seeded, clock)
    assert "Rotate the pasted sandbox key" in client.get("/digest/digest-2026-10-06").text
    assert client.get("/digest/digest-2026-10-09").status_code == 404
    for bad in ("..%2f..%2fetc", "digest-2026-10-06.md", "digest-x", "digest-2026-10-06-r2x"):
        assert client.get(f"/digest/{bad}").status_code == 404, bad


# --- Projects -------------------------------------------------------------------------------------------


def test_projects_keeps_the_old_repos_table_behind_a_disclosure(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/projects").text
    raw = body.split('<details id="raw-repos">')[1]
    assert "alpha-repo" in raw and "beta-repo" in raw
    assert re.search(r"alpha-repo.*?main.*?2.*?1.*?3", raw, re.S)
    assert "Quiet: 2 repos." in raw  # the count line, shown rather than dropped
    assert "GitHub not read: 2 repos" in raw
    assert "digest-2026-10-06" in raw  # says which digest the facts come from
    assert "58d5ed8d" not in body.split('<details id="raw-repos">')[0]


# --- Activity ---------------------------------------------------------------------------------------------


def test_activity_runs_row(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/activity").text
    runs = body.split('<details id="runs">')[1].split('<details id="delivered">')[0]
    assert "2026-10-06" in runs and "written" in runs
    assert "0.0285" in runs
    assert "6 summarised" in runs
    assert 'href="/digest/digest-2026-10-06"' in runs
    assert re.search(r"Audit record 3 <code>[0-9a-f]{12}</code>", runs)  # the audit witness
    assert "seq 3" not in runs


def test_a_run_whose_audit_record_is_gone_says_so(seeded: Config, clock: FakeClock) -> None:
    manifest = seeded.daemon.state_dir / "runs" / "digest-2026-10-06" / "run.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["audit_seq"] = 9999
    manifest.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    assert "not found in the audit log" in _client(seeded, clock).get("/activity").text


def test_activity_strip_tiles(seeded: Config, clock: FakeClock) -> None:
    tiles = hub_data.HubData(seeded, clock).activity()["tiles"]
    assert tiles["runs"] == 1 and tiles["failed"] == 0 and tiles["cost_usd"] == pytest.approx(0.0285)
    assert tiles["held"] == 2 and tiles["flagged"] == 0 and tiles["verified"] is True
    assert tiles["proposed"] == 0 and tiles["confirmed"] == 0 and tiles["rejected"] == 0
    body = _client(seeded, clock).get("/activity").text
    assert '<dl class="tiles">' in body and "<dt>Digest runs</dt><dd>1</dd>" in body and "<dt>Next digest</dt>" in body
    for anchor in ("runs", "delivered", "held", "audit", "status"):
        assert f'<details id="{anchor}">' in body, anchor


WEEKLY = """---
type: jarvis-weekly
generator: jarvisd
week: 2026-W40
from: 2026-09-28
to: 2026-10-04
generated: 2026-10-05T05:31:00+00:00
cost_usd: 0.1900
n_runs: 7
n_failed: 1
n_decided: 1
n_dropped: 1
n_done: 1
n_snoozed: 1
n_flagged: 1
tags: [jarvis, weekly]
---
# Week 2026-W40

## Runs
- 7 runs, 1 failed, $0.19 Claude.

## Decided this week
- 2026-10-03: Keep the strict sum of 100 for the scoring quotas [9f8e7d6c]

## Dropped threads
- An old synthetic thread nobody mentioned again (2026-09-20 to 2026-09-28) [d0d0d0d0]

## Snoozed and done
- Done 2026-10-02: Rotate the pasted sandbox key before the demo tomorrow [5c4d04ed]
- Snoozed until 2026-10-09 2026-10-01: Confirm the retry budget with the reviewer [6b6b6b6b]
- a line the parser does not know

## Flagged wrong
- 2026-10-01 09:12: w-b33f54, should hold, leak.

## Cost by day
- 2026-10-04: 1 run, $0.03.
- 2026-10-03: 2 runs, $0.05.
"""


def test_activity_this_week_without_a_note_shows_the_live_flagged_list(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/activity").text
    week = body.split('<details id="week">')[1].split("</details>")[0]
    assert week.startswith("<summary>This week</summary>") and "No weekly note yet." in week
    assert "<h3>Flagged wrong</h3>" in week and "Nothing flagged wrong in the last 7 days." in week
    # a correction recorded with `jarvis wrong` shows up by id and reason, never the note
    AuditLog(daemon.audit_path(seeded), clock=clock, mirror_stdout=False).emit(
        "correction", item_id="58d5ed8d", should="escalate", leak=False, note_chars=42)
    AuditLog(daemon.audit_path(seeded), clock=clock, mirror_stdout=False).emit(
        "correction", item_id="w-b33f54", should="hold", leak=True, note_chars=9)
    model = hub_data.HubData(seeded, clock).activity()
    assert [f["item_id"] for f in model["flagged"]] == ["w-b33f54", "58d5ed8d"]  # newest first
    assert model["flagged"][0] == {"ts": model["flagged"][0]["ts"], "item_id": "w-b33f54", "should": "hold", "leak": True}
    assert model["tiles"]["flagged"] == 2
    week = _client(seeded, clock).get("/activity").text.split('<details id="week">')[1].split("</details>")[0]
    assert "<code>58d5ed8d</code>, should have been escalated" in week
    assert "<code>w-b33f54</code>, should have been held back <span class=\"pill bad\">leak</span>" in week
    assert "note_chars" not in week and "42" not in week.split("<h3>Flagged wrong</h3>")[1]


def test_activity_this_week_renders_the_weekly_note(seeded: Config, clock: FakeClock) -> None:
    raw = Path(seeded.paths.vault_write_raw)
    (raw / "weekly-2026-W39.md").write_text(WEEKLY.replace("2026-W40", "2026-W39"), encoding="utf-8", newline="\n")
    (raw / "weekly-2026-W40.md").write_text(WEEKLY, encoding="utf-8", newline="\n")
    (raw / "weekly-notes.md").write_text("# not a weekly note\n", encoding="utf-8", newline="\n")
    data = hub_data.HubData(seeded, clock)
    assert [p.name for p in data.weekly_files()] == ["weekly-2026-W39.md", "weekly-2026-W40.md"]
    week = data.weekly()
    assert week is not None and week["week"] == "2026-W40" and week["name"] == "weekly-2026-W40"
    assert week["counts"]["runs"] == 7 and week["sections"]["runs"]["rows"][0]["fields"]["usd"] == "0.19"
    body = _client(seeded, clock).get("/activity").text
    block = body.split('<details id="week">')[1].split("</details>")[0]
    assert "Week 2026-W40 (2026-09-28 to 2026-10-04): 7 runs, 1 failed, 0.1900 USD of Claude calls." in block
    for heading in ("Runs", "Decided this week", "Dropped threads", "Snoozed and done", "Cost by day"):
        assert f"<h3>{heading}</h3>" in block, heading
    assert block.count("<h3>Flagged wrong</h3>") == 1  # the note's section and the live list are one heading
    assert '<li data-id="9f8e7d6c">2026-10-03: Keep the strict sum of 100 for the scoring quotas</li>' in block
    assert "An old synthetic thread nobody mentioned again (2026-09-20 to 2026-09-28)" in block
    assert "Done 2026-10-02: Rotate the pasted sandbox key before the demo tomorrow" in block
    assert '<ul class="raw"><li>a line the parser does not know</li></ul>' in block  # the tolerant fallback
    assert "2026-10-04: 1 run, $0.03." in block
    # without a correction in the audit the note's own Flagged wrong rows are shown, in words
    assert "From the weekly note" in block and "<code>w-b33f54</code>, should have been held back" in block
    visible = re.sub(r"<[^>]+>", " ", block)
    assert not re.search(r"\[[0-9a-f]{8}\]", visible)
    assert "generator: jarvisd" not in block


def test_activity_shows_held_references_and_instructions_never_content(seeded: Config, clock: FakeClock) -> None:
    client = _client(seeded, clock)
    body = client.get("/activity").text
    held = body.split('<details id="held">')[1].split('<details id="audit">')[0]
    assert "w-b33f54" in held and "matched private rule 3" in held and "brain_note" in held
    assert "term:3" not in held, "a reason the label map translates is shown once, in words"
    assert "jarvis wrong w-b33f54" in held
    assert "--leak" in held and "jarvis held" in held
    for path in [*VIEWS, "/inbox", "/api/status", "/digest/digest-2026-10-06"]:
        text = client.get(path).text
        assert SECRET_PATH not in text and "secret-place" not in text, path


def test_activity_verifies_the_chain_and_shows_budget(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/activity").text
    assert "Chain verified" in body
    assert "claude_call" in body and "daemon_start" in body
    assert "0.03" in body  # spent today
    assert "calls" in body.lower()


def test_activity_reports_a_broken_chain(seeded: Config, clock: FakeClock) -> None:
    path = daemon.audit_path(seeded)
    lines = path.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[1])
    rec["confidence"] = 0.9  # content changed, hash not
    lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    body = _client(seeded, clock).get("/activity").text
    assert "BROKEN" in body and "audit record 2" in body


def test_activity_caps_audit_rows(seeded: Config, clock: FakeClock) -> None:
    audit = AuditLog(daemon.audit_path(seeded), clock=clock, mirror_stdout=False)
    for n in range(70):
        audit.emit("housekeeping", n=n)
    client = _client(seeded, clock)
    body = client.get("/activity").text
    assert body.count('class="ev"') == 50
    cfg = seeded.model_copy(deep=True)
    cfg.hub.audit_rows = 5
    assert _client(cfg, clock).get("/activity").text.count('class="ev"') == 5


def test_audit_cache_follows_new_records(seeded: Config, clock: FakeClock) -> None:
    client = _client(seeded, clock)
    assert "zz_marker_event" not in client.get("/activity").text
    AuditLog(daemon.audit_path(seeded), clock=clock, mirror_stdout=False).emit("zz_marker_event")
    assert "zz_marker_event" in client.get("/activity").text


def test_activity_prints_the_cli_status_lines(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/activity").text
    status = body.split('<details id="status">')[1]
    # the facts in words first, the CLI block behind a nested disclosure
    facts = status.split('<details id="status-raw">')[0]
    assert "<dt>Local model</dt><dd>not installed</dd>" in facts and "<dt>Daemon</dt><dd>running</dd>" in facts
    assert "<dt>Kept back references</dt><dd>2</dd>" in facts and "<dt>Claude calls</dt><dd>Claude calls allowed</dd>" in facts
    assert "Local tier: not_installed" not in facts
    raw = status.split('<details id="status-raw">')[1]
    assert "JARVIS daemon: running" in raw and "Budget today" in raw and "Held references: 2" in raw


# --- Status ----------------------------------------------------------------------------------------------


def test_status_matches_the_cli_where_the_facts_are_the_same(seeded: Config, clock: FakeClock) -> None:
    ctx = cli.Ctx(seeded, clock=clock)
    expected = cli.collect_status(ctx)
    got = _client(seeded, clock).get("/api/status").json()
    assert set(expected) <= set(got)
    for key in ("budget", "queue", "breaker", "held_count", "audit", "kill", "pause", "local_tier",
                "watermark", "next_due", "claude_cli_version"):
        assert got[key] == json.loads(json.dumps(expected[key], default=str)), key
    last = dict(got["last_digest"])
    last.pop("at")  # the hub adds the finish stamp for the strip; everything else is the CLI's
    assert last == expected["last_digest"]
    assert got["running"] is True  # a fresh heartbeat from a live pid (this process)


def test_a_warm_status_reads_almost_no_files(seeded: Config, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    """The face polls /api/status every 5 s: the second call must not parse the queue or the held folder again."""
    data = hub_data.HubData(seeded, clock)
    data.status()
    reads: list[Path] = []
    real = hub_data._read_json

    def counting(path: Path) -> Any:
        reads.append(path)
        return real(path)

    monkeypatch.setattr(hub_data, "_read_json", counting)
    data.status()
    assert len(reads) <= 10, reads
    # and a new file is still seen: the cache is keyed by the folder's signature
    store_dir = seeded.paths.queue / "held"
    (store_dir / "w-aaaaaa.json").write_text(json.dumps({"id": "w-aaaaaa", "kind": "brain_note", "reason": "term:1",
                                                        "digest_ids": ["digest-2026-10-06"]}), encoding="utf-8")
    assert data.status()["held_count"] == 3


def test_last_digest_reads_only_the_newest_done_files(seeded: Config, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    done = seeded.paths.queue / "done"
    for n in range(12):
        (done / f"old-{n:02d}.json").write_text("{not json", encoding="utf-8")
    data = hub_data.HubData(seeded, clock)
    reads: list[Path] = []
    real = hub_data._read_json
    monkeypatch.setattr(hub_data, "_read_json", lambda p: (reads.append(p), real(p))[1])
    data._last_digest(clock())
    assert len([p for p in reads if p.parent == done]) <= hub_data.NEWEST_JOBS


def test_a_stale_heartbeat_reads_as_stopped(seeded: Config, clock: FakeClock) -> None:
    later = FakeClock(clock() + timedelta(hours=2))
    data = _client(seeded, later).get("/api/status").json()
    assert data["running"] is False
    assert "Stopped." in _client(seeded, later).get("/").text


def test_a_kill_file_shows_on_every_page(seeded: Config, clock: FakeClock) -> None:
    (seeded.daemon.state_dir / "KILL").write_text("", encoding="utf-8")
    body = _client(seeded, clock).get("/").text
    assert "KILL" in body and "Kill switch on." in body


# --- assets, refresh, preferences -----------------------------------------------------------------------------


def test_static_assets(seeded: Config) -> None:
    client = _client(seeded)
    css = client.get("/static/hub.css")
    js = client.get("/static/hub.js")
    prefs = client.get("/static/prefs.js")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert prefs.status_code == 200 and "javascript" in prefs.headers["content-type"]
    assert 100 <= len(css.text.splitlines()) <= 400  # a few hundred lines, no framework
    assert "prefers-color-scheme" in css.text and "@media" in css.text
    assert "fetch(" in js.text and "hub:refreshed" in js.text
    assert "localStorage" in prefs.text and "hub:refreshed" in prefs.text and "fetch(" not in prefs.text
    assert "hub:seen" in prefs.text and "data-digest" in prefs.text and "XMLHttpRequest" not in prefs.text
    assert client.get("/static/other.js").status_code == 404


def test_refresh_script_follows_config_and_prefs_is_always_there(seeded: Config) -> None:
    on = _client(seeded).get("/activity").text
    assert 'data-refresh="30"' in on and '<script src="/static/prefs.js"></script>' in on
    cfg = seeded.model_copy(deep=True)
    cfg.hub.refresh_s = 0
    off = _client(cfg).get("/activity").text
    assert "hub.js" not in off
    assert '<script src="/static/prefs.js"></script>' in off


def test_the_phone_layout_keeps_the_health_word_and_shrinks_the_avatar() -> None:
    from jarvisd.hub.assets import CSS

    phone = CSS.split("@media (max-width: 40rem)")[1]
    # nothing that carries the health word is hidden on phones: only the long strip text (the short one replaces
    # it) and the table header of the card list
    hidden = phone.replace(".cards thead { display: none; }", "").replace(".strip .long { display: none; }", "")
    assert "display: none" not in hidden.replace(".cards td:empty { display: none; }", "")
    assert ".strip .short { display: inline; }" in phone
    # the avatar link keeps a 2.75rem (44px) hit box; the puppet inside it shrinks to 1.75rem
    assert ".companion lantern-puppet { width: 1.75rem; height: 1.75rem; }" in phone
    assert ".companion a { width: 2.75rem; height: 2.75rem; }" in CSS.split("@media (max-width: 87.99rem)")[1]
    assert ".pill.live" not in CSS  # the daemon pill is gone: the strip's health word is the live region


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
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in banned}
        if hits or defined:
            offenders[path.name] = hits | defined
    assert not offenders, offenders


def test_views_are_pure_and_never_call_the_data_layer() -> None:
    tree = ast.parse((ROOT / "jarvisd" / "hub" / "views.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert "jarvisd.hub.data" not in imported


# --- the command ----------------------------------------------------------------------------------------


def test_hub_check_passes_on_a_seeded_tree(seeded: Config, clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["hub", "--check"], cfg=seeded, clock=clock) == 0
    out = capsys.readouterr().out
    for name in ("Today", "Inbox", "Projects", "Activity", "Face"):
        assert f"PASS  {name}" in out
    assert "FAIL" not in out


def test_hub_check_passes_on_an_empty_tree(tmp_cfg: Config, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["hub", "--check"], cfg=tmp_cfg) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_hub_check_fails_loudly_when_a_view_breaks(seeded: Config, clock: FakeClock, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    def boom(self: Any, *a: Any, **k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr("jarvisd.hub.data.HubData.activity", boom)
    assert cli.main(["hub", "--check"], cfg=seeded, clock=clock) == 1
    assert "FAIL  Activity" in capsys.readouterr().out


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
    assert "always_dirty" not in raw["hub"] and HubCfg().always_dirty == []  # default in code, private names in the local file


def test_hub_doc_states_the_limits() -> None:
    text = (ROOT / "docs" / "hub.md").read_text(encoding="utf-8")
    for needle in ("tailscale serve", "127.0.0.1", "never writes", "jarvis hub --check", "httpx2", "Not built",
                   "always_dirty", "prefs.js", "/activity", "301"):
        assert needle in text, needle
    for label in NAV_LABELS:
        assert f"| {label} |" in text, label


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


# --- phase 2 review fixes -------------------------------------------------------------------------------------


def test_activity_tiles_say_tomorrow_and_the_page_has_h2s(seeded: Config, clock: FakeClock) -> None:
    body = _client(seeded, clock).get("/activity").text
    assert re.search(r"<dt>Next digest</dt><dd>(Today|Tomorrow) \d\d:\d\d</dd>", body)
    assert "<h2>Last 7 days</h2>" in body and "<h2>Records</h2>" in body
    assert (body.index("<h2>Last 7 days</h2>") < body.index('<dl class="tiles">') < body.index('<details id="week">')
            < body.index("<h2>Records</h2>") < body.index('<details id="runs">'))
    assert "1 file," in body and "file(s)" not in body


def test_when_due_and_system_line_translation() -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    assert views.when_due("2026-10-09T18:00:00+00:00", now) == "Today 18:00"
    assert views.when_due("2026-10-10T06:30:00+00:00", now) == "Tomorrow 06:30"
    assert views.when_due("2026-10-12T06:30:00+00:00", now) == "2026-10-12 06:30"
    assert views.when_due(None, now) == "unknown"
    assert views.system_line_text("ExampleNightly: last run 2026-10-09 06:00, result 2147946720.") == (
        "ExampleNightly: last run 2026-10-09 06:00, result: refused by the operator or administrator (0x800710E0).")
    assert views.system_line_text("RECENT.md age 4.0 h.") == "notes index updated 4.0 h ago."


def test_the_stylesheet_has_tokens_tap_targets_and_states() -> None:
    from jarvisd.hub.assets import CSS

    for token in ("--space-1", "--space-5", "--radius-sm", "--tap: 2.75rem"):
        assert token in CSS, token
    assert "Bahnschrift" in CSS and not re.search(r"\b(Inter|Roboto|Arial)\b", CSS)
    assert "system-ui" not in CSS.split("--sans:")[1].split(";")[0]
    for state in ("button:hover", "button.primary:hover", "details > summary:hover", "a:focus-visible, nav a:focus-visible"):
        assert state in CSS, state
    phone = CSS.split("@media (max-width: 40rem)")[1]
    assert "min-height: var(--tap)" in phone and "a.tap { display: inline-block; min-width: var(--tap); padding: var(--space-3) 0; }" in phone
    # the item line controls: 2rem tall on a desktop, a 44px tap target on a phone, one row that wraps
    assert "li[data-key] { display: flex; flex-wrap: wrap;" in CSS and "li[data-key] .t { flex: 1 1 20rem; min-width: 0; }" in CSS
    assert "li[data-key] .act { margin-left: auto; white-space: nowrap;" in CSS and ".seen .t { opacity: 0.6; }" in CSS
    assert ".act button, .act input[type=\"date\"] { min-height: 2rem;" in CSS
    assert ".act button, .act input[type=\"date\"], details.snooze > summary { min-height: var(--tap); }" in phone
    assert "dl.tiles { grid-template-columns: repeat(5, 1fr); }" in CSS.split("@media (min-width: 64rem)")[1]
    # the spacing scale replaced the stray literals: no spacing property carries a bare rem value, no radius a px
    base = CSS.split("@media (max-width: 87.99rem)")[0].split("@media (prefers-color-scheme: dark)")[1]
    assert not re.search(r"(?:padding|margin|gap):[^;]*0\.\d+rem", base)
    assert not re.search(r"border-radius: (?!999px)\d+px", base)  # 999px is the pill's "fully round", not a radius step
