"""The planner sees short aliases, never the 16-hex event ids it used to mangle.

On 2026-09-25 both plan attempts failed because deepseek-v4-pro copied
``fdaccec414cc35e8`` back as ``fdaccec41435e8``; the retry was told only
"referenced an unknown event" and could not know which one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from test_content import _grouped_plan, numbered_event, valid_global_plan

from ai_daily.config import load_config
from ai_daily.content import plan_digest
from ai_daily.models import EditorialPlan, Event, JudgeDecision


class AliasAnsweringGateway:
    """Answers like the model does: with the ids printed in the prompt.

    ``event-N`` in the fixture plan is the Nth numbered candidate, so it is
    rewritten to whatever alias the prompt gave candidate N. The validator runs
    as the real gateway runs it, and its rejections are kept for inspection.
    """

    def __init__(self, plan: EditorialPlan, edit: Any = None) -> None:
        self.plan = plan
        self.edit = edit
        self.prompt = ""
        self.rejections: list[str] = []

    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        self.prompt = prompt
        aliases = {
            f"event-{index}": candidate["event_id"]
            for index, candidate in enumerate(json.loads(prompt)["candidates"])
        }

        def alias(value: str) -> str:
            event_id, _, suffix = value.rpartition("-")
            if event_id in aliases:
                return f"{aliases[event_id]}-{suffix}"
            return aliases[value]

        grouped = _grouped_plan(self.plan)
        for field in ("lead", "follow", "brief"):
            for choice in grouped[field]:  # type: ignore[attr-defined]
                choice["event_id"] = alias(choice["event_id"])
                choice["evidence_ids"] = [alias(item) for item in choice["evidence_ids"]]
        grouped["editor_viewpoint"] = [
            {"text": insight.text, "evidence_ids": [alias(item) for item in insight.evidence_ids]}
            for insight in self.plan.editor_viewpoint
        ]
        if self.edit is not None:
            self.edit(grouped)
        output = output_type.model_validate(grouped)
        try:
            validator(output)
        except ValueError as error:
            self.rejections.append(str(error))
            raise
        return output


def _judged(events: list[Event]) -> list[JudgeDecision]:
    return [
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


async def test_prompt_carries_only_aliases() -> None:
    events = [numbered_event(index) for index in range(17)]
    gateway = AliasAnsweringGateway(valid_global_plan())

    await plan_digest(  # type: ignore[arg-type]
        gateway, events, _judged(events), load_config(Path("config")).pipeline
    )

    candidates = json.loads(gateway.prompt)["candidates"]
    assert [candidate["event_id"] for candidate in candidates][:3] == ["c1", "c2", "c3"]
    assert candidates[0]["evidence"][0]["evidence_id"] == "c1-1"
    assert candidates[0]["initial_judge"]["event_id"] == "c1"
    assert candidates[0]["initial_judge"]["evidence_ids"] == ["c1-1"]
    assert "event-" not in gateway.prompt


async def test_aliased_answer_is_restored_to_real_ids() -> None:
    events = [numbered_event(index) for index in range(17)]
    gateway = AliasAnsweringGateway(valid_global_plan())

    plan = await plan_digest(  # type: ignore[arg-type]
        gateway, events, _judged(events), load_config(Path("config")).pipeline
    )

    assert [selection.event_id for selection in plan.selections] == [
        selection.event_id for selection in valid_global_plan().selections
    ]
    assert plan.selections[0].evidence_ids == ["event-0-1"]
    assert all(
        evidence_id.startswith("event-")
        for insight in plan.editor_viewpoint
        for evidence_id in insight.evidence_ids
    )


async def test_a_mangled_alias_is_named_in_the_retry_message() -> None:
    events = [numbered_event(index) for index in range(17)]

    def mangle(grouped: dict[str, Any]) -> None:
        grouped["lead"][0]["event_id"] = "c1x"

    gateway = AliasAnsweringGateway(valid_global_plan(), mangle)

    with pytest.raises(ValueError, match="unknown event"):
        await plan_digest(  # type: ignore[arg-type]
            gateway, events, _judged(events), load_config(Path("config")).pipeline
        )

    assert "c1x" in gateway.rejections[0]


async def test_validator_messages_speak_in_aliases() -> None:
    """A retry that quotes ids the model never saw cannot be acted on."""

    events = [numbered_event(index) for index in range(17)]

    def cite_missing_evidence(grouped: dict[str, Any]) -> None:
        grouped["lead"][0]["evidence_ids"] = ["c1-1", "c1-9"]

    gateway = AliasAnsweringGateway(valid_global_plan(), cite_missing_evidence)

    with pytest.raises(ValueError, match="unknown evidence"):
        await plan_digest(  # type: ignore[arg-type]
            gateway, events, _judged(events), load_config(Path("config")).pipeline
        )

    message = gateway.rejections[0]
    assert "ids=['c1-9'] for event_id=c1; use only ids=['c1-1']" in message
    assert "event-" not in message
