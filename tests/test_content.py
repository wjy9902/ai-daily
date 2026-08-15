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
    DRAFT_EVIDENCE_EXCERPT_CHARS,
    JUDGE_EVIDENCE_EXCERPT_CHARS,
    QUOTE_MIN_CHARS,
    JudgeBatch,
    draft_selected,
    enforce_lead_corroboration,
    judge_events,
    lead_is_corroborated,
    normalize_quote_text,
    plan_digest,
    quote_supports,
    registrable_domain,
    validate_editorial_plan,
    validate_evidence_quotes,
)
from ai_daily.model_gateway import ModelInvocationFailed
from ai_daily.models import (
    DraftItem,
    EditorialInsight,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    Event,
    Evidence,
    EvidenceBundle,
    FactClaim,
    JudgeDecision,
    RawItem,
    SourceChannel,
    SourceTier,
    SourceTimeKind,
)


def event() -> Event:
    item = RawItem(
        source="official",
        source_label="Official Lab",
        source_tier=SourceTier.A,
        source_channel=SourceChannel.OFFICIAL,
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
        stage: Any = None,
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
        evidence = json.loads(prompt)["bundle"]["evidence"][0]
        evidence_id = evidence["evidence_id"]
        quote = evidence["excerpt"][:60]
        return DraftItem(
            event_id="event-1",
            tldr="官方发布了新模型。",
            tldr_evidence_id=evidence_id,
            tldr_quote=quote,
            facts=[
                FactClaim(text="官方公告确认发布。", evidence_id=evidence_id, quote=quote)
            ],
            why_it_matters="开发者可以开始评估。",
            action="阅读官方说明并运行小规模评测。",
            evidence_ids=["event-1-1"],
        )


async def test_judge_and_editor_preserve_evidence_ids() -> None:
    gateway = FakeGateway()
    decisions, _ = await judge_events(gateway, [event()])  # type: ignore[arg-type]
    drafts, _ = await draft_selected(gateway, [event()], plan())  # type: ignore[arg-type]
    assert decisions[0].evidence_ids == ["event-1-1"]
    assert drafts[0].evidence_ids == ["event-1-1"]


class PromptCaptureGateway(FakeGateway):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[tuple[type[BaseModel], object]] = []

    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        self.prompts.append((output_type, json.loads(prompt)))
        return await super().generate(role, output_type, instructions, prompt, validator)


async def test_judge_uses_compact_evidence_and_draft_uses_full_evidence() -> None:
    long_summary = "x" * 7_000
    value = event()
    item = value.items[0].model_copy(update={"summary": long_summary})
    value = value.model_copy(update={"summary": long_summary, "items": [item]})
    gateway = PromptCaptureGateway()

    await judge_events(gateway, [value])  # type: ignore[arg-type]
    await draft_selected(gateway, [value], plan())  # type: ignore[arg-type]

    judge_payload = next(payload for output, payload in gateway.prompts if output is JudgeBatch)
    draft_payload = next(payload for output, payload in gateway.prompts if output is DraftItem)
    assert len(judge_payload[0]["evidence"][0]["excerpt"]) == JUDGE_EVIDENCE_EXCERPT_CHARS  # type: ignore[index]
    assert len(draft_payload["bundle"]["evidence"][0]["excerpt"]) == DRAFT_EVIDENCE_EXCERPT_CHARS  # type: ignore[index]


class BadGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(update={"evidence_ids": ["invented"]})
        return value


async def test_editor_cannot_invent_evidence() -> None:
    drafts, failures = await draft_selected(  # type: ignore[arg-type]
        BadGateway(), [event()], plan()
    )

    assert drafts == [], "invented evidence was accepted"
    assert failures and "unknown evidence" in failures[0]


class WrongDraftEventGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(update={"event_id": "wrong-event"})
        return value


async def test_editor_cannot_change_draft_event() -> None:
    _, failures = await draft_selected(WrongDraftEventGateway(), [event()], plan())  # type: ignore[arg-type]
    assert failures, "a rejected draft must be reported"
    assert "changed event_id" in failures[0]


class SpeculativeDraftGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(
                update={
                    "why_it_matters": (
                        "若平台默认切换该模型，可能牺牲质量来改善利润率。"
                    ),
                    "caveat": "这只是推测。",
                }
            )
        return value


async def test_editor_must_keep_speculation_out_of_factual_draft_fields() -> None:
    _, failures = await draft_selected(SpeculativeDraftGateway(), [event()], plan())  # type: ignore[arg-type]
    assert failures, "a rejected draft must be reported"
    assert "speculation outside caveat" in failures[0]


class FirstAvailabilityDraftGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(
                update={"why_it_matters": "该模型首次以开源权重形式可用。"}
            )
        return value


async def test_editor_normalizes_repository_update_copy_before_validation() -> None:
    repository_event = event().model_copy(
        update={
            "source_time_kind": SourceTimeKind.REPOSITORY_UPDATED,
            "items": [
                event().items[0].model_copy(
                    update={"source_time_kind": SourceTimeKind.REPOSITORY_UPDATED}
                )
            ],
        }
    )

    drafts, _ = await draft_selected(  # type: ignore[arg-type]
        FakeGateway(), [repository_event], plan()
    )

    assert drafts[0].tldr == "官方更新了新模型。"
    assert [claim.text for claim in drafts[0].facts] == ["官方公告确认更新。"]


async def test_editor_removes_unverified_first_availability_claim() -> None:
    repository_event = event().model_copy(
        update={
            "source_time_kind": SourceTimeKind.REPOSITORY_UPDATED,
            "items": [
                event().items[0].model_copy(
                    update={"source_time_kind": SourceTimeKind.REPOSITORY_UPDATED}
                )
            ],
        }
    )

    drafts, _ = await draft_selected(  # type: ignore[arg-type]
        FirstAvailabilityDraftGateway(), [repository_event], plan()
    )

    assert drafts[0].why_it_matters == "该模型以开源权重形式可用。"


class SpeculativeActionGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(update={"action": "后续可能发布权重，可以尝试本地部署。"})
        return value


async def test_editor_drops_speculative_optional_action() -> None:
    drafts, _ = await draft_selected(SpeculativeActionGateway(), [event()], plan())  # type: ignore[arg-type]

    assert drafts[0].action is None


class PipelineMetadataDraftGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(update={"caveat": "证据摘要在句末被截断，数量不明确。"})
        return value


async def test_editor_cannot_expose_evidence_pipeline_metadata() -> None:
    _, failures = await draft_selected(PipelineMetadataDraftGateway(), [event()], plan())  # type: ignore[arg-type]
    assert failures, "a rejected draft must be reported"
    assert "pipeline metadata" in failures[0]


class InventedJudgeEvidenceGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, JudgeBatch):
            value.decisions[0].evidence_ids = ["invented"]
        return value


