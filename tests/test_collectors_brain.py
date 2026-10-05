"""Brain and task collectors, window logic and the collector runner (design section 8).

Also covers collectors/__init__.py, because the plan lists no separate test file for it.
Everything is synthetic: a tmp_path vault and a fake ~/.claude directory.
"""
from __future__ import annotations

import builtins
import io
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import CANARY, FakeClock
from jarvisd.collectors import CollectContext, compute_window, run_collectors
from jarvisd.collectors.brain import BrainCollector
from jarvisd.collectors.task import TaskCollector
from jarvisd.config import Config
from jarvisd.models import CollectResult
from jarvisd.render import DigestContext, render_digest
from jarvisd.state import StateStore
from jarvisd.tier import DERIVED_TAG

NOW = datetime(2026, 10, 6, 6, 30, 0, tzinfo=timezone.utc)

RECENT_TEXT = """# Recent (last 7 days)

## Open Threads
- [2026-10-05] Synthetic thread from the sensitive day
- [2026-10-04] Synthetic open thread, waiting on a reviewer
- [2026-09-20] Synthetic stale thread

## Recent Decisions
- [2026-10-05] Synthetic decision from the sensitive day, because it is a fixture
- [2026-10-03] Decision synthetique en francais, parce que c'est un test
"""

SESSION_BODY = """---
type: session
date: {date}
---
## What we built
- nothing real

## Next session entry point
Continue at fixture.py:10 - wire the synthetic thing

## Open threads
- Synthetic follow up one
- Suivi synthetique deux
"""


def make_ctx(cfg: Config, *, now: datetime = NOW, hours: float = 36.0) -> CollectContext:
    return CollectContext(cfg=cfg, window_start=now - timedelta(hours=hours), window_end=now, now=now)


def set_age(path: Path, hours: float, now: datetime = NOW) -> None:
    stamp = (now - timedelta(hours=hours)).timestamp()
    os.utime(path, (stamp, stamp))


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def seed_vault(vault: Path) -> dict[str, Path]:
    """Plant files in every tree the collector must not open, each holding the canary."""
    planted = {
        "notes": write(vault / "notes" / "private-note.md", f"# note\n{CANARY}\n"),
        "insights": write(vault / "insights" / "2026-10-01-x.md", f"# insight\n{CANARY}\n"),
        "raw": write(vault / "raw" / "other.md", f"# raw\n{CANARY}\n"),
        "digest": write(vault / "raw" / "jarvis" / "digest-2026-10-05.md", "generator: jarvisd\n## Open threads\n- x\n"),
        "cp_live": write(vault / "session-checkpoints" / "a.json", f'{{"cwd": "{CANARY}"}}'),
        "cp_done": write(vault / "session-checkpoints" / "processed" / "b.json", "{}"),
        "cp_old": write(vault / "session-checkpoints" / "from-old-machine" / "c.json", "{}"),
    }
    write(vault / "RECENT.md", RECENT_TEXT)
    set_age(vault / "RECENT.md", 4.0)
    return planted


