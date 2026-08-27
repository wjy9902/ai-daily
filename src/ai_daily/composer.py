"""Turn pipeline output into the persisted publication record.

Each degraded level has its own builder rather than being a mutated copy of a
full issue. Demoting a normal plan in place would keep tripping the strict
quota validation that a full issue is checked against, which is how "publish
something smaller" turns into "publish nothing".
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from .degradation import DegradationTracker
from .models import (
    DraftItem,
    EditorialPlan,
    EditorialTier,
    Event,
    JudgeDecision,
    RawItem,
    SourceTimeKind,
)
from .publication import (
    BriefCard,
    Claim,
    DailyPublication,
    PublicationLevel,
    SourceRef,
    Viewpoint,
)

#: How many first-party links to show under a story.
MAX_SOURCE_REFS = 3

#: How many items a model-free issue carries.
RANKED_BRIEF_LIMIT = 12


class ComposeError(RuntimeError):
    """The pipeline output cannot be turned into a publishable record."""


def _source_refs(event: Event) -> list[SourceRef]:
    refs: list[SourceRef] = []
    for item in event.items[:MAX_SOURCE_REFS]:
        refs.append(
            SourceRef(
                title=item.title,
                url=item.url,
                source=item.source_label or item.source,
                published_at=item.published_at,
                time_kind=item.source_time_kind,
            )
        )
    return refs


def _publication_time(event: Event) -> datetime:
    """The event's verified publication time.

    A community submission time is when somebody posted a link, not when the
    thing happened, so it can never stand in for a publication time.
    """

    if event.published_at is None:
        raise ComposeError(f"{event.event_id} has no verified publication time")
    if event.source_time_kind is SourceTimeKind.COMMUNITY_SUBMITTED:
        raise ComposeError(f"{event.event_id} only has a community submission time")
    return event.published_at


def _first_dated_item(event: Event) -> RawItem | None:
    for item in event.items:
        if item.published_at is not None:
            return item
    return None


def _claims(draft: DraftItem) -> list[Claim]:
    return [
        Claim(text=fact.text, evidence_id=fact.evidence_id, quote=fact.quote)
        for fact in draft.facts
    ]


def build_full_publication(
    target_date: date,
    plan: EditorialPlan,
    drafts: list[DraftItem],
    events: list[Event],
    tracker: DegradationTracker,
    now: datetime | None = None,
) -> DailyPublication:
    """Build an L0 or L1 issue from a complete editorial plan."""

    from .publication import StoryCard  # local import keeps the module graph flat

    by_id = {event.event_id: event for event in events}
    drafts_by_id = {draft.event_id: draft for draft in drafts}

    details: list[StoryCard] = []
    briefs: list[BriefCard] = []
    demoted = 0

    for selection in plan.selections:
        event = by_id.get(selection.event_id)
        if event is None:
            raise ComposeError(f"selection {selection.event_id} is not a candidate")
        refs = _source_refs(event)
        published_at = _publication_time(event)

        if selection.tier is EditorialTier.BRIEF:
            briefs.append(
                BriefCard(
                    event_id=selection.event_id,
                    category=selection.category,
                    headline=selection.headline,
                    brief=selection.brief,
                    sources=refs,
                    published_at=published_at,
                )
            )
            continue

        draft = drafts_by_id.get(selection.event_id)
        if draft is None:
            # A detailed story with no draft becomes a brief instead of an
            # error: a shorter issue beats no issue.
            demoted += 1
            briefs.append(
                BriefCard(
                    event_id=selection.event_id,
                    category=selection.category,
                    headline=selection.headline,
                    brief=selection.brief,
                    sources=refs,
                    published_at=published_at,
                )
            )
            continue

        details.append(
            StoryCard(
                event_id=selection.event_id,
                tier=selection.tier,
                category=selection.category,
                headline=selection.headline,
                tldr=draft.tldr,
                facts=_claims(draft),
                why_it_matters=draft.why_it_matters,
                action=draft.action,
                caveat=draft.caveat,
                sources=refs,
                published_at=published_at,
            )
        )

    if demoted:
        from .degradation import FailureClass

        tracker.record(FailureClass.DRAFT_PARTIAL)

    level = tracker.clamp(PublicationLevel.L0)
    if level in (PublicationLevel.L2A, PublicationLevel.L2B, PublicationLevel.L3):
        # A failure severe enough to forbid detailed stories means the details
        # we did build must not be shown as if the issue were intact.
        return build_brief_only_publication(
            target_date=target_date,
            briefs=briefs + [_story_to_brief(story) for story in details],
            level=level,
            tracker=tracker,
            now=now,
        )

    viewpoints = [Viewpoint(text=insight.text, sources=[]) for insight in plan.editor_viewpoint]
    return _finalise(
        DailyPublication(
            target_date=target_date,
            level=level,
            generated_at=now or datetime.now(UTC),
            highlight=plan.today_highlight,
            details=details,
            briefs=briefs,
            viewpoints=viewpoints,
            notice=None,
            degradation_reasons=tracker.reasons(),
        )
    )


def _story_to_brief(story: object) -> BriefCard:
    from .publication import StoryCard

    assert isinstance(story, StoryCard)
    return BriefCard(
        event_id=story.event_id,
        category=story.category,
        headline=story.headline,
        brief=story.tldr[:240],
        sources=story.sources,
        published_at=story.published_at,
    )


def build_judged_publication(
    target_date: date,
    decisions: list[JudgeDecision],
    events: list[Event],
    tracker: DegradationTracker,
    now: datetime | None = None,
) -> DailyPublication:
    """L2A: editorial prose failed, but relevance judgements survived."""

    by_id = {event.event_id: event for event in events}
    selected = [decision for decision in decisions if decision.selected]
    selected.sort(key=lambda decision: decision.relevance, reverse=True)

    briefs: list[BriefCard] = []
    for decision in selected[:RANKED_BRIEF_LIMIT]:
        event = by_id.get(decision.event_id)
        if event is None:
            continue
        try:
            published_at = _publication_time(event)
        except ComposeError:
            continue
        briefs.append(
            BriefCard(
                event_id=event.event_id,
                category=decision.category,
                headline=event.title[:100],
                brief=(event.summary or event.title)[:240],
                sources=_source_refs(event),
                published_at=published_at,
            )
        )

    return build_brief_only_publication(
        target_date=target_date,
        briefs=briefs,
        level=tracker.clamp(PublicationLevel.L2A),
        tracker=tracker,
        now=now,
    )


def build_ranked_publication(
    target_date: date,
    events: list[Event],
    tracker: DegradationTracker,
    now: datetime | None = None,
) -> DailyPublication:
    """L2B: no usable model output at all, so ranking alone builds the issue."""

    ranked = sorted(events, key=lambda event: event.score, reverse=True)
    briefs: list[BriefCard] = []
    for event in ranked:
        if len(briefs) >= RANKED_BRIEF_LIMIT:
            break
        try:
            published_at = _publication_time(event)
        except ComposeError:
            continue
        briefs.append(
            BriefCard(
                event_id=event.event_id,
                category="快讯",
                headline=event.title[:100],
                brief=(event.summary or event.title)[:240],
                sources=_source_refs(event),
                published_at=published_at,
            )
        )

    return build_brief_only_publication(
        target_date=target_date,
        briefs=briefs,
        level=tracker.clamp(PublicationLevel.L2B),
        tracker=tracker,
        now=now,
    )


def build_brief_only_publication(
    target_date: date,
    briefs: list[BriefCard],
    level: PublicationLevel,
    tracker: DegradationTracker,
    now: datetime | None = None,
) -> DailyPublication:
    from .degradation import FailureClass
    from .publication import LEVEL_NOTICE

    if not briefs:
        tracker.record(FailureClass.CANDIDATES_EXHAUSTED)
        level = PublicationLevel.L3

    seen: set[str] = set()
    unique: list[BriefCard] = []
    for brief in briefs:
        if brief.event_id in seen:
            continue
        seen.add(brief.event_id)
        unique.append(brief)
    unique.sort(key=lambda brief: brief.published_at, reverse=True)

    return _finalise(
        DailyPublication(
            target_date=target_date,
            level=level,
            generated_at=now or datetime.now(UTC),
            highlight=_ranked_highlight(unique),
            details=[],
            briefs=unique,
            viewpoints=[],
            notice=LEVEL_NOTICE[level],
            degradation_reasons=tracker.reasons(),
        )
    )


def _ranked_highlight(briefs: list[BriefCard]) -> str:
    """A factual, computed summary line. Never model-written at this level."""

    if not briefs:
        return "今日未产出条目。"
    return f"本期共 {len(briefs)} 条快讯，按发布时间排列。"[:300]


def _finalise(publication: DailyPublication) -> DailyPublication:
    from .publication import LEVEL_NOTICE

    notice = publication.notice or LEVEL_NOTICE[publication.level]
    return publication.model_copy(update={"notice": notice}).signed()