async def test_judge_cannot_invent_evidence() -> None:
    # The only batch fails, so nothing was judged and the stage raises. The
    # originating message must survive into the error or the operator is left
    # with a bare exception name.
    with pytest.raises(ModelInvocationFailed, match="unknown evidence"):
        await judge_events(InventedJudgeEvidenceGateway(), [event()])  # type: ignore[arg-type]


class DuplicateJudgeGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, JudgeBatch):
            return JudgeBatch(decisions=[value.decisions[0], value.decisions[0]])
        return value


async def test_judge_must_return_each_event_exactly_once() -> None:
    gateway = DuplicateJudgeGateway()
    try:
        await judge_events(gateway, [event()])  # type: ignore[arg-type]
    except ModelInvocationFailed as error:
        assert "exactly once" in str(error)
    else:
        raise AssertionError("duplicate judge decision was accepted")


def test_judge_batch_is_valid_structured_tool_output() -> None:
    assert JudgeBatch.model_json_schema()["type"] == "object"
    Agent(TestModel(), output_type=JudgeBatch)


async def test_judge_splits_large_candidate_sets_into_small_batches() -> None:
    gateway = FakeGateway()
    events = [event().model_copy(update={"event_id": f"event-{index}"}) for index in range(21)]

    decisions, _ = await judge_events(gateway, events)  # type: ignore[arg-type]

    assert len(decisions) == 21
    assert gateway.calls == 3


