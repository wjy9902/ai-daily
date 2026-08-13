from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, ModelSettings
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.alibaba import AlibabaProvider
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.usage import RunUsage, UsageLimits

from ai_daily.budget import BudgetExceeded, BudgetLedger, BudgetStage
from ai_daily.config import Secrets
from ai_daily.models import ModelEndpoint, ModelRun, ModelsConfig

OutputT = TypeVar("OutputT", bound=BaseModel)
ModelRole = Literal["judge", "editor"]
OUTPUT_RETRIES = 1
# One provider call at a time keeps daily token and cost accounting globally ordered.
# It also means calls never contend for budget, so no reservation bookkeeping is
# needed: each call simply asks the ledger what it may spend before it starts.
MAX_MODEL_CONCURRENCY = 1


@dataclass(frozen=True)
class Invocation:
    role: ModelRole
    requested: ModelEndpoint
    endpoint: ModelEndpoint
    attempt: int
    fallback_reason: str | None


class MissingProviderSecret(RuntimeError):
    pass


class ModelInvocationFailed(RuntimeError):
    pass


def is_recoverable(error: Exception) -> bool:
    if isinstance(error, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(error, ModelHTTPError):
        return error.status_code == 429 or 500 <= error.status_code < 600
    # Pydantic AI maps OpenAI SDK connection and timeout failures to ModelAPIError.
    if isinstance(error, ModelAPIError):
        return True
    return False


class ModelGateway:
    def __init__(
        self,
        config: ModelsConfig,
        secrets: Secrets | None = None,
        clock: Callable[[], float] = time.monotonic,
        ledger: BudgetLedger | None = None,
    ) -> None:
        self.config = config
        self.secrets = secrets or Secrets()
        self.ledger = ledger or BudgetLedger(config.budget)
        self.runs: list[ModelRun] = []
        self.clock = clock
        self._concurrency = asyncio.Semaphore(MAX_MODEL_CONCURRENCY)

    async def generate(
        self,
        role: ModelRole,
        output_type: type[OutputT],
        instructions: str,
        prompt: str,
        validator: Callable[[OutputT], None] | None = None,
        stage: BudgetStage = BudgetStage.JUDGE,
    ) -> OutputT:
        role_config = self.config.roles[role]
        fallback_reason: str | None = None
        for attempt, endpoint in enumerate((role_config.primary, role_config.fallback), start=1):
            invocation = Invocation(role, role_config.primary, endpoint, attempt, fallback_reason)
            try:
                return await self._invoke_endpoint(
                    invocation,
                    output_type,
                    instructions,
                    prompt,
                    validator,
                    stage,
                )
            except (BudgetExceeded, UsageLimitExceeded, MissingProviderSecret):
                raise
            except Exception as error:
                if attempt == 1 and is_recoverable(error):
                    fallback_reason = self._safe_error(error)
                    continue
                raise ModelInvocationFailed(self._safe_error(error)) from error
        raise AssertionError("unreachable")

    async def _invoke_endpoint(
        self,
        invocation: Invocation,
        output_type: type[OutputT],
        instructions: str,
        prompt: str,
        validator: Callable[[OutputT], None] | None,
        stage: BudgetStage,
    ) -> OutputT:
        async with self._concurrency:
            return await self._invoke_endpoint_bounded(
                invocation,
                output_type,
                instructions,
                prompt,
                validator,
                stage,
            )

    async def _invoke_endpoint_bounded(
        self,
        invocation: Invocation,
        output_type: type[OutputT],
        instructions: str,
        prompt: str,
        validator: Callable[[OutputT], None] | None,
        stage: BudgetStage,
    ) -> OutputT:
        started = self.clock()
        # Decided up front: pydantic-ai fixes its request limit when the run starts.
        request_limit = self.ledger.request_allowance(stage, 1 + OUTPUT_RETRIES)
        input_token_limit, output_token_limit = self._remaining_token_budget()
        agent: Agent[None, OutputT] = Agent(
            self._build_model(invocation.endpoint),
            output_type=output_type,
            instructions=instructions,
            retries=OUTPUT_RETRIES,
        )
        if validator is not None:
            agent.output_validator(self._semantic_validator(validator))
        usage = RunUsage()
        try:
            async with agent:
                result = await agent.run(
                    prompt,
                    model_settings=self._model_settings(invocation.endpoint, output_token_limit),
                    usage_limits=UsageLimits(
                        request_limit=request_limit,
                        input_tokens_limit=input_token_limit,
                        output_tokens_limit=output_token_limit,
                    ),
                    usage=usage,
                )
        except asyncio.CancelledError as error:
            self._record_failed_run(invocation, started, error, usage, stage)
            raise
        except Exception as error:
            self._record_failed_run(invocation, started, error, usage, stage)
            raise
        self.ledger.record_requests(max(1, usage.requests), stage)
        run = self._success_run(
            invocation,
            started,
            usage.input_tokens,
            usage.output_tokens,
            usage.requests,
        )
        self.runs.append(run)
        self.ledger.record(run, stage)
        return result.output

    def _remaining_token_budget(self) -> tuple[int, int]:
        input_tokens = self.config.budget.input_token_limit - self.ledger.input_tokens
        output_tokens = self.config.budget.output_token_limit - self.ledger.output_tokens
        remaining_cost = self.config.budget.cost_cny_limit - self.ledger.cost_cny
        if input_tokens <= 0:
            raise BudgetExceeded("input token limit exceeded")
        if output_tokens <= 0:
            raise BudgetExceeded("output token limit exceeded")
        if remaining_cost <= 0:
            raise BudgetExceeded("cost limit exceeded")
        return input_tokens, output_tokens

    def _record_failed_run(
        self,
        invocation: Invocation,
        started: float,
        error: BaseException,
        usage: RunUsage,
        stage: BudgetStage,
    ) -> None:
        request_count = max(1, usage.requests)
        run = self._failed_run(
            invocation,
            started,
            error,
            usage.input_tokens,
            usage.output_tokens,
            request_count,
        )
        self.runs.append(run)
        # A failed call still consumed provider quota, so it is recorded and
        # persisted. Any ceiling it trips is swallowed here because the caller
        # is already raising the real error; the next call's check_stage() is
        # what turns an exhausted budget into a clean degradation.
        for record in (
            lambda: self.ledger.record_requests(request_count, stage),
            lambda: self.ledger.record(run, stage),
        ):
            try:
                record()
            except BudgetExceeded:
                pass

    @staticmethod
    def _semantic_validator(
        validator: Callable[[OutputT], None],
    ) -> Callable[[OutputT], OutputT]:
        def validate(output: OutputT) -> OutputT:
            try:
                validator(output)
            except ValueError as error:
                raise ModelRetry(str(error)) from error
            return output

        return validate

    @staticmethod
    def _model_settings(
        endpoint: ModelEndpoint, output_token_limit: int | None = None
    ) -> ModelSettings:
        extra_body = {"enable_thinking": False} if endpoint.provider == "alibaba" else None
        return ModelSettings(
            temperature=endpoint.temperature,
            max_tokens=min(
                endpoint.max_output_tokens,
                output_token_limit or endpoint.max_output_tokens,
            ),
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
            alibaba_provider.client.max_retries = 0
            return OpenAIChatModel(endpoint.model, provider=alibaba_provider)
        elif endpoint.provider == "deepseek":
            if not self.secrets.deepseek_api_key:
                raise MissingProviderSecret("DEEPSEEK_API_KEY is required")
            deepseek_provider = DeepSeekProvider(api_key=self.secrets.deepseek_api_key)
            deepseek_provider.client.max_retries = 0
            return OpenAIChatModel(endpoint.model, provider=deepseek_provider)
        else:
            raise MissingProviderSecret("Ollama is local-only and must use a separate profile")

    def _success_run(
        self,
        invocation: Invocation,
        started: float,
        input_tokens: int,
        output_tokens: int,
        request_count: int = 1,
    ) -> ModelRun:
        endpoint = invocation.endpoint
        cost = (
            input_tokens * endpoint.input_cost_cny_per_million
            + output_tokens * endpoint.output_cost_cny_per_million
        ) / 1_000_000
        return ModelRun(
            role=invocation.role,
            requested_provider=invocation.requested.provider,
            requested_model=invocation.requested.model,
            actual_provider=endpoint.provider,
            actual_model=endpoint.model,
            attempt=invocation.attempt,
            status="ok",
            fallback_reason=invocation.fallback_reason,
            request_count=request_count,
            latency_ms=int((self.clock() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=cost,
        )

    def _failed_run(
        self,
        invocation: Invocation,
        started: float,
        error: BaseException,
        input_tokens: int = 0,
        output_tokens: int = 0,
        request_count: int = 1,
    ) -> ModelRun:
        endpoint = invocation.endpoint
        cost = (
            input_tokens * endpoint.input_cost_cny_per_million
            + output_tokens * endpoint.output_cost_cny_per_million
        ) / 1_000_000
        return ModelRun(
            role=invocation.role,
            requested_provider=invocation.requested.provider,
            requested_model=invocation.requested.model,
            actual_provider=endpoint.provider,
            actual_model=endpoint.model,
            attempt=invocation.attempt,
            status="failed",
            fallback_reason=invocation.fallback_reason,
            request_count=request_count,
            latency_ms=int((self.clock() - started) * 1000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=cost,
            error_type=type(error).__name__,
        )

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(error, ModelHTTPError):
            return f"ModelHTTPError:{error.status_code}"
        if isinstance(error, ModelAPIError):
            return type(error).__name__
        return type(error).__name__
