from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

type Category = Literal["模型与平台", "前沿研究", "值得试的项目", "行业动态", "国内 AI", "快讯"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceTier(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class SourceConfig(StrictModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    kind: Literal["rss", "hackernews", "github_releases", "arxiv", "huggingface", "html"]
    url: HttpUrl
    tier: SourceTier
    enabled: bool = True
    limit: int = Field(default=30, ge=1, le=100)
    timeout_seconds: float = Field(default=20, gt=0, le=60)


class RawItem(StrictModel):
    source: str
    source_tier: SourceTier
    source_item_id: str
    url: HttpUrl
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=10000)
    published_at: datetime | None = None
    discovered_at: datetime
    author: str | None = None
    metrics: dict[str, int | float | str] = Field(default_factory=dict)


class SourceHealth(StrictModel):
    source: str
    tier: SourceTier
    status: Literal["ok", "not_modified", "failed"]
    item_count: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    error: str | None = None


class Event(StrictModel):
    event_id: str
    canonical_url: HttpUrl
    title: str
    summary: str
    published_at: datetime | None = None
    items: list[RawItem] = Field(min_length=1)
    score: float = Field(default=0, ge=0, le=100)


class Evidence(StrictModel):
    evidence_id: str
    url: HttpUrl
    title: str
    excerpt: str = Field(max_length=4000)
    source: str


class EvidenceBundle(StrictModel):
    event_id: str
    evidence: list[Evidence] = Field(min_length=1)


class JudgeDecision(StrictModel):
    event_id: str
    selected: bool
    category: Category
    relevance: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(min_length=1)


class DraftItem(StrictModel):
    event_id: str
    category: Category
    title: str = Field(min_length=1, max_length=120)
    tldr: str = Field(min_length=1, max_length=300)
    facts: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        min_length=1, max_length=5
    )
    why_it_matters: str = Field(min_length=1, max_length=800)
    action: str = Field(min_length=1, max_length=500)
    caveat: str | None = Field(default=None, max_length=500)
    evidence_ids: list[str] = Field(min_length=1)


class ModelRun(StrictModel):
    role: Literal["judge", "editor"]
    requested_provider: str
    requested_model: str
    actual_provider: str
    actual_model: str
    attempt: int = Field(ge=1)
    status: Literal["ok", "failed"]
    fallback_reason: str | None = None
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_cny: float | None = Field(default=None, ge=0)
    error_type: str | None = None


class Publication(StrictModel):
    target_date: date
    issue_number: int | None = None
    issue_url: HttpUrl | None = None
    page_url: HttpUrl | None = None
    rss_url: HttpUrl | None = None
    status: Literal["dry_run", "issue_published", "verified", "failed"]
    marker: str


class ModelEndpoint(StrictModel):
    provider: Literal["alibaba", "deepseek", "ollama"]
    model: str
    timeout_seconds: float = Field(gt=0, le=180)
    max_output_tokens: int = Field(gt=0, le=30000)
    temperature: float = Field(ge=0, lt=2)
    input_cost_cny_per_million: float = Field(ge=0)
    output_cost_cny_per_million: float = Field(ge=0)


class RoleModelConfig(StrictModel):
    primary: ModelEndpoint
    fallback: ModelEndpoint

    @model_validator(mode="after")
    def require_cross_provider_fallback(self) -> RoleModelConfig:
        if self.primary.provider == self.fallback.provider:
            raise ValueError("fallback must use a different provider")
        if self.fallback.provider == "ollama":
            raise ValueError("Ollama cannot be a production fallback")
        return self


class BudgetConfig(StrictModel):
    request_limit: int = Field(gt=0, le=100)
    input_token_limit: int = Field(gt=0)
    output_token_limit: int = Field(gt=0)
    cost_cny_limit: float = Field(gt=0)


class ModelsConfig(StrictModel):
    budget: BudgetConfig
    roles: dict[Literal["judge", "editor"], RoleModelConfig]


class PipelineConfig(StrictModel):
    timezone: str
    site_base_url: HttpUrl
    repository: str = Field(pattern=r"^[^/]+/[^/]+$")
    artifacts_dir: str
    candidate_limit: int = Field(ge=1, le=60)
    selected_min: int = Field(ge=1)
    selected_max: int = Field(ge=1, le=20)
    tier_a_min_coverage: float = Field(ge=0, le=1)
    collection_window_hours: int = Field(gt=0, le=72)
    cluster_window_hours: int = Field(gt=0, le=96)
    history_window_days: int = Field(gt=0, le=90)

    @model_validator(mode="after")
    def validate_selection_range(self) -> PipelineConfig:
        if self.selected_min > self.selected_max:
            raise ValueError("selected_min cannot exceed selected_max")
        return self


class RunArtifact(StrictModel):
    run_id: str
    target_date: date
    items: list[RawItem]
    health: list[SourceHealth]
    events: list[Event]
    model_runs: list[ModelRun]
    publication: Publication | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
