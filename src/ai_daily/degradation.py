"""Failure classes and the ladder that maps them onto publication levels.

The pipeline used to treat every quality problem as fatal, so one blocked
publisher cost a whole day's issue. Each failure now names the poorest level
it can still produce, and the pipeline publishes the best level that survives.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .publication import LEVEL_ORDER, PublicationLevel


class FailureClass(StrEnum):
    """Every way a run can fall short, in pipeline order."""

    SOURCE_COVERAGE_LOW = "source_coverage_low"
    """Tier A coverage below the configured floor. Recorded, never fatal:
    coverage counts sources that answered, not sources that yielded news."""

    CANDIDATES_THIN = "candidates_thin"
    """Fewer fresh candidates than a full issue needs, but enough to publish."""

    CANDIDATES_EXHAUSTED = "candidates_exhausted"
    """Too few fresh candidates to publish anything worth reading."""

    JUDGE_PARTIAL = "judge_partial"
    """Some judge batches failed. Completed batches are still usable."""

    JUDGE_FAILED = "judge_failed"
    """No judge decision survived, so relevance is unknown."""

    PLAN_FAILED = "plan_failed"
    """Editorial planning failed; judge decisions can still be ranked."""

    DRAFT_PARTIAL = "draft_partial"
    """Some stories could not be drafted and were demoted to briefs."""

    DRAFT_FAILED = "draft_failed"
    """No story could be drafted."""

    DETAIL_EVIDENCE_THIN = "detail_evidence_thin"
    """A detailed story never reached the evidence floor, even after repair."""

    BUDGET_EXHAUSTED = "budget_exhausted"
    """The day's model budget ran out mid-run."""

    RENDER_FAILED = "render_failed"
    """The renderer itself raised. Nothing new can be shown."""


#: The poorest level each failure still permits. A run's level is the worst
#: ceiling among the failures it hit.
FAILURE_CEILING: dict[FailureClass, PublicationLevel] = {
    FailureClass.SOURCE_COVERAGE_LOW: PublicationLevel.L0,
    FailureClass.CANDIDATES_THIN: PublicationLevel.L2B,
    FailureClass.CANDIDATES_EXHAUSTED: PublicationLevel.L3,
    FailureClass.JUDGE_PARTIAL: PublicationLevel.L1,
    FailureClass.JUDGE_FAILED: PublicationLevel.L2B,
    FailureClass.PLAN_FAILED: PublicationLevel.L2A,
    FailureClass.DRAFT_PARTIAL: PublicationLevel.L1,
    FailureClass.DRAFT_FAILED: PublicationLevel.L2A,
    FailureClass.DETAIL_EVIDENCE_THIN: PublicationLevel.L1,
    FailureClass.BUDGET_EXHAUSTED: PublicationLevel.L2A,
    FailureClass.RENDER_FAILED: PublicationLevel.L3,
}

FAILURE_REASON: dict[FailureClass, str] = {
    FailureClass.SOURCE_COVERAGE_LOW: "部分一线来源未响应",
    FailureClass.CANDIDATES_THIN: "新鲜候选不足以支撑完整刊期",
    FailureClass.CANDIDATES_EXHAUSTED: "新鲜候选过少，无法出刊",
    FailureClass.JUDGE_PARTIAL: "部分筛选批次失败",
    FailureClass.JUDGE_FAILED: "筛选环节失败",
    FailureClass.PLAN_FAILED: "编辑规划失败",
    FailureClass.DRAFT_PARTIAL: "部分详报起草失败",
    FailureClass.DRAFT_FAILED: "详报起草失败",
    FailureClass.DETAIL_EVIDENCE_THIN: "详报证据不足",
    FailureClass.BUDGET_EXHAUSTED: "当日模型预算用尽",
    FailureClass.RENDER_FAILED: "渲染失败",
}


@dataclass
class DegradationTracker:
    """Accumulates failures during a run and reports the level they allow."""

    failures: list[FailureClass] = field(default_factory=list)

    def record(self, failure: FailureClass) -> None:
        if failure not in self.failures:
            self.failures.append(failure)

    def ceiling(self) -> PublicationLevel:
        """The best level still permitted by everything recorded so far."""

        level = PublicationLevel.L0
        for failure in self.failures:
            candidate = FAILURE_CEILING[failure]
            if LEVEL_ORDER[candidate] > LEVEL_ORDER[level]:
                level = candidate
        return level

    def clamp(self, level: PublicationLevel) -> PublicationLevel:
        """Lower ``level`` to the ceiling when a failure demands it."""

        ceiling = self.ceiling()
        return ceiling if LEVEL_ORDER[ceiling] > LEVEL_ORDER[level] else level

    def reasons(self) -> list[str]:
        return [FAILURE_REASON[failure] for failure in self.failures]

    @property
    def blocked(self) -> bool:
        return self.ceiling() is PublicationLevel.L3