async def test_one_bad_batch_does_not_discard_the_others() -> None:
    """A failed batch costs its own candidates, not the whole stage.

    Stopping at the first failure threw away six good batches to punish one bad
    one, which is how a single flaky response used to cost the day's editorial
    input entirely.
    """

    class FlakyGateway(FakeGateway):
        attempts = 0

        async def generate(
            self,
            role: str,
            output_type: type[BaseModel],
            instructions: str,
            prompt: str,
            validator: Any = None,
        stage: Any = None,
        ) -> Any:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("provider failed")
            return await super().generate(role, output_type, instructions, prompt)

    gateway = FlakyGateway()
    events = [event().model_copy(update={"event_id": f"event-{index}"}) for index in range(21)]

    decisions, failures = await judge_events(gateway, events)  # type: ignore[arg-type]

    assert gateway.attempts == 3, "every batch is attempted"
    assert len(decisions) == 11, "the two surviving batches are kept"
    assert failures == ["batch 1/3: RuntimeError: provider failed"]


async def test_every_batch_failing_is_still_a_stage_failure() -> None:
    """Partial tolerance must not silently turn a total outage into success."""

    class DeadGateway(FakeGateway):
        attempts = 0

        async def generate(
            self,
            role: str,
            output_type: type[BaseModel],
            instructions: str,
            prompt: str,
            validator: Any = None,
        stage: Any = None,
        ) -> Any:
            self.attempts += 1
            raise RuntimeError("provider failed")

    gateway = DeadGateway()
    events = [event().model_copy(update={"event_id": f"event-{index}"}) for index in range(21)]

    with pytest.raises(ModelInvocationFailed, match="provider failed"):
        await judge_events(gateway, events)  # type: ignore[arg-type]

    assert gateway.attempts == 3, "every batch is attempted before giving up"


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
        stage: Any = None,
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
    assert result.today_highlight == "；".join(
        selection.headline for selection in result.selections[:4]
    )
    assert gateway.output_schema["properties"]["lead"]["minItems"] == 4
    assert gateway.output_schema["properties"]["follow"]["maxItems"] == 7
    assert gateway.output_schema["properties"]["brief"]["minItems"] == 8


async def test_global_editor_normalizes_repository_update_copy() -> None:
    events = [numbered_event(index) for index in range(17)]
    events[0].source_time_kind = SourceTimeKind.REPOSITORY_UPDATED
    events[0].items[0].source_time_kind = SourceTimeKind.REPOSITORY_UPDATED
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
    plan_value = valid_global_plan()
    plan_value.selections[0].headline = "Qwen开源权重发布"
    plan_value.selections[0].brief = "官方首次发布Qwen开源权重。"
    plan_value.editor_viewpoint[0].text = "Qwen发布推动开源生态。"

    result = await plan_digest(  # type: ignore[arg-type]
        PlanningGateway(plan_value), events, decisions, load_config(Path("config")).pipeline
    )

    assert result.selections[0].headline == "Qwen开源权重更新"
    assert result.selections[0].brief == "官方更新Qwen开源权重"
    assert result.editor_viewpoint[0].text == "Qwen更新推动开源生态。"
    assert result.today_highlight.startswith("Qwen开源权重更新；")


