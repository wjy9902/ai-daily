"""Every stage must be charged to its own budget.

The stage argument existed on ModelGateway.generate from the day the per-stage
budget was written, but no call site in content.py passed it, so every judge,
plan and draft call was billed to the judge allowance. Production ran three
days before the symptom surfaced: on 2026-08-15 the 04:20 run finished judging
and planning, then the draft stage died with "judge request allowance
exhausted" and the day published as brief-only while the plan and draft
allowances sat at zero, entirely unused.

The whole test suite passed throughout, because every test drove the stages
directly rather than checking who paid for them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from test_content import FakeGateway, event, plan

from ai_daily.budget import BudgetLedger, BudgetStage, StageBudgetExceeded
from ai_daily.config import load_config
from ai_daily.content import draft_selected, judge_events, plan_digest
from ai_daily.models import ModelRun


class StageRecordingGateway(FakeGateway):  # type: ignore[misc]
    """Records the stage each call declares, exactly as the real ledger sees it."""

    def __init__(self) -> None:
        super().__init__()
        self.stages: list[BudgetStage] = []

    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: BudgetStage = BudgetStage.JUDGE,
    ) -> Any:
        self.stages.append(stage)
        return await super().generate(role, output_type, instructions, prompt)


async def test_judging_is_billed_to_the_judge_allowance() -> None:
    gateway = StageRecordingGateway()

    await judge_events(gateway, [event()])  # type: ignore[arg-type]

    assert gateway.stages == [BudgetStage.JUDGE]


async def test_planning_is_billed_to_the_plan_allowance() -> None:
    gateway = StageRecordingGateway()
    config = load_config(Path("config")).pipeline
    decisions, _ = await judge_events(gateway, [event()])  # type: ignore[arg-type]
    gateway.stages.clear()

    try:
        await plan_digest(gateway, [event()], decisions, config)  # type: ignore[arg-type]
    except Exception:
        # The fake gateway cannot satisfy the real plan schema; the billing
        # decision is made before any of that matters.
        pass

    assert gateway.stages == [BudgetStage.PLAN]


async def test_drafting_is_billed_to_the_draft_allowance() -> None:
    gateway = StageRecordingGateway()

    await draft_selected(gateway, [event()], plan())  # type: ignore[arg-type]

    assert gateway.stages, "drafting made no model call"
    assert set(gateway.stages) == {BudgetStage.DRAFT}


def test_an_exhausted_stage_does_not_starve_the_others() -> None:
    """This is the property the missing argument silently destroyed."""

    config = load_config(Path("config")).models.budget
    ledger = BudgetLedger(config)
    ledger.record_requests(ledger.stage_remaining_requests(BudgetStage.JUDGE), BudgetStage.JUDGE)

    with pytest.raises(StageBudgetExceeded, match="judge"):
        ledger.check_stage(BudgetStage.JUDGE)

    ledger.check_stage(BudgetStage.PLAN)
    ledger.check_stage(BudgetStage.DRAFT)
    assert ledger.stage_remaining_requests(BudgetStage.DRAFT) > 0


def test_a_full_issue_fits_inside_each_stage_allowance() -> None:
    """A normal day must not be able to exhaust a stage.

    Sizes come from the pipeline's own shape: one judge call per batch of ten
    candidates, one planning call, one draft per detailed story — each able to
    spend a second request on its retry.
    """

    config = load_config(Path("config"))
    budget = config.models.budget
    pipeline = config.pipeline
    ledger = BudgetLedger(budget)

    judge_calls = -(-pipeline.candidate_limit // 10)
    draft_calls = pipeline.lead_max + pipeline.follow_max
    worst_case = {
        BudgetStage.JUDGE: judge_calls * 2,
        BudgetStage.PLAN: 1 * 2,
        BudgetStage.DRAFT: draft_calls * 2,
    }

    for stage, needed in worst_case.items():
        available = ledger.stage_remaining_requests(stage)
        assert available >= needed, (
            f"{stage.value} allows {available} requests but a single issue can "
            f"need {needed}; a normal day would degrade on budget alone"
        )


def test_persona_budget_can_reserve_three_concurrent_analysts_after_planning() -> None:
    config = load_config(Path("config"))
    assert config.persona is not None
    ledger = BudgetLedger(config.persona.budget)
    failed_attempt = ModelRun(
        role="persona_planner",
        requested_provider="deepseek",
        requested_model="deepseek-v4-pro",
        actual_provider="deepseek",
        actual_model="deepseek-v4-pro",
        attempt=1,
        status="failed",
        latency_ms=1,
        input_tokens=64_326,
        output_tokens=61_410,
        cost_cny=0.585767,
        error_type="SchemaError",
    )
    ledger.record_requests(7, BudgetStage.PERSONA)
    ledger.record(failed_attempt, BudgetStage.PERSONA)
    planner = ModelRun(
        role="persona_planner",
        requested_provider="deepseek",
        requested_model="deepseek-v4-pro",
        actual_provider="deepseek",
        actual_model="deepseek-v4-pro",
        attempt=1,
        status="ok",
        latency_ms=1,
        input_tokens=12_305,
        output_tokens=23_663,
        cost_cny=0.25,
    )
    ledger.record_requests(1, BudgetStage.PERSONA)
    ledger.record(planner, BudgetStage.PERSONA)

    for _ in range(config.persona.analyst_concurrency):
        ledger.reserve(
            BudgetStage.PERSONA,
            2,
            config.persona.max_call_cost_cny,
            input_tokens=20_000,
            output_tokens=96_000,
        )

    assert ledger.reserved_requests == 6
    assert ledger.reserved_output_tokens == 288_000
    assert ledger.remaining_cost() == pytest.approx(1.164233)


def test_stage_totals_are_reported_separately(tmp_path: Path) -> None:
    """status.json must show where the money went, per stage."""

    ledger = BudgetLedger(
        load_config(Path("config")).models.budget,
        store_path=tmp_path / "budget.json",
    )
    ledger.record_requests(3, BudgetStage.JUDGE)
    ledger.record_requests(1, BudgetStage.PLAN)

    snapshot = ledger.snapshot()
    assert snapshot["stage_requests"] == {
        "judge": 3,
        "plan": 1,
        "draft": 0,
        "persona": 0,
    }

    stored = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert stored["stage_requests"]["judge"] == 3
