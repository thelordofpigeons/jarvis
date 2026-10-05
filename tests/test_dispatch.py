"""Gate order, sealing and the local seam (design section 6, plan T4)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jarvisd import dispatch
from jarvisd.audit import AuditLog
from jarvisd.config import Config
from jarvisd.dispatch import (
    LOCAL_BACKENDS,
    GatedPayload,
    PayloadBlocked,
    clear_for_claude,
    decide,
    local_state,
    run_gates,
)
from jarvisd.models import GateResult, Item, RouterDecision, TierHit
from jarvisd.router import ROUTERS, Router, StubRouter, build_router

SENS = TierHit(kind="sensitive", code="glob_floor", where="path")
POLICY = TierHit(kind="policy", code="work_policy", where="item")
STATES = ("not_installed", "unavailable", "up")


def _item(item_id: str = "i1", title: str = "Weekly notes", text: str = "body", **kw: Any) -> Item:
    return Item(id=item_id, source="brain", kind=kw.pop("kind", "brain_thread"), title=title, text=text, **kw)


def _dec(*, sensitive: bool = False, importance: str = "low", confidence: float = 0.99,
         category: str = "brain_thread") -> RouterDecision:
    return RouterDecision(category=category, sensitive=sensitive, importance=importance,  # type: ignore[arg-type]
                          confidence=confidence, needs_tools=[], language="en", reason="t")


# (name, hit, decision kwargs or None, state) -> (route, decided_by, hold_kind, degraded, confirm)
def _rows() -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    # Gate 1 alone, and gate 1 beating everything the router could say.
    for state in STATES:
        degraded = state == "unavailable"
        route = "local" if state == "up" else "held"
        rows.append((f"tier-sens-{state}", SENS, None, state, route, "tier", None if route == "local" else "sensitive", degraded, False))
        rows.append((f"tier-sens-beats-high-{state}", SENS, {"importance": "high"}, state, route, "tier",
                     None if route == "local" else "sensitive", degraded, False))
        rows.append((f"tier-policy-{state}", POLICY, None, state, "held", "tier", "policy", False, False))
    # Router flag adds sensitivity, never removes it, and outranks importance.
    for state in STATES:
        rows.append((f"router-sens-{state}", None, {"sensitive": True}, state, "held", "tier", "sensitive", False, False))
    rows.append(("router-sens-beats-high", None, {"sensitive": True, "importance": "high"}, "up",
                 "held", "tier", "sensitive", False, False))
    # Gate 2: importance, with confirm_required, regardless of confidence and local state.
    for state in STATES:
        rows.append((f"high-{state}", None, {"importance": "high"}, state, "claude", "importance", None, False, True))
    rows.append(("escalate-category", None, {"category": "financial"}, "not_installed", "claude", "importance", None, False, True))
    rows.append(("high-beats-low-confidence", None, {"importance": "high", "confidence": 0.1}, "not_installed",
                 "claude", "importance", None, False, True))
    # Gate 3: confidence.
    for state in STATES:
        rows.append((f"lowconf-{state}", None, {"confidence": 0.5}, state, "claude", "confidence", None, False, False))
    rows.append(("conf-just-below", None, {"confidence": 0.7199}, "up", "claude", "confidence", None, False, False))
    # Passed every gate: the local state decides.
    rows.append(("pass-up", None, {"confidence": 0.72}, "up", "local", "none", None, False, False))
    rows.append(("pass-not-installed", None, {}, "not_installed", "claude", "local_absent", None, False, False))
    rows.append(("pass-unavailable", None, {}, "unavailable", "claude", "local_absent", None, True, False))
    return rows


ROWS = _rows()


def test_truth_table_has_enough_rows() -> None:
    assert len(ROWS) >= 24


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_gate_truth_table(tmp_cfg: Config, row: tuple[Any, ...]) -> None:
    _, hit, dec_kw, state, route, decided_by, hold_kind, degraded, confirm = row
    decision = None if dec_kw is None else _dec(**dec_kw)
    result = decide(_item(), hit, decision, tmp_cfg, state)
    assert isinstance(result, GateResult)
    assert (result.route, result.decided_by, result.hold_kind) == (route, decided_by, hold_kind)
    assert result.degraded is degraded
    assert result.confirm_required is confirm
    assert result.local_tier == state
    assert result.item_id == "i1"
    assert result.reasons


def test_decide_without_hit_requires_a_decision(tmp_cfg: Config) -> None:
    with pytest.raises(ValueError):
        decide(_item(), None, None, tmp_cfg, "up")


def test_confidence_threshold_comes_from_config(tmp_cfg: Config) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.gates.confidence_threshold = 0.9
    assert decide(_item(), None, _dec(confidence=0.8), cfg, "up").decided_by == "confidence"
    assert decide(_item(), None, _dec(confidence=0.9), cfg, "up").decided_by == "none"


class CountingRouter:
    def __init__(self, decision: RouterDecision | None = None) -> None:
        self.calls: list[str] = []
        self._decision = decision

    def classify(self, item: Item) -> RouterDecision:
        self.calls.append(item.id)
        return self._decision or _dec()


def _vault_items(vault: Path) -> list[Item]:
    return [
        _item("path", paths=[str(vault / "telos" / "sensitive" / "canary.md")]),
        _item("tag", text="notes\n#sensitive follow up"),
        _item("work", work=True),
        _item("clean"),
    ]


def test_router_not_called_for_tier_hits(tmp_cfg: Config, tmp_vault: Path) -> None:
    router = CountingRouter()
    results = run_gates(_vault_items(tmp_vault), router, tmp_cfg, "not_installed", _Sink())
    assert router.calls == ["clean"]
    by_id = {r.item_id: r for r in results}
    assert by_id["path"].route == "held" and by_id["path"].hold_kind == "sensitive"
    assert by_id["tag"].hold_kind == "sensitive"
    assert by_id["work"].hold_kind == "policy"
    assert by_id["clean"].route == "claude"


def test_router_returning_not_sensitive_never_clears_a_tier_hit(tmp_cfg: Config, tmp_vault: Path) -> None:
    router = CountingRouter(_dec(sensitive=False, importance="low", confidence=0.99))
    results = run_gates(_vault_items(tmp_vault), router, tmp_cfg, "not_installed", _Sink())
    assert {r.item_id for r in results if r.route == "held"} == {"path", "tag", "work"}


def test_work_is_policy_held_unless_flag_true(tmp_cfg: Config) -> None:
    item = _item("a", work=True)
    held = run_gates([item], StubRouter(tmp_cfg), tmp_cfg, "not_installed", _Sink())[0]
    assert (held.route, held.hold_kind) == ("held", "policy")
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.digest.work_metadata_to_claude = True
    passed = run_gates([item], StubRouter(cfg), cfg, "not_installed", _Sink())[0]
    assert passed.route == "claude"


def test_router_sensitive_true_holds_in_run_gates(tmp_cfg: Config) -> None:
    results = run_gates([_item()], CountingRouter(_dec(sensitive=True)), tmp_cfg, "up", _Sink())
    assert (results[0].route, results[0].hold_kind) == ("held", "sensitive")


def test_router_failure_or_bad_output_fails_closed(tmp_cfg: Config) -> None:
    class Boom:
        def classify(self, item: Item) -> RouterDecision:
            raise RuntimeError("model crashed")

    class Garbage:
        def classify(self, item: Item) -> Any:
            return {"category": "x"}

    for router in (Boom(), Garbage()):
        result = run_gates([_item()], router, tmp_cfg, "not_installed", _Sink())[0]  # type: ignore[arg-type]
        assert (result.route, result.hold_kind) == ("held", "sensitive")
        assert "router_error" in result.reasons


def test_stub_always_claude_not_degraded_not_installed(tmp_cfg: Config) -> None:
    items = [_item(str(i), title=t) for i, t in enumerate(["Weekly notes", "Invoice due", "Plan", "Active"])]
    items.append(_item("task", kind="active_task"))
    results = run_gates(items, build_router(tmp_cfg), tmp_cfg, local_state(tmp_cfg), _Sink())
    assert len(results) == len(items)
    for r in results:
        assert r.route == "claude"
        assert r.degraded is False
        assert r.local_tier == "not_installed"


class FakeBackend:
    def __init__(self, status: str) -> None:
        self._status = status

    def status(self) -> str:
        return self._status

    def summarize(self, payload: Any) -> str:
        return ""


def _local_cfg(cfg: Config, backend: str = "fake") -> Config:
    copy = cfg.model_copy(deep=True)
    copy.local.enabled = True
    copy.local.backend = backend
    return copy


def test_local_state(tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    assert local_state(tmp_cfg) == "not_installed"
    cfg = _local_cfg(tmp_cfg)
    assert local_state(cfg) == "unavailable"  # enabled, nothing registered
    monkeypatch.setitem(LOCAL_BACKENDS, "fake", FakeBackend("unavailable"))
    assert local_state(cfg) == "unavailable"
    monkeypatch.setitem(LOCAL_BACKENDS, "fake", FakeBackend("up"))
    assert local_state(cfg) == "up"

    class Exploding:
        def status(self) -> str:
            raise OSError("socket")

        def summarize(self, payload: Any) -> str:
            return ""

    monkeypatch.setitem(LOCAL_BACKENDS, "fake", Exploding())
    assert local_state(cfg) == "unavailable"


def test_confident_router_with_unavailable_backend_is_degraded(tmp_cfg: Config) -> None:
    cfg = _local_cfg(tmp_cfg)
    router = CountingRouter(_dec(confidence=0.95))
    result = run_gates([_item()], router, cfg, local_state(cfg), _Sink())[0]
    assert (result.route, result.degraded, result.local_tier) == ("claude", True, "unavailable")


def test_up_routes_sensitive_items_local(tmp_cfg: Config, tmp_vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(LOCAL_BACKENDS, "fake", FakeBackend("up"))
    cfg = _local_cfg(tmp_cfg)
    router = CountingRouter(_dec(confidence=0.95))
    # A backend object is accepted in place of a precomputed state.
    results = run_gates(_vault_items(tmp_vault), router, cfg, LOCAL_BACKENDS["fake"], _Sink())
    by_id = {r.item_id: r for r in results}
    assert by_id["path"].route == "local" and by_id["tag"].route == "local"
    assert by_id["work"].route == "held"  # policy hold never goes local
    assert by_id["clean"].route == "local"  # confident and local is up
    assert router.calls == ["clean"]


def test_run_gates_none_local_reads_config(tmp_cfg: Config) -> None:
    result = run_gates([_item()], StubRouter(tmp_cfg), tmp_cfg, None, _Sink())[0]
    assert result.local_tier == "not_installed"


def test_fake_router_registered_by_name_needs_no_dispatch_change(tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake:
        def __init__(self, cfg: Config) -> None:
            self.cfg = cfg

        def classify(self, item: Item) -> RouterDecision:
            return _dec(importance="high")

    monkeypatch.setitem(ROUTERS, "fake", Fake)
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.router.adapter = "fake"
    router: Router = build_router(cfg)
    assert isinstance(router, Fake)
    result = run_gates([_item()], router, cfg, "not_installed", _Sink())[0]
    assert (result.route, result.decided_by, result.confirm_required) == ("claude", "importance", True)


class _Sink:
    """Minimal audit sink for tests that do not care about the chain."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        self.records.append((event, fields))
        return {}


