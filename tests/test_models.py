"""Tests for jarvisd.models."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jarvisd import models

EM = "\u2014"

DESIGN_JOB = {
    "schema": 1,
    "id": "digest-2026-10-06",
    "kind": "morning_digest",
    "key": "2026-10-06",
    "class": "observe_only",
    "latency_class": "background_batch",
    "state": "pending",
    "origin": "schedule",
    "created_at": "2026-10-06T04:31:02+00:00",
    "not_before": "2026-10-06T04:31:02+00:00",
    "deadline": "2026-10-06T07:31:02+00:00",
    "attempts": 0,
    "max_attempts": 3,
    "window": {"start": "2026-10-05T04:30:11+00:00", "end": "2026-10-06T04:31:02+00:00"},
    "params": {"force": False, "dry_run": False, "no_claude": False, "notify": True},
    "config_sha256": None,
    "router": None,
    "tier": None,
    "importance": None,
    "confidence": None,
    "sensitive": False,
    "degraded": {"flag": False, "reasons": []},
    "local_tier": "not_installed",
    "cost_usd": 0.0,
    "result": None,
    "last_error": None,
    "history": [
        {"ts": "2026-10-06T04:31:02+00:00", "from": None, "to": "pending", "note": "reconcile"}
    ],
}


def good_decision(**over: object) -> dict:
    base = {
        "category": "brain_thread",
        "sensitive": False,
        "importance": "low",
        "confidence": 0.0,
        "needs_tools": [],
        "language": "en",
        "reason": "local_tier_not_installed",
    }
    base.update(over)
    return base


def test_router_decision_accepts_valid_contract() -> None:
    d = models.RouterDecision(**good_decision())
    assert d.importance == "low"
    assert set(models.RouterDecision.model_fields) == {
        "category", "sensitive", "importance", "confidence",
        "needs_tools", "language", "reason",
    }


def test_router_decision_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        models.RouterDecision(**good_decision(extra_field="x"))


@pytest.mark.parametrize("conf", [1.5, -0.1, float("nan"), "0.9"])
def test_router_decision_rejects_bad_confidence(conf: object) -> None:
    with pytest.raises(ValidationError):
        models.RouterDecision(**good_decision(confidence=conf))


def test_router_decision_accepts_confidence_bounds() -> None:
    models.RouterDecision(**good_decision(confidence=0.0))
    models.RouterDecision(**good_decision(confidence=1.0))


@pytest.mark.parametrize("imp", ["urgent", "HIGH", "medium", ""])
def test_router_decision_rejects_bad_importance(imp: str) -> None:
    with pytest.raises(ValidationError):
        models.RouterDecision(**good_decision(importance=imp))


def test_router_decision_rejects_missing_field() -> None:
    data = good_decision()
    del data["reason"]
    with pytest.raises(ValidationError):
        models.RouterDecision(**data)


def test_job_roundtrips_design_example_with_aliases() -> None:
    job = models.Job.model_validate(DESIGN_JOB)
    assert job.job_class == "observe_only"
    assert job.history[0].from_state is None
    dumped = json.loads(job.to_json())
    assert dumped == DESIGN_JOB


def test_job_normalizes_timestamps_to_utc_seconds() -> None:
    data = dict(DESIGN_JOB, created_at="2026-10-06T05:31:02+01:00")
    job = models.Job.model_validate(data)
    assert job.created_at == "2026-10-06T04:31:02+00:00"


def test_job_rejects_naive_timestamp_and_bad_state() -> None:
    with pytest.raises(ValidationError):
        models.Job.model_validate(dict(DESIGN_JOB, created_at="2026-10-06T04:31:02"))
    with pytest.raises(ValidationError):
        models.Job.model_validate(dict(DESIGN_JOB, state="sleeping"))


def test_withheld_item_carries_no_content_fields() -> None:
    w = models.WithheldItem(
        id="w-3a9f1c", kind="brain_session", source_ref="C:/x/brain/sessions/a.md",
        reason="path_under_sensitive",
    )
    assert w.hold_kind == "sensitive"
    for forbidden in ("title", "text", "body", "content"):
        assert forbidden not in models.WithheldItem.model_fields
    with pytest.raises(ValidationError):
        models.WithheldItem(
            id="w-1", kind="k", source_ref="p", reason="r", title="leak",
        )


def test_item_defaults() -> None:
    it = models.Item(id="a1b2c3d4", source="brain", kind="brain_thread", title="t", text="x")
    assert it.work is False
    assert it.paths == []
    assert it.tags == []


def test_gate_result_route_values() -> None:
    dec = models.RouterDecision(**good_decision())
    g = models.GateResult(
        item_id="a1", route="claude", decided_by="confidence",
        local_tier="not_installed", decision=dec,
    )
    assert g.degraded is False
    assert g.confirm_required is False
    with pytest.raises(ValidationError):
        models.GateResult(item_id="a1", route="nowhere", decided_by="none", local_tier="up")


def test_tier_hit_fields() -> None:
    h = models.TierHit(kind="sensitive", code="term:3", where="text")
    assert h.kind == "sensitive"
    with pytest.raises(ValidationError):
        models.TierHit(kind="other", code="x", where="text")


def test_collect_result_defaults() -> None:
    r = models.CollectResult(source="brain", ok=False, error="boom")
    assert r.items == [] and r.withheld == [] and r.facts == {}
    assert r.duration_ms == 0


def test_digest_summary_caps_and_strips_dashes() -> None:
    raw = {
        "headline": f"Big {EM} news " + "x" * 400,
        "attention": [{"id": f"i{n}", "why": "y" * 300} for n in range(8)],
        "summaries": {"a": f"one {EM} two " + "z" * 300},
        "notes": "n",
    }
    s = models.DigestSummary.model_validate(raw)
    assert len(s.headline) <= 200
    assert EM not in s.headline
    assert len(s.attention) == 5
    assert all(len(a.why) <= 160 for a in s.attention)
    assert len(s.summaries["a"]) <= 140
    assert EM not in s.summaries["a"]


def test_digest_summary_requires_headline() -> None:
    with pytest.raises(ValidationError):
        models.DigestSummary.model_validate({"attention": [], "summaries": {}})


def test_claude_reply_parses_cli_json() -> None:
    cli = {
        "type": "result", "subtype": "success", "is_error": False, "result": "{}",
        "num_turns": 1, "session_id": "s", "total_cost_usd": 0.00065,
        "usage": {"input_tokens": 437, "output_tokens": 5,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        "modelUsage": {"claude-haiku": {"inputTokens": 437}},
        "duration_ms": 900, "duration_api_ms": 800, "stop_reason": "end_turn",
        "permission_denials": [], "api_error_status": None, "unknown_future_key": 1,
    }
    r = models.ClaudeReply.model_validate(cli)
    assert r.usage.input_tokens == 437
    assert r.total_input_tokens == 437
    assert r.model_usage["claude-haiku"]["inputTokens"] == 437
    assert r.is_error is False


def test_attention_and_run_manifest_construct() -> None:
    a = models.Attention(id="a1", why="due today")
    m = models.RunManifest(job_id="digest-2026-10-06", status="complete")
    assert a.id == "a1"
    assert m.counts == {} and m.stages == {}