async def test_global_editor_drops_speculative_brief_clause() -> None:
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
    plan_value = valid_global_plan()
    plan_value.selections[0].brief = (
        "模型已通过API提供；权重可能随后发布；支持三档推理等级。"
    )

    result = await plan_digest(  # type: ignore[arg-type]
        PlanningGateway(plan_value), events, decisions, load_config(Path("config")).pipeline
    )

    assert result.selections[0].brief == "模型已通过API提供；支持三档推理等级"


async def test_global_editor_uses_headline_when_brief_is_all_speculation() -> None:
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
    plan_value = valid_global_plan()
    plan_value.selections[0].headline = "DeepSeek V4 Pro已通过API提供"
    plan_value.selections[0].brief = "权重可能随后发布。"

    result = await plan_digest(  # type: ignore[arg-type]
        PlanningGateway(plan_value), events, decisions, load_config(Path("config")).pipeline
    )

    assert result.selections[0].brief == "DeepSeek V4 Pro已通过API提供"


async def test_global_editor_drops_viewpoint_that_cites_unselected_news() -> None:
    events = [numbered_event(index) for index in range(18)]
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
    plan_value = valid_global_plan()
    plan_value.editor_viewpoint.append(
        EditorialInsight(text="未入选新闻观察。", evidence_ids=["event-17-1"])
    )

    result = await plan_digest(  # type: ignore[arg-type]
        PlanningGateway(plan_value), events, decisions, load_config(Path("config")).pipeline
    )

    assert len(result.editor_viewpoint) == 2
    assert all("event-17-1" not in item.evidence_ids for item in result.editor_viewpoint)


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
        ("speculative-headline", "unverified speculation"),
        ("speculative-brief", "unverified speculation"),
        ("sample-extrapolation", "beyond cited samples"),
        ("repository-release-insight", "update as release"),
        ("repository-release-headline", "headline rewrote repository update"),
        ("repository-first-availability", "brief rewrote repository update"),
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
    elif mutation == "speculative-headline":
        plan_value.selections[0].headline = "新模型或随后发布开源权重"
    elif mutation == "speculative-brief":
        plan_value.selections[0].brief = "暂无权重确认，但社区认为开源可能性高。"
    elif mutation == "sample-extrapolation":
        plan_value.editor_viewpoint[0].text = "两个平台数据表明用户选择已全面改变。"
    elif mutation == "repository-release-insight":
        events[0].source_time_kind = SourceTimeKind.REPOSITORY_UPDATED
        events[0].items[0].source_time_kind = SourceTimeKind.REPOSITORY_UPDATED
        plan_value.editor_viewpoint[0].text = "Qwen模型发布推动开源生态。"
    elif mutation == "repository-release-headline":
        events[0].source_time_kind = SourceTimeKind.REPOSITORY_UPDATED
        events[0].items[0].source_time_kind = SourceTimeKind.REPOSITORY_UPDATED
        plan_value.selections[0].headline = "Qwen开源权重发布"
    elif mutation == "repository-first-availability":
        events[0].source_time_kind = SourceTimeKind.REPOSITORY_UPDATED
        events[0].items[0].source_time_kind = SourceTimeKind.REPOSITORY_UPDATED
        plan_value.selections[0].brief = "Qwen模型首次以开源权重形式可用。"
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
        stage: Any = None,
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
    assert "训练数据覆盖" in captured["instructions"]
    assert "已上线产品范围" in captured["instructions"]


WRAPPED_EXCERPT = (
    "官方公告确认，新模型已经在今天正式发布，\n"
    "并且已通过 API 提供给所有开发者。\n"
    "输入价格为每百万 Token 2 美元。"
)
OTHER_EXCERPT = "另一家媒体报道了完全不同的一次内部人事调整安排。"


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        event_id="event-1",
        evidence=[
            Evidence(
                evidence_id="event-1-1",
                url="https://example.com/release",
                title="Official release",
                excerpt=WRAPPED_EXCERPT,
                source="Official Lab",
            ),
            Evidence(
                evidence_id="event-1-2",
                url="https://other.example.org/report",
                title="Second report",
                excerpt=OTHER_EXCERPT,
                source="Other Media",
            ),
        ],
    )


