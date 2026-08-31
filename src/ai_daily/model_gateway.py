from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx
from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, ModelSettings
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.alibaba import AlibabaProvider
from pydantic_ai.providers.deepseek import DeepSeekProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import RunUsage, UsageLimits

from ai_daily.budget import BudgetExceeded, BudgetLedger, BudgetStage
from ai_daily.config import Secrets
from ai_daily.models import ModelEndpoint, ModelRole, ModelRun, ModelsConfig

OutputT = TypeVar("OutputT", bound=BaseModel)
OUTPUT_RETRIES = 1
# The outer loop in generate() only retries transport failures, so a schema or
# validator rejection gets exactly 1 + OUTPUT_RETRIES provider requests. That is
# enough for roles whose output is a handful of fields. The edition editor is not
# one of them: a standard edition is about thirty independent text fields that
# must simultaneously stay inside their own length cap, keep their 判断/建议/不确定性
# prefix, avoid first person, and add up to a bounded body. One over-long field
# anywhere kills the run - and it kills it after every analyst has already been
# paid for, which is why 2026-08-27 and 2026-08-28 held at about ¥1.1 a window.
# Each extra retry sends the validation error back to the model and costs one
# editor call (~¥0.05), against a whole run's spend.
# The finalizer is the same shape as the editor and fails even later: it has to
# echo back every blocker id the critic invented, exactly once, while rewriting
# a draft that still has to pass the whole of verify_edition. On 2026-08-31 the
# critic raised 5, 12 and 5 blockers on the three windows and the finalizer lost
# all three - after the planner, the baselines, five analysts, the editor and
# the critic had all been paid for.
ROLE_OUTPUT_RETRIES: dict[ModelRole, int] = {
    "persona_edition_editor": 3,
    "persona_finalizer": 3,
}
# The daily pipeline defaults to one provider call at a time. The persona pipeline
# explicitly opts into at most three concurrent analysts and reserves request/cost
# allowance before each call so their combined in-flight spend cannot exceed budget.
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


class ModelOutputValidationFailed(RuntimeError):
    pass


