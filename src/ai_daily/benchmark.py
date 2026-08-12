from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from ai_daily.config import AppConfig, Secrets
from ai_daily.model_gateway import ModelGateway, ModelInvocationFailed
from ai_daily.models import JudgeDecision, ModelEndpoint, ModelsConfig, RoleModelConfig

CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
JUDGE_INSTRUCTIONS = (
    "判断一条候选是否应进入中文 AI 日报。只使用给定证据，reason 使用中文，"
    "evidence_ids 必须原样返回。"
)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    summary: str
    url: HttpUrl
    expected_selected: bool
    expected_category: str


class PredictionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    expected_selected: bool
    expected_category: str
    prediction: JudgeDecision | None = None
    error: str | None = None
    latency_ms: int = Field(ge=0)
    cost_cny: float = Field(ge=0)
    fallback_used: bool


def load_dataset(path: Path) -> list[EvalCase]:
    values = json.loads(path.read_text(encoding="utf-8"))
    cases = [EvalCase.model_validate(value) for value in values]
    if not 20 <= len(cases) <= 30:
        raise ValueError("benchmark dataset must contain 20 to 30 cases")
    return cases


def score_records(records: list[PredictionRecord]) -> dict[str, float | int | bool]:
    if len(records) < 20:
        return {"cases": len(records), "score": 0, "eligible": False}
    schema_rate = sum(record.prediction is not None for record in records) / len(records)
    evidence_rate = sum(
        record.prediction is not None
        and record.prediction.evidence_ids == [f"{record.case_id}-1"]
        for record in records
    ) / len(records)
    selection_rate = sum(
        record.prediction is not None
        and record.prediction.selected == record.expected_selected
        and record.prediction.category == record.expected_category
        for record in records
    ) / len(records)
    chinese_rate = sum(
        record.prediction is not None and bool(CHINESE_RE.search(record.prediction.reason))
        for record in records
    ) / len(records)
    average_latency = sum(record.latency_ms for record in records) / len(records)
    total_cost = sum(record.cost_cny for record in records)
    latency_score = 10 if average_latency <= 30_000 else 5 if average_latency <= 60_000 else 0
    cost_score = 5 if total_cost <= 5 else 2.5 if total_cost <= 10 else 0
    score = evidence_rate * 35 + selection_rate * 20 + schema_rate * 15 + chinese_rate * 15
    score += latency_score + cost_score
    eligible = (
        schema_rate == 1
        and evidence_rate == 1
        and not any(record.fallback_used for record in records)
        and average_latency <= 90_000
    )
    return {
        "cases": len(records),
        "schema_rate": round(schema_rate, 4),
        "evidence_rate": round(evidence_rate, 4),
        "selection_rate": round(selection_rate, 4),
        "chinese_rate": round(chinese_rate, 4),
        "average_latency_ms": round(average_latency),
        "total_cost_cny": round(total_cost, 4),
        "score": round(score, 2),
        "eligible": eligible,
    }


def case_prompt(case: EvalCase) -> str:
    return json.dumps(
        {
            "event_id": case.id,
            "title": case.title,
            "summary": case.summary,
            "evidence": [{"evidence_id": f"{case.id}-1", "url": str(case.url)}],
        },
        ensure_ascii=False,
    )


async def evaluate_profile(
    cases: list[EvalCase],
    primary: ModelEndpoint,
    fallback: ModelEndpoint,
    config: AppConfig,
    secrets: Secrets,
) -> list[PredictionRecord]:
    role_config = RoleModelConfig(primary=primary, fallback=fallback)
    model_config = ModelsConfig(
        budget=config.models.budget,
        roles={"judge": role_config, "editor": role_config},
    )
    gateway = ModelGateway(model_config, secrets)
    records: list[PredictionRecord] = []
    for case in cases:
        before_runs = len(gateway.runs)
        try:
            prediction = await gateway.generate(
                "judge",
                JudgeDecision,
                instructions=JUDGE_INSTRUCTIONS,
                prompt=case_prompt(case),
            )
            error = None
        except ModelInvocationFailed as exc:
            prediction = None
            error = str(exc)
        new_runs = gateway.runs[before_runs:]
        final_run = new_runs[-1]
        records.append(
            PredictionRecord(
                case_id=case.id,
                expected_selected=case.expected_selected,
                expected_category=case.expected_category,
                prediction=prediction,
                error=error,
                latency_ms=final_run.latency_ms,
                cost_cny=final_run.cost_cny or 0,
                fallback_used=final_run.actual_model != primary.model,
            )
        )
    return records


async def benchmark_models(dataset: Path, config: AppConfig, secrets: Secrets) -> dict[str, object]:
    cases = load_dataset(dataset)
    roles = config.models.roles
    profiles = {
        "alibaba-judge": (roles["judge"].primary, roles["judge"].fallback),
        "deepseek-judge": (roles["judge"].fallback, roles["judge"].primary),
        "alibaba-editor": (roles["editor"].primary, roles["editor"].fallback),
        "deepseek-editor": (roles["editor"].fallback, roles["editor"].primary),
    }
    results: dict[str, object] = {}
    ranked: list[tuple[str, float]] = []
    for profile, (primary, fallback) in profiles.items():
        records = await evaluate_profile(cases, primary, fallback, config, secrets)
        metrics = score_records(records)
        results[profile] = {
            "primary": f"{primary.provider}:{primary.model}",
            "metrics": metrics,
            "records": [record.model_dump(mode="json") for record in records],
        }
        if metrics["eligible"] is True:
            ranked.append((profile, float(metrics["score"])))
    ranked.sort(key=lambda item: item[1], reverse=True)
    recommendation = "keep-current"
    if len(ranked) >= 2 and ranked[0][1] - ranked[1][1] >= 5:
        recommendation = ranked[0][0]
    return {"profiles": results, "recommendation": recommendation}
