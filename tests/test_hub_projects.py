"""The hub's Projects and Ledger views (Q3): read-only, derived from digests, proposals and run manifests.

The tree is synthetic: repo names are made up and every file is written into tmp_path.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvisd.config import Config, RepoCfg  # noqa: E402
from jarvisd.hub import app as hub_app  # noqa: E402
from jarvisd.hub.data import HubData  # noqa: E402
from conftest import FakeClock  # noqa: E402

HOST = "http://127.0.0.1:8765"


def _client(cfg: Config, clock: FakeClock) -> TestClient:
    return TestClient(hub_app.create_app(cfg, clock=clock), base_url=HOST)


def _digest(cfg: Config, day: str, repos: list[str], task: str = "- No active task recorded.") -> None:
    raw = Path(cfg.paths.vault_write_raw)
    raw.mkdir(parents=True, exist_ok=True)
    text = (f"---\ntype: jarvis-digest\njob_id: digest-{day}\ndate: {day}\nstatus: complete\n---\n"
            f"# Digest {day}\n\n## Active task\n{task}\n\n## Repos\n" + "\n".join(repos) + "\n\n## Held back\n- none\n")
    (raw / f"digest-{day}.md").write_text(text, encoding="utf-8", newline="\n")


def _proposal(cfg: Config, name: str, **fields: Any) -> Path:
    folder = cfg.daemon.state_dir / "proposals"
    folder.mkdir(parents=True, exist_ok=True)
    body = {"id": name, "created_at": "2026-10-01T06:00:00+00:00", "run_id": "digest-2026-10-01", "title": f"Title {name}",
            "project": "alpha-repo", "kind": "task", "evidence": ["abcd1234"], "suggested_status": "BACKLOG",
            "due_hint": None, "rationale": "because", "status": "proposed", "tracker_ref": None,
            "rejected_reason": None, "edits": {}}
    body.update(fields)
    path = folder / f"{name}.json"
    path.write_text(json.dumps(body), encoding="utf-8", newline="\n")
    return path


def _stamp(path: Path, when: str) -> None:
    ts = datetime.fromisoformat(when).replace(tzinfo=timezone.utc).timestamp()
    os.utime(path, (ts, ts))


def _manifest(cfg: Config, job_id: str, status: str, finished: str, cost: float, note: str | None) -> None:
    run = cfg.daemon.state_dir / "runs" / job_id
    run.mkdir(parents=True, exist_ok=True)
    data = {"job_id": job_id, "status": status, "started_at": finished, "finished_at": finished, "cost_usd": cost,
            "paths": {"note": note} if note else {}}
    (run / "run.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")


@pytest.fixture
def cfg(tmp_cfg: Config) -> Config:
    c = tmp_cfg.model_copy(deep=True)
    c.digest.repos = [RepoCfg(name="alpha-repo", path=Path("nowhere/a")),
                      RepoCfg(name="beta-repo", path=Path("nowhere/b"), work=True)]
    c.hub.task_projects = {"beta-repo": ["widget"]}
    return c


@pytest.fixture
def history(cfg: Config) -> Config:
    _digest(cfg, "2026-09-10", ["- alpha-repo branch main, 2 commits since window, 0 modified, 0 untracked [aaaa0001]",
                                "- Quiet: beta-repo."])
    _digest(cfg, "2026-10-04", ["- beta-repo branch dev, 0 commits since window, 1 modified, 0 untracked [bbbb0001]",
                                "- Quiet: alpha-repo."])
    _digest(cfg, "2026-10-05", ["- beta-repo branch dev, 0 commits since window, 2 modified, 0 untracked [bbbb0002]",
                                "- Quiet: alpha-repo."])
    _digest(cfg, "2026-10-06", [
        "- beta-repo (work) branch dev, 0 commits since window, 3 modified, 1 untracked [bbbb0003]",
        "- Quiet: alpha-repo.",
        "- GitHub beta-repo (work): 1 open PR (1 yours, 0 awaiting your review), CI failure on main: Fix it [cccc0001]",
        "- GitHub quiet: alpha-repo (CI success, 1 stale branch)."],
        task="- 1234 Fix the widget, status IN REVIEW, due 2026-10-05 (OVERDUE). No ClickUp call was made (v1).")
    return cfg


def _rows(cfg: Config, clock: FakeClock) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in HubData(cfg, clock).projects()}


# --- Projects ---------------------------------------------------------------------------------------


def test_both_views_render_with_zero_repos_and_no_state(tmp_cfg: Config, clock: FakeClock) -> None:
    client = _client(tmp_cfg, clock)
    for path in ("/projects", "/ledger"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert 'href="/projects"' in resp.text and 'href="/ledger"' in resp.text
    assert "No repositories" in client.get("/projects").text
    assert "Nothing delivered" in client.get("/ledger").text


def test_rows_follow_config_order_with_digest_facts(history: Config, clock: FakeClock) -> None:
    rows = HubData(history, clock).projects()
    assert [r["name"] for r in rows] == ["alpha-repo", "beta-repo"]
    alpha, beta = rows
    assert beta["work"] is True and alpha["work"] is False
    assert (beta["branch"], beta["commits"], beta["modified"], beta["untracked"]) == ("dev", 0, 3, 1)
    assert (alpha["commits"], alpha["modified"], alpha["untracked"]) == (0, 0, 0)
    assert beta["prs"] == 1 and beta["ci"] == "failure"
    assert alpha["prs"] == 0 and alpha["ci"] == "success"
    assert "Fix the widget" in beta["task"] and alpha["task"] == ""
    body = _client(history, clock).get("/projects").text
    assert "alpha-repo" in body and "beta-repo" in body and "failure" in body


def test_proposal_counts_absent_and_present(history: Config, clock: FakeClock) -> None:
    assert {r["open_proposals"] for r in HubData(history, clock).projects()} == {0}
    assert not (history.daemon.state_dir / "proposals").exists()
    _proposal(history, "p1")
    _proposal(history, "p2", project="beta-repo")
    _proposal(history, "p3", project="beta-repo", status="confirmed", tracker_ref="https://tracker.example.test/t/1")
    _proposal(history, "p4", project="general")
    (history.daemon.state_dir / "proposals" / "broken.json").write_text("{nope", encoding="utf-8")
    rows = _rows(history, clock)
    assert rows["alpha-repo"]["open_proposals"] == 1
    assert rows["beta-repo"]["open_proposals"] == 1


def test_stale_badge_uses_stale_days(history: Config, clock: FakeClock) -> None:
    rows = _rows(history, clock)
    assert rows["alpha-repo"]["days_since"] == 26 and rows["alpha-repo"]["stale"] is True
    assert rows["beta-repo"]["days_since"] == 0 and rows["beta-repo"]["stale"] is False
    assert any("stale" in r for r in rows["alpha-repo"]["risks"])
    history.hub.stale_days = 30
    assert _rows(history, clock)["alpha-repo"]["stale"] is False
    history.hub.stale_days = 26
    assert _rows(history, clock)["alpha-repo"]["stale"] is True  # the limit itself counts
    assert "stale" in _client(history, clock).get("/projects").text.lower()


def test_derived_risks(history: Config, clock: FakeClock) -> None:
    beta = _rows(history, clock)["beta-repo"]
    text = " | ".join(beta["risks"])
    assert "uncommitted" in text and "CI failing" in text and "overdue" in text
    assert beta["dirty_days"] == 2
    clock.set(datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc))
    assert not any("uncommitted" in r for r in _rows(history, clock)["beta-repo"]["risks"])  # dirty only since 10-04


def test_a_repo_without_digest_facts_has_no_invented_numbers(cfg: Config, clock: FakeClock) -> None:
    row = HubData(cfg, clock).projects()[0]
    assert row["known"] is False and row["days_since"] is None and row["stale"] is False and row["risks"] == []
    assert "no digest data" in _client(cfg, clock).get("/projects").text.lower()


# --- Ledger -----------------------------------------------------------------------------------------


@pytest.fixture
def delivered(history: Config) -> Config:
    p1 = _proposal(history, "p1", status="confirmed", tracker_ref="https://tracker.example.test/t/1", title="First task")
    p2 = _proposal(history, "p2", status="edited_confirmed", tracker_ref="https://tracker.example.test/t/2")
    _proposal(history, "p3", status="rejected")
    _proposal(history, "p4", status="proposed")
    _proposal(history, "p5", status="confirmed", tracker_ref=None)
    _stamp(p1, "2026-10-05T10:00:00")
    _stamp(p2, "2026-09-20T10:00:00")
    _manifest(history, "digest-2026-10-06", "complete", "2026-10-06T05:31:00+00:00", 0.03, "raw/jarvis/digest-2026-10-06.md")
    _manifest(history, "digest-2026-09-10", "complete", "2026-09-10T05:31:00+00:00", 0.02, "raw/jarvis/digest-2026-09-10.md")
    _manifest(history, "digest-2026-10-01", "failed", "2026-10-01T05:31:00+00:00", 0.5, None)
    _manifest(history, "consolidate-2026-10-02", "written", "2026-10-02T02:10:00+00:00", 0.01,
              "raw/jarvis/candidates-2026-10-02.md")
    return history


def test_ledger_is_newest_first_and_skips_undelivered(delivered: Config, clock: FakeClock) -> None:
    led = HubData(delivered, clock).ledger()
    stamps = [e["stamp"] for e in led["entries"]]
    assert stamps == sorted(stamps, reverse=True)
    kinds = [e["kind"] for e in led["entries"]]
    assert kinds.count("proposal") == 2 and kinds.count("digest") == 2 and kinds.count("consolidation") == 1
    assert led["entries"][0]["kind"] == "digest" and led["entries"][0]["ref"] == "digest-2026-10-06"
    props = {e["ref"]: e for e in led["entries"] if e["kind"] == "proposal"}
    assert set(props) == {"p1", "p2"} and props["p1"]["cost"] is None
    assert props["p1"]["links"] == ["https://tracker.example.test/t/1"]


def test_monthly_rollup_sums(delivered: Config, clock: FakeClock) -> None:
    roll = {r["month"]: r for r in HubData(delivered, clock).ledger()["rollup"]}
    assert list(roll) == ["2026-10", "2026-09"]
    assert roll["2026-10"]["count"] == 3 and roll["2026-10"]["cost"] == pytest.approx(0.04)
    assert roll["2026-09"]["count"] == 2 and roll["2026-09"]["cost"] == pytest.approx(0.02)
    assert roll["2026-10"]["proposals"] == 1 and roll["2026-10"]["digests"] == 1 and roll["2026-10"]["notes"] == 1


def test_ledger_page_shows_links_and_escapes(delivered: Config, clock: FakeClock) -> None:
    _proposal(delivered, "p9", status="confirmed", tracker_ref="https://tracker.example.test/t/<b>", title="<script>x</script>")
    body = _client(delivered, clock).get("/ledger").text
    assert 'href="/digest/digest-2026-10-06"' in body
    assert 'href="https://tracker.example.test/t/1"' in body and "candidates-2026-10-02.md" in body
    assert "<script>x</script>" not in body and "<b>" not in body
    assert "2026-10" in body and "0.0400" in body


def test_javascript_tracker_refs_are_not_linked(delivered: Config, clock: FakeClock) -> None:
    _proposal(delivered, "p8", status="confirmed", tracker_ref="javascript:alert(1)")
    body = _client(delivered, clock).get("/ledger").text
    assert 'href="javascript' not in body


def test_views_never_write_and_refuse_post(delivered: Config, clock: FakeClock, tmp_path: Path) -> None:
    def snap() -> dict[str, tuple[int, int]]:
        return {p.as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    before = snap()
    client = _client(delivered, clock)
    for path in ("/projects", "/ledger"):
        assert client.get(path).status_code == 200
        assert client.post(path).status_code == 405
    assert snap() == before
