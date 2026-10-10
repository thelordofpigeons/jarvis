"""Done and Snooze on a Today line (docs/hub-rework-contract.md, section 7), over the hub and over the module.

Mirrors tests/test_inbox.py: every test drives the real FastAPI app through TestClient against a throwaway
tree with a synthetic digest note, its item sidecar and a few attention files. Nothing here talks to a
network or touches the owner's state; ids, keys and texts are synthetic.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import FakeClock  # noqa: E402
from jarvisd import attention, daemon  # noqa: E402
from jarvisd.config import Config  # noqa: E402
from jarvisd.fsio import FileLock, lock_path_for  # noqa: E402
from jarvisd.hub import app as hub_app  # noqa: E402
from test_hub import DIGEST, HISTORY, SIDECAR, YESTERDAY, write_json  # noqa: E402

HOST = "http://127.0.0.1:8765"
JOB = "digest-2026-10-06"
RANKED = "5c4d04ed"  # Rotate the pasted sandbox key ...
RANKED_KEY = "rotate the pasted sandbox key before the demo tomorrow"
THREAD = "6b6b6b6b"  # Confirm the retry budget ... (Still open)
ATTENTION_ONLY = "58d5ed8d"  # an Attention and Repos line: in the sidecar, but the page draws no button for it
NOT_IN_SIDECAR = "feedface"
# The fixture clock is 2026-10-06 05:31 UTC, a Tuesday.
TODAY = date(2026, 10, 6)


@pytest.fixture
def cfg(tmp_cfg: Config) -> Config:
    raw = Path(tmp_cfg.paths.vault_write_raw)
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "digest-2026-10-05.md").write_text(YESTERDAY, encoding="utf-8", newline="\n")
    (raw / f"{JOB}.md").write_text(DIGEST, encoding="utf-8", newline="\n")
    write_json(tmp_cfg.daemon.state_dir / "runs" / JOB / "items.json", SIDECAR)
    write_json(tmp_cfg.daemon.state_dir / "item-history.json", HISTORY)
    return tmp_cfg


def folder(cfg: Config) -> Path:
    return attention.attention_dir(cfg.daemon.state_dir)


def files(cfg: Config) -> list[Path]:
    return sorted(p for p in folder(cfg).glob("*") if p.is_file()) if folder(cfg).exists() else []


def decision(cfg: Config, key: str = RANKED_KEY) -> dict[str, Any]:
    return json.loads(attention.decision_path(cfg.daemon.state_dir, key).read_text(encoding="utf-8"))


def events(cfg: Config, name: str | None = None) -> list[dict[str, Any]]:
    path = daemon.audit_path(cfg)
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if name is None or r["event"] == name]


def client_for(cfg: Config, clock: FakeClock, **kw: Any) -> TestClient:
    return TestClient(hub_app.create_app(cfg, clock=clock, **kw), base_url=HOST)


def token_of(client: TestClient) -> str:
    cached = getattr(client, "csrf_cache", None)
    if cached is None:
        found = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text)
        assert found, "the Today page carries no CSRF field"
        cached = client.csrf_cache = found.group(1)  # type: ignore[attr-defined]
    return cached


def post(client: TestClient, path: str, fields: dict[str, str] | None = None, *, csrf: str | None = None,
         origin: str | None = HOST, follow: bool = False, **kw: Any) -> Any:
    body = dict(fields or {})
    body["csrf"] = token_of(client) if csrf is None else csrf
    headers = {"Origin": origin} if origin is not None else {}
    return client.post(path, data=body, headers=headers, follow_redirects=follow, **kw)


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# --- the module ---------------------------------------------------------------------------------------------------------


def test_attention_name_is_the_words_then_eight_hex_of_the_hash() -> None:
    name = attention.attention_name(RANKED_KEY)
    assert re.fullmatch(r"rotate-the-pasted-sandbox-key-before-the-demo-tomorrow-[0-9a-f]{8}", name)
    long_key = " ".join(["word"] * 40)
    assert len(attention.attention_name(long_key)) == 72 + 1 + 8
    assert attention.attention_name("a b") != attention.attention_name("a  b ")  # the hash is of the key as given
    assert "/" not in attention.attention_name("a/b ../c") and ".." not in attention.attention_name("a/b ../c")


@pytest.mark.parametrize("value,expected", [
    ("tomorrow", date(2026, 10, 7)), ("3d", date(2026, 10, 9)), ("monday", date(2026, 10, 12)), (" Monday ", date(2026, 10, 12)),
    ("2026-10-07", date(2026, 10, 7)), ("2027-01-04", date(2027, 1, 4)),
    ("2026-10-06", None), ("2026-10-05", None), ("2027-01-05", None), ("soon", None), ("", None), (None, None),
    ("2026-13-01", None), ("06/10/2026", None),
])
def test_resolve_until_follows_the_contract(value: str | None, expected: date | None) -> None:
    assert attention.resolve_until(value, TODAY) == expected


def test_monday_is_never_today() -> None:
    assert attention.resolve_until("monday", date(2026, 10, 5)) == date(2026, 10, 12)  # asked on a Monday
    assert attention.resolve_until("monday", date(2026, 10, 11)) == date(2026, 10, 12)  # asked on a Sunday


def test_in_force_and_decision_record() -> None:
    assert attention.in_force({"action": "done", "until": None}, TODAY)
    assert attention.in_force({"action": "snooze", "until": "2026-10-07"}, TODAY)
    assert not attention.in_force({"action": "snooze", "until": "2026-10-06"}, TODAY)
    assert not attention.in_force({"action": "snooze", "until": None}, TODAY)
    assert attention.decision_record({"key": "k", "id": "c0ffee01", "action": "snooze", "until": "nope"})["until"] is None
    for bad in ({"key": "", "id": "c0ffee01", "action": "done"}, {"key": "k", "id": "zz", "action": "done"},
                {"key": "k", "id": "c0ffee01", "action": "wipe"}, "text", None, []):
        assert attention.decision_record(bad) is None, bad


def test_load_sidecar_is_tolerant_and_never_follows_a_path(cfg: Config) -> None:
    items = attention.load_sidecar(cfg.daemon.state_dir, JOB)
    assert [r["id"] for r in items][:2] == [RANKED, "8d94ef0d"] and items[0]["key"] == RANKED_KEY
    assert attention.load_sidecar(cfg.daemon.state_dir, "digest-2026-10-05") == []
    for bad in (None, "", "../runs", "runs/../../x", ".hidden"):
        assert attention.load_sidecar(cfg.daemon.state_dir, bad) == [], bad
    path = attention.sidecar_path(cfg.daemon.state_dir, JOB)
    path.write_text('{"items": [1, {"key": "", "id": "c0ffee01"}, {"key": "ok", "id": "not-an-id"}]}', encoding="utf-8")
    assert attention.load_sidecar(cfg.daemon.state_dir, JOB) == []
    path.write_text("{not json", encoding="utf-8")
    assert attention.load_sidecar(cfg.daemon.state_dir, JOB) == []


# --- the view: GET never writes ---------------------------------------------------------------------------------------


def test_get_never_mutates_anything(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    before = tree(cfg.daemon.state_dir)
    audit_before = events(cfg)
    for url in ("/", f"/?ok=done&id={RANKED}", f"/?ok=snoozed&id={RANKED}", "/?ok=done&id=../x", f"/today/{RANKED}/done",
                f"/today/{RANKED}/snooze", "/activity"):
        resp = client.get(url)
        assert resp.status_code in (200, 404, 405), url
    assert tree(cfg.daemon.state_dir) == before and events(cfg) == audit_before
    assert not folder(cfg).exists()


def test_a_flash_code_without_a_decision_on_disk_shows_nothing(cfg: Config, clock: FakeClock) -> None:
    body = client_for(cfg, clock).get(f"/?ok=done&id={RANKED}").text
    assert 'class="banner ok"' not in body


# --- done -------------------------------------------------------------------------------------------------------------


def test_done_writes_one_file_audits_the_ids_and_redirects_to_today(cfg: Config, clock: FakeClock, tmp_path: Path) -> None:
    client = client_for(cfg, clock)
    before = tree(tmp_path)
    resp = post(client, f"/today/{RANKED}/done")
    assert resp.status_code == 303 and resp.headers["location"] == f"/?ok=done&id={RANKED}"
    # exactly one file, in state/attention/, named by the key, with the contract's fields
    assert sorted(p.name for p in files(cfg)) == sorted([f"{attention.attention_name(RANKED_KEY)}.json", "decide.lock"])
    doc = decision(cfg)
    assert doc == {"key": RANKED_KEY, "id": RANKED, "action": "done", "until": None,
                   "decided_at": "2026-10-06T05:31:00+00:00", "note": JOB}
    # nothing else moved: the only new paths are under state/attention and the audit log
    after = tree(tmp_path)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert all(("/state/attention/" in k) or k.endswith((".jsonl", ".jsonl.lock")) for k in changed), changed
    rec = events(cfg, "attention_decided")
    assert len(rec) == 1
    assert {k: rec[0][k] for k in ("item_id", "action", "until", "digest_run_id")} == {
        "item_id": RANKED, "action": "done", "until": None, "digest_run_id": JOB}
    assert "key" not in rec[0] and "text" not in rec[0] and "sandbox" not in json.dumps(rec[0])
    # the page after the redirect: a flash, and the line is gone at once
    page = client.get(resp.headers["location"]).text
    assert '<div class="banner ok" role="status">Done.' in page
    main = page.split('<main id="main">')[1].split('<details id="full">')[0]
    assert "Rotate the pasted sandbox key" not in main and 'data-id="8d94ef0d"' in main
    assert "1 line decided, applied at the next digest." in main


def test_done_on_a_still_open_line(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    assert post(client, f"/today/{THREAD}/done").status_code == 303
    doc = decision(cfg, "confirm the retry budget with the reviewer")
    assert doc["id"] == THREAD and doc["action"] == "done"
    main = client.get("/").text.split('<main id="main">')[1].split('<details id="full">')[0]
    assert "Confirm the retry budget" not in main and "<h4>parser rework</h4>" not in main


# --- snooze -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value,until", [("tomorrow", "2026-10-07"), ("3d", "2026-10-09"), ("monday", "2026-10-12"),
                                         ("2026-10-20", "2026-10-20")])
def test_snooze_choices_write_the_date_and_say_it_back(cfg: Config, clock: FakeClock, value: str, until: str) -> None:
    client = client_for(cfg, clock)
    resp = post(client, f"/today/{RANKED}/snooze", {"until": value})
    assert resp.status_code == 303 and resp.headers["location"] == f"/?ok=snoozed&id={RANKED}"
    doc = decision(cfg)
    assert doc["action"] == "snooze" and doc["until"] == until and doc["id"] == RANKED and doc["note"] == JOB
    rec = events(cfg, "attention_decided")[0]
    assert rec["action"] == "snooze" and rec["until"] == until and rec["item_id"] == RANKED
    page = client.get(resp.headers["location"]).text
    assert f"Snoozed until {until}." in page
    assert "Rotate the pasted sandbox key" not in page.split('<main id="main">')[1].split('<details id="full">')[0]


def test_the_date_input_and_the_buttons_share_a_name_and_the_first_value_wins(cfg: Config, clock: FakeClock) -> None:
    """A browser sends the clicked button's `until` and then the (empty) date input's `until`."""
    client = client_for(cfg, clock)
    body = f"csrf={token_of(client)}&until=tomorrow&until="
    resp = client.post(f"/today/{RANKED}/snooze", content=body, follow_redirects=False,
                       headers={"Origin": HOST, "Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 303 and decision(cfg)["until"] == "2026-10-07"


@pytest.mark.parametrize("value", ["", "soon", "2026-10-06", "2026-10-05", "2027-01-05", "yesterday", "tomorrow;", "2026-1-7"])
def test_snooze_with_a_bad_date_is_422_and_writes_nothing(cfg: Config, clock: FakeClock, value: str) -> None:
    client = client_for(cfg, clock)
    resp = post(client, f"/today/{RANKED}/snooze", {"until": value})
    assert resp.status_code == 422, value
    assert "tomorrow, 3d, monday or a date after today within 90 days" in resp.text
    assert not folder(cfg).exists() and events(cfg, "attention_decided") == [] and events(cfg, "attention_decide_failed") == []
    assert '<main id="main">' in resp.text and "Rotate the pasted sandbox key" in resp.text  # the page, with the error


def test_snooze_without_an_until_field_at_all_is_422(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    assert post(client, f"/today/{RANKED}/snooze").status_code == 422
    assert not folder(cfg).exists()


# --- not found, decided, busy --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("item_id", [NOT_IN_SIDECAR, "zzzzzzzz", "5C4D04ED", "5c4d04e", "5c4d04ed9", "p-00000001", "w-b33f54"])
def test_an_id_that_is_not_in_the_latest_sidecar_is_404_and_creates_nothing(cfg: Config, clock: FakeClock, item_id: str) -> None:
    client = client_for(cfg, clock)
    for action in ("done", "snooze"):
        resp = post(client, f"/today/{item_id}/{action}", {"until": "tomorrow"})
        assert resp.status_code == 404, (item_id, action)
        assert "No item with that id in the latest digest" in resp.text
    assert not folder(cfg).exists() and events(cfg, "attention_decided") == []


def test_without_a_sidecar_the_page_has_no_buttons_and_a_post_is_404(cfg: Config, clock: FakeClock) -> None:
    attention.sidecar_path(cfg.daemon.state_dir, JOB).unlink()
    client = client_for(cfg, clock)
    page = client.get("/").text
    assert "/today/" not in page and 'data-key="' in page
    found = re.search(r'name="csrf" value="([^"]+)"', page)  # the Waiting block has no form either: take the token elsewhere
    token = found.group(1) if found else None
    if token is None:
        from jarvisd.models import Proposal
        from jarvisd.propose import proposals_dir, save_proposal

        save_proposal(proposals_dir(cfg.daemon.state_dir), Proposal(
            id="p-00000001", created_at="2026-10-01T08:00:00+00:00", run_id=JOB, title="Synthetic proposal", project="alpha-repo",
            kind="task", evidence=[RANKED], suggested_status="to do", rationale="Synthetic reason."))
        token = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)  # type: ignore[union-attr]
    resp = client.post(f"/today/{RANKED}/done", data={"csrf": token}, headers={"Origin": HOST})
    assert resp.status_code == 404 and "older notes have no item list" in resp.text
    assert not folder(cfg).exists()


def test_the_key_comes_from_the_sidecar_never_from_the_form(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    assert post(client, f"/today/{RANKED}/done", {"key": "something else", "id": "deadbeef", "text": "<b>x</b>"}).status_code == 303
    doc = decision(cfg)
    assert doc["key"] == RANKED_KEY and doc["id"] == RANKED
    assert [p.name for p in files(cfg) if p.suffix == ".json"] == [f"{attention.attention_name(RANKED_KEY)}.json"]


def test_a_decision_in_force_is_409_and_changes_nothing(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    assert post(client, f"/today/{RANKED}/done").status_code == 303
    first = decision(cfg)
    stamp = attention.decision_path(cfg.daemon.state_dir, RANKED_KEY).stat().st_mtime_ns
    for action, fields in (("done", {}), ("snooze", {"until": "tomorrow"})):
        resp = post(client, f"/today/{RANKED}/{action}", fields)
        assert resp.status_code == 409 and "already done" in resp.text, action
    assert decision(cfg) == first and attention.decision_path(cfg.daemon.state_dir, RANKED_KEY).stat().st_mtime_ns == stamp
    assert len(events(cfg, "attention_decided")) == 1


def test_a_snooze_in_force_refuses_a_second_decision_until_its_date(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    assert post(client, f"/today/{RANKED}/snooze", {"until": "3d"}).status_code == 303
    resp = post(client, f"/today/{RANKED}/done")
    assert resp.status_code == 409 and "already snoozed until 2026-10-09" in resp.text
    assert decision(cfg)["action"] == "snooze"


def test_an_expired_snooze_is_replaced(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    assert post(client, f"/today/{RANKED}/snooze", {"until": "tomorrow"}).status_code == 303
    clock.set(datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc))  # the snooze day: the line is open again
    main = client.get("/").text.split('<main id="main">')[1].split('<details id="full">')[0]
    assert "Rotate the pasted sandbox key" in main
    assert post(client, f"/today/{RANKED}/done").status_code == 303
    doc = decision(cfg)
    assert doc["action"] == "done" and doc["decided_at"] == "2026-10-07T06:00:00+00:00"
    assert len([p for p in files(cfg) if p.suffix == ".json"]) == 1  # the same file, replaced
    assert [r["action"] for r in events(cfg, "attention_decided")] == ["snooze", "done"]


def test_a_held_lock_is_409_busy_and_writes_nothing(cfg: Config, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(attention, "LOCK_WAIT_S", 0.2)
    client = client_for(cfg, clock)
    folder(cfg).mkdir(parents=True, exist_ok=True)
    lock = FileLock(lock_path_for(folder(cfg) / "decide"), timeout=1.0)
    assert lock.acquire()
    try:
        resp = post(client, f"/today/{RANKED}/done")
    finally:
        lock.release()
    assert resp.status_code == 409 and "Another decision is being processed" in resp.text
    assert [p.suffix for p in files(cfg)] == [".lock"] and events(cfg, "attention_decided") == []
    assert post(client, f"/today/{RANKED}/done").status_code == 303  # and it works once the lock is free


def test_two_simultaneous_decisions_write_one_file_and_one_record(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    token_of(client)
    codes: list[int] = []
    gate = threading.Barrier(2)

    def go(action: str, fields: dict[str, str]) -> None:
        gate.wait()
        codes.append(post(client, f"/today/{RANKED}/{action}", fields).status_code)

    threads = [threading.Thread(target=go, args=("done", {})), threading.Thread(target=go, args=("snooze", {"until": "3d"}))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(codes) == [303, 409]
    assert len([p for p in files(cfg) if p.suffix == ".json"]) == 1 and len(events(cfg, "attention_decided")) == 1


def test_a_save_failure_is_502_and_audited_without_the_key(cfg: Config, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(path: Any, text: str, **kw: Any) -> None:
        raise PermissionError("locked by another program")

    monkeypatch.setattr(attention, "atomic_write_text", boom)
    client = client_for(cfg, clock)
    resp = post(client, f"/today/{RANKED}/done")
    assert resp.status_code == 502 and "could not be saved (PermissionError)" in resp.text
    assert [p.suffix for p in files(cfg)] == [".lock"]
    failed = events(cfg, "attention_decide_failed")
    assert len(failed) == 1 and failed[0]["error"] == "save_failed:PermissionError" and failed[0]["item_id"] == RANKED
    assert "key" not in failed[0] and "sandbox" not in json.dumps(failed[0]) and events(cfg, "attention_decided") == []


def test_decisions_leave_no_temp_files_behind(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    for item_id, action, fields in ((RANKED, "done", {}), (THREAD, "snooze", {"until": "monday"}), ("8d94ef0d", "done", {})):
        assert post(client, f"/today/{item_id}/{action}", fields).status_code == 303
    names = [p.name for p in files(cfg)]
    assert len([n for n in names if n.endswith(".json")]) == 3 and names.count("decide.lock") == 1
    assert not any(n.startswith(".") or n.endswith(".tmp") for n in names)
    assert all(re.fullmatch(r"[\w.-]+-[0-9a-f]{8}\.json|decide\.lock", n) for n in names), names


# --- the guards, in the Inbox order -------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["done", "snooze"])
@pytest.mark.parametrize("csrf", ["", "wrong", None])
def test_a_missing_or_wrong_csrf_token_is_403_and_changes_nothing(cfg: Config, clock: FakeClock, action: str, csrf: str | None) -> None:
    client = client_for(cfg, clock)
    body = {"until": "tomorrow"}
    if csrf is None:
        resp = client.post(f"/today/{RANKED}/{action}", data=body, headers={"Origin": HOST})
    else:
        resp = post(client, f"/today/{RANKED}/{action}", body, csrf=csrf)
    assert resp.status_code == 403 and "CSRF" in resp.text
    assert not folder(cfg).exists() and events(cfg) == []


def test_a_token_from_another_process_is_refused(cfg: Config, clock: FakeClock) -> None:
    other = client_for(cfg, clock)
    client = client_for(cfg, clock)
    assert post(client, f"/today/{RANKED}/done", csrf=token_of(other)).status_code == 403
    assert not folder(cfg).exists()


@pytest.mark.parametrize("origin", ["http://evil.example", "http://127.0.0.1:9999", "null", "https://127.0.0.1:8765", HOST + "/x"])
def test_a_wrong_origin_is_403(cfg: Config, clock: FakeClock, origin: str) -> None:
    client = client_for(cfg, clock)
    resp = post(client, f"/today/{RANKED}/done", origin=origin)
    assert resp.status_code == 403 and "Origin not allowed" in resp.text
    assert not folder(cfg).exists()


def test_an_absent_origin_is_accepted_and_the_right_one_too(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    assert post(client, f"/today/{RANKED}/done", origin=None).status_code == 303
    assert post(client, f"/today/{THREAD}/done", origin=HOST).status_code == 303


def test_a_foreign_host_header_is_403_even_with_a_valid_token(cfg: Config, clock: FakeClock) -> None:
    app = hub_app.create_app(cfg, clock=clock)
    good = TestClient(app, base_url=HOST)
    token = re.search(r'name="csrf" value="([^"]+)"', good.get("/").text).group(1)  # type: ignore[union-attr]
    bad = TestClient(app, base_url="http://evil.example:8765")
    resp = bad.post(f"/today/{RANKED}/done", data={"csrf": token}, headers={"Origin": "http://evil.example:8765"})
    assert resp.status_code == 403 and not folder(cfg).exists()


def test_a_body_that_is_not_a_form_is_415_and_an_oversized_one_is_413(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    resp = client.post(f"/today/{RANKED}/done", content=json.dumps({"csrf": token_of(client)}),
                       headers={"Origin": HOST, "Content-Type": "application/json"})
    assert resp.status_code == 415
    big = client.post(f"/today/{RANKED}/done", content=f"csrf={token_of(client)}&pad=" + "x" * (hub_app.MAX_FORM_BYTES + 10),
                      headers={"Origin": HOST, "Content-Type": "application/x-www-form-urlencoded"})
    assert big.status_code == 413
    assert not folder(cfg).exists()


def test_other_methods_on_the_today_routes_are_405(cfg: Config, clock: FakeClock) -> None:
    client = client_for(cfg, clock)
    for method in ("get", "put", "delete", "patch"):
        for action in ("done", "snooze"):
            assert getattr(client, method)(f"/today/{RANKED}/{action}").status_code == 405, (method, action)
    assert not folder(cfg).exists()


def test_the_form_post_is_allowed_by_the_content_security_policy(cfg: Config, clock: FakeClock) -> None:
    csp = client_for(cfg, clock).get("/").headers["content-security-policy"]
    assert "form-action 'self'" in csp and "default-src 'none'" in csp


# --- the terminal door, and the writer's view of the files -----------------------------------------------------------------


class _Ctx:
    def __init__(self, cfg: Config, clock: FakeClock) -> None:
        from jarvisd import inbox

        self.cfg, self.clock = cfg, clock
        self._audit = inbox.audit_for(cfg, clock)

    def audit(self) -> Any:
        return self._audit


class _Args:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def test_cmd_attend_matches_the_hub_path(cfg: Config, clock: FakeClock, capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _Ctx(cfg, clock)
    assert attention.cmd_attend(ctx, _Args(id=RANKED, done=True, until=None)) == 0
    assert "Done." in capsys.readouterr().out
    assert decision(cfg)["action"] == "done" and decision(cfg)["note"] == JOB
    assert attention.cmd_attend(ctx, _Args(id=THREAD, done=False, until="monday")) == 0
    assert decision(cfg, "confirm the retry budget with the reviewer")["until"] == "2026-10-12"
    assert attention.cmd_attend(ctx, _Args(id=RANKED, done=True, until=None)) == 1  # already done
    assert attention.cmd_attend(ctx, _Args(id=NOT_IN_SIDECAR, done=True, until=None)) == 1
    assert attention.cmd_attend(ctx, _Args(id="8d94ef0d", done=False, until="never")) == 1
    assert len(events(cfg, "attention_decided")) == 2


def test_latest_job_id_picks_the_newest_note_and_its_rerun(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    for name in ("digest-2026-10-05.md", "digest-2026-10-06.md", "digest-2026-10-06-r2.md", "weekly-2026-W40.md", "notes.md"):
        (raw / name).write_text("x", encoding="utf-8")
    assert attention.latest_job_id(raw) == "digest-2026-10-06-r2"
    assert attention.latest_job_id(tmp_path / "missing") is None


def test_the_files_are_what_the_digest_writer_reads(cfg: Config, clock: FakeClock) -> None:
    """The writer side (jarvisd/state.py) lists `state/attention/*.json` with a key and an action; the hub's own
    reader and the writer's must see the same decisions."""
    from jarvisd.state import StateStore

    client = client_for(cfg, clock)
    assert post(client, f"/today/{RANKED}/done").status_code == 303
    assert post(client, f"/today/{THREAD}/snooze", {"until": "3d"}).status_code == 303
    mine = attention.load_decisions(cfg.daemon.state_dir)
    assert set(mine) == {RANKED_KEY, "confirm the retry budget with the reviewer"}
    store = StateStore.from_config(cfg, clock=clock)
    if not hasattr(store, "items"):
        pytest.skip("the writer side of contract section 6 has not landed in this tree yet")
    theirs = {d["key"]: d for d in store.items.decisions()}
    assert set(theirs) == set(mine)
    assert theirs[RANKED_KEY]["action"] == "done" and theirs["confirm the retry budget with the reviewer"]["until"] == "2026-10-09"


def test_no_dashes_and_no_banned_names_in_the_new_module() -> None:
    import ast

    from jarvisd import ROOT

    source = (ROOT / "jarvisd" / "attention.py").read_text(encoding="utf-8")
    assert chr(0x2014) not in source and chr(0x2013) not in source
    tree_ = ast.parse(source)
    imported = {n.module for n in ast.walk(tree_) if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name for n in ast.walk(tree_) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"jarvisd.claude", "subprocess", "socket", "urllib", "urllib.request"}
    # writes go through fsio only
    assert "atomic_write_text" in source and "open(" not in source.replace("os.open", "")