class OpenTracer:
    """Records every file opened through builtins.open or io.open with the calling module.

    Paths are stored as realpaths (long names, lowercase, forward slashes) so the comparison
    does not depend on whether tmp_path came back as an 8.3 short name.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def norm(path: str | os.PathLike[str]) -> str:
        return os.path.realpath(path).replace("\\", "/").lower()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = io.open

        def traced(file, *args, **kwargs):  # type: ignore[no-untyped-def]
            if isinstance(file, (str, os.PathLike)):
                caller = sys._getframe(1).f_globals.get("__name__", "?")
                self.calls.append((self.norm(file), caller))
            return real(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", traced)
        monkeypatch.setattr(io, "open", traced)

    def opened_under(self, root: Path) -> list[tuple[str, str]]:
        prefix = self.norm(root)
        return [(p, c) for p, c in self.calls if p == prefix or p.startswith(prefix + "/")]


# --- brain -----------------------------------------------------------------------------


def test_sensitive_session_withheld_whole_and_never_opened(
    tmp_cfg: Config, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tagged session is withheld whole. The collector itself never opens anything.

    A tag cannot be seen without reading the file, so the one read of a session note is
    tier.safe_read_text, which drops the content on a hit. This test pins both halves: no
    open() from the collector module, and nothing from the file in any result.
    """
    seed_vault(tmp_vault)
    tagged = write(
        tmp_vault / "sessions" / "2026-10-05-09.md",
        SESSION_BODY.format(date="2026-10-05").replace("type: session", "type: session\ntags: [sensitive]")
        + f"\n{CANARY}\n",
    )
    clean = write(tmp_vault / "sessions" / "2026-10-05-11.md", SESSION_BODY.format(date="2026-10-05"))
    set_age(tagged, 5.0)
    set_age(clean, 3.0)
    # The telos canary from the fixture must stay unread as well.
    tracer = OpenTracer()
    tracer.install(monkeypatch)

    result = BrainCollector().collect(make_ctx(tmp_cfg))
    monkeypatch.undo()

    assert result.ok, result.error
    blob = result.model_dump_json()
    assert CANARY not in blob
    assert [w.kind for w in result.withheld] == ["brain_session"]
    withheld = result.withheld[0]
    assert withheld.id.startswith("w-")
    assert withheld.hold_kind == "sensitive"
    assert result.facts["withheld_count"] == 1
    assert result.facts["new_sessions"] == 1  # only the clean one was read

    # No item came from the tagged session.
    assert all("sensitive" not in (item.meta.get("session") or "") for item in result.items)
    session_items = [i for i in result.items if i.kind == "brain_session"]
    assert {i.meta["session"] for i in session_items} == {"2026-10-05-11"}

    # Same-date RECENT bullets are tainted, other dates are not.
    by_date = {(i.meta["date"], i.kind): i for i in result.items if i.kind in ("brain_thread", "brain_decision")}
    assert DERIVED_TAG in by_date[("2026-10-05", "brain_thread")].tags
    assert DERIVED_TAG in by_date[("2026-10-05", "brain_decision")].tags
    assert DERIVED_TAG not in by_date[("2026-10-04", "brain_thread")].tags

    # Tracer: nothing under the forbidden trees or the checkpoints, and no session file or
    # RECENT.md was opened by anyone except tier.
    for tree in ("telos", "notes", "insights", "raw", "session-checkpoints"):
        assert tracer.opened_under(tmp_vault / tree) == [], tree
    for path, caller in tracer.opened_under(tmp_vault):
        assert caller == "jarvisd.tier", (path, caller)
    opened = {p for p, _ in tracer.opened_under(tmp_vault / "sessions")}
    assert OpenTracer.norm(tagged) in opened  # read by tier only, content dropped


def test_recent_bullets_become_items_with_stable_ids_and_stale_flags(tmp_cfg: Config, tmp_vault: Path) -> None:
    seed_vault(tmp_vault)
    first = BrainCollector().collect(make_ctx(tmp_cfg))
    second = BrainCollector().collect(make_ctx(tmp_cfg))
    assert first.ok and second.ok

    threads = [i for i in first.items if i.kind == "brain_thread"]
    decisions = [i for i in first.items if i.kind == "brain_decision"]
    assert len(threads) == 3 and len(decisions) == 2
    assert [i.id for i in first.items] == [i.id for i in second.items]
    assert len({i.id for i in first.items}) == len(first.items)
    assert all(i.source == "brain" and len(i.id) == 8 for i in first.items)

    stale = {i.meta["date"]: i.meta["stale"] for i in threads}
    assert stale == {"2026-10-05": False, "2026-10-04": False, "2026-09-20": True}
    assert "stale" in next(i for i in threads if i.meta["date"] == "2026-09-20").tags
    french = next(i for i in decisions if "francais" in i.text)
    assert french.meta["date"] == "2026-10-03"


