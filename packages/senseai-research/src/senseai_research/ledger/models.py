# SPDX-License-Identifier: Apache-2.0
"""Ledger records (spec §4). Every record has ``id``, ``created_at`` and ``created_by``.

``created_by`` is ``user:<name>``, ``pipeline:<step>@<version>`` or ``agent:<conversation-id>``.
The JSON Schema of every model is written to ``schema/`` by ``senseai-research schema``.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ClaimStatus = Literal["unverified", "extracted", "disputed", "verified", "rejected", "superseded"]
CLAIM_STATUSES: tuple[str, ...] = (
    "unverified",
    "extracted",
    "disputed",
    "verified",
    "rejected",
    "superseded",
)
#: Status transitions only a human Approval may make (N1).
HUMAN_ONLY_STATUSES: frozenset[str] = frozenset({"verified", "rejected", "superseded"})
#: Statuses that require a passage (N2): a claim at or above ``extracted``.
NEEDS_PASSAGE: frozenset[str] = frozenset({"extracted", "disputed", "verified", "superseded"})
Method = Literal["behavioural", "electrophysiology", "anatomical", "model", "review"]
MappingKind = Literal["exact", "close", "loose"]
Provenance = Literal["pipeline", "user", "agent-proposed", "library"]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    """Time-sortable id: ``<prefix>-<ms hex><random hex>`` (ULID-like, no dependency)."""
    return f"{prefix}-{int(time.time() * 1000):012x}{secrets.token_hex(4)}"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    created_at: str = Field(default_factory=now)
    created_by: str

    @field_validator("created_by")
    @classmethod
    def _actor(cls, v: str) -> str:
        if not v.startswith(("user:", "pipeline:", "agent:")):
            raise ValueError("created_by must be user:<name>, pipeline:<step>@<v> or agent:<id>")
        return v


class Vetting(BaseModel):
    model_config = ConfigDict(extra="allow")

    retracted: bool = False
    crossref_match: bool | None = None
    flags: list[str] = Field(default_factory=list)


class Source(Record):
    doi: str | None = None
    url: str | None = None
    title: str = ""
    authors: str = ""
    year: int | None = None
    venue: str = ""
    oa_status: str | None = None
    fulltext_available: bool = False
    fetched_text_sha256: str | None = None
    fetched_from: str | None = None  # europepmc | unpaywall | core | arxiv | abstract | fixture
    retrieved_at: str | None = None
    vetting: Vetting | None = None
    checked: str | None = None  # CITATIONS.md "Checked" column: how much a person read

    @model_validator(mode="after")
    def _ident(self) -> Source:
        if not self.doi and not self.url:
            raise ValueError("a source needs a doi or a url")
        return self


class Passage(Record):
    """Verbatim text found by code in a fetched document. Never typed by a model."""

    source_id: str
    char_start: int
    char_end: int
    text: str
    section: str = ""
    page: str | None = None
    table: str | None = None
    figure: str | None = None
    context_before: str = Field(default="", max_length=600)
    context_after: str = Field(default="", max_length=600)


class Quantity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float | None = None
    low: float | None = None
    high: float | None = None
    unit: str
    canonical_value: float | None = None
    canonical_low: float | None = None
    canonical_high: float | None = None
    canonical_unit: str | None = None

    @model_validator(mode="after")
    def _one_form(self) -> Quantity:
        if self.value is None and (self.low is None or self.high is None):
            raise ValueError("a quantity needs value, or low and high")
        return self

    def text(self) -> str:
        if self.value is not None:
            return f"{self.value:g} {self.unit}"
        return f"{self.low:g} to {self.high:g} {self.unit}"

    def library_value(self) -> float | list[float]:
        if self.value is not None:
            return self.value
        assert self.low is not None and self.high is not None
        return [self.low, self.high]


class Claim(Record):
    library_path: str
    quantity: Quantity
    species: str | None = None
    conditions: str | None = None
    method: Method | None = None
    source_ids: list[str] = Field(default_factory=list)
    passage_id: str | None = None
    mapping: MappingKind | None = None
    status: ClaimStatus = "unverified"
    provenance: Provenance = "library"
    revision: int = 0
    library_notes: str | None = None  # the library entry's own notes on this value
    library_confidence: str | None = None
    notes_by_reviewer: str = ""
    unverified_reason: str | None = None

    @model_validator(mode="after")
    def _passage(self) -> Claim:
        if self.status in NEEDS_PASSAGE and not self.passage_id:
            raise ValueError(f"a claim with status {self.status!r} needs a passage (N2)")
        return self

    @property
    def clade(self) -> str:
        return self.library_path.split(".")[0]


class FieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    old: Any = None
    new: Any = None


class ClaimRevision(BaseModel):
    """One revision of a claim, as recorded in the event log."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    revision: int
    changes: list[FieldChange]
    reason: str
    by: str
    at: str
    approval_id: str | None = None


