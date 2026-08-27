import asyncio

import httpx
import pytest
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_daily.budget import BudgetExceeded, BudgetLedger, BudgetStage
from ai_daily.config import Secrets, load_config
from ai_daily.model_gateway import (
    Invocation,
    MissingProviderSecret,
    ModelGateway,
    ModelInvocationFailed,
    is_recoverable,
)
from ai_daily.models import BudgetConfig, JudgeDecision, ModelEndpoint


def test_only_transient_failures_are_recoverable() -> None:
    assert is_recoverable(httpx.ConnectError("offline"))
    assert is_recoverable(ModelAPIError("model", "Request timed out."))
    assert is_recoverable(ModelHTTPError(429, "model"))
    assert is_recoverable(ModelHTTPError(503, "model"))
    assert not is_recoverable(ModelHTTPError(401, "model"))
    assert not is_recoverable(ModelHTTPError(400, "model"))


async def test_generate_falls_back_after_pydantic_ai_wrapped_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = ModelGateway(load_config().models, Secrets())
    calls: list[Invocation] = []

    async def invoke(
        invocation: Invocation,
        output_type: type[JudgeDecision],
        instructions: str,
        prompt: str,
        validator: object,
        stage: object = None,
    ) -> JudgeDecision:
        calls.append(invocation)
        if invocation.attempt == 1:
            raise ModelAPIError(invocation.endpoint.model, "Request timed out.")
        return JudgeDecision(
            event_id="event-1",
            selected=True,
            category="模型与平台",
            relevance=90,
            confidence=0.9,
            reason="有效候选",
            evidence_ids=["event-1-1"],
        )

    monkeypatch.setattr(gateway, "_invoke_endpoint", invoke)

    result = await gateway.generate("judge", JudgeDecision, "instructions", "prompt")

    assert result.event_id == "event-1"
    role = gateway.config.roles["judge"]
    assert [call.endpoint.provider for call in calls] == [
        role.primary.provider,
        role.fallback.provider,
    ]
    assert calls[1].fallback_reason == "ModelAPIError"


def test_provider_requires_environment_secrets() -> None:
    """Every configured provider must refuse to run without its own key."""

    gateway = ModelGateway(load_config().models, Secrets())
    for role in gateway.config.roles.values():
        for endpoint in (role.primary, role.fallback):
            try:
                gateway._build_model(endpoint)
            except MissingProviderSecret as error:
                assert "API_KEY" in str(error)
            else:
                raise AssertionError(f"{endpoint.provider} accepted missing secrets")


def test_only_alibaba_gets_the_thinking_mode_override() -> None:
    """extra_body is provider-specific and must not leak to other providers."""

    gateway = ModelGateway(load_config().models, Secrets())
    alibaba = ModelEndpoint(
        provider="alibaba",
        model="qwen3.8-max",
        timeout_seconds=45,
        max_output_tokens=4096,
        temperature=0.1,
        input_cost_cny_per_million=2.5,
        output_cost_cny_per_million=10.0,
    )
    assert gateway._model_settings(alibaba)["extra_body"] == {"enable_thinking": False}
    for role in gateway.config.roles.values():
        for endpoint in (role.primary, role.fallback):
            assert gateway._model_settings(endpoint)["extra_body"] is None


async def test_provider_sdk_retries_are_disabled() -> None:
    secrets = Secrets(
        dashscope_api_key="test-key",
        dashscope_base_url="https://example.com/v1",
        deepseek_api_key="test-key",
        openai_api_key="test-key",
    )
    gateway = ModelGateway(load_config().models, secrets)
    for role in gateway.config.roles.values():
        for endpoint in (role.primary, role.fallback):
            model = gateway._build_model(endpoint)
            assert model.provider.client.max_retries == 0
            await model.provider.client.close()


def test_fallback_audit_preserves_requested_model() -> None:
    gateway = ModelGateway(load_config().models, Secrets(), clock=lambda: 1.0)
    role = gateway.config.roles["judge"]
    run = gateway._success_run(
        Invocation(
            role="judge",
            requested=role.primary,
            endpoint=role.fallback,
            attempt=2,
            fallback_reason="ModelHTTPError:503",
        ),
        1.0,
        100,
        20,
    )
    assert run.requested_provider == role.primary.provider
    assert run.requested_model == role.primary.model
    assert run.actual_provider == role.fallback.provider
    assert run.actual_model == role.fallback.model


