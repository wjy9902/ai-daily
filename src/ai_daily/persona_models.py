from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, HttpUrl, model_validator

from ai_daily.models import (
    BudgetConfig,
    EditorialPlan,
    Event,
    EvidenceBundle,
    JudgeDecision,
    ModelRun,
    StrictModel,
)
from ai_daily.publication import PublicationLevel

SCHEMA_VERSION: Literal[1] = 1


class PersonaRuntimeConfig(StrictModel):
    enabled: bool = True
    column_id: str = Field(default="jiayu-editorial", pattern=r"^[a-z0-9-]+$")
    column_name: str = Field(default="甲鱼主编版", min_length=1, max_length=40)
    memories_path: str = "config/persona/memories.yaml"
    constitution_path: str = "config/persona/source-artifacts/editorial-constitution-v1.md"
    candidate_limit: int = Field(default=30, ge=1, le=30)
    memory_limit: int = Field(default=16, ge=1, le=16)
    analyst_concurrency: int = Field(default=3, ge=1, le=3)
    max_call_cost_cny: float = Field(default=1.5, gt=0, le=5)
    baseline_window_days: int = Field(default=90, ge=1, le=90)
    evidence_retention_days: int = Field(default=120, ge=120)
    standard_min_chars: int = Field(default=700, ge=100)
    standard_max_chars: int = Field(default=1600, ge=300)
    no_major_min_chars: int = Field(default=300, ge=100)
    no_major_max_chars: int = Field(default=600, ge=200)
    publish_mode: Literal["disabled", "draft_only"] = "draft_only"
    ai_disclosure: str = "本文由 AI 参与资料整理和初稿生成，并经过证据约束与自动审稿。"
    budget: BudgetConfig

    @model_validator(mode="after")
    def validate_lengths(self) -> PersonaRuntimeConfig:
        if self.standard_min_chars > self.standard_max_chars:
            raise ValueError("standard_min_chars cannot exceed standard_max_chars")
        if self.no_major_min_chars > self.no_major_max_chars:
            raise ValueError("no_major_min_chars cannot exceed no_major_max_chars")
        return self