HypothesisStatus = Literal["open", "supported", "refuted", "inconclusive"]


class Hypothesis(Record):
    statement: str
    origin: str  # open_question:<path>#<n> | request:<id> | experiment:<id> | user
    linked_claims: list[str] = Field(default_factory=list)
    prediction: str = ""
    status: HypothesisStatus = "open"
    priority: Literal["critical", "high", "medium", "low"] = "medium"
    target: str | None = None
    consumers: list[str] = Field(default_factory=list)
    experiments: list[str] = Field(default_factory=list)


ExperimentStatus = Literal["queued", "running", "done", "failed"]


class ArmResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    arm: str
    tracks: int | None = None
    hits: int | None = None
    hit_rate: float | None = None
    chance_hit_rate: float | None = None
    median_delay_s: float | None = None
    unattributed_fires_per_h: float | None = None
    latency_p99_ms: float | None = None
    errors: list[str] = Field(default_factory=list)


class Experiment(Record):
    hypothesis_id: str | None = None
    claim_ids: list[str] = Field(default_factory=list)
    library_patch: str = ""
    library_patch_sha: str = ""
    profiles_sha: str = ""
    world_sha: str = ""
    bench_sha: str = ""
    sequences: list[str] = Field(default_factory=list)
    arms: list[str] = Field(default_factory=list)
    seed: int = 0
    command: list[str] = Field(default_factory=list)
    started: str | None = None
    finished: str | None = None
    status: ExperimentStatus = "queued"
    run_dir: str | None = None
    outputs: dict[str, ArmResult] = Field(default_factory=dict)
    report_headline: str = ""
    baseline_experiment_id: str | None = None
    is_baseline: bool = False
    cache_key: str = ""
    error: str | None = None


ProposalStatus = Literal["draft", "approved", "pr_open", "merged", "closed"]


class Proposal(Record):
    action_id: str | None = None  # the pending action whose Approval this proposal carries
    claim_ids: list[str]
    library_file: str
    yaml_patch: str
    experiment_ids: list[str] = Field(default_factory=list)
    impact_ids: list[str] = Field(default_factory=list)
    rationale: str
    status: ProposalStatus = "draft"
    pr_url: str | None = None
    patch_file: str | None = None
    pr_body_file: str | None = None
    title: str = ""


class Approval(Record):
    target_kind: Literal["claim_revision", "new_claim", "hypothesis", "proposal"]
    target_id: str  # the pending action id
    decision: Literal["approve", "reject"]
    by: str
    rationale: str
    at: str = Field(default_factory=now)

    @field_validator("by")
    @classmethod
    def _human(cls, v: str) -> str:
        if not v.startswith("user:"):
            raise ValueError("only a person can approve (N1)")
        return v

    @field_validator("rationale")
    @classmethod
    def _sentence(cls, v: str) -> str:
        words = v.strip().split()
        if len(words) < 3:
            raise ValueError("rationale must be at least one sentence")
        return v.strip()


