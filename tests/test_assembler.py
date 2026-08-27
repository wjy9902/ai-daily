from datetime import UTC, date, datetime

import pytest
from lxml import html
from marko.ext.gfm import gfm as marko

from ai_daily.assembler import assemble_markdown
from ai_daily.models import (
    DraftItem,
    EditorialInsight,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    Event,
    FactClaim,
    RawItem,
    SourceChannel,
    SourceTier,
    SourceTimeKind,
)


def _event(index: int) -> Event:
    item = RawItem(
        source=f"official-{index}",
        source_label=f"官方来源 {index}",
        source_tier=SourceTier.A,
        source_channel=SourceChannel.OFFICIAL,
        source_item_id=str(index),
        url=f"https://example.com/{index}",
        title=f"Release {index}",
        published_at=datetime(2026, 8, 12, tzinfo=UTC),
        discovered_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return Event(
        event_id=f"event-{index}",
        canonical_url=item.url,
        title=item.title,
        summary="Official release",
        published_at=item.published_at,
        items=[item],
    )


def _selection(index: int, tier: EditorialTier) -> EditorialSelection:
    return EditorialSelection(
        event_id=f"event-{index}",
        tier=tier,
        category="模型与平台",
        headline=f"模型发布 {index}",
        brief="官方发布新模型，值得进行针对性评估。",
        importance=90,
        confidence=0.9,
        reason="官方发布",
        evidence_ids=[f"event-{index}-1"],
    )


def _draft(index: int, fact_size: int = 20) -> DraftItem:
    evidence_id = f"event-{index}-1"
    quote = "官方公告确认该模型已经正式发布。"
    return DraftItem(
        event_id=f"event-{index}",
        tldr="官方发布新模型。",
        tldr_evidence_id=evidence_id,
        tldr_quote=quote,
        facts=[
            FactClaim(text="甲" * fact_size, evidence_id=evidence_id, quote=quote) for _ in range(4)
        ],
        why_it_matters="开发者需要评估。",
        action="阅读公告并测试。",
        evidence_ids=[evidence_id],
    )


def _plan(selections: list[EditorialSelection]) -> EditorialPlan:
    return EditorialPlan(
        today_highlight="模型与开发工具出现多项实质更新。",
        selections=selections,
        editor_viewpoint=[
            EditorialInsight(text="模型竞争继续加速。", evidence_ids=["event-0-1"]),
            EditorialInsight(text="评测应转向真实任务。", evidence_ids=["event-0-1"]),
        ],
    )


def test_digest_restores_editorial_hierarchy_and_human_sources() -> None:
    events = [_event(index) for index in range(3)]
    selections = [
        _selection(0, EditorialTier.LEAD),
        _selection(1, EditorialTier.FOLLOW),
        _selection(2, EditorialTier.BRIEF),
    ]
    body = assemble_markdown(date(2026, 8, 12), _plan(selections), [_draft(0), _draft(1)], events)
    assert "## 今日必读" in body
    assert "展开其余 2 条目录" in body
    assert '<ol class="brief-list">' in body
    assert "## 编辑观点" in body
    assert '<a href="https://example.com/0">官方来源 0</a>' in body
    assert ">official-0<" not in body
    assert body.count("来源发布：08-12 08:00 北京时间") == 3
    assert '<time datetime="2026-08-12T08:00:00+08:00">' in body
    document = html.fromstring(marko(body))
    assert document.xpath("//h1") == []
    assert document.xpath("//blockquote") == []
    assert (
        len(
            document.xpath(
                '//*[contains(concat(" ", normalize-space(@class), " "), " story-card ")]'
            )
        )
        == 2
    )


def test_detail_lists_evidence_used_by_both_plan_and_draft() -> None:
    first = _event(0)
    corroborating = first.items[0].model_copy(
        update={
            "source": "corroborating",
            "source_label": "交叉验证来源",
            "source_item_id": "second",
            "url": "https://other.example.com/report",
        }
    )
    event = first.model_copy(update={"items": [first.items[0], corroborating]})
    selection = _selection(0, EditorialTier.LEAD)
    draft = _draft(0).model_copy(update={"evidence_ids": ["event-0-2"]})

    body = assemble_markdown(date(2026, 8, 12), _plan([selection]), [draft], [event])

    assert '<a href="https://example.com/0">官方来源 0</a>' in body
    assert '<a href="https://other.example.com/report">交叉验证来源</a>' in body


def test_viewpoint_keeps_distinct_articles_from_the_same_source() -> None:
    first = _event(0)
    second = _event(1)
    second.items[0].source_label = first.items[0].source_label
    selections = [
        _selection(0, EditorialTier.LEAD),
        _selection(1, EditorialTier.FOLLOW),
    ]
    plan = _plan(selections)
    plan.editor_viewpoint[0].evidence_ids = ["event-0-1", "event-1-1"]

    body = assemble_markdown(date(2026, 8, 12), plan, [_draft(0), _draft(1)], [first, second])
    viewpoint = body.split("## 编辑观点", maxsplit=1)[1]

    assert '<a href="https://example.com/0" title="Release 0">官方来源 0 1/2</a>' in viewpoint
    assert '<a href="https://example.com/1" title="Release 1">官方来源 0 2/2</a>' in viewpoint


def test_source_url_cannot_break_out_of_its_link() -> None:
    value = _event(0)
    malicious = value.items[0].model_copy(
        update={"url": "https://example.com/a)![x](https://tracker.test/pixel"}
    )
    event = value.model_copy(update={"canonical_url": malicious.url, "items": [malicious]})

    body = assemble_markdown(
        date(2026, 8, 12),
        _plan([_selection(0, EditorialTier.LEAD)]),
        [_draft(0)],
        [event],
    )

    document = html.fromstring(marko(body))
    assert document.xpath("//img") == []
    assert len(document.xpath('//a[contains(@href, "tracker.test/pixel")]')) == 3


def test_issue_body_size_is_bounded() -> None:
    events = [_event(index) for index in range(24)]
    selections = [_selection(index, EditorialTier.LEAD) for index in range(24)]
    drafts = [_draft(index, fact_size=500) for index in range(24)]
    with pytest.raises(ValueError, match="Issue body limit"):
        assemble_markdown(date(2026, 8, 12), _plan(selections), drafts, events)


def test_selected_event_without_verified_publication_time_is_rejected() -> None:
    event = _event(0).model_copy(update={"published_at": None})

    with pytest.raises(ValueError, match="verified publication time"):
        assemble_markdown(
            date(2026, 8, 12),
            _plan([_selection(0, EditorialTier.LEAD)]),
            [_draft(0)],
            [event],
        )


def test_repository_change_is_labeled_as_update_not_publication() -> None:
    event = _event(0).model_copy(update={"source_time_kind": SourceTimeKind.REPOSITORY_UPDATED})

    body = assemble_markdown(
        date(2026, 8, 12),
        _plan([_selection(0, EditorialTier.LEAD)]),
        [_draft(0)],
        [event],
    )

    assert "来源更新：08-12 08:00 北京时间" in body
    assert "来源发布：08-12 08:00 北京时间" not in body


def test_community_submission_time_cannot_be_rendered_as_publication() -> None:
    event = _event(0).model_copy(update={"source_time_kind": SourceTimeKind.COMMUNITY_SUBMITTED})

    with pytest.raises(ValueError, match="community submission time"):
        assemble_markdown(
            date(2026, 8, 12),
            _plan([_selection(0, EditorialTier.LEAD)]),
            [_draft(0)],
            [event],
        )
