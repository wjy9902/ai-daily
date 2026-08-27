"""Model spend accounting, shared across every run of the same day.

The ledger used to live only in memory, so the ¥5 ceiling was a *per process*
limit: three timer windows in one morning could spend ¥15. It now persists to
``budget/<date>.json`` and every run of that date resumes from the same totals.

Each stage also gets its own slice of the day's budget. A stage that runs out
degrades on its own rather than consuming what the later stages need.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
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
    PERSONA = "persona"


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
    # Persona uses a separate ledger and owns its full configured allowance.
    BudgetStage.PERSONA: 1.0,
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
    reserved_requests: int = 0
    reserved_input_tokens: int = 0
    reserved_output_tokens: int = 0
    reserved_cost_cny: float = 0
    stage_reserved_requests: dict[str, int] = field(default_factory=_empty_stage_ints)
    stage_reserved_cost: dict[str, float] = field(default_factory=_empty_stage_floats)
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
        self.reserved_requests = int(payload.get("reserved_requests", 0))
        self.reserved_input_tokens = int(payload.get("reserved_input_tokens", 0))
        self.reserved_output_tokens = int(payload.get("reserved_output_tokens", 0))
        self.reserved_cost_cny = float(payload.get("reserved_cost_cny", 0))
        stored_requests = payload.get("stage_requests", {})
        stored_cost = payload.get("stage_cost", {})
        reserved_requests = payload.get("stage_reserved_requests", {})
        reserved_cost = payload.get("stage_reserved_cost", {})
        for stage in BudgetStage:
            self.stage_requests[stage.value] = int(stored_requests.get(stage.value, 0))
            self.stage_cost[stage.value] = float(stored_cost.get(stage.value, 0))
            self.stage_reserved_requests[stage.value] = int(reserved_requests.get(stage.value, 0))
            self.stage_reserved_cost[stage.value] = float(reserved_cost.get(stage.value, 0))

    def _persist_unlocked(self) -> None:
        if self.store_path is None:
            return
        payload = {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_cny": round(self.cost_cny, 6),
            "stage_requests": dict(self.stage_requests),
            "stage_cost": {key: round(value, 6) for key, value in self.stage_cost.items()},
            "reserved_requests": self.reserved_requests,
            "reserved_input_tokens": self.reserved_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "reserved_cost_cny": round(self.reserved_cost_cny, 6),
            "stage_reserved_requests": dict(self.stage_reserved_requests),
            "stage_reserved_cost": {
                key: round(value, 6) for key, value in self.stage_reserved_cost.items()
            },
            "runs_today": self.runs_today,
        }
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.store_path.with_suffix(f"{self.store_path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.store_path)

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self.store_path is None:
            yield
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.store_path.with_suffix(f"{self.store_path.suffix}.lock")
        with lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            if self.store_path.exists():
                self._load()
            try:
                yield
                self._persist_unlocked()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def persist(self) -> None:
        with self._transaction():
            pass

    def start_run(self, *, recover_stale_reservations: bool = False) -> None:
        with self._transaction():
            if recover_stale_reservations and self.reserved_requests:
                self._charge_stale_reservations()
            self.runs_today += 1

    def _charge_stale_reservations(self) -> None:
        self.requests += self.reserved_requests
        self.input_tokens += self.reserved_input_tokens
        self.output_tokens += self.reserved_output_tokens
        self.cost_cny += self.reserved_cost_cny
        for stage in BudgetStage:
            key = stage.value
            self.stage_requests[key] += self.stage_reserved_requests[key]
            self.stage_cost[key] += self.stage_reserved_cost[key]
            self.stage_reserved_requests[key] = 0
            self.stage_reserved_cost[key] = 0.0
        self.reserved_requests = 0
        self.reserved_input_tokens = 0
        self.reserved_output_tokens = 0
        self.reserved_cost_cny = 0.0

    # --------------------------------------------------------------- budgets
    def remaining_requests(self) -> int:
        return max(0, self.config.request_limit - self.requests - self.reserved_requests)

    def remaining_cost(self) -> float:
        return max(
            0.0,
            self.config.cost_cny_limit - self.cost_cny - self.reserved_cost_cny,
        )

    def remaining_input_tokens(self) -> int:
        return max(
            0,
            self.config.input_token_limit - self.input_tokens - self.reserved_input_tokens,
        )

    def remaining_output_tokens(self) -> int:
        return max(
            0,
            self.config.output_token_limit - self.output_tokens - self.reserved_output_tokens,
        )

    def stage_remaining_requests(self, stage: BudgetStage) -> int:
        allowance = int(self.config.request_limit * STAGE_SHARE[stage])
        used = self.stage_requests[stage.value] + self.stage_reserved_requests[stage.value]
        return max(0, min(allowance - used, self.remaining_requests()))

    def stage_remaining_cost(self, stage: BudgetStage) -> float:
        allowance = self.config.cost_cny_limit * STAGE_SHARE[stage]
        used = self.stage_cost[stage.value] + self.stage_reserved_cost[stage.value]
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

    def reserve(
        self,
        stage: BudgetStage,
        requests: int,
        cost_cny: float,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        if requests < 1 or cost_cny <= 0:
            raise ValueError("budget reservation must be positive")
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token reservation cannot be negative")
        with self._transaction():
            self.check_stage(stage)
            if requests > self.stage_remaining_requests(stage):
                raise StageBudgetExceeded(f"{stage.value} request reservation exceeds allowance")
            if cost_cny > self.stage_remaining_cost(stage):
                raise StageBudgetExceeded(f"{stage.value} cost reservation exceeds allowance")
            if input_tokens > self.remaining_input_tokens():
                raise BudgetExceeded("input token reservation exceeds allowance")
            if output_tokens > self.remaining_output_tokens():
                raise BudgetExceeded("output token reservation exceeds allowance")
            self.reserved_requests += requests
            self.reserved_input_tokens += input_tokens
            self.reserved_output_tokens += output_tokens
            self.reserved_cost_cny += cost_cny
            self.stage_reserved_requests[stage.value] += requests
            self.stage_reserved_cost[stage.value] += cost_cny

    def release_reservation(
        self,
        stage: BudgetStage,
        requests: int,
        cost_cny: float,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        with self._transaction():
            if (
                requests > self.stage_reserved_requests[stage.value]
                or cost_cny > self.stage_reserved_cost[stage.value] + 1e-9
                or input_tokens > self.reserved_input_tokens
                or output_tokens > self.reserved_output_tokens
            ):
                raise BudgetExceeded("budget reservation state is inconsistent")
            self.reserved_requests -= requests
            self.reserved_input_tokens -= input_tokens
            self.reserved_output_tokens -= output_tokens
            self.reserved_cost_cny -= cost_cny
            self.stage_reserved_requests[stage.value] -= requests
            self.stage_reserved_cost[stage.value] -= cost_cny

    def settle_reservation(
        self,
        stage: BudgetStage,
        requests: int,
        cost_cny: float,
        input_tokens: int,
        output_tokens: int,
        run: ModelRun,
    ) -> None:
        actual_requests = max(1, run.request_count)
        with self._transaction():
            if (
                requests > self.stage_reserved_requests[stage.value]
                or cost_cny > self.stage_reserved_cost[stage.value] + 1e-9
                or input_tokens > self.reserved_input_tokens
                or output_tokens > self.reserved_output_tokens
            ):
                raise BudgetExceeded("budget reservation state is inconsistent")
            self.reserved_requests -= requests
            self.reserved_input_tokens -= input_tokens
            self.reserved_output_tokens -= output_tokens
            self.reserved_cost_cny -= cost_cny
            self.stage_reserved_requests[stage.value] -= requests
            self.stage_reserved_cost[stage.value] -= cost_cny
            self.requests += actual_requests
            self.input_tokens += run.input_tokens
            self.output_tokens += run.output_tokens
            self.cost_cny += run.cost_cny or 0
            self.stage_requests[stage.value] += actual_requests
            self.stage_cost[stage.value] += run.cost_cny or 0
        self._check_actual_limits()

    def _check_actual_limits(self) -> None:
        if self.requests > self.config.request_limit:
            raise BudgetExceeded("model request limit exceeded")
        if self.input_tokens > self.config.input_token_limit:
            raise BudgetExceeded("input token limit exceeded")
        if self.output_tokens > self.config.output_token_limit:
            raise BudgetExceeded("output token limit exceeded")
        if self.cost_cny > self.config.cost_cny_limit:
            raise BudgetExceeded("cost limit exceeded")

    # --------------------------------------------------------------- recording
    def record_requests(self, count: int, stage: BudgetStage | None = None) -> None:
        if count < 1:
            raise ValueError("request count must be positive")
        with self._transaction():
            self.requests += count
            if stage is not None:
                self.stage_requests[stage.value] += count
        if self.requests > self.config.request_limit:
            raise BudgetExceeded("model request limit exceeded")

    def record(self, run: ModelRun, stage: BudgetStage | None = None) -> None:
        """Record what a call actually consumed.

        Totals advance before any limit check because the spend already
        happened; persisting first means a crash cannot lose it.
        """

        with self._transaction():
            self.input_tokens += run.input_tokens
            self.output_tokens += run.output_tokens
            self.cost_cny += run.cost_cny or 0
            if stage is not None:
                self.stage_cost[stage.value] += run.cost_cny or 0
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
            "reserved_requests": self.reserved_requests,
            "reserved_input_tokens": self.reserved_input_tokens,
            "reserved_output_tokens": self.reserved_output_tokens,
            "reserved_cost_cny": round(self.reserved_cost_cny, 6),
            "runs_today": self.runs_today,
        }
