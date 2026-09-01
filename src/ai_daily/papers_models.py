"""Data contracts for the independent daily papers pipeline."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

PAPERS_SCHEMA_VERSION: Literal[1] = 1
PaperTopic = Literal["agent", "模型架构与训练", "推理与对齐", "其他"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaperSignals(StrictModel):
    hf_listed: bool = False
    upvotes: int = Field(default=0, ge=0)
    organization: str | None = None
    org_tier: float = Field(default=0, ge=0, le=1)
    github_repo: str | None = None
    github_stars: int = Field(default=0, ge=0)
    cross_mentions: int = Field(default=0, ge=0, le=2)
    agent_bonus: float = Field(default=0, ge=0, le=1)
    base_score: float = Field(default=0, ge=0)
    final_score: float = Field(default=0, ge=0)


class PaperCandidate(StrictModel):
    arxiv_id: str | None = Field(default=None, pattern=r"^\d{4}\.\d{4,5}$")
    title_key: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=500)
    abstract: str = Field(default="", max_length=20_000)
    authors: str | None = Field(default=None, max_length=4000)
    submitted_at: datetime | None = None
    arxiv_url: HttpUrl
    hf_url: HttpUrl | None = None
    signals: PaperSignals = Field(default_factory=PaperSignals)
    topic: PaperTopic = "其他"
    supplement: bool = False
    supplement_reason: str | None = Field(default=None, max_length=600)


class TopicDecision(StrictModel):
    candidate_id: str
    topic: PaperTopic


class TopicBatch(StrictModel):
    decisions: list[TopicDecision]


class SupplementChoice(StrictModel):
    candidate_id: str
    reason: str = Field(min_length=1, max_length=600)


class SupplementBatch(StrictModel):
    choices: list[SupplementChoice] = Field(max_length=2)


class ExperimentClaim(StrictModel):
    claim: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=12, max_length=2000)


class DeepRead(StrictModel):
    positioning: str = Field(min_length=1, max_length=2000)
    background: str = Field(min_length=1, max_length=6000)
    mechanism: str = Field(min_length=1, max_length=16_000)
    experiment_summary: str = Field(min_length=1, max_length=2000)
    experiments: list[ExperimentClaim] = Field(min_length=1, max_length=12)
    novelty: str = Field(min_length=1, max_length=2000)
    soundness: str = Field(min_length=1, max_length=2000)
    significance: str = Field(min_length=1, max_length=2000)
    limitations: str = Field(min_length=1, max_length=6000)
    follow_up: str = Field(min_length=1, max_length=6000)


class PaperCard(StrictModel):
    arxiv_id: str | None
    title: str = Field(min_length=1, max_length=500)
    abstract: str = Field(default="", max_length=20_000)
    authors: str | None = Field(default=None, max_length=4000)
    arxiv_url: HttpUrl
    hf_url: HttpUrl | None = None
    alphaxiv_url: HttpUrl | None = None
    signals: PaperSignals
    topic: PaperTopic
    deep_read: DeepRead | None = None
    fallback_reason: str | None = Field(default=None, max_length=500)

    @property
    def is_deep_read(self) -> bool:
        return self.deep_read is not None


class PapersPublication(StrictModel):
    """Self-contained paper issue protected by a corruption checksum."""

    schema_version: Literal[1] = PAPERS_SCHEMA_VERSION
    target_date: date
    generated_at: datetime
    papers: list[PaperCard] = Field(min_length=1, max_length=8)
    marker: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload.pop("marker", None)
        return payload

    def compute_marker(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def signed(self) -> PapersPublication:
        return self.model_copy(update={"marker": self.compute_marker()})

    def marker_is_valid(self) -> bool:
        return bool(self.marker) and self.marker == self.compute_marker()

    @property
    def deep_read_count(self) -> int:
        return sum(paper.is_deep_read for paper in self.papers)


def load_papers_publication(raw: str) -> PapersPublication:
    publication = PapersPublication.model_validate_json(raw)
    if not publication.marker_is_valid():
        raise ValueError("papers publication marker does not match its content")
    return publication


class PapersRunArtifact(StrictModel):
    run_id: str
    target_date: date
    generated_at: datetime
    selected: list[PaperCandidate]
    candidates_seen: int = Field(ge=0)
    source_health: list[dict[str, Any]]
    model_runs: list[dict[str, Any]]
    status: Literal["selected", "selection_gate", "published", "publication_gate", "failed"]
    reasons: list[Annotated[str, Field(max_length=500)]] = Field(default_factory=list)
