from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import HttpUrl

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
from ai_daily.publication import (
    BriefCard,
    Claim,
    DailyPublication,
    PublicationLevel,
    SourceRef,
    StoryCard,
    Viewpoint,
)
from ai_daily.render import (
    ArchiveEntry,
    render_archive,
    render_daily,
    render_fallback,
    render_index,
    render_rss,
)

DEFAULT_SITE_BASE_URL = "https://daily.jiayutool.cn"
REPO_ROOT = Path(__file__).resolve().parent.parent


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
    corroborating = [
        raw.model_copy(
            update={
                "source": extra["source"],
                "source_label": extra["source_label"],
                "source_item_id": f"{story['id']}-{index}",
                "url": HttpUrl(extra["url"]),
                "title": extra["title"],
            }
        )
        for index, extra in enumerate(story.get("corroboration", []), start=2)
    ]
    return Event(
        event_id=story["id"],
        canonical_url=raw.url,
        title=raw.title,
        summary=raw.summary,
        published_at=raw.published_at,
        items=[raw, *corroborating],
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
    quote = story["summary"][:500]
    return DraftItem.model_validate(
        {
            "event_id": story["id"],
            "tldr": story["tldr"],
            "tldr_evidence_id": evidence_id,
            "tldr_quote": quote,
            "facts": [
                {"text": fact, "evidence_id": evidence_id, "quote": quote}
                for fact in story["facts"]
            ],
            "why_it_matters": story["why_it_matters"],
            "action": story.get("action"),
            "caveat": story.get("caveat"),
            "evidence_ids": [evidence_id],
        }
    )


def _source_ref(story: dict[str, Any]) -> SourceRef:
    return SourceRef(
        title=story["title"],
        url=story["url"],
        source=story["source_label"],
        published_at=datetime.fromisoformat(story["published_at"]),
    )


def _story_card(story: dict[str, Any], source: SourceRef) -> StoryCard:
    evidence_id = f"{story['id']}-1"
    quote = story["summary"][:500]
    return StoryCard(
        event_id=story["id"],
        tier=story["tier"],
        category=story["category"],
        headline=story["headline"],
        tldr=story["tldr"],
        facts=[Claim(text=fact, evidence_id=evidence_id, quote=quote) for fact in story["facts"]],
        why_it_matters=story["why_it_matters"],
        action=story.get("action"),
        caveat=story.get("caveat"),
        sources=[source],
        published_at=datetime.fromisoformat(story["published_at"]),
    )


def _brief_card(story: dict[str, Any], source: SourceRef) -> BriefCard:
    return BriefCard(
        event_id=story["id"],
        category=story["category"],
        headline=story["headline"],
        brief=story["brief"],
        sources=[source],
        published_at=datetime.fromisoformat(story["published_at"]),
    )


def _publication(value: dict[str, Any]) -> DailyPublication:
    sources = {f"{story['id']}-1": _source_ref(story) for story in value["stories"]}
    details: list[StoryCard] = []
    briefs: list[BriefCard] = []
    for story in value["stories"]:
        source = sources[f"{story['id']}-1"]
        if story["tier"] == "brief":
            briefs.append(_brief_card(story, source))
        else:
            details.append(_story_card(story, source))
    viewpoints = [
        Viewpoint(
            text=insight["text"],
            sources=[sources[evidence_id] for evidence_id in insight["evidence_ids"]],
        )
        for insight in value["insights"]
    ]
    return DailyPublication(
        target_date=date.fromisoformat(value["target_date"]),
        level=PublicationLevel.L0,
        generated_at=datetime.now(UTC),
        highlight=value["today_highlight"],
        details=details,
        briefs=briefs,
        viewpoints=viewpoints,
    ).signed()


def _archive_entry(publication: DailyPublication) -> ArchiveEntry:
    leading = publication.details or publication.briefs
    if not leading:
        raise ValueError("fixture publication has no content to headline the archive")
    return ArchiveEntry(
        target_date=publication.target_date,
        level=publication.level,
        headline=leading[0].headline,
        story_count=publication.story_count,
        published=True,
    )


def _write_site(publication: DailyPublication, output: Path, site_base_url: str) -> None:
    archive = [_archive_entry(publication)]
    daily_dir = output / "daily" / publication.target_date.isoformat()
    daily_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = output / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    pages = {
        output / "index.html": render_index(publication, archive, site_base_url),
        output / "archive.html": render_archive(archive, site_base_url),
        output / "fallback.html": render_fallback(site_base_url),
        output / "rss.xml": render_rss([publication], site_base_url),
        daily_dir / "index.html": render_daily(publication, site_base_url),
    }
    for path, body in pages.items():
        path.write_text(body, encoding="utf-8")
    shutil.copyfile(REPO_ROOT / "static" / "site.css", assets_dir / "site.css")
    print(f"site preview written to {output / 'index.html'}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="write the markdown digest to this file")
    parser.add_argument("--site", type=Path, help="write a full static site preview here")
    parser.add_argument("--site-base-url", default=DEFAULT_SITE_BASE_URL)
    args = parser.parse_args()
    if args.output is None and args.site is None:
        parser.error("pass --output, --site, or both")
    value = json.loads(args.fixture.read_text(encoding="utf-8"))
    if args.output is not None:
        plan, drafts, events = _objects(value)
        validate_editorial_plan(plan, events, load_config(Path("config")).pipeline)
        body = assemble_markdown(date.fromisoformat(value["target_date"]), plan, drafts, events)
        args.output.write_text(body, encoding="utf-8")
    if args.site is not None:
        _write_site(_publication(value), args.site, args.site_base_url)


if __name__ == "__main__":
    main()
