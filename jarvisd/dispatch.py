"""The gates, the local-backend seam and the sealing step (design section 6).

Layer L3. Gate order is code, not config and not prompt: tier, then router sensitivity,
then importance, then confidence, then the local state. `decide` is a pure function so the
truth table can be tested exhaustively; `run_gates` is the only place a router is called,
and it never calls one for an item that already has a tier hit.

`GatedPayload` is the only input type `ClaudeClient.complete` accepts. Its constructor needs
a module-private token, and `clear_for_claude` is the only code that holds it.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field

from jarvisd.config import Config
from jarvisd.models import GateResult, Item, LocalTier, RouterDecision, TierHit
from jarvisd.router import Router
from jarvisd.tier import TierViolation, assert_clean, item_hit

_STATES: tuple[str, ...] = get_args(LocalTier)
# Private to this module: whoever cannot see this object cannot build a GatedPayload.
_SEAL = object()
_TITLE_CHARS = 200
# Opening and closing data tags in any case or spacing; both are removed from item strings so
# item text can never close (or reopen) the data block the model is told to treat as inert.
_DATA_TAG = re.compile(r"<\s*/?\s*data\s*>", re.IGNORECASE)
_BLOCK_OVERHEAD = len("<data>\n[]\n</data>".encode("utf-8"))
_ROW_SEPARATOR = len(",\n")
_AUDIT_CATEGORY_CHARS = 40


# --- the local backend seam ------------------------------------------------------------


class LocalBackend(Protocol):
    """A local model runtime (phase 1). Nothing implements this in v1."""

    def status(self) -> LocalTier:
        """Return 'up' only when the backend can take a request right now."""
        ...

    def summarize(self, payload: Any) -> str:
        """Summarize items that must not leave the machine."""
        ...


LOCAL_BACKENDS: dict[str, LocalBackend] = {}


def _backend_state(backend: LocalBackend) -> LocalTier:
    """A backend that raises, or says anything but 'up', is unavailable, never trusted."""
    try:
        return "up" if backend.status() == "up" else "unavailable"
    except Exception:  # noqa: BLE001  a flaky backend must not take the gate down
        return "unavailable"


def local_state(cfg: Config) -> LocalTier:
    """not_installed when [local] is off, unavailable when on without a healthy backend, else up."""
    if not cfg.local.enabled:
        return "not_installed"
    backend = LOCAL_BACKENDS.get(cfg.local.backend)
    if backend is None:
        return "unavailable"
    return _backend_state(backend)


# --- the audit seam --------------------------------------------------------------------


class AuditSink(Protocol):
    """What dispatch needs from the audit log (jarvisd.audit.AuditLog satisfies it)."""

    def emit(self, event: str, **fields: Any) -> Any: ...


# --- gates -----------------------------------------------------------------------------


def decide(item: Item, hit: TierHit | None, decision: RouterDecision | None, cfg: Config,
           local: LocalTier) -> GateResult:
    """Apply the gates in their fixed order. Pure: no I/O, no router call."""
    base: dict[str, Any] = {"item_id": item.id, "local_tier": local, "decision": decision}

    # Gate 1: tier. Computed before any router call; the router never overrides it.
    if hit is not None:
        if hit.kind == "sensitive" and local == "up":
            return GateResult(route="local", decided_by="tier", reasons=[hit.code], **base)
        return GateResult(route="held", decided_by="tier", hold_kind=hit.kind, reasons=[hit.code],
                          degraded=(hit.kind == "sensitive" and local == "unavailable"), **base)

    if decision is None:
        raise ValueError("decide needs a router decision when there is no tier hit")

    # The router may add sensitivity, never remove it.
    if decision.sensitive:
        return GateResult(route="held", decided_by="tier", hold_kind="sensitive",
                          reasons=["router_sensitive"], **base)

    # Gate 2: importance.
    if decision.importance == "high" or decision.category in cfg.gates.importance_escalate:
        why = "importance_high" if decision.importance == "high" else f"category:{decision.category}"
        return GateResult(route="claude", decided_by="importance", confirm_required=True, reasons=[why], **base)

    # Gate 3: confidence. Strictly below the threshold escalates.
    if decision.confidence < cfg.gates.confidence_threshold:
        return GateResult(route="claude", decided_by="confidence", reasons=["confidence_below_threshold"], **base)

    # Every gate passed: the local state decides where it runs.
    if local == "up":
        return GateResult(route="local", decided_by="none", reasons=["passed_all_gates"], **base)
    return GateResult(route="claude", decided_by="local_absent", degraded=(local == "unavailable"),
                      reasons=[f"local_{local}"], **base)


def _resolve_state(local: LocalTier | LocalBackend | None, cfg: Config) -> LocalTier:
    if local is None:
        return local_state(cfg)
    if isinstance(local, str):
        if local not in _STATES:
            raise ValueError(f"unknown local state {local!r}")
        return local  # type: ignore[return-value]
    return _backend_state(local)


def _classify(router: Router, item: Item) -> RouterDecision | None:
    """Router output, re-validated so a model_construct'ed or malformed result cannot pass."""
    try:
        raw = router.classify(item)
        if isinstance(raw, RouterDecision):
            raw = raw.model_dump()
        return RouterDecision.model_validate(raw)
    except Exception:  # noqa: BLE001  any router failure holds the item
        return None


def _audit_fields(result: GateResult) -> dict[str, Any]:
    """Ids, codes and numbers only. Never titles or text, and no free-form router strings."""
    fields: dict[str, Any] = {
        "item_id": result.item_id,
        "route": result.route,
        "decided_by": result.decided_by,
        "local_tier": result.local_tier,
        "confirm_required": result.confirm_required,
        "degraded": result.degraded,
        "hold_kind": result.hold_kind,
        "reasons": list(result.reasons),
    }
    if result.decision is not None:
        fields["importance"] = result.decision.importance
        fields["confidence"] = result.decision.confidence
        fields["category"] = result.decision.category[:_AUDIT_CATEGORY_CHARS]
    return fields