async def test_generate_falls_back_only_after_a_recoverable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = ModelGateway(load_config().models, Secrets())
    calls: list[Invocation] = []

    async def invoke(
        invocation: Invocation,
        output_type: type[JudgeDecision],
        instructions: str,
        prompt: str,
        validator: object,
        stage: object = None,
    ) -> JudgeDecision:
        calls.append(invocation)
        if invocation.attempt == 1:
            raise httpx.ReadTimeout("timeout")
        return JudgeDecision(
            event_id="event-1",
            selected=True,
            category="模型与平台",
            relevance=90,
            confidence=0.9,
            reason="有效候选",
            evidence_ids=["event-1-1"],
        )

    monkeypatch.setattr(gateway, "_invoke_endpoint", invoke)

    result = await gateway.generate("judge", JudgeDecision, "instructions", "prompt")

    assert result.event_id == "event-1"
    assert [call.endpoint for call in calls] == [
        gateway.config.roles["judge"].primary,
        gateway.config.roles["judge"].fallback,
    ]
    assert calls[1].fallback_reason.startswith("ReadTimeout")


async def test_generate_does_not_fallback_after_unrecoverable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = ModelGateway(load_config().models, Secrets())
    calls: list[Invocation] = []

    async def invoke(
        invocation: Invocation,
        output_type: type[JudgeDecision],
        instructions: str,
        prompt: str,
        validator: object,
        stage: object = None,
    ) -> JudgeDecision:
        calls.append(invocation)
        raise ModelHTTPError(401, "unauthorized")

    monkeypatch.setattr(gateway, "_invoke_endpoint", invoke)

    with pytest.raises(ModelInvocationFailed, match="401"):
        await gateway.generate("judge", JudgeDecision, "instructions", "prompt")

    assert len(calls) == 1


async def test_generate_repairs_invalid_structured_output_with_same_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = ModelGateway(load_config().models, Secrets())
    calls = 0

    def respond(messages: object, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        category = "安全事件" if calls == 1 else "行业动态"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "event_id": "event-1",
                        "selected": True,
                        "category": category,
                        "relevance": 90,
                        "confidence": 0.9,
                        "reason": "安全事件值得关注",
                        "evidence_ids": ["event-1-1"],
                    },
                )
            ]
        )

    monkeypatch.setattr(gateway, "_build_model", lambda endpoint: FunctionModel(respond))

    result = await gateway.generate("judge", JudgeDecision, "instructions", "prompt")

    assert result.category == "行业动态"
    assert calls == 2
    assert gateway.ledger.requests == 2
    assert gateway.runs[-1].request_count == 2


async def test_generate_repairs_semantically_invalid_output_with_same_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = ModelGateway(load_config().models, Secrets())
    calls = 0

    def respond(messages: object, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        event_id = "wrong-event" if calls == 1 else "event-1"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "event_id": event_id,
                        "selected": True,
                        "category": "行业动态",
                        "relevance": 90,
                        "confidence": 0.9,
                        "reason": "重要事件",
                        "evidence_ids": ["event-1-1"],
                    },
                )
            ]
        )

    def validate(output: JudgeDecision) -> None:
        if output.event_id != "event-1":
            raise ValueError("event_id must be event-1")

    monkeypatch.setattr(gateway, "_build_model", lambda endpoint: FunctionModel(respond))

    result = await gateway.generate(
        "judge",
        JudgeDecision,
        "instructions",
        "prompt",
        validator=validate,
    )

    assert result.event_id == "event-1"
    assert calls == 2
    assert gateway.ledger.requests == 2
    assert gateway.runs[-1].request_count == 2


async def test_semantic_validation_failure_does_not_cross_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = ModelGateway(load_config().models, Secrets(), reservation_cost_cny=0.5)
    endpoints: list[str] = []
    calls = 0

    def respond(messages: object, info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "event_id": "wrong-event",
                        "selected": True,
                        "category": "行业动态",
                        "relevance": 90,
                        "confidence": 0.9,
                        "reason": "重要事件",
                        "evidence_ids": ["event-1-1"],
                    },
                )
            ]
        )

    def build_model(endpoint: object) -> FunctionModel:
        endpoints.append(str(endpoint))
        return FunctionModel(respond)

    def validate(output: JudgeDecision) -> None:
        raise ValueError("event_id must be event-1")

    monkeypatch.setattr(gateway, "_build_model", build_model)

    with pytest.raises(ModelInvocationFailed, match="event_id must be event-1"):
        await gateway.generate(
            "judge",
            JudgeDecision,
            "instructions",
            "prompt",
            validator=validate,
        )

    assert calls == 2
    assert len(endpoints) == 1
    assert gateway.ledger.requests == 2
    assert gateway.runs[-1].request_count == 2
    assert gateway.runs[-1].status == "failed"
    assert gateway.runs[-1].error_type == "ModelOutputValidationFailed"
    assert gateway.runs[-1].input_tokens > 0
    assert gateway.ledger.reserved_requests == 0
    assert gateway.ledger.reserved_cost_cny == pytest.approx(0)


