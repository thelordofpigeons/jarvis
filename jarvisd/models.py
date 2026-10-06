"""Pydantic v2 models shared across jarvisd (design sections 4, 5, 6, 7, 8).

Layer L0: no imports from other jarvisd modules except common. Models that cross a trust
boundary (RouterDecision, DigestSummary, ClaudeReply) are strict about shape; models that
only move data between our own modules are plain.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from jarvisd.common import iso, parse_iso, strip_dashes


def _normalize_ts(value: str) -> str:
    """Timestamps are stored as UTC ISO strings with seconds, offset +00:00."""
    return iso(parse_iso(value))


# A string on disk and in JSON (so jobs round-trip byte for byte), validated as an aware time.
IsoStr = Annotated[str, AfterValidator(_normalize_ts)]

Importance = Literal["low", "med", "high"]
LocalTier = Literal["not_installed", "unavailable", "up"]
Route = Literal["local", "claude", "held"]
HoldKind = Literal["sensitive", "policy"]
DecidedBy = Literal["tier", "importance", "confidence", "local_absent", "none"]
JobState = Literal["pending", "running", "done", "failed"]
JobOrigin = Literal["schedule", "catchup", "manual"]
Language = Literal["en", "fr", "ar-darija-latin", "other"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --- collectors and gating -------------------------------------------------------------


class Item(_Model):
    """One thing a collector found. Claude never sees paths, only id, source, title, text, ts, work."""

    id: str
    source: str  # brain, task, git, system, clickup
    kind: str  # brain_thread, brain_decision, brain_session, active_task, git_repo, ...
    title: str
    text: str = ""
    ts: IsoStr | None = None
    tags: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    origin: str = ""
    work: bool = False
    # Lower sorts first when the payload is capped (active task, threads, repos, the rest).
    priority: int = 3
    meta: dict[str, Any] = Field(default_factory=dict)


class WithheldItem(_Model):
    """A content-free reference to something not read or not sent (design D5).

    Deliberately has no title or text field, and forbids extras, so a leak cannot be
    smuggled in through this type.
    """

    id: str
    kind: str
    source_ref: str
    reason: str
    hold_kind: HoldKind = "sensitive"


class TierHit(_Model):
    """Result of gate 1. The code never echoes a matched term, only e.g. term:3."""

    kind: HoldKind
    code: str
    where: str = ""  # path, text, tag, router, error


class RouterDecision(_Model):
    """The seven-field router contract (spec 4a). Anything else is rejected."""

    category: str = Field(min_length=1)
    sensitive: StrictBool
    importance: Importance
    confidence: Annotated[float, Field(ge=0.0, le=1.0, strict=True)]
    needs_tools: list[str]
    language: Language
    reason: str


class GateResult(_Model):
    item_id: str
    route: Route
    decided_by: DecidedBy
    local_tier: LocalTier
    confirm_required: bool = False
    degraded: bool = False
    hold_kind: HoldKind | None = None
    reasons: list[str] = Field(default_factory=list)
    decision: RouterDecision | None = None


class CollectResult(_Model):
    source: str
    ok: bool
    error: str | None = None
    items: list[Item] = Field(default_factory=list)
    withheld: list[WithheldItem] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int = 0


# --- jobs ------------------------------------------------------------------------------


class JobWindow(_Model):
    start: IsoStr
    end: IsoStr


class JobParams(_Model):
    force: bool = False
    dry_run: bool = False
    no_claude: bool = False
    notify: bool = True


class Degraded(_Model):
    flag: bool = False
    reasons: list[str] = Field(default_factory=list)


class HistoryEntry(_Model):
    ts: IsoStr
    # "from" is a Python keyword, so the attribute is from_state and the JSON key is "from".
    from_state: str | None = Field(default=None, alias="from")
    to: str
    note: str = ""


class Job(_Model):
    """Queue job, schema 1 (design section 5). The directory is the truth, `state` is advisory."""

    schema_version: int = Field(default=1, alias="schema")
    id: str
    kind: str
    key: str
    job_class: str = Field(alias="class")
    latency_class: str
    state: JobState = "pending"
    origin: JobOrigin = "schedule"
    created_at: IsoStr
    not_before: IsoStr
    deadline: IsoStr | None = None
    attempts: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    window: JobWindow | None = None
    params: JobParams = Field(default_factory=JobParams)
    config_sha256: str | None = None
    router: str | None = None
    tier: str | None = None
    importance: Importance | None = None
    confidence: float | None = None
    sensitive: bool = False
    degraded: Degraded = Field(default_factory=Degraded)
    local_tier: LocalTier = "not_installed"
    cost_usd: float = 0.0
    result: dict[str, Any] | None = None
    last_error: str | None = None
    history: list[HistoryEntry] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


# --- Claude-facing ---------------------------------------------------------------------


class Attention(_Model):
    # Part of Claude's reply, so extra keys are dropped instead of failing the digest.
    model_config = ConfigDict(extra="ignore")

    id: str
    why: str

    @field_validator("why")
    @classmethod
    def _cap_why(cls, value: str) -> str:
        return strip_dashes(value)[:160]


class DigestSummary(BaseModel):
    """What the single Claude call must return. Overlong strings are truncated, not rejected.

    Unknown keys are ignored rather than failing the digest: they are dropped here and
    never rendered, so tolerating them has no safety cost.
    """

    model_config = ConfigDict(extra="ignore")

    headline: str = Field(min_length=1)
    attention: list[Attention] = Field(default_factory=list)
    summaries: dict[str, str] = Field(default_factory=dict)
    notes: str = ""

    @field_validator("headline")
    @classmethod
    def _cap_headline(cls, value: str) -> str:
        return strip_dashes(value)[:200]

    @field_validator("notes")
    @classmethod
    def _clean_notes(cls, value: str) -> str:
        return strip_dashes(value)

    @field_validator("summaries")
    @classmethod
    def _cap_summaries(cls, value: dict[str, str]) -> dict[str, str]:
        return {k: strip_dashes(v)[:140] for k, v in value.items()}

    @model_validator(mode="after")
    def _cap_attention(self) -> "DigestSummary":
        # At most 5, most important first, so keeping the head keeps the best.
        self.attention = self.attention[:5]
        return self


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ClaudeReply(BaseModel):
    """Parsed `claude -p --output-format json` envelope (design section 7).

    Accepts the CLI's own key names (modelUsage) and ignores keys added by newer CLI
    versions. `result` is the raw model text, validated separately into DigestSummary.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    result: str = ""
    is_error: bool = False
    subtype: str = ""
    num_turns: int = 0
    session_id: str = ""
    total_cost_usd: float = 0.0
    usage: Usage = Field(default_factory=Usage)
    model_usage: dict[str, Any] = Field(
        default_factory=dict, validation_alias=AliasChoices("model_usage", "modelUsage")
    )
    duration_ms: int = 0
    duration_api_ms: int = 0
    stop_reason: str | None = None
    permission_denials: list[Any] = Field(default_factory=list)
    api_error_status: int | str | None = None

    @property
    def total_input_tokens(self) -> int:
        """Input plus cache tokens: the quantity the isolation tripwire compares."""
        u = self.usage
        return u.input_tokens + u.cache_creation_input_tokens + u.cache_read_input_tokens