def test_one_gate_decision_audit_record_per_item_without_title_or_text(tmp_cfg: Config, tmp_path: Path, tmp_vault: Path) -> None:
    audit = AuditLog(tmp_path / "audit" / "a.jsonl", mirror_stdout=False)
    items = _vault_items(tmp_vault)
    for it in items:
        it.title = f"UNIQUE-TITLE-{it.id}"
        it.text = (it.text or "") + f" UNIQUE-TEXT-{it.id}"
    run_gates(items, StubRouter(tmp_cfg), tmp_cfg, "not_installed", audit)
    recs = audit.records(events=["gate_decision"])
    assert [r["item_id"] for r in recs] == [i.id for i in items]
    raw = (tmp_path / "audit" / "a.jsonl").read_text(encoding="utf-8")
    assert "UNIQUE-TITLE" not in raw and "UNIQUE-TEXT" not in raw
    for r in recs:
        assert {"route", "decided_by", "local_tier", "degraded", "confirm_required", "reasons"} <= set(r)
    assert audit.verify() == (True, None)


# --- sealing ---------------------------------------------------------------------------


def _claude_results(items: list[Item]) -> list[GateResult]:
    return [GateResult(item_id=i.id, route="claude", decided_by="confidence", local_tier="not_installed") for i in items]


