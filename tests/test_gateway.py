import httpx
from pydantic_ai.exceptions import ModelHTTPError

from ai_daily.config import Secrets, load_config
from ai_daily.model_gateway import MissingProviderSecret, ModelGateway, is_recoverable


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
        "judge",
        role.primary,
        role.fallback,
        2,
        "ModelHTTPError:503",
        1.0,
        100,
        20,
    )
    assert run.requested_provider == role.primary.provider
    assert run.requested_model == role.primary.model
    assert run.actual_provider == role.fallback.provider
    assert run.actual_model == role.fallback.model
