from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ai_daily.assembler import assemble_markdown
from ai_daily.config import load_config
from ai_daily.content import validate_editorial_plan
from ai_daily.models import (
    DraftItem,
    EditorialInsight,
    EditorialPlan,
    EditorialSelection,
    Event,
    RawItem,
)


def _objects(value: dict[str, Any]) -> tuple[EditorialPlan, list[DraftItem], list[Event]]:
    events: list[Event] = []
    selections: list[EditorialSelection] = []
    drafts: list[DraftItem] = []
    for story in value["stories"]:
        evidence_id = f"{story['id']}-1"
        events.append(_event(story))
        selections.append(_selection(story, evidence_id))
        if story["tier"] != "brief":
            drafts.append(_draft(story, evidence_id))
    plan = EditorialPlan(
        today_highlight=value["today_highlight"],
        selections=selections,
        editor_viewpoint=[EditorialInsight.model_validate(item) for item in value["insights"]],
    )
    return plan, drafts, events


def _event(story: dict[str, Any]) -> Event:
    raw = RawItem.model_validate(
        {
            "source": story["source"],
            "source_label": story["source_label"],
            "source_tier": story["source_tier"],
            "source_channel": story["source_channel"],
            "source_region": story.get("source_region", "global"),
            "source_item_id": story["id"],
            "url": story["url"],
            "title": story["title"],
            "summary": story["summary"],
            "published_at": story["published_at"],
            "discovered_at": datetime.now(UTC),
        }
    )
    return Event(
        event_id=story["id"],
        canonical_url=raw.url,
        title=raw.title,
        summary=raw.summary,
        published_at=raw.published_at,
        items=[raw],
        score=story["importance"],
    )


def _selection(story: dict[str, Any], evidence_id: str) -> EditorialSelection:
    return EditorialSelection.model_validate(
        {
            "event_id": story["id"],
            "tier": story["tier"],
            "category": story["category"],
            "headline": story["headline"],
            "brief": story["brief"],
            "importance": story["importance"],
            "confidence": story["confidence"],
            "reason": story["reason"],
            "evidence_ids": [evidence_id],
        }
    )


def _draft(story: dict[str, Any], evidence_id: str) -> DraftItem:
    return DraftItem.model_validate(
        {
            "event_id": story["id"],
            "tldr": story["tldr"],
            "facts": story["facts"],
            "why_it_matters": story["why_it_matters"],
            "action": story.get("action"),
            "caveat": story.get("caveat"),
            "evidence_ids": [evidence_id],
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = json.loads(args.fixture.read_text(encoding="utf-8"))
    plan, drafts, events = _objects(value)
    validate_editorial_plan(plan, events, load_config(Path("config")).pipeline)
    body = assemble_markdown(date.fromisoformat(value["target_date"]), plan, drafts, events)
    args.output.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    main()