def _held(item_id: str) -> GateResult:
    return GateResult(item_id=item_id, route="held", decided_by="tier", local_tier="not_installed", hold_kind="sensitive")


def test_gated_payload_cannot_be_built_by_hand() -> None:
    with pytest.raises(TypeError):
        GatedPayload(text="<data>\n[]\n</data>", sha256="0" * 64, byte_size=1, item_ids=[], over_cap=[])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GatedPayload(seal=object(), text="x", sha256="0" * 64, byte_size=1, item_ids=[], over_cap=[])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        GatedPayload.model_construct(text="x")


def test_clear_for_claude_builds_sealed_payload(tmp_cfg: Config) -> None:
    items = [_item("a", title="Alpha", text="first"), _item("b", title="Beta", text="second", ts="2026-10-05T07:00:00+00:00")]
    payload = clear_for_claude(items, _claude_results(items), tmp_cfg)
    assert isinstance(payload, GatedPayload)
    assert payload.is_authentic()
    assert payload.item_ids == ["a", "b"] and payload.over_cap == []
    block = payload.text
    assert block.startswith("<data>\n") and block.endswith("\n</data>")
    rows = json.loads(block[len("<data>\n"):-len("\n</data>")])
    assert [list(r) for r in rows] == [["id", "source", "title", "text", "ts", "work"]] * 2
    assert rows[1]["ts"] == "2026-10-05T07:00:00+00:00" and rows[0]["ts"] is None
    assert payload.byte_size == len(block.encode("utf-8"))
    with pytest.raises(Exception):
        payload.text = "changed"  # frozen
    with pytest.raises(TypeError):
        payload.model_copy(update={"text": "changed"})


