import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from ai_daily.config import load_config
from ai_daily.content import (
    JudgeBatch,
    draft_selected,
    judge_events,
    plan_digest,
    validate_editorial_plan,
)
from ai_daily.models import (
    DraftItem,
    EditorialInsight,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    Event,
    JudgeDecision,
    RawItem,
    SourceTier,
)


def event() -> Event:
    item = RawItem(
        source="official",
        source_label="Official Lab",
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


def plan() -> EditorialPlan:
    selection = EditorialSelection(
        event_id="event-1",
        tier=EditorialTier.LEAD,
        category="模型与平台",
        headline="模型正式发布",
        brief="官方发布新模型，可开始评估。",
        importance=95,
        confidence=0.9,
        reason="Official material change",
        evidence_ids=["event-1-1"],
    )
    return EditorialPlan(
        today_highlight="新模型正式发布。",
        selections=[selection],
        editor_viewpoint=[
            EditorialInsight(text="模型更新加速。", evidence_ids=["event-1-1"]),
            EditorialInsight(text="真实评测更重要。", evidence_ids=["event-1-1"]),
        ],
    )


class FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
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
            tldr="官方发布了新模型。",
            facts=["官方公告确认发布。"],
            why_it_matters="开发者可以开始评估。",
            action="阅读官方说明并运行小规模评测。",
            evidence_ids=["event-1-1"],
        )


async def test_judge_and_editor_preserve_evidence_ids() -> None:
    gateway = FakeGateway()
    decisions = await judge_events(gateway, [event()])  # type: ignore[arg-type]
    drafts = await draft_selected(gateway, [event()], plan())  # type: ignore[arg-type]
    assert decisions[0].evidence_ids == ["event-1-1"]
    assert drafts[0].evidence_ids == ["event-1-1"]


class BadGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(update={"evidence_ids": ["invented"]})
        return value


async def test_editor_cannot_invent_evidence() -> None:
    try:
        await draft_selected(BadGateway(), [event()], plan())  # type: ignore[arg-type]
    except ValueError as error:
        assert "unknown evidence" in str(error)
    else:
        raise AssertionError("invented evidence was accepted")


class WrongDraftEventGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(update={"event_id": "wrong-event"})
        return value


async def test_editor_cannot_change_draft_event() -> None:
    with pytest.raises(ValueError, match="changed event_id"):
        await draft_selected(WrongDraftEventGateway(), [event()], plan())  # type: ignore[arg-type]


class InventedJudgeEvidenceGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, JudgeBatch):
            value.decisions[0].evidence_ids = ["invented"]
        return value


async def test_judge_cannot_invent_evidence() -> None:
    with pytest.raises(ValueError, match="unknown evidence"):
        await judge_events(InventedJudgeEvidenceGateway(), [event()])  # type: ignore[arg-type]


class DuplicateJudgeGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, JudgeBatch):
            return JudgeBatch(decisions=[value.decisions[0], value.decisions[0]])
        return value


async def test_judge_must_return_each_event_exactly_once() -> None:
    gateway = DuplicateJudgeGateway()
    try:
        await judge_events(gateway, [event()])  # type: ignore[arg-type]
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


async def test_judge_stops_after_first_failed_batch() -> None:
    class FailingGateway(FakeGateway):
        async def generate(
            self,
            role: str,
            output_type: type[BaseModel],
            instructions: str,
            prompt: str,
            validator: Any = None,
        ) -> Any:
            self.calls += 1
            raise RuntimeError("provider failed")

    gateway = FailingGateway()
    events = [event().model_copy(update={"event_id": f"event-{index}"}) for index in range(21)]

    with pytest.raises(RuntimeError, match="provider failed"):
        await judge_events(gateway, events)  # type: ignore[arg-type]

    assert gateway.calls == 1


def numbered_event(index: int) -> Event:
    value = event()
    item = value.items[0].model_copy(
        update={
            "source": f"source-{index}",
            "source_label": f"Source {index}",
            "source_item_id": str(index),
            "url": f"https://example.com/{index}",
            "title": f"Candidate {index}",
        }
    )
    return Event(
        event_id=f"event-{index}",
        canonical_url=item.url,
        title=item.title,
        summary=f"Evidence for candidate {index}",
        items=[item],
        score=80 - index,
    )


