"""Small builders for the fixtures the publication tests share.

Every builder returns the smallest object that still validates, so a test only
has to say what it actually cares about.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from ai_daily.models import (
    BudgetConfig,
    DraftItem,
    EditorialInsight,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    Event,
    FactClaim,
    JudgeDecision,
    RawItem,
    SourceChannel,
    SourceTier,
    SourceTimeKind,
)
from ai_daily.publication import (
    BriefCard,
    Claim,
    DailyPublication,
    PublicationLevel,
    SourceRef,
    StoryCard,
    Viewpoint,
)

TARGET_DATE = date(2026, 8, 13)
PUBLISHED_AT = datetime(2026, 8, 12, 18, 33, tzinfo=UTC)
GENERATED_AT = datetime(2026, 8, 13, 1, 30, tzinfo=UTC)
SITE = "https://daily.example.test"


def source_ref(
    index: int = 0,
    *,
    time_kind: SourceTimeKind = SourceTimeKind.PUBLISHED,
    published_at: datetime | None = PUBLISHED_AT,
) -> SourceRef:
    return SourceRef(
        title=f"Source story {index}",
        url=f"https://example.test/story-{index}",
        source=f"Source {index}",
        published_at=published_at,
        time_kind=time_kind,
    )


def story_card(
    index: int = 0,
    *,
    tier: EditorialTier = EditorialTier.LEAD,
    published_at: datetime = PUBLISHED_AT,
) -> StoryCard:
    return StoryCard(
        event_id=f"event-{index}",
        tier=tier,
        category="模型与平台",
        headline=f"详报标题 {index}",
        tldr=f"这是第 {index} 条详报的摘要。",
        facts=[
            Claim(
                text=f"事实 {index} 已经确认。",
                evidence_id=f"event-{index}-1",
                quote=f"Fact {index} is confirmed by the source.",
            )
        ],
        why_it_matters="它会改变模型选型的判断。",
        sources=[source_ref(index)],
        published_at=published_at,
    )


def brief_card(
    index: int = 0,
    *,
    headline: str | None = None,
    published_at: datetime = PUBLISHED_AT,
) -> BriefCard:
    return BriefCard(
        event_id=f"event-{index}",
        category="行业动态",
        headline=headline or f"快讯标题 {index}",
        brief=f"这是第 {index} 条快讯的正文。",
        sources=[source_ref(index)],
        published_at=published_at,
    )


def publication(
    *,
    target_date: date = TARGET_DATE,
    level: PublicationLevel = PublicationLevel.L0,
    details: list[StoryCard] | None = None,
    briefs: list[BriefCard] | None = None,
    viewpoints: list[Viewpoint] | None = None,
    highlight: str = "今日模型与产品均有变化。",
    notice: str | None = None,
    degradation_reasons: list[str] | None = None,
    generated_at: datetime = GENERATED_AT,
    sign: bool = True,
) -> DailyPublication:
    """A publishable issue. Brief-only levels get no detailed stories."""

    brief_only = level in (PublicationLevel.L2A, PublicationLevel.L2B)
    if details is None:
        details = [] if brief_only else [story_card()]
    if briefs is None:
        briefs = [brief_card(1)]
    if viewpoints is None:
        viewpoints = [] if brief_only else [Viewpoint(text="竞争转向价格与分发。")]
    record = DailyPublication(
        target_date=target_date,
        level=level,
        generated_at=generated_at,
        highlight=highlight,
        details=details,
        briefs=briefs,
        viewpoints=viewpoints,
        notice=notice,
        degradation_reasons=degradation_reasons or [],
    )
    return record.signed() if sign else record


# ------------------------------------------------------------------- pipeline


def raw_item(
    index: int = 0,
    *,
    summary: str | None = None,
    published_at: datetime | None = PUBLISHED_AT,
    time_kind: SourceTimeKind = SourceTimeKind.PUBLISHED,
) -> RawItem:
    return RawItem(
        source=f"source-{index}",
        source_label=f"Source {index}",
        source_tier=SourceTier.A,
        source_channel=SourceChannel.OFFICIAL,
        source_item_id=str(index),
        url=f"https://example.test/story-{index}",
        title=f"Source story {index}",
        summary=summary if summary is not None else f"Evidence for story {index}.",
        published_at=published_at,
        source_time_kind=time_kind,
        discovered_at=PUBLISHED_AT,
    )


def event(
    index: int = 0,
    *,
    published_at: datetime | None = PUBLISHED_AT,
    time_kind: SourceTimeKind = SourceTimeKind.PUBLISHED,
    score: float = 50,
) -> Event:
    item = raw_item(index, published_at=published_at, time_kind=time_kind)
    return Event(
        event_id=f"event-{index}",
        canonical_url=item.url,
        title=item.title,
        summary=item.summary,
        published_at=published_at,
        source_time_kind=time_kind,
        items=[item],
        score=score,
    )


def selection(
    index: int = 0,
    *,
    tier: EditorialTier = EditorialTier.LEAD,
    importance: int = 90,
) -> EditorialSelection:
    return EditorialSelection(
        event_id=f"event-{index}",
        tier=tier,
        category="模型与平台",
        headline=f"编辑标题 {index}",
        brief=f"编辑给出的第 {index} 条摘要。",
        importance=importance,
        confidence=0.9,
        reason="重要性较高",
        evidence_ids=[f"event-{index}-1"],
    )


def plan(selections: list[EditorialSelection]) -> EditorialPlan:
    return EditorialPlan(
        today_highlight="模型、产品和产业均有重要变化。",
        selections=selections,
        editor_viewpoint=[
            EditorialInsight(text="产品更新更关注落地。", evidence_ids=["event-0-1"]),
            EditorialInsight(text="来源覆盖决定选题质量。", evidence_ids=["event-0-1"]),
        ],
    )


def draft(index: int = 0) -> DraftItem:
    evidence_id = f"event-{index}-1"
    quote = f"Fact {index} is confirmed by the source."
    return DraftItem(
        event_id=f"event-{index}",
        tldr=f"第 {index} 条详报的摘要。",
        tldr_evidence_id=evidence_id,
        tldr_quote=quote,
        facts=[FactClaim(text=f"事实 {index} 已经确认。", evidence_id=evidence_id, quote=quote)],
        why_it_matters="它会改变模型选型的判断。",
        evidence_ids=[evidence_id],
    )


def judge_decision(
    index: int = 0,
    *,
    selected: bool = True,
    relevance: int = 80,
) -> JudgeDecision:
    return JudgeDecision(
        event_id=f"event-{index}",
        selected=selected,
        category="模型与平台",
        relevance=relevance,
        confidence=0.9,
        reason="相关候选",
        evidence_ids=[f"event-{index}-1"],
    )


def budget_config(
    *,
    request_limit: int = 10,
    input_token_limit: int = 100_000,
    output_token_limit: int = 50_000,
    cost_cny_limit: float = 5,
) -> BudgetConfig:
    return BudgetConfig(
        request_limit=request_limit,
        input_token_limit=input_token_limit,
        output_token_limit=output_token_limit,
        cost_cny_limit=cost_cny_limit,
    )