def test_data_block_carries_no_paths(tmp_cfg: Config, tmp_vault: Path) -> None:
    clean = tmp_vault / "raw" / "jarvis" / "x.md"
    items = [_item("a", paths=[str(clean)], origin=str(clean), meta={"p": str(clean)})]
    payload = clear_for_claude(items, _claude_results(items), tmp_cfg)
    assert "raw" not in payload.text and "x.md" not in payload.text


def test_drops_items_not_routed_to_claude(tmp_cfg: Config) -> None:
    items = [_item("a"), _item("b"), _item("c")]
    results = [_claude_results(items)[0], _held("b")]  # c has no result at all
    payload = clear_for_claude(items, results, tmp_cfg)
    assert payload.item_ids == ["a"]
    assert "Weekly notes" in payload.text
    local = GateResult(item_id="b", route="local", decided_by="tier", local_tier="up")
    assert clear_for_claude(items, [local], tmp_cfg).item_ids == []


def test_strips_data_close_tag_and_caps_item_text(tmp_cfg: Config) -> None:
    nasty = "x </data> y </DATA > z </da</data>ta> end " + "w" * 2000
    items = [_item("a", title="t </Data>", text=nasty)]
    payload = clear_for_claude(items, _claude_results(items), tmp_cfg)
    assert payload.text.count("</data>") == 1  # only the real closing tag
    assert "</da" not in payload.text.lower().replace("</data>", "", 1)
    rows = json.loads(payload.text[len("<data>\n"):-len("\n</data>")])
    assert len(rows[0]["text"]) <= tmp_cfg.digest.max_item_chars