def _quoted_draft(
    *,
    tldr_evidence_id: str = "event-1-1",
    tldr_quote: str = "新模型已经在今天正式发布",
    fact_evidence_id: str = "event-1-1",
    fact_quote: str = "输入价格为每百万 Token 2 美元。",
) -> DraftItem:
    return DraftItem(
        event_id="event-1",
        tldr="官方发布了新模型。",
        tldr_evidence_id=tldr_evidence_id,
        tldr_quote=tldr_quote,
        facts=[
            FactClaim(
                text="模型已通过 API 提供。",
                evidence_id=fact_evidence_id,
                quote=fact_quote,
            )
        ],
        why_it_matters="开发者可以开始评估。",
        evidence_ids=["event-1-1"],
    )


def test_normalize_quote_text_removes_whitespace_and_folds_punctuation() -> None:
    assert normalize_quote_text("模型已经\n通过 API 提供，\t价格不变。") == (
        "模型已经通过API提供,价格不变."
    )


def test_quote_supports_matches_across_line_wrapping() -> None:
    assert quote_supports("新模型已经在今天正式发布，并且已通过 API 提供", WRAPPED_EXCERPT)


def test_quote_supports_matches_across_fullwidth_and_halfwidth_punctuation() -> None:
    assert quote_supports("官方公告确认,新模型已经在今天正式发布", WRAPPED_EXCERPT)
    assert quote_supports("输入价格为每百万Token 2美元．", WRAPPED_EXCERPT)  # noqa: RUF001


def test_quote_supports_rejects_a_quote_that_lives_in_a_different_evidence() -> None:
    assert quote_supports("完全不同的一次内部人事调整安排", OTHER_EXCERPT)
    assert not quote_supports("完全不同的一次内部人事调整安排", WRAPPED_EXCERPT)


@pytest.mark.parametrize("quote", ["已发布", "新模型发布了", "a b c"])
def test_quote_supports_rejects_quotes_under_the_minimum_length(quote: str) -> None:
    assert len(normalize_quote_text(quote)) < QUOTE_MIN_CHARS
    assert not quote_supports(quote, f"前缀 {quote} 后缀")


def test_validate_evidence_quotes_accepts_a_grounded_draft() -> None:
    validate_evidence_quotes(_quoted_draft(), _bundle())


def test_validate_evidence_quotes_rejects_an_invented_evidence_id() -> None:
    with pytest.raises(ValueError) as error:
        validate_evidence_quotes(_quoted_draft(fact_evidence_id="event-1-9"), _bundle())

    assert "facts[0]" in str(error.value)
    assert "event-1-9" in str(error.value)


def test_validate_evidence_quotes_rejects_a_quote_from_another_evidence() -> None:
    with pytest.raises(ValueError) as error:
        validate_evidence_quotes(
            _quoted_draft(fact_quote="完全不同的一次内部人事调整安排"), _bundle()
        )

    assert "facts[0]" in str(error.value)
    assert "event-1-1" in str(error.value)


def test_validate_evidence_quotes_rejects_a_tldr_quote_that_is_not_in_its_evidence() -> None:
    with pytest.raises(ValueError) as error:
        validate_evidence_quotes(_quoted_draft(tldr_evidence_id="event-1-2"), _bundle())

    assert "tldr" in str(error.value)
    assert "event-1-2" in str(error.value)


def test_validate_evidence_quotes_rejects_a_short_quote() -> None:
    # Long enough for the pydantic floor, too short once whitespace is stripped.
    short = "已 经 发 布 了 吧 啊"
    assert len(short) >= QUOTE_MIN_CHARS
    assert len(normalize_quote_text(short)) < QUOTE_MIN_CHARS
    grounded = _bundle().model_copy(
        update={
            "evidence": [
                Evidence(
                    evidence_id="event-1-1",
                    url="https://example.com/release",
                    title="Official release",
                    excerpt=f"公告：{short}。",
                    source="Official Lab",
                )
            ]
        }
    )

    with pytest.raises(ValueError) as error:
        validate_evidence_quotes(_quoted_draft(tldr_quote=short), grounded)

    assert "tldr" in str(error.value)
    assert "event-1-1" in str(error.value)
    assert "shorter than" in str(error.value)


