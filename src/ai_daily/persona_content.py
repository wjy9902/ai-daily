from __future__ import annotations

from collections import defaultdict, deque
from typing import Literal

from ai_daily.models import EditorialTier
from ai_daily.persona_models import PersonaShortlist, ShortlistAuditEntry, UpstreamSnapshot

type ShortlistPhase = Literal["editorial_tier", "category_round_robin", "score_fallback", "omitted"]

CATEGORY_ORDER = (
    "模型与平台",
    "行业动态",
    "国内 AI",
    "值得试的项目",
    "前沿研究",
    "前瞻与传闻",
)
CATEGORY_ROUND_ROBIN_LIMIT = 4


def build_shortlist(snapshot: UpstreamSnapshot, limit: int = 30) -> PersonaShortlist:
    """Keep editorial prominence, then rotate categories before score fallback."""

    events = {event.event_id: event for event in snapshot.events}
    selected: list[str] = []
    phases: dict[str, tuple[ShortlistPhase, str]] = {}

    if snapshot.editorial_plan is not None:
        for tier in (EditorialTier.LEAD, EditorialTier.FOLLOW):
            choices = sorted(
                (
                    choice
                    for choice in snapshot.editorial_plan.selections
                    if choice.tier is tier and choice.event_id in events
                ),
                key=lambda choice: (-choice.importance, choice.event_id),
            )
            for choice in choices:
                if choice.event_id in events:
                    _append(selected, choice.event_id, limit)
                    phases[choice.event_id] = (
                        "editorial_tier",
                        f"upstream_{tier.value}",
                    )

    decisions = {item.event_id: item for item in snapshot.decisions if item.selected}
    category_queues: dict[str, deque[str]] = defaultdict(deque)
    for event_id, decision in sorted(
        decisions.items(),
        key=lambda row: (
            -row[1].relevance,
            -events.get(row[0], _zero_event()).score,
            row[0],
        ),
    ):
        if event_id in events and event_id not in selected:
            category_queues[decision.category].append(event_id)
    category_counts: dict[str, int] = defaultdict(int)
    while len(selected) < limit and any(
        category_queues[category] and category_counts[category] < CATEGORY_ROUND_ROBIN_LIMIT
        for category in CATEGORY_ORDER
    ):
        for category in CATEGORY_ORDER:
            if category_queues[category] and len(selected) < limit:
                event_id = category_queues[category].popleft()
                _append(selected, event_id, limit)
                phases[event_id] = ("category_round_robin", f"category={category}")
                category_counts[category] += 1
                if category_counts[category] >= CATEGORY_ROUND_ROBIN_LIMIT:
                    category_queues[category].clear()

    for event_id, decision in sorted(
        decisions.items(),
        key=lambda row: (
            -row[1].relevance,
            -events.get(row[0], _zero_event()).score,
            row[0],
        ),
    ):
        if len(selected) >= limit:
            break
        if event_id in events and event_id not in selected:
            selected.append(event_id)
            phases[event_id] = (
                "score_fallback",
                f"relevance={decision.relevance};score={events[event_id].score:.2f}",
            )

    ranks = {event_id: index for index, event_id in enumerate(selected, start=1)}
    audit = []
    for event in snapshot.events:
        phase, reason = phases.get(event.event_id, ("omitted", "candidate_limit"))
        audit.append(
            ShortlistAuditEntry(
                event_id=event.event_id,
                selected=event.event_id in ranks,
                phase=phase,
                reason=reason,
                rank=ranks.get(event.event_id),
            )
        )
    return PersonaShortlist(event_ids=selected, audit=audit)


def _append(selected: list[str], event_id: str, limit: int) -> None:
    if event_id not in selected and len(selected) < limit:
        selected.append(event_id)


class _ZeroEvent:
    score = 0.0


def _zero_event() -> _ZeroEvent:
    return _ZeroEvent()
