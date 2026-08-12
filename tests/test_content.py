import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from ai_daily.content import JudgeBatch, draft_selected, judge_events
from ai_daily.models import DraftItem, Event, JudgeDecision, RawItem, SourceTier


def event() -> Event:
    item = RawItem(
        source="official",
        source_tier=SourceTier.A,
        source_item_id="1",
        url="https://example.com/release",
        title="Official release",
        summary="A new model was released.",
        discovered_at=datetime(2026, 8, 12, tzinfo=UTC),
    )
    return Event(
        event_id="event-1",
        canonical_url=item.url,
        title=item.title,
        summary=item.summary,
        items=[item],
    )


class FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self, role: str, output_type: type[BaseModel], instructions: str, prompt: str
    ) -> Any:
        if output_type is JudgeBatch:
            self.calls += 1
            bundles = json.loads(prompt)
            return JudgeBatch(
                decisions=[
                    JudgeDecision(
                        event_id=bundle["event_id"],
                        selected=True,
                        category="模型与平台",
                        relevance=95,
                        confidence=0.9,
                        reason="Official material change",
                        evidence_ids=[bundle["evidence"][0]["evidence_id"]],
                    )
                    for bundle in bundles
                ]
            )
        return DraftItem(
            event_id="event-1",
            category="模型与平台",
            title="模型正式发布",
            tldr="官方发布了新模型。",
            facts=["官方公告确认发布。"],
            why_it_matters="开发者可以开始评估。",
            action="阅读官方说明并运行小规模评测。",
            evidence_ids=["event-1-1"],
        )


async def test_judge_and_editor_preserve_evidence_ids() -> None:
    gateway = FakeGateway()
    decisions = await judge_events(gateway, [event()])  # type: ignore[arg-type]
    drafts = await draft_selected(gateway, [event()], decisions, 1, 1)  # type: ignore[arg-type]
    assert drafts[0].evidence_ids == ["event-1-1"]


class BadGateway(FakeGateway):
    async def generate(
        self, role: str, output_type: type[BaseModel], instructions: str, prompt: str
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(update={"evidence_ids": ["invented"]})
        return value


async def test_editor_cannot_invent_evidence() -> None:
    decisions = await judge_events(BadGateway(), [event()])  # type: ignore[arg-type]
    try:
        await draft_selected(BadGateway(), [event()], decisions, 1, 1)  # type: ignore[arg-type]
    except ValueError as error:
        assert "unknown evidence" in str(error)
    else:
        raise AssertionError("invented evidence was accepted")


class DuplicateJudgeGateway(FakeGateway):
    async def generate(
        self, role: str, output_type: type[BaseModel], instructions: str, prompt: str
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, JudgeBatch):
            return JudgeBatch(decisions=[value.decisions[0], value.decisions[0]])
        return value


async def test_judge_must_return_each_event_exactly_once() -> None:
    try:
        await judge_events(DuplicateJudgeGateway(), [event()])  # type: ignore[arg-type]
    except ValueError as error:
        assert "exactly once" in str(error)
    else:
        raise AssertionError("duplicate judge decision was accepted")


def test_judge_batch_is_valid_structured_tool_output() -> None:
    assert JudgeBatch.model_json_schema()["type"] == "object"
    Agent(TestModel(), output_type=JudgeBatch)


async def test_judge_splits_large_candidate_sets_into_small_batches() -> None:
    gateway = FakeGateway()
    events = [event().model_copy(update={"event_id": f"event-{index}"}) for index in range(21)]

    decisions = await judge_events(gateway, events)  # type: ignore[arg-type]

    assert len(decisions) == 21
    assert gateway.calls == 3
