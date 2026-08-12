from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent, ModelSettings
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.alibaba import AlibabaProvider
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import UsageLimits

from ai_daily.budget import BudgetLedger
from ai_daily.config import Secrets
from ai_daily.models import ModelEndpoint, ModelRun, ModelsConfig

OutputT = TypeVar("OutputT", bound=BaseModel)


class MissingProviderSecret(RuntimeError):
    pass


class ModelInvocationFailed(RuntimeError):
    pass


def is_recoverable(error: Exception) -> bool:
    if isinstance(error, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(error, ModelHTTPError):
        return error.status_code == 429 or 500 <= error.status_code < 600
    return False


class ModelGateway:
    def __init__(
        self,
        config: ModelsConfig,
        secrets: Secrets | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.secrets = secrets or Secrets()
        self.ledger = BudgetLedger(config.budget)
        self.runs: list[ModelRun] = []
        self.clock = clock

    async def generate(
        self,
        role: Literal["judge", "editor"],
        output_type: type[OutputT],
        instructions: str,
        prompt: str,
    ) -> OutputT:
        role_config = self.config.roles[role]
        fallback_reason: str | None = None
        for attempt, endpoint in enumerate((role_config.primary, role_config.fallback), start=1):
            self.ledger.reserve_request()
            started = self.clock()
            try:
                model = self._build_model(endpoint)
                agent: Agent[None, OutputT] = Agent(
                    model,
                    output_type=output_type,
                    instructions=instructions,
                    retries=0,
                )
                result = await agent.run(
                    prompt,
                    model_settings=self._model_settings(endpoint),
                    usage_limits=UsageLimits(
                        request_limit=1,
                        input_tokens_limit=self.config.budget.input_token_limit,
                        output_tokens_limit=self.config.budget.output_token_limit,
                    ),
                )
                usage = result.usage()
                self.ledger.reconcile_requests(usage.requests)
                run = self._success_run(
                    role,
                    role_config.primary,
                    endpoint,
                    attempt,
                    fallback_reason,
                    started,
                    usage.input_tokens,
                    usage.output_tokens,
                )
                self.runs.append(run)
                self.ledger.record(run)
                return result.output
            except (UsageLimitExceeded, MissingProviderSecret):
                raise
            except Exception as error:
                run = self._failed_run(
                    role,
                    role_config.primary,
                    endpoint,
                    attempt,
                    fallback_reason,
                    started,
                    error,
                )
                self.runs.append(run)
                if attempt == 1 and is_recoverable(error):
                    fallback_reason = self._safe_error(error)
                    continue
                raise ModelInvocationFailed(self._safe_error(error)) from error
        raise AssertionError("unreachable")

    @staticmethod
    def _model_settings(endpoint: ModelEndpoint) -> ModelSettings:
        extra_body = {"enable_thinking": False} if endpoint.provider == "alibaba" else None
        return ModelSettings(
            temperature=endpoint.temperature,
            max_tokens=endpoint.max_output_tokens,
            timeout=endpoint.timeout_seconds,
            extra_body=extra_body,
        )

    def _build_model(self, endpoint: ModelEndpoint) -> OpenAIChatModel:
        if endpoint.provider == "alibaba":
            key = self.secrets.dashscope_api_key
            base_url = self.secrets.dashscope_base_url
            if not key or not base_url:
                raise MissingProviderSecret("DASHSCOPE_API_KEY and DASHSCOPE_BASE_URL are required")
            alibaba_provider = AlibabaProvider(api_key=key, base_url=base_url)
            return OpenAIChatModel(endpoint.model, provider=alibaba_provider)
        elif endpoint.provider == "deepseek":
            if not self.secrets.deepseek_api_key:
                raise MissingProviderSecret("DEEPSEEK_API_KEY is required")
            deepseek_provider = DeepSeekProvider(api_key=self.secrets.deepseek_api_key)
            return OpenAIChatModel(endpoint.model, provider=deepseek_provider)
        else:
            raise MissingProviderSecret("Ollama is local-only and must use a separate profile")

    def _success_run(
        self,
        role: Literal["judge", "editor"],
        requested: ModelEndpoint,
        endpoint: ModelEndpoint,
        attempt: int,
        fallback_reason: str | None,
        started: float,
        input_tokens: int,
        output_tokens: int,
    ) -> ModelRun:
        cost = (
            input_tokens * endpoint.input_cost_cny_per_million
            + output_tokens * endpoint.output_cost_cny_per_million
        ) / 1_000_000
        return ModelRun(
            role=role,
            requested_provider=requested.provider,
            requested_model=requested.model,
            actual_provider=endpoint.provider,
            actual_model=endpoint.model,
            attempt=attempt,
            status="ok",
            fallback_reason=fallback_reason,
            latency_ms=int((self.clock() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=cost,
        )

    def _failed_run(
        self,
        role: Literal["judge", "editor"],
        requested: ModelEndpoint,
        endpoint: ModelEndpoint,
        attempt: int,
        fallback_reason: str | None,
        started: float,
        error: Exception,
    ) -> ModelRun:
        return ModelRun(
            role=role,
            requested_provider=requested.provider,
            requested_model=requested.model,
            actual_provider=endpoint.provider,
            actual_model=endpoint.model,
            attempt=attempt,
            status="failed",
            fallback_reason=fallback_reason,
            latency_ms=int((self.clock() - started) * 1000),
            input_tokens=0,
            output_tokens=0,
            error_type=type(error).__name__,
        )

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(error, ModelHTTPError):
            return f"ModelHTTPError:{error.status_code}"
        if isinstance(error, ModelAPIError):
            return type(error).__name__
        return type(error).__name__