def valid_global_plan() -> EditorialPlan:
    tiers = [EditorialTier.LEAD] * 4 + [EditorialTier.FOLLOW] * 5
    tiers += [EditorialTier.BRIEF] * 8
    categories = ["模型与平台", "行业动态", "国内 AI", "前沿研究", "值得试的项目"]
    selections = [
        EditorialSelection(
            event_id=f"event-{index}",
            tier=tier,
            category=categories[index % len(categories)],
            headline=f"候选新闻 {index}",
            brief="这是一条经全局比较后保留的重要消息。",
            importance=95 - index,
            confidence=0.9,
            reason="全局重要性较高",
            evidence_ids=[f"event-{index}-1"],
        )
        for index, tier in enumerate(tiers)
    ]
    return EditorialPlan(
        today_highlight="模型、工具和产业均有重要变化。",
        selections=selections,
        editor_viewpoint=[
            EditorialInsight(text="产品能力持续收敛。", evidence_ids=["event-0-1"]),
            EditorialInsight(text="开发工具更重视可靠性。", evidence_ids=["event-1-1"]),
        ],
    )


def test_editorial_selection_schema_reserves_brief_for_tier() -> None:
    value = plan().selections[0].model_dump()
    value["category"] = "快讯"

    with pytest.raises(ValueError):
        EditorialSelection.model_validate(value)


class PlanningGateway:
    def __init__(self, output: EditorialPlan) -> None:
        self.output = output
        self.candidate_count = 0
        self.output_schema: dict[str, Any] = {}

    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
    ) -> Any:
        self.candidate_count = len(json.loads(prompt))
        self.output_schema = output_type.model_json_schema()
        return output_type.model_validate(_grouped_plan(self.output))


def _grouped_plan(value: EditorialPlan) -> dict[str, object]:
    groups = {
        tier.value: [
            selection.model_dump(exclude={"tier"})
            for selection in value.selections
            if selection.tier == tier
        ]
        for tier in EditorialTier
    }
    return {
        "today_highlight": value.today_highlight,
        "editor_viewpoint": value.editor_viewpoint,
        **groups,
    }


async def test_global_editor_compares_all_candidates_and_can_correct_initial_judge() -> None:
    events = [numbered_event(index) for index in range(17)]
    decisions = [
        JudgeDecision(
            event_id=value.event_id,
            selected=index != 0,
            category="模型与平台",
            relevance=20 if index == 0 else 80,
            confidence=0.8,
            reason="初筛意见",
            evidence_ids=[f"{value.event_id}-1"],
        )
        for index, value in enumerate(events)
    ]
    gateway = PlanningGateway(valid_global_plan())

    result = await plan_digest(  # type: ignore[arg-type]
        gateway, events, decisions, load_config(Path("config")).pipeline
    )

    assert gateway.candidate_count == 17
    assert result.selections[0].event_id == "event-0"
    assert gateway.output_schema["properties"]["lead"]["minItems"] == 4
    assert gateway.output_schema["properties"]["follow"]["maxItems"] == 7
    assert gateway.output_schema["properties"]["brief"]["minItems"] == 8


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate", "duplicate"),
        ("unknown-event", "unknown event"),
        ("unknown-evidence", "unknown evidence"),
        ("unselected-insight", "unselected evidence"),
        ("tier-order", "tiers are not ordered"),
        ("importance-order", "not ordered by importance"),
        ("lead-quota", "lead count"),
        ("research-quota", "too many detailed research"),
        ("category-breadth", "lacks category breadth"),
    ],
)
def test_editorial_plan_gates_fail_closed(mutation: str, message: str) -> None:
    events = [numbered_event(index) for index in range(17)]
    plan_value = valid_global_plan()
    pipeline = load_config(Path("config")).pipeline
    if mutation == "duplicate":
        plan_value.selections[1] = plan_value.selections[0]
    elif mutation == "unknown-event":
        plan_value.selections[0].event_id = "missing"
    elif mutation == "unknown-evidence":
        plan_value.selections[0].evidence_ids = ["invented"]
    elif mutation == "unselected-insight":
        plan_value.editor_viewpoint[0].evidence_ids = ["event-99-1"]
    elif mutation == "tier-order":
        plan_value.selections[0], plan_value.selections[4] = (
            plan_value.selections[4],
            plan_value.selections[0],
        )
    elif mutation == "importance-order":
        plan_value.selections[0].importance = 1
    elif mutation == "lead-quota":
        for selection in plan_value.selections[:4]:
            selection.tier = EditorialTier.FOLLOW
    elif mutation == "research-quota":
        for selection in plan_value.selections[:3]:
            selection.category = "前沿研究"
    elif mutation == "category-breadth":
        for selection in plan_value.selections[:9]:
            selection.category = "模型与平台"
    with pytest.raises(ValueError, match=message):
        validate_editorial_plan(plan_value, events, pipeline)


