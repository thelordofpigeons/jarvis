"""The Inbox (Q4): confirm, edit and confirm, reject, over the hub and over the CLI.

Every test drives the real FastAPI app through TestClient (or the real CLI) against a throwaway
tree. The tracker is a fake unless a test says it uses the markdown default. Nothing here talks
to a network, spends budget or touches the owner's state; ids, titles and projects are synthetic.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import FakeClock  # noqa: E402
from jarvisd import cli, daemon, inbox  # noqa: E402
from jarvisd.config import Config  # noqa: E402
from jarvisd.hub import app as hub_app  # noqa: E402
from jarvisd.models import Proposal  # noqa: E402
from jarvisd.propose import load_proposals, negative_examples, proposals_dir, save_proposal  # noqa: E402
from jarvisd.tracker import TrackerResult  # noqa: E402

HOST = "http://127.0.0.1:8765"
EM, EN = chr(0x2014), chr(0x2013)
CLEARED_ID = "c0ffee01"
HELD_ID = "w-b33f54"
UNKNOWN_ID = "feedface"
HELD_SECRET = "SECRET-TEXT-THAT-MUST-NOT-APPEAR"

DIGEST = f"""---
type: jarvis-digest
job_id: digest-2026-10-01
date: 2026-10-01
status: complete
---
# Digest

## Start here
1. [{CLEARED_ID}] Synthetic open thread about the example release.