def test_new_sessions_become_items_and_jarvis_notes_are_excluded(tmp_cfg: Config, tmp_vault: Path) -> None:
    seed_vault(tmp_vault)
    fresh = write(tmp_vault / "sessions" / "2026-10-05-20.md", SESSION_BODY.format(date="2026-10-05"))
    old = write(tmp_vault / "sessions" / "2026-09-30-08.md", SESSION_BODY.format(date="2026-09-30"))
    ours = write(tmp_vault / "sessions" / "jarvis-digest-note.md", SESSION_BODY.format(date="2026-10-05"))
    set_age(fresh, 2.0)
    set_age(old, 200.0)
    set_age(ours, 1.0)

    result = BrainCollector().collect(make_ctx(tmp_cfg))
    sessions = [i for i in result.items if i.kind == "brain_session"]

    assert {i.meta["session"] for i in sessions} == {"2026-10-05-20"}
    sections = {i.meta["section"] for i in sessions}
    assert sections == {"entry_point", "open_thread"}
    entry = next(i for i in sessions if i.meta["section"] == "entry_point")
    assert entry.text.startswith("Continue at fixture.py:10")
    assert len([i for i in sessions if i.meta["section"] == "open_thread"]) == 2
    assert result.facts["new_sessions"] == 1


def test_checkpoints_counted_not_opened(tmp_cfg: Config, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_vault(tmp_vault)
    write(tmp_vault / "session-checkpoints" / "second.json", "{}")
    write(tmp_vault / "session-checkpoints" / "notes.txt", "not a checkpoint")
    tracer = OpenTracer()
    tracer.install(monkeypatch)
    result = BrainCollector().collect(make_ctx(tmp_cfg))
    monkeypatch.undo()
    assert result.facts["orphan_checkpoints"] == 2  # a.json and second.json only
    assert tracer.opened_under(tmp_vault / "session-checkpoints") == []


def test_stale_recent_is_reported(tmp_cfg: Config, tmp_vault: Path) -> None:
    seed_vault(tmp_vault)
    set_age(tmp_vault / "RECENT.md", 40.0)
    result = BrainCollector().collect(make_ctx(tmp_cfg))
    assert result.ok
    assert result.facts["recent_stale"] is True
    assert result.facts["recent_age_hours"] == 40.0
    assert "recent_mtime" in result.facts

    set_age(tmp_vault / "RECENT.md", 4.0)
    fresh = BrainCollector().collect(make_ctx(tmp_cfg))
    assert fresh.facts["recent_stale"] is False
    assert fresh.facts["recent_age_hours"] == 4.0


def test_missing_recent_is_a_fact_and_missing_vault_is_a_failure(tmp_cfg: Config, tmp_vault: Path) -> None:
    (tmp_vault / "RECENT.md").unlink()
    result = BrainCollector().collect(make_ctx(tmp_cfg))
    assert result.ok and result.facts["recent_missing"] is True and result.items == []

    broken = tmp_cfg.model_copy(deep=True)
    broken.paths.vault_write_sessions = tmp_vault.parent / "nowhere" / "sessions"
    failed = BrainCollector().collect(make_ctx(broken))
    assert not failed.ok and failed.error


def test_old_sensitive_session_still_taints_its_date(tmp_cfg: Config, tmp_vault: Path) -> None:
    """A tagged session older than the window is not an item, but its date still taints RECENT."""
    seed_vault(tmp_vault)
    old = write(
        tmp_vault / "sessions" / "2026-10-05-01.md",
        SESSION_BODY.format(date="2026-10-05").replace("type: session", "type: session\nsensitive: true"),
    )
    set_age(old, 100.0)  # well before the window start
    result = BrainCollector().collect(make_ctx(tmp_cfg))
    tainted = [i for i in result.items if DERIVED_TAG in i.tags]
    assert {i.meta["date"] for i in tainted} == {"2026-10-05"}
    assert len(tainted) == 2
    assert result.facts["withheld_count"] == 0  # not in the window, so not a digest-visible withheld


def test_sensitive_recent_is_withheld_whole(tmp_cfg: Config, tmp_vault: Path) -> None:
    seed_vault(tmp_vault)
    write(tmp_vault / "RECENT.md", RECENT_TEXT + "\n- [2026-10-05] a #private aside\n")
    result = BrainCollector().collect(make_ctx(tmp_cfg))
    assert result.ok
    assert [i for i in result.items if i.kind in ("brain_thread", "brain_decision")] == []
    assert result.facts["recent_withheld"] is True
    assert [w.kind for w in result.withheld] == ["brain_recent"]


def _with_terms(cfg: Config, *terms: str) -> Config:
    copy = cfg.model_copy(deep=True)
    copy.gates.sensitive_terms = list(terms)
    return copy


TERM = "zephyr cafe"
RECENT_ONE_TERM = f"""# Recent (last 7 days)

## Open Threads
- [2026-10-04] Synthetic open thread, waiting on a reviewer
- [2026-10-04] Meet at the {TERM} about the lease
- [2026-10-03] Another synthetic thread

## Recent Decisions
"""


def test_term_hit_withholds_only_its_bullet(tmp_cfg: Config, tmp_vault: Path) -> None:
    seed_vault(tmp_vault)
    write(tmp_vault / "RECENT.md", RECENT_ONE_TERM)
    set_age(tmp_vault / "RECENT.md", 4.0)
    cfg = _with_terms(tmp_cfg, "unrelated", TERM)

    result = BrainCollector().collect(make_ctx(cfg))

    assert result.ok, result.error
    threads = [i for i in result.items if i.kind == "brain_thread"]
    assert len(threads) == 2
    held = [w for w in result.withheld if w.kind == "brain_recent"]
    assert len(held) == 1
    assert held[0].reason == "term:1" and held[0].hold_kind == "sensitive"
    assert held[0].source_ref.endswith("RECENT.md#thread-2026-10-04-2")
    assert result.facts["recent_withheld"] is False
    assert result.facts["recent_held_bullets"] == 1

    # The withheld bullet text appears nowhere, neither in items nor in any reference field.
    blob = result.model_dump_json().casefold()
    assert TERM not in blob and "lease" not in blob

    # Ids and refs are stable across runs.
    again = BrainCollector().collect(make_ctx(cfg))
    assert [w.model_dump() for w in again.withheld] == [w.model_dump() for w in result.withheld]


def test_file_level_tag_still_withholds_every_bullet(tmp_cfg: Config, tmp_vault: Path) -> None:
    seed_vault(tmp_vault)
    write(tmp_vault / "RECENT.md", "---\ntags: [medical]\n---\n" + RECENT_ONE_TERM)
    result = BrainCollector().collect(make_ctx(_with_terms(tmp_cfg, TERM)))
    assert [i for i in result.items if i.kind in ("brain_thread", "brain_decision")] == []
    assert result.facts["recent_withheld"] is True
    assert [(w.kind, w.reason) for w in result.withheld] == [("brain_recent", "tag_frontmatter")]


def test_term_in_non_bullet_text_does_not_hide_clean_bullets(tmp_cfg: Config, tmp_vault: Path) -> None:
    seed_vault(tmp_vault)
    clean_lines = [ln for ln in RECENT_ONE_TERM.splitlines() if "lease" not in ln]
    text = "\n".join(clean_lines).replace("# Recent (last 7 days)", f"# Recent {TERM}") + "\n"
    write(tmp_vault / "RECENT.md", text)
    result = BrainCollector().collect(make_ctx(_with_terms(tmp_cfg, TERM)))
    assert len([i for i in result.items if i.kind == "brain_thread"]) == 2
    assert TERM not in result.model_dump_json().casefold()
    assert result.withheld == []


def test_session_bullet_with_term_is_held_alone(tmp_cfg: Config, tmp_vault: Path) -> None:
    seed_vault(tmp_vault)
    body = SESSION_BODY.format(date="2026-10-05").replace("Suivi synthetique deux", f"Call about the {TERM}")
    note = write(tmp_vault / "sessions" / "2026-10-05-20.md", body)
    set_age(note, 2.0)
    result = BrainCollector().collect(make_ctx(_with_terms(tmp_cfg, TERM)))
    sessions = [i for i in result.items if i.kind == "brain_session"]
    assert {i.meta["section"] for i in sessions} == {"entry_point", "open_thread"}
    assert len([i for i in sessions if i.meta["section"] == "open_thread"]) == 1
    held = [w for w in result.withheld if w.kind == "brain_session"]
    assert len(held) == 1 and held[0].reason == "term:0"
    assert "#open_thread-2" in held[0].source_ref
    assert TERM not in result.model_dump_json().casefold()
    assert result.facts["new_sessions"] == 1
    # A term hit holds its own line only: the same-date RECENT.md bullets are not tainted,
    # because each of them is term-scanned on its own.
    same_day = [i for i in result.items if i.kind in ("brain_thread", "brain_decision") and i.meta["date"] == "2026-10-05"]
    assert same_day, "fixture RECENT.md must carry 2026-10-05 bullets for this assertion"
    assert all(DERIVED_TAG not in i.tags for i in same_day)


def test_digest_lists_held_bullet_by_id_and_reason_only(tmp_cfg: Config, tmp_vault: Path) -> None:
    from datetime import date as _date

    seed_vault(tmp_vault)
    write(tmp_vault / "RECENT.md", RECENT_ONE_TERM)
    set_age(tmp_vault / "RECENT.md", 4.0)
    cfg = _with_terms(tmp_cfg, TERM)
    result = BrainCollector().collect(make_ctx(cfg))
    ref = next(w for w in result.withheld if w.kind == "brain_recent")

    ctx = DigestContext(
        job_id="digest-2026-10-06",
        day=_date(2026, 10, 6),
        generated_at=NOW,
        window_start=NOW - timedelta(hours=36),
        window_end=NOW,
        results={"brain": result},
        held=list(result.withheld),
        claude_status="no_items",
    )
    text = render_digest(ctx)
    held_part = text.split("## Held back and not summarized")[1].split("\n## ")[0]
    assert ref.id in held_part and "term:0" in held_part
    assert TERM not in text.casefold() and "lease" not in text.casefold()
    assert "Another synthetic thread" in text  # the clean bullets still render


# --- task ------------------------------------------------------------------------------


def make_task_dir(base: Path, **overrides: str | None) -> Path:
    values: dict[str, str | None] = {
        "current-task": "abc123xyz",
        "current-task-name": "fix(parser): synthetic task name",
        "current-task-status": "IN REVIEW",
        "current-task-step": "in_review",
        "current-task-due": "2026-10-05T07:07:19Z",
    }
    values.update(overrides)
    base.mkdir(parents=True, exist_ok=True)
    for name, value in values.items():
        if value is not None:
            write(base / name, value + "\n")
    write(base / ".credentials.json", f'{{"token": "{CANARY}"}}')
    write(base / "clickup-config.json", f'{{"api_token": "{CANARY}"}}')
    return base


def test_task_opens_exactly_the_five_named_files(
    tmp_cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = make_task_dir(tmp_path / "dot-claude")
    tracer = OpenTracer()
    tracer.install(monkeypatch)
    result = TaskCollector(base_dir=base).collect(make_ctx(tmp_cfg))
    monkeypatch.undo()

    names = sorted(Path(p).name for p, _ in tracer.opened_under(base))
    assert names == sorted(
        ["current-task", "current-task-name", "current-task-status", "current-task-step", "current-task-due"]
    )
    assert CANARY not in result.model_dump_json()
    assert result.ok and len(result.items) == 1
    item = result.items[0]
    assert item.kind == "active_task" and item.source == "task" and item.work is True
    assert item.meta["task_id"] == "abc123xyz"
    assert item.meta["status"] == "IN REVIEW"
    assert item.title == "fix(parser): synthetic task name"


@pytest.mark.parametrize(
    ("due", "state"),
    [
        ("2026-10-05T07:07:19Z", "overdue"),
        ("2026-10-06T23:00:00+00:00", "today"),
        ("2026-10-09", "later"),
        ("", "none"),
        (None, "none"),
        ("not a date", "none"),
    ],
)
def test_task_due_state(tmp_cfg: Config, tmp_path: Path, due: str | None, state: str) -> None:
    base = make_task_dir(tmp_path / "c", **{"current-task-due": due})
    result = TaskCollector(base_dir=base).collect(make_ctx(tmp_cfg))
    assert result.items[0].meta["due_state"] == state


def test_task_missing_files_are_handled(tmp_cfg: Config, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    none = TaskCollector(base_dir=empty).collect(make_ctx(tmp_cfg))
    assert none.ok and none.items == [] and none.facts["task_active"] is False

    partial = make_task_dir(
        tmp_path / "partial", **{"current-task-name": None, "current-task-status": None, "current-task-step": None}
    )
    result = TaskCollector(base_dir=partial).collect(make_ctx(tmp_cfg))
    assert result.ok and len(result.items) == 1
    assert result.items[0].title == "abc123xyz"
    assert result.items[0].meta["status"] == ""


def test_task_with_a_sensitive_name_is_withheld(tmp_cfg: Config, tmp_path: Path) -> None:
    base = make_task_dir(tmp_path / "c", **{"current-task-name": "plan #private trip"})
    result = TaskCollector(base_dir=base).collect(make_ctx(tmp_cfg))
    assert result.items == []
    assert [w.kind for w in result.withheld] == ["task_field"]


# --- window and runner -----------------------------------------------------------------


def test_compute_window(tmp_cfg: Config, tmp_path: Path, clock: FakeClock) -> None:
    state = StateStore.from_config(tmp_cfg, clock=clock)
    now = clock()
    start, end = compute_window(state, tmp_cfg, now)
    assert end == now and now - start == timedelta(hours=tmp_cfg.digest.window_hours_default)

    state.watermark.advance(now - timedelta(hours=10), "digest-x")
    start, _ = compute_window(state, tmp_cfg, now)
    assert now - start == timedelta(hours=10)

    far = StateStore(tmp_path / "far", clock=clock)
    far.watermark.advance(now - timedelta(days=9), "digest-old")
    start, _ = compute_window(far, tmp_cfg, now)
    assert now - start == timedelta(hours=tmp_cfg.digest.window_hours_max)

    ahead = StateStore(tmp_path / "ahead", clock=clock)
    ahead.watermark.advance(now + timedelta(hours=5), "digest-future")
    start, end = compute_window(ahead, tmp_cfg, now)
    assert start <= end

    with pytest.raises(ValueError):
        compute_window(state, tmp_cfg, datetime(2026, 1, 1))  # naive


class _Boom:
    name = "boom"

    def collect(self, ctx: CollectContext) -> CollectResult:
        raise RuntimeError(f"leaky detail {CANARY}")


class _Slow:
    name = "slow"

    def collect(self, ctx: CollectContext) -> CollectResult:
        time.sleep(2.0)
        return CollectResult(source="slow", ok=True)


class _Fine:
    name = "fine"

    def collect(self, ctx: CollectContext) -> CollectResult:
        return CollectResult(source="fine", ok=True, facts={"n": 1})


class _Lies:
    name = "lies"

    def collect(self, ctx: CollectContext) -> CollectResult:
        return {"source": "lies"}  # type: ignore[return-value]


def test_run_collectors_converts_exceptions_and_timeouts(tmp_cfg: Config) -> None:
    ctx = make_ctx(tmp_cfg)
    results = run_collectors(ctx, [_Fine(), _Boom(), _Slow(), _Lies()], timeout_s=0.2)
    assert [r.source for r in results] == ["fine", "boom", "slow", "lies"]
    fine, boom, slow, lies = results
    assert fine.ok and fine.facts == {"n": 1}
    assert not boom.ok and boom.error == "RuntimeError"
    assert CANARY not in boom.model_dump_json()  # exception text is never copied
    assert not slow.ok and slow.error.startswith("timeout")
    assert not lies.ok and lies.error == "bad_result"
    assert all(r.duration_ms >= 0 for r in results)