def test_editorial_plan_allows_distinct_major_news_from_one_source() -> None:
    events = [numbered_event(index) for index in range(17)]
    for event_value in events[:4]:
        event_value.items[0].source = "same-major-source"
        event_value.items[0].source_label = "Same Major Source"

    validate_editorial_plan(valid_global_plan(), events, load_config(Path("config")).pipeline)


def test_unselected_viewpoint_evidence_error_lists_allowed_ids() -> None:
    events = [numbered_event(index) for index in range(17)]
    plan_value = valid_global_plan()
    plan_value.editor_viewpoint[0].evidence_ids = ["event-99-1"]

    with pytest.raises(ValueError) as error:
        validate_editorial_plan(plan_value, events, load_config(Path("config")).pipeline)

    assert "unselected evidence" in str(error.value)
    assert "event-99-1" in str(error.value)
    assert "event-0-1" in str(error.value)


def test_viewpoint_cannot_use_uncited_evidence_from_a_selected_event() -> None:
    events = [numbered_event(index) for index in range(17)]
    second_item = (
        events[0]
        .items[0]
        .model_copy(
            update={
                "source_item_id": "corroborating",
                "url": "https://example.com/corroborating",
            }
        )
    )
    events[0].items.append(second_item)
    plan_value = valid_global_plan()
    plan_value.editor_viewpoint[0].evidence_ids = ["event-0-2"]

    with pytest.raises(ValueError, match="unselected evidence"):
        validate_editorial_plan(plan_value, events, load_config(Path("config")).pipeline)


async def test_planning_prompt_uses_configured_detail_caps() -> None:
    config = load_config(Path("config")).pipeline.model_copy(
        update={"max_research_details": 1, "max_source_details": 3}
    )
    gateway = PlanningGateway(valid_global_plan())

    captured: dict[str, str] = {}

    async def generate(
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
    ) -> EditorialPlan:
        captured["instructions"] = instructions
        return output_type.model_validate(_grouped_plan(gateway.output))

    gateway.generate = generate  # type: ignore[method-assign]
    events = [numbered_event(index) for index in range(17)]
    decisions = [
        JudgeDecision(
            event_id=value.event_id,
            selected=True,
            category="模型与平台",
            relevance=80,
            confidence=0.8,
            reason="初筛意见",
            evidence_ids=[f"{value.event_id}-1"],
        )
        for value in events
    ]

    with pytest.raises(ValueError, match="too many detailed research"):
        await plan_digest(gateway, events, decisions, config)  # type: ignore[arg-type]

    assert "前沿研究最多 1 条" in captured["instructions"]
    assert "同一来源通常不超过 3 条" in captured["instructions"]
    assert "重大新闻，可以例外" in captured["instructions"]
    assert "不要为了来源均衡删除重大新闻" in captured["instructions"]
    assert "快讯由 brief 数组表示" in captured["instructions"]
    assert "每条只能引用已经进入三个数组的 evidence_ids" in captured["instructions"]
    assert "禁止引用未选候选" in captured["instructions"]