class QuoteMismatchGateway(FakeGateway):
    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt)
        if isinstance(value, DraftItem):
            return value.model_copy(
                update={
                    "facts": [
                        value.facts[0].model_copy(
                            update={"quote": "这句话从未出现在任何证据里面。"}
                        )
                    ]
                }
            )
        return value


async def test_editor_cannot_cite_a_quote_that_is_not_in_the_evidence() -> None:
    _, failures = await draft_selected(QuoteMismatchGateway(), [event()], plan())  # type: ignore[arg-type]
    assert failures, "a rejected draft must be reported"
    assert "not present in evidence_id" in failures[0]


async def test_draft_instructions_require_verbatim_quotes() -> None:
    captured: dict[str, str] = {}
    gateway = FakeGateway()
    inner = gateway.generate

    async def generate(
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        captured["instructions"] = instructions
        return await inner(role, output_type, instructions, prompt, validator)

    gateway.generate = generate  # type: ignore[method-assign]
    await draft_selected(gateway, [event()], plan())  # type: ignore[arg-type]

    assert "逐字复制" in captured["instructions"]
    assert "tldr_quote" in captured["instructions"]
    assert f"至少 {QUOTE_MIN_CHARS} 个字符" in captured["instructions"]
    assert "不可信文本" in captured["instructions"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a", "example.com"),
        ("https://www.example.com/a", "example.com"),
        ("https://news.example.com/a", "example.com"),
        ("https://example.com.cn/a", "example.com.cn"),
        ("https://www.example.com.cn/a", "example.com.cn"),
        ("https://example.co.uk/a", "example.co.uk"),
        ("https://deep.sub.example.co.uk/a", "example.co.uk"),
        ("https://sub.example.org.cn/a", "example.org.cn"),
    ],
)
def test_registrable_domain_reduces_to_etld_plus_one(url: str, expected: str) -> None:
    assert registrable_domain(url) == expected


