from __future__ import annotations

from dataclasses import dataclass

from ai_daily.models import BudgetConfig, ModelRun


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BudgetLedger:
    config: BudgetConfig
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cny: float = 0

    def reserve_request(self) -> None:
        self.record_requests(1)

    def record_requests(self, count: int) -> None:
        if count < 1:
            raise ValueError("request count must be positive")
        if self.requests + count > self.config.request_limit:
            raise BudgetExceeded("model request limit exceeded")
        self.requests += count

    def reconcile_requests(self, actual_requests: int) -> None:
        additional = max(0, actual_requests - 1)
        if self.requests + additional > self.config.request_limit:
            raise BudgetExceeded("model request limit exceeded")
        self.requests += additional

    def record(self, run: ModelRun) -> None:
        next_input = self.input_tokens + run.input_tokens
        next_output = self.output_tokens + run.output_tokens
        next_cost = self.cost_cny + (run.cost_cny or 0)
        self.input_tokens = next_input
        self.output_tokens = next_output
        self.cost_cny = next_cost
        if next_input > self.config.input_token_limit:
            raise BudgetExceeded("input token limit exceeded")
        if next_output > self.config.output_token_limit:
            raise BudgetExceeded("output token limit exceeded")
        if next_cost > self.config.cost_cny_limit:
            raise BudgetExceeded("cost limit exceeded")