class RunManifest(_Model):
    """Per-run record under state/runs/<job_id>/run.json. Counts, hashes and paths only."""

    job_id: str
    status: str
    started_at: IsoStr | None = None
    finished_at: IsoStr | None = None
    stages: dict[str, str] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    cost_usd: float = 0.0
    paths: dict[str, str] = Field(default_factory=dict)
    hashes: dict[str, str] = Field(default_factory=dict)
    config_sha256: str | None = None
    audit_seq: int | None = None


# --- proposals (jarvisd/propose.py) ----------------------------------------------------------

ProposalKind = Literal["task", "decision", "followup", "risk"]
ProposalStatus = Literal["proposed", "confirmed", "rejected", "edited_confirmed"]
PROPOSAL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
PROPOSAL_RATIONALE_MAX = 240


class ProposalEdits(_Model):
    """What the owner changed before confirming. A missing key means 'as proposed'."""

    title: str | None = Field(default=None, min_length=1, max_length=120)
    project: str | None = Field(default=None, min_length=1, max_length=80)
    due: date | None = None

    @field_validator("title", "project")
    @classmethod
    def _no_dashes(cls, value: str | None) -> str | None:
        return None if value is None else strip_dashes(value)  # house rule: no em or en dashes


class Proposal(_Model):
    """One task proposal, stored as state/proposals/<id>.json.

    Everything except `status`, `tracker_ref`, `rejected_reason` and `edits` is written once by
    the proposals job. Those four change only through a human action in the Inbox or the CLI.
    `evidence` holds item ids of the run that were cleared for Claude; it never holds text.
    """

    id: str = Field(pattern=PROPOSAL_ID_PATTERN)
    created_at: IsoStr
    run_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=120)
    project: str = Field(min_length=1, max_length=80)
    kind: ProposalKind
    evidence: list[str] = Field(min_length=1, max_length=8)
    suggested_status: str = Field(min_length=1, max_length=40)
    due_hint: date | None = None
    rationale: str = Field(max_length=PROPOSAL_RATIONALE_MAX)
    status: ProposalStatus = "proposed"
    tracker_ref: str | None = Field(default=None, max_length=500)
    rejected_reason: str | None = Field(default=None, max_length=500)
    edits: ProposalEdits = Field(default_factory=ProposalEdits)

    @field_validator("title", "project", "suggested_status", "rationale", "rejected_reason")
    @classmethod
    def _no_dashes(cls, value: str | None) -> str | None:
        return None if value is None else strip_dashes(value)

    @field_validator("evidence")
    @classmethod
    def _evidence_ids(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 200 or item != item.strip() for item in value):
            raise ValueError("evidence entries are item ids")
        return value

    @field_validator("tracker_ref")
    @classmethod
    def _tracker_url(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"(https?://|file:///)[^\s]+", value):
            raise ValueError("tracker_ref is an http, https or file URL (markdown adapter), or null")
        return value