def canonical_json(value: Any) -> bytes:
    normalized = unicodedata.normalize(
        "NFC",
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return normalized.encode("utf-8")


def sha256_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class MemoryKind(StrEnum):
    PRINCIPLE = "principle"
    PREFERENCE = "preference"
    EXPERIENCE = "experience"
    DECISION = "decision"
    OUTCOME = "outcome"
    STYLE_RULE = "style_rule"


class EditorialMemory(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    memory_id: str = Field(pattern=r"^mem-[a-z0-9-]+$")
    kind: MemoryKind
    statement: str = Field(min_length=1, max_length=500)
    topics: list[str] = Field(min_length=1, max_length=12)
    audiences: list[str] = Field(default_factory=list, max_length=8)
    product_stages: list[str] = Field(default_factory=list, max_length=8)
    valid_from: date
    valid_until: date | None = None
    status: Literal["candidate", "approved", "expired", "revoked"]
    confidence: Literal["explicit", "inferred"]
    usage: Literal["ranking_only", "analysis_context", "first_person_allowed"]
    source_path: str
    source_artifact_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_excerpt: str = Field(min_length=1, max_length=1000)
    source_context: str = Field(default="", max_length=1500)
    publicity: Literal["public", "internal", "prohibited"]
    supersedes: list[str] = Field(default_factory=list)
    conflict_group_id: str | None = None

    @model_validator(mode="after")
    def validate_usage(self) -> EditorialMemory:
        if self.confidence == "inferred" and self.usage != "ranking_only":
            raise ValueError("inferred memory is ranking_only")
        if self.usage == "first_person_allowed" and not (
            self.confidence == "explicit"
            and self.publicity == "public"
            and self.status == "approved"
        ):
            raise ValueError("first_person_allowed requires explicit public approved memory")
        if self.publicity == "internal" and self.usage != "ranking_only":
            raise ValueError("internal memory is ranking_only")
        if self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("valid_until cannot precede valid_from")
        return self


class RetrievedMemory(StrictModel):
    memory_id: str
    score: int = Field(ge=0)


class PersonaContext(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    target_date: date
    upstream_marker: str = Field(pattern=r"^[a-f0-9]{64}$")
    constitution_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    memory_context_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    candidate_event_ids: list[str] = Field(max_length=30)
    retrieved_memories: list[RetrievedMemory] = Field(max_length=16)
    has_conflicts: bool = False


class ShortlistAuditEntry(StrictModel):
    event_id: str
    selected: bool
    phase: Literal["editorial_tier", "category_round_robin", "score_fallback", "omitted"]
    reason: str
    rank: int | None = None


class PersonaShortlist(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    event_ids: list[str] = Field(min_length=1, max_length=30)
    audit: list[ShortlistAuditEntry]


class PlanSelection(StrictModel):
    event_id: str
    grade: Literal["S", "A", "B", "C"]
    importance_reason: str = Field(min_length=1, max_length=500)
    evidence_ids: list[str] = Field(min_length=1)
    memory_ids: list[str] = Field(default_factory=list)


class OmittedEvent(StrictModel):
    event_id: str
    reason: Literal["noise", "duplicate", "insufficient_evidence", "capacity"]


class PersonaPlan(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    edition_type: Literal["standard", "no_major_update"]
    today_thesis: str = Field(min_length=1, max_length=500)
    selections: list[PlanSelection] = Field(max_length=5)
    watchlist_event_ids: list[str] = Field(max_length=2)
    omitted: list[OmittedEvent]

    @model_validator(mode="after")
    def validate_edition_type(self) -> PersonaPlan:
        expanded = [item for item in self.selections if item.grade in {"S", "A"}]
        if self.edition_type == "standard" and not expanded:
            raise ValueError("standard edition requires an S/A selection")
        if self.edition_type == "no_major_update" and expanded:
            raise ValueError("no_major_update cannot contain S/A selections")
        if len({item.event_id for item in self.selections}) != len(self.selections):
            raise ValueError("persona selections contain duplicate event ids")
        return self


class ClaimQuote(StrictModel):
    source_kind: Literal["current_evidence", "baseline_evidence", "experience_memory"]
    source_id: str
    quote: str = Field(min_length=12, max_length=1000)


class AnalysisClaim(StrictModel):
    claim_id: str = Field(pattern=r"^claim-[a-z0-9-]+$")
    field_path: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=800)
    claim_type: Literal[
        "current_fact",
        "baseline_fact",
        "experience_fact",
        "inference",
        "recommendation",
        "uncertainty",
    ]
    current_evidence_ids: list[str] = Field(default_factory=list)
    baseline_evidence_ids: list[str] = Field(default_factory=list)
    experience_memory_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    quotes: list[ClaimQuote] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_provenance_shape(self) -> AnalysisClaim:
        required = {
            "current_fact": self.current_evidence_ids,
            "baseline_fact": self.baseline_evidence_ids,
            "experience_fact": self.experience_memory_ids,
        }
        if self.claim_type in required and not required[self.claim_type]:
            raise ValueError(f"{self.claim_type} requires its matching source ids")
        if self.claim_type in required and not self.quotes:
            raise ValueError(f"{self.claim_type} requires a verbatim quote")
        if self.claim_type not in required and not any(
            (
                self.current_evidence_ids,
                self.baseline_evidence_ids,
                self.experience_memory_ids,
                self.artifact_ids,
            )
        ):
            raise ValueError(f"{self.claim_type} requires at least one input reference")
        return self


class PublicTextBlock(StrictModel):
    block_id: str = Field(pattern=r"^block-[a-z0-9-]+$")
    block_type: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=2000)
    claim_ids: list[str] = Field(min_length=1, max_length=12)


class AnalysisItem(StrictModel):
    event_id: str
    grade: Literal["S", "A"]
    headline_block: PublicTextBlock
    confirmed_change_block: PublicTextBlock
    delta_from_before_block: PublicTextBlock | None = None
    importance_block: PublicTextBlock
    product_implication_block: PublicTextBlock
    recommended_action_block: PublicTextBlock | None = None
    counter_case_block: PublicTextBlock
    watch_signal_block: PublicTextBlock
    claims: list[AnalysisClaim] = Field(min_length=1, max_length=12)
    evidence_ids: list[str] = Field(min_length=1)
    memory_ids: list[str] = Field(default_factory=list)
    analysis_confidence: float = Field(ge=0, le=1)


class AnalystOutput(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    item: AnalysisItem


class EditionDraft(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    column_id: str = Field(pattern=r"^[a-z0-9-]+$")
    target_date: date
    edition_type: Literal["standard", "no_major_update"]
    title_block: PublicTextBlock
    digest_block: PublicTextBlock
    thesis_block: PublicTextBlock
    items: list[AnalysisItem] = Field(max_length=5)
    watchlist_blocks: list[PublicTextBlock] = Field(max_length=2)
    source_links: list[HttpUrl]
    ai_disclosure: str = Field(min_length=1, max_length=300)
    input_marker: str = Field(pattern=r"^[a-f0-9]{64}$")
    claims: list[AnalysisClaim] = Field(min_length=1)


class PersonaEdition(EditionDraft):
    payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload.pop("payload_sha256", None)
        return payload

    def compute_payload_sha256(self) -> str:
        return sha256_payload(self.canonical_payload())

    def hash_is_valid(self) -> bool:
        return self.payload_sha256 == self.compute_payload_sha256()


class CritiqueFinding(StrictModel):
    blocker_id: str = Field(pattern=r"^blocker-[a-z0-9-]+$")
    severity: Literal["blocker", "warning"]
    field_path: str
    issue_type: Literal[
        "unsupported_entailment",
        "source_conflict",
        "invented_experience",
        "product_impact_gap",
        "missing_counter_case",
        "internal_inconsistency",
        "style_violation",
    ]
    explanation: str = Field(min_length=1, max_length=800)
    evidence_ids: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    status: Literal["open", "resolved"]


class Critique(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    draft_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_round: Literal[1, 2]
    findings: list[CritiqueFinding]

    @property
    def open_blockers(self) -> list[CritiqueFinding]:
        return [
            item for item in self.findings if item.severity == "blocker" and item.status == "open"
        ]


class FinalizerResolution(StrictModel):
    blocker_id: str
    resolution: str = Field(min_length=1, max_length=800)
    changed_fields: list[str] = Field(min_length=1)


class FinalizerOutput(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    draft: EditionDraft
    resolutions: list[FinalizerResolution]


class BaselineMatch(StrictModel):
    event_id: str
    matched_event_id: str | None = None
    baseline_evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(min_length=1, max_length=500)


class BaselineResolution(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    matches: list[BaselineMatch] = Field(max_length=5)


class UpstreamSnapshot(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    target_date: date
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    publication_level: PublicationLevel
    publication_marker: str = Field(pattern=r"^[a-f0-9]{64}$")
    events: list[Event]
    evidence_bundles: list[EvidenceBundle]
    decisions: list[JudgeDecision]
    editorial_plan: EditorialPlan | None
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload.pop("snapshot_sha256", None)
        return payload

    def hash_is_valid(self) -> bool:
        return self.snapshot_sha256 == sha256_payload(self.canonical_payload())


class PersonaRunResult(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    target_date: date
    editorial_state: Literal["ready", "held"]
    site_state: Literal["not_attempted", "published", "failed"] = "not_attempted"
    wechat_state: Literal[
        "disabled", "not_attempted", "draft_creating", "draft_verified", "failed", "unknown"
    ] = "disabled"
    aggregate_state: Literal["held", "ready", "draft_complete", "partial"]
    edition_path: str | None = None
    reason: str | None = None
    model_runs: list[ModelRun] = Field(default_factory=list)


class RenderReceipt(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    edition_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    markdown_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    html_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    renderer_version: str
    template_version: str


class AuthorizationRecord(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    authorization_id: str
    issuer: str
    column_id: str
    account_stable_id: str
    environment: Literal["production", "staging"]
    allowed_actions: list[Literal["create_draft", "update_draft", "reconcile_draft"]]
    valid_from: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    key_id: str
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReleaseAttestation(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    column_id: str
    target_date: date
    publication_slot: str
    edition_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    markdown_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    html_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    renderer_version: str
    template_version: str
    authorization_id: str
    account_stable_id: str
    account_fingerprint: str
    key_id: str
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")


class DailyAutoManifest(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    publication_slot: str
    edition_payload_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    account_stable_id: str
    account_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    authorization_id: str
    release_attestation: ReleaseAttestation
    editorial_state: Literal["ready", "held"]
    site_state: Literal["not_attempted", "published", "failed"]
    wechat_state: Literal["not_attempted", "draft_verified", "unknown", "failed"]
    publish_mode: Literal["draft_only"] = "draft_only"
    render_receipt_path: str
    wechat_receipt_path: str | None = None


class OperationReceipt(StrictModel):
    schema_version: Literal[1] = SCHEMA_VERSION
    kind: Literal["wechat_draft", "wechat_reconcile"]
    attempt_id: str
    publication_slot: str
    operation: Literal["create_draft", "update_draft", "reconcile_draft"]
    state: Literal["prepared", "submitted", "verified", "failed", "unknown"]
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    account_fingerprint: str
    response_sha256: str | None = None
    remote_id: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def validate_state_fields(self) -> OperationReceipt:
        if self.state == "verified" and (not self.response_sha256 or not self.remote_id):
            raise ValueError("verified operation receipt requires response hash and remote id")
        if self.state == "unknown" and not self.error_code:
            raise ValueError("unknown operation receipt requires an error code")
        if self.state in {"prepared", "submitted"} and self.error_code:
            raise ValueError("non-error operation receipt cannot contain an error code")
        return self


class WechatTarget(StrictModel):
    """Immutable bytes and metadata for one production draft attempt."""

    schema_version: Literal[1] = SCHEMA_VERSION
    edition: PersonaEdition
    html: str
    render_receipt: RenderReceipt
    authorization: AuthorizationRecord
    attestation: ReleaseAttestation
    cover_media_id: str
    author: str
    request_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
