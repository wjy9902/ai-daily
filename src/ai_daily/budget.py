"""Model spend accounting, shared across every run of the same day.

The ledger used to live only in memory, so the ¥5 ceiling was a *per process*
limit: three timer windows in one morning could spend ¥15. It now persists to
``budget/<date>.json`` and every run of that date resumes from the same totals.

Each stage also gets its own slice of the day's budget. A stage that runs out
degrades on its own rather than consuming what the later stages need.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import BudgetConfig, ModelRun


class BudgetExceeded(RuntimeError):
    """The day's overall budget is gone. Nothing more may be spent."""


class StageBudgetExceeded(BudgetExceeded):
    """One stage's slice is gone. That stage degrades; later stages continue."""


class BudgetStage(StrEnum):
    JUDGE = "judge"
    PLAN = "plan"
    DRAFT = "draft"


#: How the day's budget is divided. Sized against observed spend, not call
#: counts: planning is one call per run but the most expensive kind of call
#: (full candidate payload in, whole plan out, validator retries billed), and
#: the day holds up to three timer windows that may each need to replan. On
#: 2026-08-27 planning alone cost ¥0.75 while its slice at 10% was ¥0.50,
#: which left the later windows unable to replan at all. Judging is the cheap
#: stage in practice (¥0.26 that same day), so its share shrinks instead.
STAGE_SHARE: dict[BudgetStage, float] = {
    BudgetStage.JUDGE: 0.30,
    BudgetStage.PLAN: 0.25,
    BudgetStage.DRAFT: 0.45,
}


def _empty_stage_ints() -> dict[str, int]:
    return {stage.value: 0 for stage in BudgetStage}


def _empty_stage_floats() -> dict[str, float]:
    return {stage.value: 0.0 for stage in BudgetStage}


@dataclass
class BudgetLedger:
    """Running totals for one calendar day.

    Pass ``store_path`` to make the totals survive across processes. Without it
    the ledger is in-memory only, which is what the tests want.
    """

    config: BudgetConfig
    store_path: Path | None = None
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cny: float = 0
    stage_requests: dict[str, int] = field(default_factory=_empty_stage_ints)
    stage_cost: dict[str, float] = field(default_factory=_empty_stage_floats)
    runs_today: int = 0

    def __post_init__(self) -> None:
        if self.store_path is not None and self.store_path.exists():
            self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        assert self.store_path is not None
        try:
            payload: dict[str, Any] = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            # A corrupt ledger must not silently reset the day's spend to zero:
            # that is exactly how a budget ceiling gets bypassed.
            raise BudgetExceeded(f"budget ledger is unreadable: {error}") from error
        self.requests = int(payload.get("requests", 0))
        self.input_tokens = int(payload.get("input_tokens", 0))
        self.output_tokens = int(payload.get("output_tokens", 0))
        self.cost_cny = float(payload.get("cost_cny", 0))
        self.runs_today = int(payload.get("runs_today", 0))
        stored_requests = payload.get("stage_requests", {})
        stored_cost = payload.get("stage_cost", {})
        for stage in BudgetStage:
            self.stage_requests[stage.value] = int(stored_requests.get(stage.value, 0))
            self.stage_cost[stage.value] = float(stored_cost.get(stage.value, 0))

    def persist(self) -> None:
        if self.store_path is None:
            return
        payload = {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_cny": round(self.cost_cny, 6),
            "stage_requests": dict(self.stage_requests),
            "stage_cost": {key: round(value, 6) for key, value in self.stage_cost.items()},
            "runs_today": self.runs_today,
        }
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store_path.with_suffix(f"{self.store_path.suffix}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.store_path)

    def start_run(self) -> None:
        self.runs_today += 1
        self.persist()

    # --------------------------------------------------------------- budgets
    def remaining_requests(self) -> int:
        return max(0, self.config.request_limit - self.requests)

    def remaining_cost(self) -> float:
        return max(0.0, self.config.cost_cny_limit - self.cost_cny)

    def stage_remaining_requests(self, stage: BudgetStage) -> int:
        allowance = int(self.config.request_limit * STAGE_SHARE[stage])
        used = self.stage_requests[stage.value]
        return max(0, min(allowance - used, self.remaining_requests()))

    def stage_remaining_cost(self, stage: BudgetStage) -> float:
        allowance = self.config.cost_cny_limit * STAGE_SHARE[stage]
        used = self.stage_cost[stage.value]
        return max(0.0, min(allowance - used, self.remaining_cost()))

    def check_stage(self, stage: BudgetStage) -> None:
        """Raise before spending anything if this stage has nothing left."""

        if self.remaining_requests() <= 0:
            raise BudgetExceeded("daily model request limit exceeded")
        if self.remaining_cost() <= 0:
            raise BudgetExceeded("daily cost limit exceeded")
        if self.stage_remaining_requests(stage) <= 0:
            raise StageBudgetExceeded(f"{stage.value} request allowance exhausted")
        if self.stage_remaining_cost(stage) <= 0:
            raise StageBudgetExceeded(f"{stage.value} cost allowance exhausted")

    def request_allowance(self, stage: BudgetStage, wanted: int) -> int:
        """How many provider requests this call may use, at most ``wanted``.

        Pydantic AI fixes its request limit when the run starts, so this is
        decided up front rather than topped up mid-flight.
        """

        self.check_stage(stage)
        return max(1, min(wanted, self.stage_remaining_requests(stage)))

    # --------------------------------------------------------------- recording
    def record_requests(self, count: int, stage: BudgetStage | None = None) -> None:
        if count < 1:
            raise ValueError("request count must be positive")
        self.requests += count
        if stage is not None:
            self.stage_requests[stage.value] += count
        self.persist()
        if self.requests > self.config.request_limit:
            raise BudgetExceeded("model request limit exceeded")

    def record(self, run: ModelRun, stage: BudgetStage | None = None) -> None:
        """Record what a call actually consumed.

        Totals advance before any limit check because the spend already
        happened; persisting first means a crash cannot lose it.
        """

        self.input_tokens += run.input_tokens
        self.output_tokens += run.output_tokens
        self.cost_cny += run.cost_cny or 0
        if stage is not None:
            self.stage_cost[stage.value] += run.cost_cny or 0
        self.persist()
        if self.input_tokens > self.config.input_token_limit:
            raise BudgetExceeded("input token limit exceeded")
        if self.output_tokens > self.config.output_token_limit:
            raise BudgetExceeded("output token limit exceeded")
        if self.cost_cny > self.config.cost_cny_limit:
            raise BudgetExceeded("cost limit exceeded")

    def snapshot(self) -> dict[str, Any]:
        """A JSON-safe view for status.json and run artifacts."""

        return {
            "requests": self.requests,
            "request_limit": self.config.request_limit,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_cny": round(self.cost_cny, 6),
            "cost_limit_cny": self.config.cost_cny_limit,
            "stage_requests": dict(self.stage_requests),
            "stage_cost": {key: round(value, 6) for key, value in self.stage_cost.items()},
            "runs_today": self.runs_today,
        }