class ProfileRow(BaseModel):
    """One derived value that changes: a gain-LUT point, an attention weight, a scalar."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    section: str  # head name or "reflex"
    field: str
    point: str = ""  # "at 0.1 lux", a weight name, or "" for a scalar
    old: float | str | None = None
    new: float | str | None = None
    delta_pct: float | None = None


class FieldSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str
    section: str
    field: str
    rule: str
    changed_points: int
    max_abs_delta_pct: float | None = None
    sentence: str


class ReflexRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arm: str
    profile: str
    commit_threshold_old: float
    commit_threshold_new: float
    theta_old: float
    theta_new: float


class WorldRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    param: str
    old: float
    new: float
    kind_old: str
    kind_new: str
    source_new: str
    measured: bool
    changed: bool


class Impact(Record):
    """What a candidate library change does, computed by code (N4). Stored; never recomputed."""

    library_path: str
    library_file: str
    old_quantity: dict[str, Any]
    new_quantity: dict[str, Any]
    library_patch: str
    library_patch_sha: str
    profiles_sha: str
    consumers: list[str] = Field(default_factory=list)
    profile_rows: list[ProfileRow] = Field(default_factory=list)
    field_summaries: list[FieldSummary] = Field(default_factory=list)
    unchanged_fields: int = 0
    reflex_rows: list[ReflexRow] = Field(default_factory=list)
    world_available: bool = False
    world_rows: list[WorldRow] = Field(default_factory=list)
    world_reached: bool = False
    world_note: str = ""
    sentences: list[str] = Field(default_factory=list)


CandidateStatus = Literal["open", "in_design", "adopted", "rejected"]


class CircuitCandidate(Record):
    """A library value scored for turning into a circuit prior (``candidates.py``).

    ``likelihood`` and ``quality`` are computed here from the ledger; ``circuitability`` is
    what ``senseai-circuits assess`` says about it (None when circuits was not available).
    People move ``status`` from Studio's Design tab; nothing here changes the library.
    """

    clade: str  # the target clade (library file stem), not necessarily where it was measured
    sense: str
    modality: str | None = None
    mechanism: str = ""
    claim_ids: list[str]
    library_path: str
    evidence_clade: str
    likelihood: float = Field(ge=0, le=1)
    quality: float = Field(ge=0, le=1)
    score_parts: dict[str, Any] = Field(default_factory=dict)
    proposed: dict[str, Any] = Field(default_factory=dict)
    circuitability: dict[str, Any] | None = None
    circuits_version: str | None = None
    circuits_note: str = ""
    priority: float | None = None
    status: CandidateStatus = "open"
    design_ids: list[str] = Field(default_factory=list)
    decided_by: str | None = None
    decision_rationale: str = ""


ActionKind = Literal["claim_revision", "new_claim", "hypothesis", "proposal"]
ActionStatus = Literal["pending", "approved", "rejected", "failed"]


class PendingAction(Record):
    """What an agent write tool produces: nothing changes until a person approves it (N1)."""

    kind: ActionKind
    conversation_id: str | None = None
    tool_use_id: str | None = None
    input: dict[str, Any]
    card: dict[str, Any] = Field(default_factory=dict)
    status: ActionStatus = "pending"
    approval_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    at: str
    actor: str
    type: str
    payload: dict[str, Any]


__all__ = [
    "CLAIM_STATUSES",
    "HUMAN_ONLY_STATUSES",
    "NEEDS_PASSAGE",
    "ActionKind",
    "ActionStatus",
    "Approval",
    "ArmResult",
    "CandidateStatus",
    "CircuitCandidate",
    "Claim",
    "ClaimRevision",
    "ClaimStatus",
    "Event",
    "Experiment",
    "FieldChange",
    "FieldSummary",
    "Hypothesis",
    "Impact",
    "MappingKind",
    "Passage",
    "PendingAction",
    "ProfileRow",
    "Proposal",
    "Provenance",
    "Quantity",
    "Record",
    "ReflexRow",
    "Source",
    "Vetting",
    "WorldRow",
    "new_id",
    "now",
]
