import json

import httpx
import pytest
import respx
from pydantic import BaseModel

from ai_daily.budget import BudgetStage
from ai_daily.config import Secrets, load_config
from ai_daily.model_gateway import MissingProviderSecret, ModelGateway, ModelInvocationFailed


class Result(BaseModel):
    value: int


@respx.mock
async def test_success_then_400_counts_both_requests_without_leaking_body():
    calls = 0
    secret = "sk-do-not-persist-prompt-text"

    def respond(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            name = json.loads(request.content)["tools"][0]["function"]["name"]
            return httpx.Response(
                200,
                json={
                    "id": "chat-1",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": name, "arguments": '{"value":0}'},
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                },
            )
        return httpx.Response(
            400,
            headers={"x-request-id": "req_abcdefgh1234"},
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "param": secret,
                    "message": "context length exceeded " + secret,
                }
            },
        )

    route = respx.post("https://api.deepseek.com/chat/completions").mock(side_effect=respond)
    gateway = ModelGateway(load_config().models, Secrets(deepseek_api_key="test"))

    def validate(value):
        raise ValueError(secret)

    with pytest.raises(ModelInvocationFailed, match="ModelHTTPError:400"):
        await gateway.generate(
            "editor", Result, "instructions", "prompt", validate, stage=BudgetStage.PLAN
        )
    assert route.call_count == 2
    run = gateway.runs[0]
    assert run.request_count == run.actual_request_count == 2
    assert run.successful_response_count == 1
    assert run.failed_request_number == 2
    assert run.http_status == 400
    assert run.error_category == "context"
    assert run.validation_categories[0] == "semantic_validation"
    assert run.input_tokens == 100 and run.output_tokens == 20
    assert gateway.ledger.requests == 2
    assert secret not in run.model_dump_json()


@respx.mock
async def test_connection_failure_counts_attempt():
    respx.post("https://api.deepseek.com/chat/completions").mock(
        side_effect=httpx.ConnectError("private host info")
    )
    gateway = ModelGateway(load_config().models, Secrets(deepseek_api_key="test"))
    with pytest.raises(MissingProviderSecret):
        await gateway.generate("editor", Result, "instructions", "prompt")
    assert gateway.runs[0].actual_request_count == 1
    assert gateway.runs[0].failed_request_number == 1
    assert gateway.runs[0].input_tokens == 0
    assert "private host info" not in gateway.runs[0].model_dump_json()


async def test_no_http_request_releases_reservation_without_phantom_charge():
    gateway = ModelGateway(
        load_config().models,
        Secrets(deepseek_api_key="", openai_api_key=""),
        reservation_cost_cny=0.01,
    )
    with pytest.raises(MissingProviderSecret):
        await gateway.generate("editor", Result, "instructions", "prompt", stage=BudgetStage.DRAFT)
    assert gateway.runs[0].request_count == 0
    assert gateway.ledger.requests == 0
    assert gateway.ledger.reserved_requests == 0
    assert gateway.ledger.reserved_cost_cny == 0