## Held back and not summarized
- Sensitive, never read or sent: 1 item (id {HELD_ID}).
"""


class FakeTracker:
    """Records every call; answers with a canned result, an optional delay or an exception."""

    name = "fake"

    def __init__(self, result: TrackerResult | None = None, delay: float = 0.0, boom: Exception | None = None,
                 not_ready: list[str] | None = None) -> None:
        self.calls: list[tuple[Any, Any]] = []
        self.result = result or TrackerResult(ok=True, url="https://tracker.example.test/t/42", external_id="42")
        self.delay = delay
        self.boom = boom
        self.not_ready = not_ready or []

    def check(self) -> list[str]:
        return []

    def problems(self) -> list[str]:
        return list(self.not_ready)

    def create_task(self, proposal: Any, edits: Any) -> TrackerResult:
        self.calls.append((proposal, edits))
        if self.delay:
            time.sleep(self.delay)
        if self.boom is not None:
            raise self.boom
        return self.result


def make_proposal(n: int = 1, **over: Any) -> Proposal:
    base: dict[str, Any] = dict(
        id=f"p-{n:08x}", created_at=f"2026-10-01T08:{n % 60:02d}:00+00:00", run_id="digest-2026-10-01",
        title=f"Synthetic proposal {n:02d}", project="example-api", kind="task",
        evidence=[CLEARED_ID, UNKNOWN_ID], suggested_status="to do", due_hint=date(2026, 10, 9),
        rationale="Synthetic reason for the example.")
    base.update(over)
    return Proposal(**base)


@pytest.fixture
def cfg(tmp_cfg: Config) -> Config:
    raw = Path(tmp_cfg.paths.vault_write_raw)
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "digest-2026-10-01.md").write_text(DIGEST, encoding="utf-8", newline="\n")
    held = tmp_cfg.paths.queue / "held"
    held.mkdir(parents=True, exist_ok=True)
    (held / f"{HELD_ID}.json").write_text(
        json.dumps({"id": HELD_ID, "kind": "brain_note", "reason": "sensitive_path", "first_seen": "2026-10-01T05:00:00+00:00",
                    "last_seen": "2026-10-01T05:00:00+00:00", "expires_at": "2026-11-01T05:00:00+00:00",
                    "digest_ids": ["digest-2026-10-01"], "source_ref": HELD_SECRET}),
        encoding="utf-8", newline="\n")
    return tmp_cfg


def folder(cfg: Config) -> Path:
    return proposals_dir(cfg.daemon.state_dir)


def put(cfg: Config, n: int = 1, **over: Any) -> Proposal:
    p = make_proposal(n, **over)
    save_proposal(folder(cfg), p)
    return p


def stored(cfg: Config, p: Proposal) -> Proposal:
    return Proposal.model_validate_json((folder(cfg) / f"{p.id}.json").read_text(encoding="utf-8"))


def events(cfg: Config, name: str | None = None) -> list[dict[str, Any]]:
    path = daemon.audit_path(cfg)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if name is None or r["event"] == name]


def client_for(cfg: Config, clock: FakeClock, tracker: FakeTracker | None = None, **kw: Any) -> TestClient:
    app = hub_app.create_app(cfg, clock=clock, tracker=tracker, **kw)
    return TestClient(app, base_url=HOST)


def token_of(client: TestClient) -> str:
    """The token the Inbox page puts in its forms. Cached on the client: a page with no open proposal has no form."""
    cached = getattr(client, "csrf_cache", None)
    if cached is None:
        found = re.search(r'name="csrf" value="([^"]+)"', client.get("/inbox").text)
        assert found, "the Inbox page carries no CSRF field"
        cached = client.csrf_cache = found.group(1)  # type: ignore[attr-defined]
    return cached


def post(client: TestClient, path: str, fields: dict[str, str] | None = None, *, csrf: str | None = None,
         origin: str | None = HOST, follow: bool = False) -> Any:
    body = dict(fields or {})
    body["csrf"] = token_of(client) if csrf is None else csrf
    headers = {"Origin": origin} if origin is not None else {}
    return client.post(path, data=body, headers=headers, follow_redirects=follow)


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# --- the view ------------------------------------------------------------------------------------------------------


def test_inbox_lists_open_proposals_with_resolved_and_held_evidence(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg, evidence=[CLEARED_ID, HELD_ID, UNKNOWN_ID])
    put(cfg, 2, status="rejected", rejected_reason="not mine", title="Already decided proposal")
    put(cfg, 3, status="confirmed", tracker_ref="https://tracker.example.test/t/1", title="Confirmed earlier")
    client = client_for(cfg, clock)
    resp = client.get("/inbox")
    assert resp.status_code == 200 and '<main id="main">' in resp.text
    text = resp.text
    assert p.title in text and p.rationale in text and "to do" in text and "2026-10-09" in text
    assert "Already decided proposal" not in text and "Confirmed earlier" not in text
    assert "Synthetic open thread about the example release" in text  # cleared and in the run: resolved to its line
    assert f"<code>{CLEARED_ID}</code>" in text
    assert HELD_ID in text and HELD_SECRET not in text and "held" in text  # a held id is shown as the id only
    assert f"<code>{UNKNOWN_ID}</code>" in text
    for action in ("confirm", "edit", "reject"):
        assert f'action="/inbox/{p.id}/{action}"' in text
    assert 'method="post"' in text and 'name="reason"' in text and "required" in text
    assert 'name="title"' in text and 'name="project"' in text and 'name="due"' in text


def test_inbox_with_no_proposals_is_a_page_not_an_error(cfg: Config, clock: FakeClock) -> None:
    resp = client_for(cfg, clock).get("/inbox")
    assert resp.status_code == 200 and "Nothing is waiting" in resp.text
    assert not folder(cfg).exists()


def test_inbox_escapes_everything_it_shows(cfg: Config, clock: FakeClock) -> None:
    put(cfg, title="<script>alert(1)</script>", rationale="<b>x</b>")
    text = client_for(cfg, clock).get("/inbox").text
    assert "<script>alert(1)" not in text and "&lt;script&gt;" in text and "<b>x</b>" not in text


def test_get_never_mutates_anything(cfg: Config, clock: FakeClock) -> None:
    put(cfg)
    client = client_for(cfg, clock)
    before = tree(cfg.daemon.state_dir)
    audit_before = events(cfg)
    for url in ("/inbox", "/inbox?ok=confirmed&id=p-00000001", "/inbox/p-00000001/confirm", "/inbox/p-00000001/reject"):
        resp = client.get(url)
        assert resp.status_code in (200, 404, 405), url
    assert tree(cfg.daemon.state_dir) == before and events(cfg) == audit_before


def test_inbox_page_has_no_auto_refresh_that_would_wipe_a_half_typed_form(cfg: Config, clock: FakeClock) -> None:
    put(cfg)
    assert 'data-refresh="0"' in client_for(cfg, clock).get("/inbox").text


def test_a_new_csrf_token_per_process(cfg: Config, clock: FakeClock) -> None:
    put(cfg)
    one, two = client_for(cfg, clock), client_for(cfg, clock)
    assert token_of(one) != token_of(two) and len(token_of(one)) >= 32
    assert token_of(one) == token_of(one)


# --- confirm ---------------------------------------------------------------------------------------------------------


def test_confirm_creates_the_task_and_records_it(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    resp = post(client, f"/inbox/{p.id}/confirm")
    assert resp.status_code == 303 and resp.headers["location"].startswith("/inbox")
    assert len(fake.calls) == 1 and fake.calls[0][0].id == p.id
    done = stored(cfg, p)
    assert done.status == "confirmed" and done.tracker_ref == "https://tracker.example.test/t/42"
    assert done.edits.title is None and done.rejected_reason is None
    ev = events(cfg, "proposal_confirmed")
    assert len(ev) == 1 and ev[0]["proposal_id"] == p.id and ev[0]["tracker"] == "fake"
    assert ev[0]["url"] == "https://tracker.example.test/t/42" and ev[0]["edited"] is False
    # the redirect target shows the outcome, and the proposal has left the list
    page = client.get(resp.headers["location"]).text
    assert "https://tracker.example.test/t/42" in page and 'action="/inbox/' + p.id not in page


def test_confirm_with_the_markdown_default_writes_through_the_vault_writer(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg, evidence=[CLEARED_ID])
    client = client_for(cfg, clock)  # no injected tracker: the configured adapter is built per click
    assert cfg.tracker.adapter == "markdown"
    assert post(client, f"/inbox/{p.id}/confirm").status_code == 303
    done = stored(cfg, p)
    assert done.status == "confirmed" and done.tracker_ref and done.tracker_ref.startswith("file:///")
    target = Path(cfg.paths.vault_write_raw) / "confirmed-tasks.md"
    assert p.title in target.read_text(encoding="utf-8")
    assert [e["adapter"] for e in events(cfg, "tracker_result")] == ["markdown"]


def test_confirm_refuses_when_the_adapter_is_not_ready_and_changes_nothing(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker(not_ready=["no ClickUp token in the environment variable X"])
    client = client_for(cfg, clock, fake)
    resp = post(client, f"/inbox/{p.id}/confirm")
    assert resp.status_code == 502 and "no ClickUp token" in resp.text
    assert fake.calls == [] and stored(cfg, p).status == "proposed"
    assert events(cfg, "proposal_confirm_failed")[0]["error"].startswith("not_ready")


# --- edit and confirm ------------------------------------------------------------------------------------------------


def test_edit_then_confirm_passes_the_edits_to_the_tracker_and_stores_them(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    resp = post(client, f"/inbox/{p.id}/edit", {"title": f"Better {EM} title", "project": "other-project", "due": "2026-10-20"})
    assert resp.status_code == 303
    (_, edits), = fake.calls
    assert edits.title and EM not in edits.title and edits.title.startswith("Better")
    assert edits.project == "other-project" and edits.due == date(2026, 10, 20)
    done = stored(cfg, p)
    assert done.status == "edited_confirmed" and done.edits.project == "other-project"
    assert EM not in done.edits.title and EN not in done.edits.title
    assert done.title == p.title  # the proposal as made is kept; the edits sit beside it
    assert events(cfg, "proposal_confirmed")[0]["edited"] is True


def test_edit_with_blank_or_unchanged_fields_is_a_plain_confirm(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock, FakeTracker())
    resp = post(client, f"/inbox/{p.id}/edit", {"title": p.title, "project": "", "due": "2026-10-09"})
    assert resp.status_code == 303
    assert stored(cfg, p).status == "confirmed" and stored(cfg, p).edits.title is None


def test_edit_with_a_bad_due_date_is_refused_and_changes_nothing(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    resp = post(client, f"/inbox/{p.id}/edit", {"title": "x", "project": "", "due": "next tuesday"})
    assert resp.status_code == 422 and "due" in resp.text.lower()
    assert fake.calls == [] and stored(cfg, p).status == "proposed" and events(cfg, "proposal_confirmed") == []


def test_edit_with_an_overlong_title_is_refused(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker()
    resp = post(client_for(cfg, clock, fake), f"/inbox/{p.id}/edit", {"title": "t" * 121, "project": "", "due": ""})
    assert resp.status_code == 422 and fake.calls == [] and stored(cfg, p).status == "proposed"


# --- reject ------------------------------------------------------------------------------------------------------------


def test_reject_records_the_reason_and_feeds_the_negative_examples(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    resp = post(client, f"/inbox/{p.id}/reject", {"reason": f"Already tracked {EM} elsewhere"})
    assert resp.status_code == 303 and fake.calls == []
    done = stored(cfg, p)
    assert done.status == "rejected" and done.rejected_reason and EM not in done.rejected_reason
    assert done.tracker_ref is None
    ev = events(cfg, "proposal_rejected")
    assert len(ev) == 1 and ev[0]["proposal_id"] == p.id and ev[0]["reason_chars"] == len(done.rejected_reason)
    examples = negative_examples(load_proposals(folder(cfg)))
    assert [e.id for e in examples] == [f"rejected-{p.id}"] and "Already tracked" in examples[0].text


@pytest.mark.parametrize("reason", [None, "", "   ", "\n\t"])
def test_reject_without_a_reason_is_refused(cfg: Config, clock: FakeClock, reason: str | None) -> None:
    p = put(cfg)
    client = client_for(cfg, clock)
    fields = {} if reason is None else {"reason": reason}
    resp = post(client, f"/inbox/{p.id}/reject", fields)
    assert resp.status_code == 422 and "reason" in resp.text.lower()
    assert stored(cfg, p).status == "proposed" and events(cfg, "proposal_rejected") == []


def test_reject_with_a_reason_that_is_too_long_is_refused(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    resp = post(client_for(cfg, clock), f"/inbox/{p.id}/reject", {"reason": "r" * 501})
    assert resp.status_code == 422 and stored(cfg, p).status == "proposed"


# --- CSRF, Origin and Host -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action,fields", [("confirm", {}), ("edit", {"title": "x"}), ("reject", {"reason": "no"})])
def test_a_missing_or_wrong_csrf_token_is_403_and_changes_nothing(cfg: Config, clock: FakeClock, action: str,
                                                                  fields: dict[str, str]) -> None:
    p = put(cfg)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    path = f"/inbox/{p.id}/{action}"
    assert client.post(path, data=fields, headers={"Origin": HOST}).status_code == 403  # no field at all
    assert post(client, path, fields, csrf="wrong").status_code == 403
    assert post(client, path, fields, csrf="").status_code == 403
    other = token_of(client_for(cfg, clock))  # a token of another process is not this process's
    assert post(client, path, fields, csrf=other).status_code == 403
    assert fake.calls == [] and stored(cfg, p).status == "proposed" and events(cfg, "proposal_confirmed") == []


@pytest.mark.parametrize("origin", ["http://evil.example", "http://127.0.0.1:9999", "http://localhost:8765",
                                    "https://127.0.0.1:8765", "null", HOST + ".evil.example", HOST + "/"])
def test_a_wrong_origin_is_403(cfg: Config, clock: FakeClock, origin: str) -> None:
    p = put(cfg)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    assert post(client, f"/inbox/{p.id}/confirm", origin=origin).status_code == 403
    assert fake.calls == [] and stored(cfg, p).status == "proposed"


def test_an_absent_origin_is_accepted_and_the_right_one_too(cfg: Config, clock: FakeClock) -> None:
    put(cfg, 1)
    put(cfg, 2)
    client = client_for(cfg, clock, FakeTracker())
    assert post(client, "/inbox/p-00000001/confirm", origin=None).status_code == 303
    assert post(client, "/inbox/p-00000002/confirm", origin=HOST).status_code == 303


def test_origin_may_be_any_host_the_host_guard_admits_and_only_that(cfg: Config, clock: FakeClock) -> None:
    put(cfg, 1)
    put(cfg, 2)
    put(cfg, 3)
    cfg.hub.allowed_hosts = ["box.tailnet.example"]
    app = hub_app.create_app(cfg, clock=clock, tracker=FakeTracker())
    # localhost is an allowed Host: the browser sends Origin http://localhost:8765
    local = TestClient(app, base_url="http://localhost:8765")
    assert post(local, "/inbox/p-00000001/confirm", origin="http://localhost:8765").status_code == 303
    # `tailscale serve`: Host and Origin both carry the tailnet name, over https
    phone = TestClient(app, base_url="https://box.tailnet.example")
    assert post(phone, "/inbox/p-00000002/confirm", origin="https://box.tailnet.example").status_code == 303
    # an Origin that is not the Host the request arrived on is still a cross-site post
    assert post(phone, "/inbox/p-00000003/confirm", origin="https://evil.example").status_code == 403
    assert post(phone, "/inbox/p-00000003/confirm", origin="http://127.0.0.1:8765").status_code == 403
    assert post(local, "/inbox/p-00000003/confirm", origin="http://localhost:9999").status_code == 403
    assert stored(cfg, make_proposal(3)).status == "proposed"


def test_a_refused_origin_does_not_tell_a_phone_to_use_loopback(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    resp = post(client_for(cfg, clock, FakeTracker()), f"/inbox/{p.id}/confirm", origin="http://evil.example")
    assert resp.status_code == 403 and "127.0.0.1" not in resp.text and "address you used" in resp.text


def test_too_many_form_fields_is_a_400_not_a_traceback(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    body = {f"f{n}": "x" for n in range(30)}
    for action in ("confirm", "edit", "reject"):
        resp = client.post(f"/inbox/{p.id}/{action}", data=body, headers={"Origin": HOST})  # no token: refused before it
        assert resp.status_code == 400 and "Traceback" not in resp.text
    assert fake.calls == [] and stored(cfg, p).status == "proposed"


def test_origin_follows_the_port_the_hub_listens_on(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    app = hub_app.create_app(cfg, clock=clock, tracker=FakeTracker(), port=9000)
    client = TestClient(app, base_url="http://127.0.0.1:9000")
    assert post(client, f"/inbox/{p.id}/confirm", origin=HOST).status_code == 403
    assert post(client, f"/inbox/{p.id}/confirm", origin="http://127.0.0.1:9000").status_code == 303


def test_a_foreign_host_header_is_403_even_with_a_valid_token(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    app = hub_app.create_app(cfg, clock=clock, tracker=FakeTracker())
    token = token_of(TestClient(app, base_url=HOST))
    foreign = TestClient(app, base_url="http://evil.example:8765")
    resp = foreign.post(f"/inbox/{p.id}/confirm", data={"csrf": token}, follow_redirects=False)
    assert resp.status_code == 403 and stored(cfg, p).status == "proposed"


def test_other_methods_on_the_inbox_routes_are_405(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock)
    for method in ("put", "delete", "patch"):
        assert getattr(client, method)(f"/inbox/{p.id}/confirm").status_code == 405
    assert client.post("/inbox").status_code == 405


def test_the_form_post_is_allowed_by_the_content_security_policy(cfg: Config, clock: FakeClock) -> None:
    csp = client_for(cfg, clock).get("/inbox").headers["content-security-policy"]
    assert "form-action 'self'" in csp and "default-src 'none'" in csp and "script-src 'self'" in csp


def test_a_body_that_is_not_a_form_is_415(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock, FakeTracker())
    token = token_of(client)
    resp = client.post(f"/inbox/{p.id}/confirm", content=f"csrf={token}", headers={"Origin": HOST, "Content-Type": "text/plain"})
    assert resp.status_code == 415 and stored(cfg, p).status == "proposed"


def test_an_oversized_body_is_refused(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock)
    resp = client.post(f"/inbox/{p.id}/reject", data={"csrf": token_of(client), "reason": "x" * 100_000},
                       headers={"Origin": HOST})
    assert resp.status_code == 413 and stored(cfg, p).status == "proposed"


# --- double decision, unknown ids -------------------------------------------------------------------------------------


def test_double_confirm_is_409_and_creates_one_task(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    assert post(client, f"/inbox/{p.id}/confirm").status_code == 303
    before = tree(cfg.daemon.state_dir)
    again = post(client, f"/inbox/{p.id}/confirm")
    assert again.status_code == 409 and len(fake.calls) == 1
    assert tree(cfg.daemon.state_dir) == before and len(events(cfg, "proposal_confirmed")) == 1


@pytest.mark.parametrize("first,second", [("confirm", "reject"), ("reject", "confirm"), ("reject", "reject"),
                                          ("confirm", "edit"), ("reject", "edit")])
def test_a_decided_proposal_cannot_be_decided_again(cfg: Config, clock: FakeClock, first: str, second: str) -> None:
    p = put(cfg)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    fields = {"confirm": {}, "edit": {"title": "Changed"}, "reject": {"reason": "no"}}
    assert post(client, f"/inbox/{p.id}/{first}", fields[first]).status_code == 303
    snapshot, calls = tree(cfg.daemon.state_dir), len(fake.calls)
    assert post(client, f"/inbox/{p.id}/{second}", fields[second]).status_code == 409
    assert tree(cfg.daemon.state_dir) == snapshot and len(fake.calls) == calls


def test_two_simultaneous_confirms_create_exactly_one_task(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker(delay=0.3)
    client = client_for(cfg, clock, fake)
    token = token_of(client)
    codes: list[int] = []

    def click() -> None:
        codes.append(client.post(f"/inbox/{p.id}/confirm", data={"csrf": token}, headers={"Origin": HOST},
                                 follow_redirects=False).status_code)

    threads = [threading.Thread(target=click) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [303, 409] and len(fake.calls) == 1


def test_unknown_and_malformed_ids_are_404(cfg: Config, clock: FakeClock) -> None:
    put(cfg)
    client = client_for(cfg, clock, FakeTracker())
    for pid in ("p-ffffffff", "..%2F..%2Fstate", "a" * 80, "x.y"):
        resp = post(client, f"/inbox/{pid}/confirm")
        assert resp.status_code == 404, pid


def test_a_damaged_proposal_file_is_reported_not_crashed_on(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock, FakeTracker())
    other = put(cfg, 2)  # keeps one form on the page, so the token can be read
    token_of(client)
    (folder(cfg) / f"{p.id}.json").write_text("{not json", encoding="utf-8")
    assert post(client, f"/inbox/{p.id}/confirm").status_code == 404
    assert client.get("/inbox").status_code == 200 and other.title in client.get("/inbox").text


# --- tracker failure ---------------------------------------------------------------------------------------------------


def test_a_tracker_failure_keeps_the_proposal_open_and_shows_the_error(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker(TrackerResult(ok=False, error="http_500: server unhappy"))
    client = client_for(cfg, clock, fake)
    resp = post(client, f"/inbox/{p.id}/confirm")
    assert resp.status_code == 502 and "http_500: server unhappy" in resp.text
    assert f'action="/inbox/{p.id}/confirm"' in resp.text  # still on the page, so the click can be repeated
    assert stored(cfg, p).status == "proposed" and stored(cfg, p).tracker_ref is None
    failed = events(cfg, "proposal_confirm_failed")
    assert len(failed) == 1 and failed[0]["proposal_id"] == p.id and failed[0]["tracker"] == "fake"
    assert failed[0]["error"] == "http_500: server unhappy" and events(cfg, "proposal_confirmed") == []
    fake.result = TrackerResult(ok=True, url="https://tracker.example.test/t/7", external_id="7")
    assert post(client, f"/inbox/{p.id}/confirm").status_code == 303  # a retry works
    assert stored(cfg, p).status == "confirmed"


def test_a_failed_edit_does_not_keep_the_edits(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock, FakeTracker(TrackerResult(ok=False, error="timeout")))
    assert post(client, f"/inbox/{p.id}/edit", {"title": "Changed", "project": "", "due": ""}).status_code == 502
    assert stored(cfg, p) == p


def test_a_tracker_that_raises_ends_in_a_message_not_a_traceback(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock, FakeTracker(boom=RuntimeError("kaput")))
    resp = post(client, f"/inbox/{p.id}/confirm")
    assert resp.status_code == 502 and "RuntimeError" in resp.text and "kaput" not in resp.text
    assert stored(cfg, p).status == "proposed"


def test_a_dry_run_send_is_not_a_confirmation(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock, FakeTracker(TrackerResult(ok=True, dry_run=True)))
    resp = post(client, f"/inbox/{p.id}/confirm")
    assert resp.status_code == 502 and "dry_run" in resp.text
    assert stored(cfg, p).status == "proposed" and events(cfg, "proposal_confirmed") == []


def test_a_save_that_fails_after_the_task_exists_says_so(cfg: Config, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    p = put(cfg)
    fake = FakeTracker()

    def broken(*args: Any, **kwargs: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(inbox, "save_proposal", broken)
    resp = post(client_for(cfg, clock, fake), f"/inbox/{p.id}/confirm")
    assert resp.status_code == 500 and "already created" in resp.text and "https://tracker.example.test/t/42" in resp.text
    failed = events(cfg, "proposal_confirm_failed")
    assert failed and failed[0]["error"].startswith("save_failed") and failed[0]["external_id"] == "42"
    assert stored(cfg, p).status == "proposed"


def test_decisions_leave_no_temp_files_behind(cfg: Config, clock: FakeClock) -> None:
    put(cfg, 1)
    put(cfg, 2)
    client = client_for(cfg, clock, FakeTracker())
    post(client, "/inbox/p-00000001/confirm")
    post(client, "/inbox/p-00000002/reject", {"reason": "no"})
    names = sorted(p.name for p in folder(cfg).iterdir())
    assert all(n.endswith(".json") or n.endswith(".lock") for n in names), names


# --- the shared functions and the CLI ----------------------------------------------------------------------------------


def test_the_shared_functions_are_what_the_hub_calls(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker()
    audit = inbox_audit(cfg, clock)
    out = inbox.confirm(cfg, audit, fake, p.id)
    assert out.ok and out.code == "confirmed" and out.proposal and out.proposal.status == "confirmed"
    assert inbox.confirm(cfg, audit, fake, p.id).code == "decided"
    assert inbox.reject(cfg, audit, p.id, "late").code == "decided"
    assert inbox.confirm(cfg, audit, fake, "nope").code == "not_found"


def inbox_audit(cfg: Config, clock: FakeClock) -> Any:
    from jarvisd.audit import AuditLog
    return AuditLog(daemon.audit_path(cfg), cfg.retention.audit_max_bytes, cfg.retention.audit_keep_days, clock=clock,
                    mirror_stdout=False)


def test_cli_confirm_matches_the_hub_path(cfg: Config, clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    p = put(cfg, evidence=[CLEARED_ID])
    assert cli.main(["proposals", "confirm", p.id], cfg=cfg, clock=clock) == 0
    out = capsys.readouterr().out
    assert p.id in out and "confirmed" in out and "file:///" in out
    done = stored(cfg, p)
    assert done.status == "confirmed" and done.tracker_ref and done.tracker_ref.startswith("file:///")
    assert len(events(cfg, "proposal_confirmed")) == 1
    assert cli.main(["proposals", "confirm", p.id], cfg=cfg, clock=clock) == 1  # decided already
    assert "already" in capsys.readouterr().out.lower()
    assert len(events(cfg, "proposal_confirmed")) == 1


def test_cli_confirm_with_edits(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg, evidence=[CLEARED_ID])
    assert cli.main(["proposals", "confirm", p.id, "--title", "Renamed", "--due", "2026-11-01"], cfg=cfg, clock=clock) == 0
    done = stored(cfg, p)
    assert done.status == "edited_confirmed" and done.edits.title == "Renamed" and done.edits.due == date(2026, 11, 1)


def test_cli_reject_needs_a_reason_and_records_it(cfg: Config, clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    p = put(cfg)
    assert cli.main(["proposals", "reject", p.id], cfg=cfg, clock=clock) == 2  # --reason is required
    assert stored(cfg, p).status == "proposed"
    assert cli.main(["proposals", "reject", p.id, "--reason", "   "], cfg=cfg, clock=clock) == 1
    assert stored(cfg, p).status == "proposed"
    capsys.readouterr()
    assert cli.main(["proposals", "reject", p.id, "--reason", "duplicate of another"], cfg=cfg, clock=clock) == 0
    assert "rejected" in capsys.readouterr().out.lower()
    done = stored(cfg, p)
    assert done.status == "rejected" and done.rejected_reason == "duplicate of another"
    assert len(events(cfg, "proposal_rejected")) == 1


def test_cli_unknown_id_and_listing_still_work(cfg: Config, clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    p = put(cfg)
    assert cli.main(["proposals", "confirm", "p-ffffffff"], cfg=cfg, clock=clock) == 1
    assert "no proposal" in capsys.readouterr().out.lower()
    assert cli.main(["proposals", "--all"], cfg=cfg, clock=clock) == 0
    assert p.id in capsys.readouterr().out
    assert cli.main(["proposals"], cfg=cfg, clock=clock) == 0


def test_cli_confirm_reports_a_tracker_that_is_not_ready(cfg: Config, clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    c = cfg.model_copy(deep=True)
    c.tracker.adapter = "clickup"  # no token in this process's environment, no list: not ready
    p = put(c)
    assert cli.main(["proposals", "confirm", p.id], cfg=c, clock=clock) == 1
    assert "not ready" in capsys.readouterr().out.lower()
    assert stored(c, p).status == "proposed"


# --- the hub check ------------------------------------------------------------------------------------------------------


def test_hub_check_renders_the_inbox(cfg: Config, clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    put(cfg)
    assert cli.main(["hub", "--check"], cfg=cfg, clock=clock) == 0
    out = capsys.readouterr().out
    assert "PASS  Inbox" in out and "FAIL" not in out


def test_hub_check_with_zero_proposals_and_no_state_folder(cfg: Config, clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    assert not folder(cfg).exists()
    assert cli.main(["hub", "--check"], cfg=cfg, clock=clock) == 0
    assert "PASS  Inbox" in capsys.readouterr().out and not folder(cfg).exists()


def test_the_inbox_is_in_the_navigation(cfg: Config, clock: FakeClock) -> None:
    text = client_for(cfg, clock).get("/status").text
    assert '<a href="/inbox"' in text


def test_no_dashes_in_the_new_source_files() -> None:
    from jarvisd import ROOT
    for rel in ("jarvisd/inbox.py", "jarvisd/hub/inbox.py", "tests/test_inbox.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        body = text.replace("EM, EN = chr(0x2014), chr(0x2013)", "")
        assert chr(0x2014) not in body and chr(0x2013) not in body, rel
        assert "\r" not in text and not text.startswith("﻿"), rel


# --- release 1.2.0: a create whose outcome is unknown must not be offered again -------------------------------------


def attempt_file(cfg: Config, p: Proposal) -> Path:
    return folder(cfg) / f"{p.id}.attempt"


class SpyTracker(FakeTracker):
    """Remembers whether the attempt marker already existed at the moment of the outward call."""

    def __init__(self, cfg: Config, p: Proposal, **kw: Any) -> None:
        super().__init__(**kw)
        self.marker_seen: list[bool] = []
        self._path = attempt_file(cfg, p)

    def create_task(self, proposal: Any, edits: Any) -> TrackerResult:
        self.marker_seen.append(self._path.exists())
        return super().create_task(proposal, edits)


UNKNOWN = TrackerResult(ok=False, error="timeout", unknown=True)


def test_the_attempt_is_written_before_the_outward_call_and_removed_on_success(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    spy = SpyTracker(cfg, p)
    assert post(client_for(cfg, clock, spy), f"/inbox/{p.id}/confirm").status_code == 303
    assert spy.marker_seen == [True] and not attempt_file(cfg, p).exists()
    assert not list(folder(cfg).glob("*.tmp")) and not list(folder(cfg).glob("*.attempt"))


def test_a_definite_failure_leaves_no_marker_and_stays_confirmable(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker(TrackerResult(ok=False, error="http_401"))
    client = client_for(cfg, clock, fake)
    assert post(client, f"/inbox/{p.id}/confirm").status_code == 502
    assert not attempt_file(cfg, p).exists()
    fake.result = TrackerResult(ok=True, url="https://tracker.example.test/t/1", external_id="1")
    assert post(client, f"/inbox/{p.id}/confirm").status_code == 303 and len(fake.calls) == 2


@pytest.mark.parametrize("how", ["unknown", "raises"])
def test_an_ambiguous_outcome_marks_the_proposal_and_a_second_confirm_is_refused(cfg: Config, clock: FakeClock, how: str) -> None:
    p = put(cfg)
    fake = FakeTracker(UNKNOWN) if how == "unknown" else FakeTracker(boom=RuntimeError("kaput"))
    client = client_for(cfg, clock, fake)
    first = post(client, f"/inbox/{p.id}/confirm")
    assert first.status_code == 502 and "may already exist" in first.text and "still open" not in first.text
    assert attempt_file(cfg, p).exists() and stored(cfg, p).status == "proposed"
    failed = events(cfg, "proposal_confirm_failed")
    assert failed[0]["outcome_unknown"] is True
    second = post(client, f"/inbox/{p.id}/confirm")
    assert second.status_code == 409 and "outcome is unknown" in second.text
    third = post(client, f"/inbox/{p.id}/edit", {"title": "Other", "project": "", "due": ""})
    assert third.status_code == 409 and len(fake.calls) == 1, "no second request went out"
    assert stored(cfg, p).status == "proposed"


def test_the_card_of_a_maybe_created_proposal_asks_for_an_explicit_override(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    q = put(cfg, 2)
    client = client_for(cfg, clock, FakeTracker(UNKNOWN))
    post(client, f"/inbox/{p.id}/confirm")
    page = client.get("/inbox").text
    cards = dict(re.findall(r'<section class="card proposal" id="([^"]+)">(.*?)</section>', page, re.S))
    assert "outcome is unknown" in cards[p.id] and "Confirm anyway" in cards[p.id]
    assert 'name="override" value="1"' in cards[p.id]
    assert 'name="override"' not in cards[q.id] and "Confirm anyway" not in cards[q.id]


def test_an_explicit_override_creates_the_task_and_clears_the_marker(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    fake = FakeTracker(UNKNOWN)
    client = client_for(cfg, clock, fake)
    post(client, f"/inbox/{p.id}/confirm")
    fake.result = TrackerResult(ok=True, url="https://tracker.example.test/t/9", external_id="9")
    assert post(client, f"/inbox/{p.id}/confirm", {"override": "1"}).status_code == 303
    assert len(fake.calls) == 2 and not attempt_file(cfg, p).exists()
    assert stored(cfg, p).status == "confirmed"
    audited = events(cfg, "proposal_confirm_override")
    assert len(audited) == 1 and audited[0]["proposal_id"] == p.id


def test_a_marker_written_by_a_crash_between_the_call_and_the_save_blocks_a_blind_retry(
        cfg: Config, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    p = put(cfg)
    fake = FakeTracker()

    def dies(*args: Any, **kwargs: Any) -> Path:
        raise OSError("power loss")

    monkeypatch.setattr(inbox, "save_proposal", dies)
    assert post(client_for(cfg, clock, fake), f"/inbox/{p.id}/confirm").status_code == 500
    monkeypatch.undo()
    assert attempt_file(cfg, p).exists()
    again = post(client_for(cfg, clock, fake), f"/inbox/{p.id}/confirm")  # a new process: nothing in memory
    assert again.status_code == 409 and len(fake.calls) == 1


def test_the_cli_refuses_a_blind_retry_and_accepts_confirm_anyway(cfg: Config, clock: FakeClock,
                                                                  capsys: pytest.CaptureFixture[str]) -> None:
    p = put(cfg)
    from jarvisd.propose import write_attempt

    write_attempt(folder(cfg), p.id, tracker="fake", error="timeout", ts="2026-10-01T09:00:00+00:00")
    assert cli.main(["proposals", "confirm", p.id], cfg=cfg, clock=clock) == 1
    assert "outcome is unknown" in capsys.readouterr().out and stored(cfg, p).status == "proposed"
    assert cli.main(["proposals", "confirm", p.id, "--confirm-anyway"], cfg=cfg, clock=clock) == 0
    assert stored(cfg, p).status == "confirmed" and not attempt_file(cfg, p).exists()


def test_reject_still_works_on_a_maybe_created_proposal(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock, FakeTracker(UNKNOWN))
    post(client, f"/inbox/{p.id}/confirm")
    assert post(client, f"/inbox/{p.id}/reject", {"reason": "I created it by hand"}).status_code == 303
    assert stored(cfg, p).status == "rejected"


# --- release 1.2.0: the gate runs again at confirm time --------------------------------------------------------------


def test_a_term_added_after_the_proposal_was_made_stops_the_confirm(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg, title="Ship the zebra-codename release", rationale="About zebra-codename.")
    cfg.gates.sensitive_terms = ["zebra-codename"]
    fake = FakeTracker()
    resp = post(client_for(cfg, clock, fake), f"/inbox/{p.id}/confirm")
    assert resp.status_code == 422 and "sensitive term" in resp.text and "Reject" in resp.text
    assert "zebra" not in resp.text.split('<section class="card proposal"')[0], "the banner never echoes the term"
    assert fake.calls == [] and stored(cfg, p).status == "proposed" and not attempt_file(cfg, p).exists()
    failed = events(cfg, "proposal_confirm_failed")
    assert failed[0]["error"] == "flagged:sensitive" and "zebra" not in json.dumps(events(cfg))


def test_a_term_typed_into_the_edit_fields_stops_the_confirm(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    cfg.gates.sensitive_terms = ["zebra-codename"]
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    assert post(client, f"/inbox/{p.id}/edit", {"title": "About ZEBRA-CODENAME", "project": "", "due": ""}).status_code == 422
    assert post(client, f"/inbox/{p.id}/edit", {"title": "", "project": "zebra-codename", "due": ""}).status_code == 422
    assert fake.calls == [] and stored(cfg, p).status == "proposed"
    assert post(client, f"/inbox/{p.id}/edit", {"title": "A harmless rename", "project": "", "due": ""}).status_code == 303


def test_a_proposal_whose_evidence_is_now_held_cannot_be_confirmed(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg, evidence=[CLEARED_ID, HELD_ID])
    fake = FakeTracker()
    resp = post(client_for(cfg, clock, fake), f"/inbox/{p.id}/confirm")
    assert resp.status_code == 422 and "held" in resp.text and HELD_ID in resp.text and HELD_SECRET not in resp.text
    assert fake.calls == [] and stored(cfg, p).status == "proposed" and not attempt_file(cfg, p).exists()
    assert events(cfg, "proposal_confirm_failed")[0]["error"] == "held_evidence"


def test_a_held_evidence_id_that_cannot_name_a_file_is_not_an_error(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg, evidence=["brain:note/with:odd*chars", CLEARED_ID])
    assert post(client_for(cfg, clock, FakeTracker()), f"/inbox/{p.id}/confirm").status_code == 303


def test_a_dry_run_shows_the_owner_the_request_but_audits_none_of_it(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    request = json.dumps({"name": "Synthetic request body", "description": "why"})
    resp = post(client_for(cfg, clock, FakeTracker(TrackerResult(ok=True, dry_run=True, request=request))),
                f"/inbox/{p.id}/confirm")
    assert resp.status_code == 502 and "Synthetic request body" in resp.text
    assert not attempt_file(cfg, p).exists()
    assert "Synthetic request body" not in json.dumps(events(cfg))


def test_a_dry_run_confirm_needs_no_token_with_the_real_clickup_adapter(cfg: Config, clock: FakeClock) -> None:
    c = cfg.model_copy(deep=True)
    c.tracker.adapter = "clickup"
    c.tracker.clickup.dry_run = True
    c.tracker.clickup.default_list_id = "901999"
    p = put(c)
    resp = post(client_for(c, clock), f"/inbox/{p.id}/confirm")  # the real adapter, no token in the environment
    assert resp.status_code == 502 and "dry_run" in resp.text and "no ClickUp token" not in resp.text
    assert "Synthetic proposal 01" in resp.text and stored(c, p).status == "proposed"


def test_decision_audit_records_keep_the_envelope_run_id(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    post(client_for(cfg, clock, FakeTracker()), f"/inbox/{p.id}/confirm")
    done = events(cfg, "proposal_confirmed")[0]
    assert done["digest_run_id"] == "digest-2026-10-01" and done["run_id"] != "digest-2026-10-01"
    q = put(cfg, 2)
    post(client_for(cfg, clock, FakeTracker(TrackerResult(ok=False, error="http_401"))), f"/inbox/{q.id}/confirm")
    bad = events(cfg, "proposal_confirm_failed")[0]
    assert bad["digest_run_id"] == "digest-2026-10-01" and bad["run_id"] != "digest-2026-10-01"
    r = put(cfg, 3)
    post(client_for(cfg, clock), f"/inbox/{r.id}/reject", {"reason": "no"})
    assert events(cfg, "proposal_rejected")[0]["digest_run_id"] == "digest-2026-10-01"


# --- phase 2: slim cards, the one-line Held section, the `next` field ------------------------------------------------

DIGEST_V2 = f"""---
type: jarvis-digest
grammar: 2
job_id: digest-2026-10-02
date: 2026-10-02
status: complete
---
# Digest