def run_gates(items: Sequence[Item], router: Router, cfg: Config,
              local: LocalTier | LocalBackend | None, audit: AuditSink) -> list[GateResult]:
    """Gate every item, in order, and audit one `gate_decision` record each.

    `local` may be a state string, a backend, or None (read it from config).
    """
    state = _resolve_state(local, cfg)
    results: list[GateResult] = []
    for item in items:
        hit = item_hit(item, cfg)
        decision: RouterDecision | None = None
        if hit is None:
            decision = _classify(router, item)
            if decision is None:
                # Fail closed: an item the router could not judge is not sent anywhere.
                result = GateResult(item_id=item.id, route="held", decided_by="tier", local_tier=state,
                                    hold_kind="sensitive", reasons=["router_error"])
            else:
                result = decide(item, None, decision, cfg, state)
        else:
            result = decide(item, hit, None, cfg, state)
        audit.emit("gate_decision", **_audit_fields(result))
        results.append(result)
    return results


# --- sealing ---------------------------------------------------------------------------


class PayloadBlocked(Exception):
    """A tier hit survived to the sealing step. The message is the hit code, never the content."""

    def __init__(self, hit: TierHit, item_id: str | None = None) -> None:
        super().__init__(hit.code)
        self.hit = hit
        self.item_id = item_id


class GatedPayload(BaseModel):
    """The only thing ClaudeClient.complete accepts. Built by `clear_for_claude` alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str  # the whole "<data>...</data>" block, the part that carries item content
    sha256: str
    byte_size: int
    item_ids: list[str]
    over_cap: list[str] = Field(default_factory=list)

    def __init__(self, *, seal: object = None, **data: Any) -> None:
        if seal is not _SEAL:
            raise TypeError("GatedPayload can only be built by dispatch.clear_for_claude")
        super().__init__(**data)

    @classmethod
    def model_construct(cls, *args: Any, **kwargs: Any) -> GatedPayload:  # type: ignore[override]
        raise TypeError("GatedPayload can only be built by dispatch.clear_for_claude")

    def model_copy(self, *args: Any, **kwargs: Any) -> GatedPayload:  # type: ignore[override]
        raise TypeError("GatedPayload cannot be copied or edited after sealing")

    @property
    def truncated(self) -> bool:
        return bool(self.over_cap)

    def is_authentic(self) -> bool:
        """True when the stored hash and size still describe the text (catches tampering)."""
        data = self.text.encode("utf-8")
        return hashlib.sha256(data).hexdigest() == self.sha256 and len(data) == self.byte_size

    def prompt(self, header: str, cfg: Config) -> str:
        """Header plus data block, scanned once more as the final serialized prompt."""
        full = f"{header}\n{self.text}"
        try:
            assert_clean(full, cfg)
        except TierViolation as exc:
            raise PayloadBlocked(exc.hit) from exc
        return full


def _clean_string(value: str, limit: int) -> str:
    value = value[:limit]
    # Removing a tag can splice its neighbours into a new one ("</da</data>ta>"), so repeat.
    while True:
        stripped = _DATA_TAG.sub("", value)
        if stripped == value:
            return value
        value = stripped


def _row(item: Item, cfg: Config) -> str:
    """One data row: id, source, title, text, ts, work. No paths, tags, origin or meta."""
    row = {
        "id": _clean_string(item.id, 200),
        "source": _clean_string(item.source, 80),
        "title": _clean_string(item.title, _TITLE_CHARS),
        "text": _clean_string(item.text, cfg.digest.max_item_chars),
        "ts": item.ts,
        "work": item.work,
    }
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def clear_for_claude(items: Sequence[Item], results: Sequence[GateResult], cfg: Config) -> GatedPayload:
    """Seal the claude-routed items into a GatedPayload, or raise PayloadBlocked.

    Items without a `claude` route are dropped silently (that is what holding means). Items
    that were cleared but now hit gate 1 again are not dropped: reaching this point with a hit
    means something changed after gating, which is a violation worth aborting the call for.
    """
    routes = {r.item_id: r.route for r in results}
    candidates = [i for i in items if routes.get(i.id) == "claude"]
    for item in candidates:
        hit = item_hit(item, cfg)
        if hit is not None:
            raise PayloadBlocked(hit, item.id)

    # Lowest priority number first; sorted() is stable so collector order breaks ties.
    ordered = sorted(candidates, key=lambda i: i.priority)
    rows: list[str] = []
    kept: list[str] = []
    over_cap: list[str] = []
    size = _BLOCK_OVERHEAD
    cut = False
    for item in ordered:
        row = _row(item, cfg)
        cost = len(row.encode("utf-8")) + (_ROW_SEPARATOR if rows else 0)
        # Strict: once one item does not fit, everything after it (lower priority) is cut too.
        if cut or size + cost > cfg.digest.max_payload_bytes:
            cut = True
            over_cap.append(item.id)
            continue
        rows.append(row)
        kept.append(item.id)
        size += cost

    text = "<data>\n[" + ",\n".join(rows) + "]\n</data>"
    try:
        assert_clean(text, cfg)
    except TierViolation as exc:
        raise PayloadBlocked(exc.hit) from exc
    data = text.encode("utf-8")
    return GatedPayload(seal=_SEAL, text=text, sha256=hashlib.sha256(data).hexdigest(),
                        byte_size=len(data), item_ids=kept, over_cap=over_cap)