def _lead_event(
    urls: list[str], channel: SourceChannel = SourceChannel.NEWS
) -> Event:
    items = [
        RawItem(
            source=f"source-{index}",
            source_label=f"Source {index}",
            source_tier=SourceTier.B,
            source_channel=channel,
            source_item_id=str(index),
            url=url,
            title="同一条新闻",
            summary="同一条新闻的报道内容。",
            discovered_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
        for index, url in enumerate(urls)
    ]
    return Event(
        event_id="event-1",
        canonical_url=items[0].url,
        title=items[0].title,
        summary=items[0].summary,
        items=items,
    )


def _lead_plan() -> EditorialPlan:
    return plan()


def test_a_first_party_source_may_lead() -> None:
    assert lead_is_corroborated(
        _lead_event(["https://example.com/release"], SourceChannel.OFFICIAL)
    )
    assert lead_is_corroborated(
        _lead_event(["https://example.com/release"], SourceChannel.RELEASE)
    )


def test_two_distinct_registrable_domains_may_lead() -> None:
    assert lead_is_corroborated(
        _lead_event(["https://example.com/story", "https://other.org/story"])
    )


def test_syndicated_copies_of_one_publisher_may_not_lead() -> None:
    """Two URLs from the same publisher are one source, not corroboration."""

    event = _lead_event(
        ["https://www.example.com/story", "https://news.example.com/story"]
    )
    syndicated = event.model_copy(
        update={
            "items": [
                item.model_copy(update={"source_tier": SourceTier.B})
                for item in event.items
            ]
        }
    )
    assert not lead_is_corroborated(syndicated)


def test_a_single_tier_a_publication_may_lead() -> None:
    """Tier A is this project's existing statement of which mastheads it trusts.

    Without this, a day whose real news was a model release and a supply-chain
    attack led with a benchmark announcement, because clustering never merges
    a story across outlets and only official blogs could qualify.
    """

    event = _lead_event(["https://www.qbitai.com/2026/08/1.html"])
    tier_a = event.model_copy(
        update={
            "items": [
                item.model_copy(update={"source_tier": SourceTier.A})
                for item in event.items
            ]
        }
    )
    assert lead_is_corroborated(tier_a)


def test_a_single_lower_tier_source_may_not_lead() -> None:
    event = _lead_event(["https://example.com/story"])
    for tier in (SourceTier.B, SourceTier.C):
        lowered = event.model_copy(
            update={
                "items": [
                    item.model_copy(update={"source_tier": tier})
                    for item in event.items
                ]
            }
        )
        assert not lead_is_corroborated(lowered)


def test_an_uncorroborated_lead_is_demoted_not_rejected() -> None:
    """The story loses its slot; the issue does not lose its plan.

    As a validator this rejected the whole plan and the model could not
    recover: on a normal day every candidate arrives from a single outlet, so
    only official blog posts qualify and an editor asked to lead with real news
    can never satisfy it.
    """

    adjusted, demoted = enforce_lead_corroboration(
        _lead_plan(), [_lead_event(["https://example.com/story"])]
    )

    assert demoted and "event-1" in demoted[0]
    assert all(
        selection.tier is not EditorialTier.LEAD for selection in adjusted.selections
    )


def test_a_qualified_follow_is_promoted_into_the_vacated_lead_slot() -> None:
    base = _lead_plan()
    lead_id = base.selections[0].event_id
    follow_id = "event-2"
    base = base.model_copy(
        update={
            "selections": [
                base.selections[0],
                base.selections[0].model_copy(
                    update={"event_id": follow_id, "tier": EditorialTier.FOLLOW}
                ),
            ]
        }
    )
    events = [
        _lead_event(["https://example.com/story"]).model_copy(
            update={"event_id": lead_id}
        ),
        _lead_event(
            ["https://official.example/post"], SourceChannel.OFFICIAL
        ).model_copy(update={"event_id": follow_id}),
    ]

    adjusted, demoted = enforce_lead_corroboration(base, events)

    tiers = {item.event_id: item.tier for item in adjusted.selections}
    assert tiers[lead_id] is EditorialTier.FOLLOW
    assert tiers[follow_id] is EditorialTier.LEAD
    assert demoted


def test_follow_and_brief_tiers_are_left_alone() -> None:
    for tier in (EditorialTier.FOLLOW, EditorialTier.BRIEF):
        untouched = _lead_plan()
        untouched.selections[0].tier = tier
        adjusted, demoted = enforce_lead_corroboration(
            untouched, [_lead_event(["https://example.com/story"])]
        )
        assert demoted == []
        assert adjusted.selections[0].tier is tier


def test_plan_validation_leaves_corroboration_to_the_enforcer() -> None:
    """Corroboration is a code-enforced invariant, not a model-facing gate.

    Rejecting the plan here made the constraint unsatisfiable in practice and
    cost whole issues. validate_editorial_plan keeps the rules the model can
    actually act on; enforce_lead_corroboration handles this one by demotion.
    """

    events = [numbered_event(index) for index in range(17)]
    events[0] = events[0].model_copy(
        update={
            "items": [
                events[0].items[0].model_copy(
                    update={
                        "source_channel": SourceChannel.NEWS,
                        "source_tier": SourceTier.B,
                    }
                )
            ]
        }
    )
    plan = valid_global_plan()

    validate_editorial_plan(plan, events, load_config(Path("config")).pipeline)

    _, demoted = enforce_lead_corroboration(plan, events)
    assert demoted, "the enforcer must still catch what the validator no longer does"
