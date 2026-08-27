"""The day's budget is a property of the day, not of the process holding it."""

from __future__ import annotations

import json
from pathlib import Path

import factories
import pytest

from ai_daily.budget import (
    STAGE_SHARE,
    BudgetExceeded,
    BudgetLedger,
    BudgetStage,
    StageBudgetExceeded,
)
from ai_daily.models import BudgetConfig, ModelRun


def _run(cost: float, *, input_tokens: int = 10, output_tokens: int = 5) -> ModelRun:
    return ModelRun(
        role="judge",
        requested_provider="alibaba",
        requested_model="model",
        actual_provider="alibaba",
        actual_model="model",
        attempt=1,
        status="ok",
        latency_ms=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_cny=cost,
    )


def _store(tmp_path: Path) -> Path:
    return tmp_path / "budget" / "2026-08-13.json"


def _ledger(store: Path, config: BudgetConfig | None = None) -> BudgetLedger:
    """A fresh ledger, as a later timer window would open it."""

    return BudgetLedger(config or factories.budget_config(), store_path=store)


def test_separate_ledgers_on_the_same_day_accumulate_instead_of_resetting(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    config = factories.budget_config(request_limit=30, cost_cny_limit=5)

    morning = _ledger(store, config)
    morning.record_requests(3, BudgetStage.JUDGE)
    morning.record(_run(2.0), BudgetStage.JUDGE)

    midday = _ledger(store, config)
    assert midday.cost_cny == pytest.approx(2.0)
    assert midday.requests == 3
    midday.record_requests(3, BudgetStage.JUDGE)
    midday.record(_run(2.0), BudgetStage.JUDGE)

    evening = _ledger(store, config)
    assert evening.cost_cny == pytest.approx(4.0)
    assert evening.requests == 6
    assert evening.remaining_cost() == pytest.approx(1.0)

    # The third window may not spend a fresh ¥5: only ¥1 of the day is left.
    with pytest.raises(BudgetExceeded, match="cost"):
        evening.record(_run(2.0), BudgetStage.JUDGE)
    assert _ledger(store, config).cost_cny == pytest.approx(6.0)


def test_run_counts_survive_across_processes(tmp_path: Path) -> None:
    store = _store(tmp_path)

    for _ in range(3):
        _ledger(store).start_run()

    assert _ledger(store).runs_today == 3
    assert json.loads(store.read_text(encoding="utf-8"))["runs_today"] == 3


def test_a_ledger_without_a_store_stays_in_memory(tmp_path: Path) -> None:
    ledger = BudgetLedger(factories.budget_config())

    ledger.record(_run(1.0), BudgetStage.JUDGE)

    assert ledger.cost_cny == pytest.approx(1.0)
    assert BudgetLedger(factories.budget_config()).cost_cny == 0
    assert list(tmp_path.iterdir()) == []


def test_an_exhausted_stage_stops_only_that_stage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = factories.budget_config(request_limit=10, cost_cny_limit=5)
    ledger = _ledger(store, config)

    ledger.record_requests(int(10 * STAGE_SHARE[BudgetStage.JUDGE]), BudgetStage.JUDGE)

    with pytest.raises(StageBudgetExceeded, match="judge"):
        ledger.check_stage(BudgetStage.JUDGE)
    ledger.check_stage(BudgetStage.PLAN)
    ledger.check_stage(BudgetStage.DRAFT)
    assert ledger.remaining_requests() == 10 - int(10 * STAGE_SHARE[BudgetStage.JUDGE])


def test_stage_exhaustion_by_cost_also_spares_the_other_stages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = factories.budget_config(request_limit=30, cost_cny_limit=5)
    ledger = _ledger(store, config)

    ledger.record(_run(5 * STAGE_SHARE[BudgetStage.PLAN]), BudgetStage.PLAN)

    with pytest.raises(StageBudgetExceeded, match="plan"):
        ledger.check_stage(BudgetStage.PLAN)
    ledger.check_stage(BudgetStage.DRAFT)
    assert ledger.stage_remaining_cost(BudgetStage.DRAFT) == pytest.approx(
        5 * STAGE_SHARE[BudgetStage.DRAFT]
    )


def test_the_day_limit_stops_every_stage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = factories.budget_config(request_limit=4, cost_cny_limit=5)
    ledger = _ledger(store, config)

    ledger.record_requests(4, BudgetStage.JUDGE)

    for stage in BudgetStage:
        with pytest.raises(BudgetExceeded, match="daily model request limit"):
            ledger.check_stage(stage)


@pytest.mark.parametrize("stage", list(BudgetStage))
def test_request_allowance_never_exceeds_the_stage_or_the_day(
    tmp_path: Path, stage: BudgetStage
) -> None:
    store = _store(tmp_path)
    config = factories.budget_config(request_limit=20, cost_cny_limit=5)
    ledger = _ledger(store, config)

    allowance = ledger.request_allowance(stage, 999)

    assert allowance == int(20 * STAGE_SHARE[stage])
    assert allowance <= ledger.remaining_requests()
    assert ledger.request_allowance(stage, 1) == 1


def test_request_allowance_shrinks_as_the_day_is_spent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    config = factories.budget_config(request_limit=20, cost_cny_limit=5)
    _ledger(store, config).record_requests(18, BudgetStage.JUDGE)

    later = _ledger(store, config)

    assert later.request_allowance(BudgetStage.DRAFT, 999) == 2


@pytest.mark.parametrize("payload", ["", "{", '{"requests": 3', "not json at all"])
def test_a_corrupt_ledger_raises_instead_of_resetting_the_day(tmp_path: Path, payload: str) -> None:
    store = _store(tmp_path)
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(payload, encoding="utf-8")

    with pytest.raises(BudgetExceeded, match="unreadable"):
        _ledger(store)


def test_a_persisted_ledger_round_trips_its_stage_totals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _ledger(store)
    first.record_requests(2, BudgetStage.DRAFT)
    first.record(_run(0.25, input_tokens=100, output_tokens=40), BudgetStage.DRAFT)

    second = _ledger(store)

    assert second.stage_requests[BudgetStage.DRAFT.value] == 2
    assert second.stage_cost[BudgetStage.DRAFT.value] == pytest.approx(0.25)
    assert second.input_tokens == 100
    assert second.output_tokens == 40
    assert second.snapshot()["cost_cny"] == pytest.approx(0.25)


def test_stale_ledger_instances_cannot_overwrite_each_others_reservations(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    config = factories.budget_config(request_limit=10, cost_cny_limit=2)
    first = _ledger(store, config)
    stale = _ledger(store, config)

    first.reserve(BudgetStage.PERSONA, 2, 1.5, input_tokens=100, output_tokens=50)

    with pytest.raises(StageBudgetExceeded, match="cost reservation"):
        stale.reserve(
            BudgetStage.PERSONA,
            2,
            1.5,
            input_tokens=100,
            output_tokens=50,
        )
    persisted = _ledger(store, config)
    assert persisted.reserved_cost_cny == pytest.approx(1.5)
    assert persisted.reserved_input_tokens == 100
    assert persisted.reserved_output_tokens == 50


def test_persisted_fractional_reservations_settle_without_rounding_drift(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    config = factories.budget_config(request_limit=20, cost_cny_limit=5)
    ledger = _ledger(store, config)
    first_cost = 0.6272326
    second_cost = 0.6206486
    for cost in (first_cost, second_cost):
        ledger.reserve(
            BudgetStage.PERSONA,
            2,
            cost,
            input_tokens=100,
            output_tokens=50,
        )

    ledger.settle_reservation(
        BudgetStage.PERSONA,
        2,
        first_cost,
        100,
        50,
        _run(0.1),
    )
    ledger.settle_reservation(
        BudgetStage.PERSONA,
        2,
        second_cost,
        100,
        50,
        _run(0.1),
    )

    persisted = _ledger(store, config)
    assert persisted.reserved_requests == 0
    assert persisted.reserved_cost_cny == 0
    assert persisted.stage_reserved_cost[BudgetStage.PERSONA.value] == 0


def test_next_run_conservatively_charges_orphaned_reservations(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    ledger = _ledger(store)
    ledger.reserve(
        BudgetStage.PERSONA,
        2,
        0.5,
        input_tokens=120,
        output_tokens=60,
    )

    recovered = _ledger(store)
    recovered.start_run(recover_stale_reservations=True)

    assert recovered.reserved_requests == 0
    assert recovered.reserved_input_tokens == 0
    assert recovered.reserved_output_tokens == 0
    assert recovered.requests == 2
    assert recovered.input_tokens == 120
    assert recovered.output_tokens == 60
    assert recovered.cost_cny == pytest.approx(0.5)
