import pytest

from ai_daily.budget import BudgetExceeded, BudgetLedger
from ai_daily.models import BudgetConfig, ModelRun


def config() -> BudgetConfig:
    return BudgetConfig(
        request_limit=1,
        input_token_limit=100,
        output_token_limit=50,
        cost_cny_limit=1,
    )


def run(**updates: object) -> ModelRun:
    values = {
        "role": "judge",
        "requested_provider": "alibaba",
        "requested_model": "model",
        "actual_provider": "alibaba",
        "actual_model": "model",
        "attempt": 1,
        "status": "ok",
        "latency_ms": 1,
        "input_tokens": 90,
        "output_tokens": 40,
        "cost_cny": 0.5,
    }
    values.update(updates)
    return ModelRun.model_validate(values)


def test_budget_records_usage_and_fails_before_second_request() -> None:
    ledger = BudgetLedger(config())
    ledger.reserve_request()
    ledger.record(run())
    assert ledger.input_tokens == 90
    with pytest.raises(BudgetExceeded, match="request"):
        ledger.reserve_request()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"input_tokens": 101}, "input"),
        ({"output_tokens": 51}, "output"),
        ({"cost_cny": 1.1}, "cost"),
    ],
)
def test_budget_rejects_overages(updates: dict[str, object], message: str) -> None:
    ledger = BudgetLedger(config())
    with pytest.raises(BudgetExceeded, match=message):
        ledger.record(run(**updates))
    assert ledger.input_tokens == updates.get("input_tokens", 90)
    assert ledger.output_tokens == updates.get("output_tokens", 40)
    assert ledger.cost_cny == updates.get("cost_cny", 0.5)
