"""The failure ladder: what each failure still allows us to publish."""

from __future__ import annotations

import pytest

from ai_daily.degradation import FAILURE_CEILING, DegradationTracker, FailureClass
from ai_daily.publication import PublicationLevel

#: The documented ceiling of every failure, written out rather than read back
#: from the mapping under test.
DOCUMENTED_CEILING = [
    (FailureClass.SOURCE_COVERAGE_LOW, PublicationLevel.L0),
    (FailureClass.CANDIDATES_THIN, PublicationLevel.L2B),
    (FailureClass.CANDIDATES_EXHAUSTED, PublicationLevel.L3),
    (FailureClass.JUDGE_PARTIAL, PublicationLevel.L1),
    (FailureClass.JUDGE_FAILED, PublicationLevel.L2B),
    (FailureClass.PLAN_FAILED, PublicationLevel.L2A),
    (FailureClass.DRAFT_PARTIAL, PublicationLevel.L1),
    (FailureClass.DRAFT_FAILED, PublicationLevel.L1),
    (FailureClass.DETAIL_EVIDENCE_THIN, PublicationLevel.L1),
    (FailureClass.LEAD_UNCORROBORATED, PublicationLevel.L1),
    (FailureClass.BUDGET_EXHAUSTED, PublicationLevel.L2A),
    (FailureClass.RENDER_FAILED, PublicationLevel.L3),
]


def test_every_failure_class_has_a_documented_ceiling() -> None:
    assert {failure for failure, _ in DOCUMENTED_CEILING} == set(FailureClass)
    assert set(FAILURE_CEILING) == set(FailureClass)


@pytest.mark.parametrize(("failure", "expected"), DOCUMENTED_CEILING)
def test_a_single_failure_caps_the_issue_at_its_documented_level(
    failure: FailureClass, expected: PublicationLevel
) -> None:
    tracker = DegradationTracker()
    tracker.record(failure)

    assert tracker.ceiling() is expected
    assert tracker.clamp(PublicationLevel.L0) is expected


def test_an_empty_tracker_allows_a_full_issue() -> None:
    tracker = DegradationTracker()

    assert tracker.ceiling() is PublicationLevel.L0
    assert tracker.clamp(PublicationLevel.L0) is PublicationLevel.L0
    assert tracker.blocked is False


def test_clamp_takes_the_worst_ceiling_among_several_failures() -> None:
    tracker = DegradationTracker()
    tracker.record(FailureClass.SOURCE_COVERAGE_LOW)  # L0
    tracker.record(FailureClass.JUDGE_PARTIAL)  # L1
    tracker.record(FailureClass.PLAN_FAILED)  # L2A
    tracker.record(FailureClass.DRAFT_PARTIAL)  # L1

    assert tracker.ceiling() is PublicationLevel.L2A
    assert tracker.clamp(PublicationLevel.L0) is PublicationLevel.L2A


def test_clamp_never_promotes_a_level_the_caller_already_lowered() -> None:
    tracker = DegradationTracker()
    tracker.record(FailureClass.JUDGE_PARTIAL)  # ceiling L1

    assert tracker.clamp(PublicationLevel.L2B) is PublicationLevel.L2B
    assert tracker.clamp(PublicationLevel.L3) is PublicationLevel.L3


@pytest.mark.parametrize(
    ("failures", "blocked"),
    [
        ([], False),
        ([FailureClass.SOURCE_COVERAGE_LOW], False),
        ([FailureClass.DRAFT_FAILED, FailureClass.JUDGE_FAILED], False),
        ([FailureClass.CANDIDATES_THIN], False),
        ([FailureClass.CANDIDATES_EXHAUSTED], True),
        ([FailureClass.RENDER_FAILED], True),
        ([FailureClass.JUDGE_PARTIAL, FailureClass.CANDIDATES_EXHAUSTED], True),
    ],
)
def test_blocked_is_true_only_at_l3(failures: list[FailureClass], blocked: bool) -> None:
    tracker = DegradationTracker()
    for failure in failures:
        tracker.record(failure)

    assert tracker.blocked is blocked
    assert (tracker.ceiling() is PublicationLevel.L3) is blocked


def test_a_repeated_failure_is_recorded_and_explained_once() -> None:
    tracker = DegradationTracker()
    tracker.record(FailureClass.DRAFT_PARTIAL)
    tracker.record(FailureClass.DRAFT_PARTIAL)

    assert tracker.failures == [FailureClass.DRAFT_PARTIAL]
    assert tracker.reasons() == ["部分详报起草失败"]


def test_reasons_are_human_readable_for_every_failure() -> None:
    tracker = DegradationTracker()
    for failure in FailureClass:
        tracker.record(failure)

    reasons = tracker.reasons()

    assert len(reasons) == len(list(FailureClass))
    assert all(reason and len(reason) <= 300 for reason in reasons)
