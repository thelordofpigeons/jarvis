"""Router contract and StubRouter tests (design section 6, plan T4)."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jarvisd import router as router_mod
from jarvisd.config import Config, ConfigError
from jarvisd.models import Item, RouterDecision
from jarvisd.router import ROUTERS, StubRouter, build_router

FIELDS = {"category", "sensitive", "importance", "confidence", "needs_tools", "language", "reason"}


def _item(title: str = "Weekly notes", text: str = "", **kw: object) -> Item:
    kind = str(kw.pop("kind", "brain_thread"))
    return Item(id="i1", source="brain", kind=kind, title=title, text=text, **kw)  # type: ignore[arg-type]


def test_stub_output_has_exactly_the_seven_fields(tmp_cfg: Config) -> None:
    decision = StubRouter(tmp_cfg).classify(_item())
    assert isinstance(decision, RouterDecision)
    assert set(decision.model_dump()) == FIELDS
    RouterDecision.model_validate(decision.model_dump())
    assert decision.sensitive is False
    assert decision.confidence == 0.0
    assert decision.needs_tools == []
    assert decision.reason == "local_tier_not_installed"


def test_router_decision_rejects_extra_field() -> None:
    good = {"category": "x", "sensitive": False, "importance": "low", "confidence": 0.5,
            "needs_tools": [], "language": "en", "reason": "r"}
    RouterDecision.model_validate(good)
    with pytest.raises(ValidationError):
        RouterDecision.model_validate({**good, "extra": 1})


@pytest.mark.parametrize(
    ("title", "text", "bucket"),
    [
        ("Invoice 42 overdue", "", "financial"),
        ("Reply to the client", "proposal due friday", "client-facing"),
        ("Cleanup", "we will force-push and delete the branch", "irreversible"),
        ("Release", "production deploy tonight", "work-prod"),
    ],
)
def test_importance_buckets_are_high(tmp_cfg: Config, title: str, text: str, bucket: str) -> None:
    decision = StubRouter(tmp_cfg).classify(_item(title, text))
    assert decision.importance == "high"
    # The category must equal an entry of [gates].importance_escalate so gate 2 can see it.
    assert decision.category == bucket
    assert bucket in tmp_cfg.gates.importance_escalate


def test_work_item_is_high_and_active_task_is_med(tmp_cfg: Config) -> None:
    router = StubRouter(tmp_cfg)
    assert router.classify(_item(work=True)).importance == "high"
    task = router.classify(_item("Current work", kind="active_task"))
    assert task.importance == "med"
    assert task.category == "active_task"
    assert router.classify(_item("Water the plants")).importance == "low"


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("the report is ready and we will send it to the team", "en"),
        ("le rapport est pret et nous allons le envoyer pour la equipe", "fr"),
        ("wach nta bghit nchouf dyal khdma daba", "ar-darija-latin"),
        ("3la hsab dyal lyoum kayn bzaf dyal khdma", "ar-darija-latin"),
        ("", "other"),
        ("zzz qqq xxx", "other"),
    ],
)
def test_language_heuristic(tmp_cfg: Config, text: str, language: str) -> None:
    assert StubRouter(tmp_cfg).classify(_item("", text)).language == language


def test_confidence_comes_from_config(tmp_cfg: Config) -> None:
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.router.stub.confidence = 0.4
    assert StubRouter(cfg).classify(_item()).confidence == 0.4


def test_build_router_default_and_unknown(tmp_cfg: Config) -> None:
    assert isinstance(build_router(tmp_cfg), StubRouter)
    assert ROUTERS["stub"] is StubRouter
    cfg = tmp_cfg.model_copy(deep=True)
    cfg.router.adapter = "does-not-exist"
    with pytest.raises(ConfigError):
        build_router(cfg)


def test_router_module_stays_local() -> None:
    # Routers see non-held text before the importance gate, so they must not reach out.
    source = Path(router_mod.__file__).read_text(encoding="utf-8")
    for banned in ("subprocess", "urllib", "socket", "http", "jarvisd.claude"):
        assert f"import {banned}" not in source
        assert f"from {banned}" not in source
