"""Composing the persisted record: shrink the issue, never fake it."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

import factories
from ai_daily.composer import (
    RANKED_BRIEF_LIMIT,
    ComposeError,
    build_brief_only_publication,
    build_full_publication,
    build_judged_publication,
    build_ranked_publication,
)
from ai_daily.degradation import DegradationTracker, FailureClass
from ai_daily.models import EditorialPlan, EditorialTier, Event, SourceTimeKind
from ai_daily.publication import LEVEL_NOTICE, DailyPublication, PublicationLevel

TARGET = factories.TARGET_DATE
NOW = factories.GENERATED_AT


def _events(count: int) -> list[Event]:
    return [
        factories.event(index, published_at=datetime(2026, 8, 12, 8 + index, tzinfo=UTC))
        for index in range(count)
    ]


def _full_plan(count: int) -> EditorialPlan:
    return factories.plan(
        [
            factories.selection(
                index,
                tier=EditorialTier.LEAD if index == 0 else EditorialTier.FOLLOW,
                importance=90 - index,
            )
            for index in range(count)
        ]
    )


# ------------------------------------------------------------------ full issue


def test_a_full_plan_with_every_draft_publishes_an_l0_issue() -> None:
    events = _events(2)
    tracker = DegradationTracker()

    record = build_full_publication(
        TARGET, _full_plan(2), [factories.draft(0), factories.draft(1)], events, tracker, NOW
    )

    assert record.level is PublicationLevel.L0
    assert [story.event_id for story in record.details] == ["event-0", "event-1"]
    assert record.briefs == []
    assert record.notice is None
    assert record.marker_is_valid()
    assert [viewpoint.text for viewpoint in record.viewpoints] == [
        "产品更新更关注落地。",
        "来源覆盖决定选题质量。",
    ]


def test_a_selection_with_no_draft_is_demoted_to_a_brief() -> None:
    events = _events(3)
    tracker = DegradationTracker()

    record = build_full_publication(
        TARGET, _full_plan(3), [factories.draft(0), factories.draft(2)], events, tracker, NOW
    )

    assert [story.event_id for story in record.details] == ["event-0", "event-2"]
    assert [brief.event_id for brief in record.briefs] == ["event-1"]
    assert tracker.failures == [FailureClass.DRAFT_PARTIAL]
    assert record.level is PublicationLevel.L1
    assert record.notice == LEVEL_NOTICE[PublicationLevel.L1]
    assert record.degradation_reasons == ["部分详报起草失败"]
    assert record.marker_is_valid()


def test_a_demoted_brief_keeps_the_editor_text_and_never_invents_one() -> None:
    events = _events(2)

    record = build_full_publication(
        TARGET, _full_plan(2), [factories.draft(0)], events, DegradationTracker(), NOW
    )

    demoted = record.briefs[0]
    assert demoted.headline == "编辑标题 1"
    assert demoted.brief == "编辑给出的第 1 条摘要。"
    assert [str(ref.url) for ref in demoted.sources] == ["https://example.test/story-1"]


def test_an_l2_ceiling_publishes_briefs_only_rather_than_half_an_issue() -> None:
    events = _events(3)
    tracker = DegradationTracker()
    tracker.record(FailureClass.BUDGET_EXHAUSTED)  # ceiling L2A

    record = build_full_publication(
        TARGET,
        _full_plan(3),
        [factories.draft(index) for index in range(3)],
        events,
        tracker,
        NOW,
    )

    assert record.level is PublicationLevel.L2A
    assert record.details == []
    assert {brief.event_id for brief in record.briefs} == {"event-0", "event-1", "event-2"}
    assert record.viewpoints == []
    assert record.notice == LEVEL_NOTICE[PublicationLevel.L2A]
    assert record.marker_is_valid()


def test_a_selection_outside_the_candidate_pool_is_refused() -> None:
    with pytest.raises(ComposeError, match="not a candidate"):
        build_full_publication(
            TARGET, _full_plan(2), [factories.draft(0)], _events(1), DegradationTracker(), NOW
        )


# ------------------------------------------------------------- brief-only paths


def test_judged_briefs_come_from_the_selected_decisions_only() -> None:
    events = _events(3)
    decisions = [
        factories.judge_decision(0, relevance=90),
        factories.judge_decision(1, selected=False),
        factories.judge_decision(2, relevance=70),
    ]
    tracker = DegradationTracker()
    tracker.record(FailureClass.PLAN_FAILED)

    record = build_judged_publication(TARGET, decisions, events, tracker, NOW)

    assert record.level is PublicationLevel.L2A
    assert {brief.event_id for brief in record.briefs} == {"event-0", "event-2"}
    assert record.details == []
    assert record.marker_is_valid()
    assert all(brief.headline == f"Source story {brief.event_id[-1]}" for brief in record.briefs)


def test_ranked_briefs_are_capped_and_ordered_newest_first() -> None:
    events = _events(RANKED_BRIEF_LIMIT + 3)
    tracker = DegradationTracker()
    tracker.record(FailureClass.JUDGE_FAILED)

    record = build_ranked_publication(TARGET, events, tracker, NOW)

    assert record.level is PublicationLevel.L2B
    assert len(record.briefs) == RANKED_BRIEF_LIMIT
    assert [brief.published_at for brief in record.briefs] == sorted(
        (brief.published_at for brief in record.briefs), reverse=True
    )
    assert {brief.category for brief in record.briefs} == {"快讯"}
    assert record.highlight == f"本期共 {RANKED_BRIEF_LIMIT} 条快讯，按发布时间排列。"
    assert record.marker_is_valid()


def test_ranked_output_takes_the_highest_scoring_events() -> None:
    events = [
        factories.event(index, published_at=datetime(2026, 8, 12, 8, tzinfo=UTC), score=index)
        for index in range(RANKED_BRIEF_LIMIT + 2)
    ]

    record = build_ranked_publication(TARGET, events, DegradationTracker(), NOW)

    assert "event-0" not in {brief.event_id for brief in record.briefs}
    assert "event-13" in {brief.event_id for brief in record.briefs}


def test_a_brief_only_issue_deduplicates_by_event() -> None:
    briefs = [factories.brief_card(0), factories.brief_card(0), factories.brief_card(1)]

    record = build_brief_only_publication(
        TARGET, briefs, PublicationLevel.L2A, DegradationTracker(), NOW
    )

    assert [brief.event_id for brief in record.briefs] == ["event-0", "event-1"]


def test_no_briefs_at_all_is_a_blocked_day_rather_than_an_empty_issue() -> None:
    tracker = DegradationTracker()

    record = build_brief_only_publication(TARGET, [], PublicationLevel.L2B, tracker, NOW)

    assert record.level is PublicationLevel.L3
    assert record.has_content is False
    assert tracker.failures == [FailureClass.CANDIDATES_EXHAUSTED]
    assert record.notice == LEVEL_NOTICE[PublicationLevel.L3]


# ------------------------------------------------------------ publication time


def test_a_community_submitted_time_is_never_used_as_a_publication_time() -> None:
    community = [factories.event(0, time_kind=SourceTimeKind.COMMUNITY_SUBMITTED)]

    with pytest.raises(ComposeError, match="community submission time"):
        build_full_publication(
            TARGET, _full_plan(1), [factories.draft(0)], community, DegradationTracker(), NOW
        )


def test_an_undated_event_is_never_given_a_publication_time() -> None:
    undated = [factories.event(0, published_at=None)]

    with pytest.raises(ComposeError, match="no verified publication time"):
        build_full_publication(
            TARGET, _full_plan(1), [factories.draft(0)], undated, DegradationTracker(), NOW
        )


def test_brief_builders_drop_a_community_submitted_event_instead_of_dating_it() -> None:
    events = [factories.event(0, time_kind=SourceTimeKind.COMMUNITY_SUBMITTED), factories.event(1)]

    ranked = build_ranked_publication(TARGET, events, DegradationTracker(), NOW)
    judged = build_judged_publication(
        TARGET,
        [factories.judge_decision(0), factories.judge_decision(1)],
        events,
        DegradationTracker(),
        NOW,
    )

    assert [brief.event_id for brief in ranked.briefs] == ["event-1"]
    assert [brief.event_id for brief in judged.briefs] == ["event-1"]


# ------------------------------------------------------------------- markers


BUILDERS: dict[str, Callable[[DegradationTracker], DailyPublication]] = {
    "full": lambda tracker: build_full_publication(
        TARGET, _full_plan(2), [factories.draft(0), factories.draft(1)], _events(2), tracker, NOW
    ),
    "full_demoted": lambda tracker: build_full_publication(
        TARGET, _full_plan(2), [factories.draft(0)], _events(2), tracker, NOW
    ),
    "judged": lambda tracker: build_judged_publication(
        TARGET, [factories.judge_decision(0)], _events(1), tracker, NOW
    ),
    "ranked": lambda tracker: build_ranked_publication(TARGET, _events(2), tracker, NOW),
    "blocked": lambda tracker: build_brief_only_publication(
        TARGET, [], PublicationLevel.L2B, tracker, NOW
    ),
}


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_every_builder_signs_what_it_produces(name: str) -> None:
    record = BUILDERS[name](DegradationTracker())

    assert record.marker_is_valid()
    assert record.marker == record.compute_marker()
    assert record.target_date == TARGET
    assert record.generated_at == NOW