async def test_new_invocation_cannot_exceed_daily_request_budget() -> None:
    gateway = ModelGateway(load_config().models, Secrets())
    gateway.ledger.requests = gateway.config.budget.request_limit

    with pytest.raises(BudgetExceeded, match="request"):
        gateway.ledger.request_allowance(BudgetStage.JUDGE, 2)


async def test_generate_preserves_budget_exceeded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = ModelGateway(load_config().models, Secrets())

    async def invoke(*args: object, **kwargs: object) -> JudgeDecision:
        raise BudgetExceeded("model request limit exceeded")

    monkeypatch.setattr(gateway, "_invoke_endpoint", invoke)

    with pytest.raises(BudgetExceeded, match="request"):
        await gateway.generate("judge", JudgeDecision, "instructions", "prompt")


async def test_concurrent_invocations_stay_bounded_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = ModelGateway(load_config().models, Secrets())
    active = 0
    maximum = 0
    calls = 0

    async def respond(messages: object, info: AgentInfo) -> ModelResponse:
        nonlocal active, maximum, calls
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        calls += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "event_id": f"event-{calls}",
                        "selected": True,
                        "category": "行业动态",
                        "relevance": 90,
                        "confidence": 0.9,
                        "reason": "重要事件",
                        "evidence_ids": [f"event-{calls}-1"],
                    },
                )
            ]
        )

    monkeypatch.setattr(gateway, "_build_model", lambda endpoint: FunctionModel(respond))

    results = await asyncio.gather(
        *[
            gateway.generate("judge", JudgeDecision, "instructions", f"prompt-{index}")
            for index in range(8)
        ]
    )

    assert len(results) == 8
    assert maximum == 1
    assert gateway.ledger.requests == 8
    assert sum(run.request_count for run in gateway.runs) == 8


async def test_three_concurrent_persona_calls_reserve_and_release_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = load_config().models
    budget = BudgetConfig(
        request_limit=6,
        input_token_limit=100_000,
        output_token_limit=60_000,
        cost_cny_limit=3.0,
    )
    ledger = BudgetLedger(budget)
    gateway = ModelGateway(
        base.model_copy(update={"budget": budget}),
        Secrets(),
        ledger=ledger,
        max_concurrency=3,
        reservation_cost_cny=1.0,
    )
    active = 0
    maximum = 0
    counter = 0

    async def respond(messages: object, info: AgentInfo) -> ModelResponse:
        nonlocal active, maximum, counter
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        counter += 1
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "event_id": f"event-{counter}",
                        "selected": True,
                        "category": "行业动态",
                        "relevance": 90,
                        "confidence": 0.9,
                        "reason": "重要事件",
                        "evidence_ids": [f"event-{counter}-1"],
                    },
                )
            ]
        )

    monkeypatch.setattr(gateway, "_build_model", lambda endpoint: FunctionModel(respond))

    results = await asyncio.gather(
        *[
            gateway.generate(
                "judge",
                JudgeDecision,
                "instructions",
                f"prompt-{index}",
                stage=BudgetStage.PERSONA,
            )
            for index in range(3)
        ]
    )

    assert len(results) == 3
    assert maximum == 3
    assert ledger.reserved_requests == 0
    assert ledger.reserved_input_tokens == 0
    assert ledger.reserved_output_tokens == 0
    assert ledger.reserved_cost_cny == pytest.approx(0)
    assert ledger.stage_reserved_requests[BudgetStage.PERSONA.value] == 0


async def test_cancelled_persona_call_releases_budget_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = load_config().models
    ledger = BudgetLedger(base.budget)
    gateway = ModelGateway(
        base,
        Secrets(),
        ledger=ledger,
        max_concurrency=3,
        reservation_cost_cny=0.5,
    )
    started = asyncio.Event()

    async def respond(messages: object, info: AgentInfo) -> ModelResponse:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(gateway, "_build_model", lambda endpoint: FunctionModel(respond))
    task = asyncio.create_task(
        gateway.generate(
            "judge",
            JudgeDecision,
            "instructions",
            "prompt",
            stage=BudgetStage.PERSONA,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert ledger.reserved_requests == 0
    assert ledger.reserved_input_tokens == 0
    assert ledger.reserved_output_tokens == 0
    assert ledger.reserved_cost_cny == pytest.approx(0)
