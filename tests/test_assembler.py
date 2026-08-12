from datetime import UTC, date, datetime

import pytest

from ai_daily.assembler import assemble_markdown
from ai_daily.models import DraftItem, Event, RawItem, SourceTier


def _event(index: int) -> Event:
    item = RawItem(
        source="official",
        source_tier=SourceTier.A,
        source_item_id=str(index),
        url=f"https://example.com/{index}",
        title=f"Release {index}",
        discovered_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return Event(
        event_id=f"event-{index}",
        canonical_url=item.url,
        title=item.title,
        summary="Official release",
        items=[item],
    )


def _draft(index: int) -> DraftItem:
    return DraftItem(
        event_id=f"event-{index}",
        category="模型与平台",
        title=f"模型发布 {index}",
        tldr="官方发布新模型。",
        facts=["甲" * 500 for _ in range(5)],
        why_it_matters="开发者需要评估。",
        action="阅读公告并测试。",
        evidence_ids=[f"event-{index}-1"],
    )


def test_issue_body_size_is_bounded() -> None:
    events = [_event(index) for index in range(12)]
    drafts = [_draft(index) for index in range(12)]
    with pytest.raises(ValueError, match="Issue body limit"):
        assemble_markdown(date(2026, 8, 12), drafts, events)