def output_retries(role: ModelRole) -> int:
    return ROLE_OUTPUT_RETRIES.get(role, OUTPUT_RETRIES)


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
        max_concurrency: int = MAX_MODEL_CONCURRENCY,
        reservation_cost_cny: float | None = None,
    ) -> None:
        self.config = config
        self.secrets = secrets or Secrets()
        self.ledger = ledger or BudgetLedger(config.budget)
        self.runs: list[ModelRun] = []
        self.clock = clock
        if max_concurrency < 1 or max_concurrency > 3:
            raise ValueError("max_concurrency must be between 1 and 3")
        self._concurrency = asyncio.Semaphore(max_concurrency)
        self._reservation_cost_cny = reservation_cost_cny

    async def generate(
        self,
        role: ModelRole,
        output_type: type[OutputT],
        instructions: str,
        prompt: str,
        validator: Callable[[OutputT], OutputT | None] | None = None,
        stage: BudgetStage = BudgetStage.JUDGE,
    ) -> OutputT:
        role_config = self.config.roles[role]
        fallback_reason: str | None = None
        endpoints = (role_config.primary, role_config.primary, role_config.fallback)
        for attempt, endpoint in enumerate(endpoints, start=1):
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
                if attempt < len(endpoints) and is_recoverable(error):
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
        validator: Callable[[OutputT], OutputT | None] | None,
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
        validator: Callable[[OutputT], OutputT | None] | None,
        stage: BudgetStage,
    ) -> OutputT:
        started = self.clock()
        input_token_limit, output_token_limit = self._remaining_token_budget()
        retries = output_retries(invocation.role)
        # Decided up front: pydantic-ai fixes its request limit when the run starts.
        #
        # Generous on purpose. A retry in pydantic-ai does not cost exactly one
        # request - a tool-call round trip and an output-validator ModelRetry are
        # counted separately - so deriving this from `retries` is guesswork, and
        # twice on 2026-08-31 the guess was low and the run died on the usage
        # limit instead of the retry ceiling. Double the nominal attempts lets
        # the agent's own counter be the thing that stops it; the money ceiling
        # is the control that matters, and a call that really does loop is
        # bounded by that.
        request_limit = self.ledger.request_allowance(stage, 2 * (1 + retries))
        reservation_cost = self._reservation_cost_cny
        reservation: tuple[int, float, int, int] | None = None
        if reservation_cost is not None:
            reserved_input, reserved_output = self._call_token_ceiling(
                invocation.endpoint,
                instructions,
                prompt,
                request_limit,
                output_token_limit,
            )
            reservation_cost = max(
                reservation_cost,
                self._token_cost_ceiling(invocation.endpoint, reserved_input, reserved_output),
            )
            reservation = (
                request_limit,
                reservation_cost,
                reserved_input,
                reserved_output,
            )
            self.ledger.reserve(
                stage,
                request_limit,
                reservation_cost,
                input_tokens=reserved_input,
                output_tokens=reserved_output,
            )
        usage = RunUsage()
        validation_errors: list[str] = []
        try:
            agent: Agent[None, OutputT] = Agent(
                self._build_model(invocation.endpoint),
                output_type=output_type,
                instructions=instructions,
                retries=retries,
            )
            if validator is not None:
                agent.output_validator(self._semantic_validator(validator, validation_errors))
            async with agent:
                result = await agent.run(
                    prompt,
                    model_settings=self._model_settings(
                        invocation.endpoint,
                        output_token_limit,
                        invocation.role,
                    ),
                    usage_limits=UsageLimits(
                        request_limit=request_limit,
                        input_tokens_limit=input_token_limit,
                        output_tokens_limit=output_token_limit,
                    ),
                    usage=usage,
                )
        except asyncio.CancelledError as error:
            self._record_failed_run(invocation, started, error, usage, stage, reservation)
            raise
        except Exception as error:
            recorded_error: Exception = error
            # Whichever ceiling stops the run, report what the model actually got
            # wrong. Sizing request_limit against pydantic-ai's retry accounting
            # was guesswork twice over - 1 + retries and then 2 + retries both
            # tripped, on 2026-08-31 at 12:29 and 12:52 - and each time the
            # persona held on "The next request would exceed the request_limit",
            # which says nothing about the edition. The validation errors are
            # right here either way.
            if isinstance(error, (UnexpectedModelBehavior, UsageLimitExceeded)) and (
                validation_errors
            ):
                recorded_error = ModelOutputValidationFailed(validation_errors[-1])
            self._record_failed_run(invocation, started, recorded_error, usage, stage, reservation)
            if recorded_error is not error:
                raise recorded_error from error
            raise
        run = self._success_run(
            invocation,
            started,
            usage.input_tokens,
            usage.output_tokens,
            usage.requests,
        )
        self.runs.append(run)
        if reservation is None:
            self.ledger.record_requests(max(1, usage.requests), stage)
            self.ledger.record(run, stage)
        else:
            self.ledger.settle_reservation(stage, *reservation, run)
        return result.output

    @staticmethod
    def _call_token_ceiling(
        endpoint: ModelEndpoint,
        instructions: str,
        prompt: str,
        request_limit: int,
        output_token_limit: int,
    ) -> tuple[int, int]:
        # One UTF-8 byte per token is deliberately pessimistic for both Chinese
        # and Latin text. Retries can resend the full prompt.
        input_tokens = len((instructions + prompt).encode("utf-8")) * request_limit
        output_tokens = min(
            output_token_limit,
            endpoint.max_output_tokens * request_limit,
        )
        return input_tokens, output_tokens

    @staticmethod
    def _token_cost_ceiling(
        endpoint: ModelEndpoint, input_tokens: int, output_tokens: int
    ) -> float:
        return (
            input_tokens * endpoint.input_cost_cny_per_million
            + output_tokens * endpoint.output_cost_cny_per_million
        ) / 1_000_000

    def _remaining_token_budget(self) -> tuple[int, int]:
        input_tokens = self.ledger.remaining_input_tokens()
        output_tokens = self.ledger.remaining_output_tokens()
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
        reservation: tuple[int, float, int, int] | None,
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
        try:
            if reservation is None:
                self.ledger.record_requests(request_count, stage)
                self.ledger.record(run, stage)
            else:
                self.ledger.settle_reservation(stage, *reservation, run)
        except BudgetExceeded:
            pass

    @staticmethod
    def _semantic_validator(
        validator: Callable[[OutputT], OutputT | None],
        errors: list[str],
    ) -> Callable[[OutputT], OutputT]:
        def validate(output: OutputT) -> OutputT:
            try:
                normalized = validator(output)
            except ValueError as error:
                message = str(error).replace("\n", " ").strip()[:300]
                errors.append(message)
                raise ModelRetry(message) from error
            return normalized if normalized is not None else output

        return validate

    @staticmethod
    def _model_settings(
        endpoint: ModelEndpoint,
        output_token_limit: int | None = None,
        role: ModelRole | None = None,
    ) -> ModelSettings:
        extra_body: dict[str, object] | None = None
        if endpoint.provider == "alibaba":
            extra_body = {"enable_thinking": False}
        elif endpoint.provider == "deepseek" and role == "persona_edition_editor":
            extra_body = {"thinking": {"type": "disabled"}}
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
        elif endpoint.provider == "openai":
            if not self.secrets.openai_api_key:
                raise MissingProviderSecret("OPENAI_API_KEY is required")
            openai_provider = OpenAIProvider(api_key=self.secrets.openai_api_key)
            openai_provider.client.max_retries = 0
            return OpenAIChatModel(endpoint.model, provider=openai_provider)
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
        """A log-safe description that is still enough to debug from.

        Provider errors can echo the prompt back, so ModelHTTPError and
        ModelAPIError stay reduced to their type and status. Everything else is
        raised by pydantic-ai itself and describes structure rather than
        content ("Exceeded maximum retries (1) for output validation"), which
        is the one thing that makes a repeated failure diagnosable. Truncated,
        because a validator message can quote the offending output.
        """

        if isinstance(error, ModelHTTPError):
            return f"ModelHTTPError:{error.status_code}"
        if isinstance(error, ModelAPIError):
            return type(error).__name__
        message = str(error).replace("\n", " ").strip()
        return f"{type(error).__name__}: {message[:300]}" if message else type(error).__name__
