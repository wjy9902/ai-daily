from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from ai_daily.budget import BudgetStage
from ai_daily.model_gateway import ModelGateway
from ai_daily.persona_models import (
    BaselineMatch,
    BaselineResolution,
    PersonaPlan,
    UpstreamSnapshot,
)
from ai_daily.persona_snapshot import load_upstream_snapshot
from ai_daily.site_publisher import SiteLayout

MATCH_CONFIDENCE = 0.8
HISTORY_CANDIDATE_LIMIT = 40


async def resolve_baselines(
    gateway: ModelGateway,
    layout: SiteLayout,
    snapshot: UpstreamSnapshot,
    plan: PersonaPlan,
    window_days: int,
) -> tuple[dict[str, BaselineMatch], dict[str, str]]:
    selected_ids = [item.event_id for item in plan.selections if item.grade in {"S", "A"}]
    if not selected_ids:
        return {}, {}
    historical = _historical_candidates(layout, snapshot.target_date, window_days)
    if not historical:
        return {}, {}
    current = [_event_row(snapshot, event_id) for event_id in selected_ids]
    candidates = _lexical_candidates(current, historical)
    prompt = json.dumps(
        {"current_events": current, "historical_candidates": candidates},
        ensure_ascii=False,
    )
    output = await gateway.generate(
        "persona_baseline",
        BaselineResolution,
        (
            "你负责判断当前事件是否是历史事件的可比较延续。只可使用给定候选。"
            "版本、价格、能力或政策必须属于同一实体和同一指标才算基线。"
            "无法可靠匹配时 matched_event_id=null、baseline_evidence_ids=[]，不要猜。"
            "matches 必须逐一覆盖 current_events，event_id 不得重复。"
        ),
        prompt,
        validator=lambda value: _validate_resolution(value, selected_ids, candidates),
        stage=BudgetStage.PERSONA,
    )
    matches = {
        item.event_id: item
        for item in output.matches
        if item.confidence >= MATCH_CONFIDENCE
        and item.matched_event_id
        and item.baseline_evidence_ids
    }
    referenced = {
        evidence_id for match in matches.values() for evidence_id in match.baseline_evidence_ids
    }
    evidence = {
        str(item["evidence_id"]): str(item["excerpt"])
        for row in historical
        for item in row["evidence"]
        if str(item["evidence_id"]) in referenced
    }
    return matches, evidence


def _historical_candidates(
    layout: SiteLayout, target: date, window_days: int
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset in range(1, window_days + 1):
        day = target - timedelta(days=offset)
        if not layout.upstream_pointer_path(day).exists():
            continue
        try:
            snapshot = load_upstream_snapshot(layout, day)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        bundles = {bundle.event_id: bundle for bundle in snapshot.evidence_bundles}
        for event in snapshot.events:
            evidence = bundles.get(event.event_id)
            rows.append(
                {
                    "date": day.isoformat(),
                    "event_id": event.event_id,
                    "title": event.title,
                    "summary": event.summary[:500],
                    "evidence": [
                        {
                            "evidence_id": item.evidence_id,
                            "excerpt": item.excerpt[:700],
                        }
                        for item in (evidence.evidence if evidence else [])
                    ],
                }
            )
    return rows


def _event_row(snapshot: UpstreamSnapshot, event_id: str) -> dict[str, Any]:
    event = next(item for item in snapshot.events if item.event_id == event_id)
    return {"event_id": event_id, "title": event.title, "summary": event.summary[:500]}


def _lexical_candidates(
    current: list[dict[str, Any]], historical: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    current_terms = set().union(*(_terms(str(item["title"])) for item in current))
    ranked = sorted(
        historical,
        key=lambda item: (
            -len(current_terms & _terms(str(item["title"]))),
            str(item["date"]),
            str(item["event_id"]),
        ),
    )
    return ranked[:HISTORY_CANDIDATE_LIMIT]


def _terms(text: str) -> set[str]:
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9][a-z0-9._-]+", lowered))
    han = "".join(re.findall(r"[\u4e00-\u9fff]", lowered))
    return words | {han[index : index + 2] for index in range(max(0, len(han) - 1))}


def _validate_resolution(
    output: BaselineResolution,
    selected_ids: list[str],
    candidates: list[dict[str, Any]],
) -> None:
    ids = [item.event_id for item in output.matches]
    if sorted(ids) != sorted(selected_ids):
        raise ValueError("baseline output must cover each selected event exactly once")
    candidate_ids = {str(item["event_id"]) for item in candidates}
    evidence_by_event = {
        str(item["event_id"]): {str(evidence["evidence_id"]) for evidence in item["evidence"]}
        for item in candidates
    }
    for match in output.matches:
        if match.matched_event_id and match.matched_event_id not in candidate_ids:
            raise ValueError("baseline output referenced unknown historical event")
        allowed = evidence_by_event.get(match.matched_event_id or "", set())
        if not set(match.baseline_evidence_ids) <= allowed:
            raise ValueError("baseline output referenced unknown historical evidence")
