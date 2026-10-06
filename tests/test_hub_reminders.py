"""The hub's Reminders view: due dates of accepted and open proposals, grouped by how late they are.

Read-only and offline: synthetic proposals written into tmp_path, a fixed clock (2026-10-06).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvisd.config import Config  # noqa: E402
from jarvisd.hub import app as hub_app  # noqa: E402
from jarvisd.hub.data import HubData  # noqa: E402
from conftest import FakeClock  # noqa: E402

HOST = "http://127.0.0.1:8765"


def _proposal(cfg: Config, name: str, **fields: Any) -> None:
    folder = cfg.daemon.state_dir / "proposals"
    folder.mkdir(parents=True, exist_ok=True)
    body = {"id": name, "created_at": "2026-10-01T06:00:00+00:00", "run_id": "digest-2026-10-01", "title": f"Title {name}",
            "project": "alpha-repo", "kind": "task", "evidence": ["abcd1234"], "suggested_status": "BACKLOG",
            "due_hint": None, "rationale": "because", "status": "proposed", "tracker_ref": None,
            "rejected_reason": None, "edits": {}}
    body.update(fields)
    (folder / f"{name}.json").write_text(json.dumps(body), encoding="utf-8", newline="\n")


def _rows(cfg: Config, clock: FakeClock) -> dict[str, dict[str, Any]]:
    return {r["id"]: r for r in HubData(cfg, clock).reminders()}


def test_empty_state_renders(tmp_cfg: Config, clock: FakeClock) -> None:
    resp = TestClient(hub_app.create_app(tmp_cfg, clock=clock), base_url=HOST).get("/reminders")
    assert resp.status_code == 200
    assert "No reminders" in resp.text and 'href="/reminders"' in resp.text


def test_buckets_and_sources(tmp_cfg: Config, clock: FakeClock) -> None:
    _proposal(tmp_cfg, "p-late", status="confirmed", due_hint="2026-10-04")
    _proposal(tmp_cfg, "p-today", status="edited_confirmed", due_hint="2026-10-30", edits={"due": "2026-10-06"})
    _proposal(tmp_cfg, "p-soon", status="proposed", due_hint="2026-10-09")
    _proposal(tmp_cfg, "p-later", status="confirmed", due_hint="2026-12-01")
    _proposal(tmp_cfg, "p-none", status="confirmed")
    _proposal(tmp_cfg, "p-rejected", status="rejected", due_hint="2026-10-05", rejected_reason="no")
    rows = _rows(tmp_cfg, clock)
    assert set(rows) == {"p-late", "p-today", "p-soon", "p-later"}
    assert rows["p-late"]["bucket"] == "overdue" and rows["p-late"]["days"] == -2
    assert rows["p-today"]["bucket"] == "today" and rows["p-today"]["due"] == "2026-10-06"  # the edit wins
    assert rows["p-soon"]["bucket"] == "soon" and rows["p-soon"]["accepted"] is False
    assert rows["p-later"]["bucket"] == "later" and rows["p-later"]["accepted"] is True
    order = [r["id"] for r in HubData(tmp_cfg, clock).reminders()]
    assert order == ["p-late", "p-today", "p-soon", "p-later"]


def test_bad_files_are_skipped(tmp_cfg: Config, clock: FakeClock) -> None:
    _proposal(tmp_cfg, "p-ok", status="confirmed", due_hint="2026-10-07")
    _proposal(tmp_cfg, "p-baddate", status="confirmed", due_hint="not a date")
    (tmp_cfg.daemon.state_dir / "proposals" / "junk.json").write_text("[1]", encoding="utf-8")
    assert set(_rows(tmp_cfg, clock)) == {"p-ok"}


def test_view_escapes_and_is_read_only(tmp_cfg: Config, clock: FakeClock) -> None:
    _proposal(tmp_cfg, "p-x", status="confirmed", due_hint="2026-10-05", title="<script>alert(1)</script>",
              tracker_ref="javascript:alert(1)")
    _proposal(tmp_cfg, "p-y", status="confirmed", due_hint="2026-10-20", tracker_ref="https://example.invalid/t/1")
    tree = sorted(str(p) for p in Path(tmp_cfg.daemon.state_dir).rglob("*"))
    client = TestClient(hub_app.create_app(tmp_cfg, clock=clock), base_url=HOST)
    body = client.get("/reminders").text
    assert "<script>alert" not in body and "&lt;script&gt;" in body
    assert 'href="javascript:' not in body and 'href="https://example.invalid/t/1"' in body
    assert "Overdue" in body
    assert client.post("/reminders").status_code == 405
    assert sorted(str(p) for p in Path(tmp_cfg.daemon.state_dir).rglob("*")) == tree
