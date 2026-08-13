import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError

from ai_daily.config import Secrets, load_config
from ai_daily.model_gateway import (
    Invocation,
    MissingProviderSecret,
    ModelGateway,
    ModelInvocationFailed,
    is_recoverable,
)
from ai_daily.models import JudgeDecision


def test_only_transient_failures_are_recoverable() -> None:
    assert is_recoverable(httpx.ConnectError("offline"))
    assert is_recoverable(ModelHTTPError(429, "model"))
    assert is_recoverable(ModelHTTPError(503, "model"))
    assert not is_recoverable(ModelHTTPError(401, "model"))
    assert not is_recoverable(ModelHTTPError(400, "model"))


def test_provider_requires_environment_secrets() -> None:
    gateway = ModelGateway(load_config().models, Secrets())
    endpoint = gateway.config.roles["judge"].primary
    try:
        gateway._build_model(endpoint)
    except MissingProviderSecret as error:
        assert "DASHSCOPE_API_KEY" in str(error)
    else:
        raise AssertionError("missing secrets were accepted")


def test_alibaba_structured_output_disables_thinking_mode() -> None:
    gateway = ModelGateway(load_config().models, Secrets())
    alibaba = gateway.config.roles["judge"].primary
    deepseek = gateway.config.roles["judge"].fallback

    assert gateway._model_settings(alibaba)["extra_body"] == {"enable_thinking": False}
    assert gateway._model_settings(deepseek)["extra_body"] is None


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
    assert calls[1].fallback_reason == "ReadTimeout"


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
    ) -> JudgeDecision:
        calls.append(invocation)
        raise ModelHTTPError(401, "unauthorized")

    monkeypatch.setattr(gateway, "_invoke_endpoint", invoke)

    with pytest.raises(ModelInvocationFailed, match="401"):
        await gateway.generate("judge", JudgeDecision, "instructions", "prompt")

    assert len(calls) == 1
