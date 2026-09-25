"""A plan nested under ``result`` is the same plan, not a reason to retry.

Recorded after validation_reasons shipped: the 2026-09-23 and 09-24 plans
lost their first attempt to ``result: Extra inputs are not permitted;
lead: Field required ...`` - a complete plan inside an envelope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from pydantic import ValidationError
from test_content import _grouped_plan, valid_global_plan
from test_model_diagnostics import _tool_call_response

from ai_daily.budget import BudgetStage
from ai_daily.config import Secrets, load_config
from ai_daily.content import _planning_output_type
from ai_daily.model_gateway import ModelGateway


def _plan_arguments() -> dict[str, object]:
    return json.loads(json.dumps(_grouped_plan(valid_global_plan()), default=dict))


def _output_type():  # type: ignore[no-untyped-def]
    return _planning_output_type(load_config(Path("config")).pipeline)


@respx.mock
async def test_an_enveloped_plan_is_accepted_on_the_first_request() -> None:
    arguments = json.dumps({"result": _plan_arguments()}, ensure_ascii=False)
    route = respx.post("https://api.deepseek.com/chat/completions").mock(
        side_effect=lambda request: _tool_call_response(request, arguments)
    )
    gateway = ModelGateway(load_config().models, Secrets(deepseek_api_key="test"))

    output = await gateway.generate(
        "editor", _output_type(), "instructions", "prompt", stage=BudgetStage.PLAN
    )

    assert route.call_count == 1
    assert len(output.lead) == 4  # type: ignore[attr-defined]
    run = gateway.runs[0]
    assert run.validation_categories == ["result_envelope"]
    assert run.validation_reasons == []


def test_an_unwrapped_plan_is_untouched() -> None:
    output = _output_type().model_validate(_plan_arguments())

    assert len(output.brief) == 8  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "value",
    [
        {"result": _plan_arguments(), "lead": []},
        {"result": "not a plan"},
    ],
)
def test_anything_but_the_exact_envelope_still_fails(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _output_type().model_validate(value)
