"""Persisted publication record: the single render input for the site.

The pipeline produces one :class:`DailyPublication` per day and writes it to
``published/<date>.json``. Everything downstream (HTML, RSS, verification)
reads that file and nothing else, so a render can be replayed without
re-running collection or the models.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from .models import EditorialCategory, EditorialTier, SourceTimeKind

SCHEMA_VERSION = 4


class PublicationLevel(StrEnum):
    """How complete today's issue is.

    The ladder is ordered: a later member is a strictly poorer issue. Same-day
    republication may only move *up* this ladder, never down.
    """

    L0 = "L0"
    """Full issue: leads, follows, briefs and editor viewpoints."""

    L1 = "L1"
    """Reduced issue: some detailed stories were demoted to briefs."""

    L2A = "L2A"
    """Brief-only issue built from judge decisions, no editorial prose."""

    L2B = "L2B"
    """Brief-only issue built from ranking alone, no model output at all."""

    L3 = "L3"
    """No issue today. The site keeps the previous release plus a notice."""


LEVEL_ORDER: dict[PublicationLevel, int] = {
    PublicationLevel.L0: 0,
    PublicationLevel.L1: 1,
    PublicationLevel.L2A: 2,
    PublicationLevel.L2B: 3,
    PublicationLevel.L3: 4,
}

LEVEL_NOTICE: dict[PublicationLevel, str | None] = {
    PublicationLevel.L0: None,
    PublicationLevel.L1: "本期部分详报因证据不足降为快讯。",
    PublicationLevel.L2A: "本期为快讯模式：编辑环节未能完成，仅保留经筛选的条目。",
    PublicationLevel.L2B: (
        "本期为自动快讯模式：模型环节未能完成，条目由排序直接产生，未经编辑加工。"
    ),
    PublicationLevel.L3: "今日采集受阻，未能出刊。页面保留上一期内容。",
}


def is_upgrade(previous: PublicationLevel, candidate: PublicationLevel) -> bool:
    """True when ``candidate`` is a strictly better issue than ``previous``."""

    return LEVEL_ORDER[candidate] < LEVEL_ORDER[previous]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(StrictModel):
    """One first-party link shown under a story."""

    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    source: str = Field(min_length=1, max_length=120)
    published_at: datetime | None = None
    time_kind: SourceTimeKind = SourceTimeKind.PUBLISHED


class Claim(StrictModel):
    """A factual sentence bound to the evidence excerpt that supports it."""

    text: str = Field(min_length=1, max_length=500)
    evidence_id: str = Field(min_length=1, max_length=120)
    quote: str = Field(min_length=1, max_length=500)


class StoryCard(StrictModel):
    """A detailed story (lead or follow)."""

    event_id: str
    tier: EditorialTier
    category: EditorialCategory
    headline: str = Field(min_length=1, max_length=100)
    tldr: str = Field(min_length=1, max_length=300)
    facts: list[Claim] = Field(min_length=1, max_length=4)
    why_it_matters: str = Field(min_length=1, max_length=500)
    action: str | None = Field(default=None, max_length=400)
    caveat: str | None = Field(default=None, max_length=500)
    sources: list[SourceRef] = Field(min_length=1)
    published_at: datetime


class BriefCard(StrictModel):
    """A one-line item in the 快讯 list."""

    event_id: str
    category: EditorialCategory | Literal["快讯"]
    headline: str = Field(min_length=1, max_length=100)
    brief: str = Field(min_length=1, max_length=240)
    sources: list[SourceRef] = Field(min_length=1)
    published_at: datetime


class Viewpoint(StrictModel):
    text: str = Field(min_length=1, max_length=300)
    sources: list[SourceRef] = Field(default_factory=list)


class DailyPublication(StrictModel):
    """The complete, self-contained record of one day's issue."""

    schema_version: Literal[4] = SCHEMA_VERSION
    target_date: date
    level: PublicationLevel
    generated_at: datetime
    highlight: str = Field(default="", max_length=300)
    details: list[StoryCard] = Field(default_factory=list)
    briefs: list[BriefCard] = Field(default_factory=list)
    viewpoints: list[Viewpoint] = Field(default_factory=list)
    notice: str | None = Field(default=None, max_length=300)
    degradation_reasons: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list
    )
    marker: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")

    def canonical_payload(self) -> dict[str, Any]:
        """The dict the marker is computed over: everything but the marker."""

        payload = self.model_dump(mode="json")
        payload.pop("marker", None)
        return payload

    def compute_marker(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def signed(self) -> DailyPublication:
        """Return a copy whose marker matches its content."""

        return self.model_copy(update={"marker": self.compute_marker()})

    def marker_is_valid(self) -> bool:
        return bool(self.marker) and self.marker == self.compute_marker()

    @property
    def has_content(self) -> bool:
        return bool(self.details or self.briefs)

    @property
    def story_count(self) -> int:
        return len(self.details) + len(self.briefs)


def load_publication(raw: str) -> DailyPublication:
    """Parse a persisted publication and reject one whose marker does not match.

    A truncated or hand-edited file must fail loudly here rather than silently
    publishing content that no longer matches its signature.
    """

    publication = DailyPublication.model_validate_json(raw)
    if not publication.marker_is_valid():
        raise ValueError("publication marker does not match its content")
    return publication