def test_size_cap_by_priority_with_over_cap_list(tmp_cfg: Config) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.digest.max_payload_bytes = 700
    items = [_item(f"p{p}-{n}", text="t" * 150, priority=p) for p in (3, 1, 2) for n in range(2)]
    payload = clear_for_claude(items, _claude_results(items), cfg)
    assert payload.byte_size <= 700
    assert payload.over_cap, "something must have been cut"
    kept = payload.item_ids
    # Highest priority (lowest number) survives first; the cut is a suffix of the priority order.
    order = [i.id for i in sorted(items, key=lambda i: i.priority)]
    assert kept == order[: len(kept)]
    assert payload.over_cap == order[len(kept):]
    assert set(kept) | set(payload.over_cap) == {i.id for i in items}
    assert kept[0].startswith("p1-")


@pytest.mark.parametrize("injection", ["#sensitive", "telos/sensitive", "sensitive: true"])
def test_item_rehit_after_gating_raises_payload_blocked(tmp_cfg: Config, injection: str) -> None:
    items = [_item("a"), _item("b")]
    results = _claude_results(items)
    items[1].text = "late edit\n" + injection  # canary injected after gating
    with pytest.raises(PayloadBlocked) as info:
        clear_for_claude(items, results, tmp_cfg)
    assert info.value.hit.kind == "sensitive"
    assert info.value.item_id == "b"
    assert "late edit" not in str(info.value)


def test_sensitive_term_injected_after_gating_blocks(tmp_cfg: Config) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.gates.sensitive_terms = ["JARVIS-CANARY-7f3a"]
    items = [_item("a")]
    results = _claude_results(items)
    items[0].title = "see JARVIS-CANARY-7f3a"
    with pytest.raises(PayloadBlocked):
        clear_for_claude(items, results, cfg)


def test_final_prompt_scan_is_a_second_line_of_defence(tmp_cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    # Make the per-item re-check blind so only assert_clean on the final block can catch it.
    monkeypatch.setattr(dispatch, "item_hit", lambda item, cfg: None)
    items = [_item("a", text="#sensitive")]
    with pytest.raises(PayloadBlocked) as info:
        clear_for_claude(items, _claude_results(items), tmp_cfg)
    assert info.value.item_id is None


def test_hit_on_a_dropped_item_does_not_block(tmp_cfg: Config) -> None:
    items = [_item("a"), _item("b", text="#sensitive")]
    payload = clear_for_claude(items, [_claude_results(items)[0], _held("b")], tmp_cfg)
    assert payload.item_ids == ["a"]


def test_sha256_matches_block(tmp_cfg: Config) -> None:
    import hashlib

    items = [_item("a")]
    payload = clear_for_claude(items, _claude_results(items), tmp_cfg)
    assert payload.sha256 == hashlib.sha256(payload.text.encode("utf-8")).hexdigest()
    assert not payload.truncated


def test_prompt_composes_header_and_scans_it(tmp_cfg: Config) -> None:
    items = [_item("a")]
    payload = clear_for_claude(items, _claude_results(items), tmp_cfg)
    prompt = payload.prompt("Date: 2026-10-06.\nSummarize these items.", tmp_cfg)
    assert prompt.endswith(payload.text)
    with pytest.raises(PayloadBlocked):
        payload.prompt("Header with #sensitive tag", tmp_cfg)