## Start here
Headline.
1. Synthetic open thread about the example release, due Friday [{CLEARED_ID}]

## Held back and not summarized
- Held: 1 sensitive (ids {HELD_ID}; reasons: term:1 x1), 0 policy, 0 over cap. Claude: ok. Run `jarvis held` in a terminal.
"""


def test_the_card_is_slim_and_keeps_the_identifiers_behind_a_disclosure(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg, evidence=[CLEARED_ID, HELD_ID, UNKNOWN_ID, "feedfade"], due_hint=date(2026, 10, 3))
    text = client_for(cfg, clock).get("/inbox").text
    card = re.search(r'<section class="card proposal" id="([^"]+)">(.*?)</section>', text, re.S).group(2)
    order = [f"<h2>{p.title}</h2>", '<p class="why">', '<p class="pills">', '<div class="actions">', '<details class="ids">']
    positions = [card.index(x) for x in order]
    assert positions == sorted(positions)
    assert '<span class="pill bad">3 days late</span>' in card and '<span class="pill ">example-api</span>' in card
    assert card.count("<form ") == 3 and "outcome unknown" not in card
    head = card.split('<details class="ids">')[0]
    assert p.run_id not in head and "Suggested status" not in head and "<dl" not in head  # identifiers are folded away
    tail = card.split('<details class="ids">')[1]
    assert p.id in tail and "to do" in tail and "<dt>Run</dt>" in tail
    # evidence: one line for the ids the note does not carry, never one row each
    assert tail.count("not in that run") == 1 and "2 ids not in that run" in tail
    assert f"<code>{UNKNOWN_ID}</code>" in tail and "<code>feedfade</code>" in tail
    assert f"<code>{CLEARED_ID}</code> Synthetic open thread about the example release" in tail
    assert f"<code>{HELD_ID}</code>" in tail and HELD_SECRET not in text


def test_the_one_line_held_section_still_hides_a_held_evidence_id(cfg: Config, clock: FakeClock) -> None:
    raw = Path(cfg.paths.vault_write_raw)
    (raw / "digest-2026-10-02.md").write_text(DIGEST_V2, encoding="utf-8", newline="\n")
    (cfg.paths.queue / "held" / f"{HELD_ID}.json").unlink()  # the note's Held line is the only trace
    p = put(cfg, run_id="digest-2026-10-02", evidence=[CLEARED_ID, HELD_ID])
    text = client_for(cfg, clock).get("/inbox").text
    assert f"<code>{HELD_ID}</code>" in text and "id only, never summarized" in text
    assert "Synthetic open thread about the example release, due Friday" in text  # the id tail is stripped from the line
    assert f"[{CLEARED_ID}]" not in text.split('<section class="card proposal"')[1]
    assert p.id in text


def test_a_maybe_created_card_carries_the_outcome_unknown_pill(cfg: Config, clock: FakeClock) -> None:
    p = put(cfg)
    client = client_for(cfg, clock, FakeTracker(UNKNOWN))
    post(client, f"/inbox/{p.id}/confirm")
    assert '<span class="pill bad">outcome unknown</span>' in client.get("/inbox").text


def test_next_sends_the_browser_back_to_today_or_the_inbox_and_nowhere_else(cfg: Config, clock: FakeClock) -> None:
    put(cfg, 1)
    put(cfg, 2)
    put(cfg, 3)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    home = post(client, "/inbox/p-00000001/confirm", {"next": "/"})
    assert home.status_code == 303 and home.headers["location"] == "/?ok=confirmed&id=p-00000001"
    assert "Confirmed: Synthetic proposal 01" in client.get(home.headers["location"]).text
    inbox_ = post(client, "/inbox/p-00000002/confirm", {"next": "/inbox"})
    assert inbox_.status_code == 303 and inbox_.headers["location"].startswith("/inbox?ok=confirmed")
    for bad in ("/activity", "https://evil.example/", "//evil.example", "/inbox/x", "", "javascript:alert(1)"):
        resp = post(client, "/inbox/p-00000003/confirm", {"next": bad})
        assert resp.status_code == 400, bad
    assert len(fake.calls) == 2 and stored(cfg, make_proposal(3)).status == "proposed"


def test_the_today_page_offers_an_inline_confirm_for_the_oldest_waiting_proposals(cfg: Config, clock: FakeClock) -> None:
    put(cfg, 1)
    fake = FakeTracker()
    client = client_for(cfg, clock, fake)
    page = client.get("/").text
    form = re.search(r'<form method="post" action="(/inbox/p-00000001/confirm)" class="inline">(.*?)</form>', page)
    assert form and 'name="next" value="/"' in form.group(2)
    token = re.search(r'name="csrf" value="([^"]+)"', form.group(2)).group(1)
    resp = client.post(form.group(1), data={"csrf": token, "next": "/"}, headers={"Origin": HOST}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/?ok=confirmed") and len(fake.calls) == 1


def test_inbox_opens_with_one_sentence_and_the_explanation_behind_a_disclosure(cfg: Config, clock: FakeClock) -> None:
    put(cfg, 1)
    put(cfg, 2)
    text = client_for(cfg, clock).get("/inbox").text
    head = text.split('<section class="card proposal"')[0]
    assert '<p class="lede">2 waiting, newest first.</p>' in head
    assert '<details class="help" id="inbox-help"><summary>How this works</summary>' in head
    assert "Only the ids of held items are shown, never their content." in head
    assert '<a class="tap" href="/inbox?sort=due">by due date</a>' in head
    due = client_for(cfg, clock).get("/inbox?sort=due").text
    assert '<p class="lede">2 waiting, by due date, undated last.</p>' in due
    card = text.split('<section class="card proposal"')[1]
    actions = card.split('<div class="actions">')[1].split('<details class="ids">')[0]
    assert (actions.index('<button type="submit" class="primary">Confirm</button>') < actions.index("<summary>Edit</summary>")
            < actions.index("<summary>Reject</summary>"))
    assert "Edit and confirm</button>" in actions
