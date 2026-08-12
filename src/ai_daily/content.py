from __future__ import annotations

import json
from collections import defaultdict

from pydantic import BaseModel

from ai_daily.model_gateway import ModelGateway
from ai_daily.models import DraftItem, Event, Evidence, EvidenceBundle, JudgeDecision


class JudgeBatch(BaseModel):
    decisions: list[JudgeDecision]


JUDGE_BATCH_SIZE = 10


def evidence_bundle(event: Event) -> EvidenceBundle:
    evidence = [
        Evidence(
            evidence_id=f"{event.event_id}-{index}",
            url=item.url,
            title=item.title,
            excerpt=(item.summary or item.title)[:4000],
            source=item.source,
        )
        for index, item in enumerate(event.items[:3], start=1)
    ]
    return EvidenceBundle(event_id=event.event_id, evidence=evidence)


async def judge_events(gateway: ModelGateway, events: list[Event]) -> list[JudgeDecision]:
    decisions = []
    for start in range(0, len(events), JUDGE_BATCH_SIZE):
        batch = events[start : start + JUDGE_BATCH_SIZE]
        decisions.extend(await _judge_batch(gateway, batch))
    return decisions


async def _judge_batch(gateway: ModelGateway, events: list[Event]) -> list[JudgeDecision]:
    bundles = [evidence_bundle(event) for event in events]
    result = await gateway.generate(
        "judge",
        JudgeBatch,
        instructions=(
            "你是中文 AI 日报的选题编辑。只依据输入证据判断，不补充外部事实。"
            "每个 event_id 必须恰好返回一个决定，evidence_ids 只能使用输入值。"
            "优先会改变技术或产品行动的官方发布、重要研究和高质量开源项目。"
        ),
        prompt=json.dumps(
            [bundle.model_dump(mode="json") for bundle in bundles], ensure_ascii=False
        ),
    )
    decisions = result.decisions
    by_event = {decision.event_id: decision for decision in decisions}
    expected = {event.event_id for event in events}
    if set(by_event) != expected or len(decisions) != len(events):
        raise ValueError("judge output does not cover every event exactly once")
    allowed = {
        bundle.event_id: {evidence.evidence_id for evidence in bundle.evidence}
        for bundle in bundles
    }
    for decision in decisions:
        if not set(decision.evidence_ids) <= allowed[decision.event_id]:
            raise ValueError("judge referenced unknown evidence")
    return decisions


async def draft_selected(
    gateway: ModelGateway,
    events: list[Event],
    decisions: list[JudgeDecision],
    selected_min: int,
    selected_max: int,
) -> list[DraftItem]:
    selected_decisions = sorted(
        (decision for decision in decisions if decision.selected),
        key=lambda value: (value.relevance, value.confidence),
        reverse=True,
    )[:selected_max]
    if len(selected_decisions) < selected_min:
        raise ValueError(f"only {len(selected_decisions)} events passed the selection gate")
    events_by_id = {event.event_id: event for event in events}
    drafts = []
    for decision in selected_decisions:
        bundle = evidence_bundle(events_by_id[decision.event_id])
        draft = await gateway.generate(
            "editor",
            DraftItem,
            instructions=(
                "你是事实优先的中文技术编辑。只能使用证据包中的事实和 evidence_id。"
                "不要把推测写成事实；信息不足时写入 caveat。action 必须具体、克制。"
            ),
            prompt=json.dumps(
                {"decision": decision.model_dump(), "bundle": bundle.model_dump(mode="json")},
                ensure_ascii=False,
            ),
        )
        if draft.event_id != decision.event_id:
            raise ValueError("editor changed event_id")
        allowed = {evidence.evidence_id for evidence in bundle.evidence}
        if not set(draft.evidence_ids) <= allowed:
            raise ValueError("editor referenced unknown evidence")
        drafts.append(draft)
    return drafts


def group_drafts(drafts: list[DraftItem]) -> dict[str, list[DraftItem]]:
    grouped: dict[str, list[DraftItem]] = defaultdict(list)
    for draft in drafts:
        grouped[draft.category].append(draft)
    return dict(grouped)
