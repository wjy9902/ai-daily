"""The day's budget must be shared by every run of that day, not per process.

These guard the two bugs that a full test suite still missed: the ledger being
built without a store path (so each timer window got a fresh ¥5), and judge
decisions being dropped when a later model stage failed (so a run that could
publish a judged issue fell all the way to bare ranking).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ai_daily.config import Secrets, load_config
from ai_daily.degradation import FailureClass
from ai_daily.models import JudgeDecision
from ai_daily.pipeline import DailyPipeline, ModelStageFailed
from ai_daily.site_publisher import SiteLayout

TARGET = date(2026, 8, 13)


def _pipeline(tmp_path: Path) -> DailyPipeline:
    return DailyPipeline(
        load_config(Path("config")),
        Secrets(),
        layout=SiteLayout(tmp_path / "site"),
    )


def test_run_binds_the_ledger_to_the_day_on_disk(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    pipeline.layout.ensure()
    pipeline._bind_daily_budget(TARGET)

    expected = pipeline.layout.budget_path(TARGET)
    assert pipeline.gateway.ledger.store_path == expected
    assert expected.exists(), "start_run must persist immediately, before any spend"


def test_a_second_window_resumes_the_same_days_spend(tmp_path: Path) -> None:
    first = _pipeline(tmp_path)
    first.layout.ensure()
    first._bind_daily_budget(TARGET)
    first.gateway.ledger.record_requests(7)

    second = _pipeline(tmp_path)
    second.layout.ensure()
    second._bind_daily_budget(TARGET)

    assert second.gateway.ledger.requests == 7, (
        "a later timer window must resume the morning's spend, not restart it"
    )
    assert second.gateway.ledger.runs_today == 2


def test_each_date_gets_its_own_budget(tmp_path: Path) -> None:
    pipeline = _pipeline(tmp_path)
    pipeline.layout.ensure()
    pipeline._bind_daily_budget(TARGET)
    pipeline.gateway.ledger.record_requests(9)

    tomorrow = _pipeline(tmp_path)
    tomorrow.layout.ensure()
    tomorrow._bind_daily_budget(date(2026, 8, 14))

    assert tomorrow.gateway.ledger.requests == 0
    stored = json.loads(pipeline.layout.budget_path(TARGET).read_text(encoding="utf-8"))
    assert stored["requests"] == 9


def test_an_injected_gateway_keeps_its_own_ledger(tmp_path: Path) -> None:
    from ai_daily.model_gateway import ModelGateway

    config = load_config(Path("config"))
    gateway = ModelGateway(config.models, Secrets())
    pipeline = DailyPipeline(
        config, Secrets(), gateway=gateway, layout=SiteLayout(tmp_path / "site")
    )
    pipeline.layout.ensure()
    pipeline._bind_daily_budget(TARGET)

    assert pipeline.gateway.ledger.store_path is None
    assert pipeline.gateway is gateway


def test_a_failed_stage_carries_its_judge_decisions_out(tmp_path: Path) -> None:
    decision = JudgeDecision(
        event_id="event-1",
        selected=True,
        category="模型与平台",
        relevance=90,
        confidence=0.9,
        reason="有效候选",
        evidence_ids=["event-1-1"],
    )
    error = ModelStageFailed(FailureClass.PLAN_FAILED, [decision])

    assert error.decisions == [decision], (
        "planning failure must not discard the judge output; that is the "
        "difference between a judged issue and bare ranking"
    )
    assert ModelStageFailed(FailureClass.JUDGE_FAILED).decisions == []


@pytest.mark.parametrize("requests", [1, 20, 40])
def test_the_stored_ledger_is_the_only_source_of_truth(tmp_path: Path, requests: int) -> None:
    pipeline = _pipeline(tmp_path)
    pipeline.layout.ensure()
    pipeline._bind_daily_budget(TARGET)
    pipeline.gateway.ledger.record_requests(requests)

    reloaded = _pipeline(tmp_path)
    reloaded.layout.ensure()
    reloaded._bind_daily_budget(TARGET)
    assert reloaded.gateway.ledger.requests == requests
