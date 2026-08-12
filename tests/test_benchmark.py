import json

import pytest

from ai_daily.benchmark import PredictionRecord, load_dataset, score_records
from ai_daily.models import JudgeDecision


def case(index: int) -> dict[str, object]:
    return {
        "id": str(index),
        "expected_selected": True,
        "expected_category": "模型与平台",
        "title": "Official model release",
        "summary": "The provider released a model.",
        "url": f"https://example.com/{index}",
    }


def test_benchmark_requires_20_cases(tmp_path) -> None:
    dataset = tmp_path / "eval.json"
    dataset.write_text(json.dumps([case(index) for index in range(20)]))
    assert len(load_dataset(dataset)) == 20


def test_benchmark_rejects_too_small_dataset(tmp_path) -> None:
    dataset = tmp_path / "eval.json"
    dataset.write_text(json.dumps([case(1)]))
    with pytest.raises(ValueError, match="20"):
        load_dataset(dataset)


def test_benchmark_scores_evidence_selection_chinese_latency_and_cost() -> None:
    records = [
        PredictionRecord(
            case_id=str(index),
            expected_selected=True,
            expected_category="模型与平台",
            prediction=JudgeDecision(
                event_id=str(index),
                selected=True,
                category="模型与平台",
                relevance=90,
                confidence=0.9,
                reason="官方发布值得关注",
                evidence_ids=[f"{index}-1"],
            ),
            latency_ms=100,
            cost_cny=0.01,
            fallback_used=False,
        )
        for index in range(20)
    ]
    result = score_records(records)
    assert result["score"] == 100
    assert result["schema_rate"] == 1
    assert result["eligible"] is True


def test_benchmark_records_schema_failures_in_score() -> None:
    records = [
        PredictionRecord(
            case_id=str(index),
            expected_selected=True,
            expected_category="模型与平台",
            prediction=None,
            error="UnexpectedModelBehavior",
            latency_ms=100,
            cost_cny=0,
            fallback_used=False,
        )
        for index in range(20)
    ]
    result = score_records(records)
    assert result["schema_rate"] == 0
    assert result["score"] == 15
    assert result["eligible"] is False
