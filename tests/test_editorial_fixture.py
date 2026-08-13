import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from ai_daily.models import RawItem, SourceChannel, SourceTier
from ai_daily.normalize import (
    canonicalize_url,
    cluster_items,
    is_ai_related,
    score_events,
    select_candidate_pool,
)


def _candidate(story: dict[str, object]) -> RawItem:
    return RawItem.model_validate(
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
            "discovered_at": "2026-08-13T04:20:00+08:00",
        }
    )


def _paper(index: int) -> RawItem:
    return RawItem(
        source="arxiv-ai",
        source_label="arXiv AI",
        source_tier=SourceTier.C,
        source_channel=SourceChannel.RESEARCH,
        source_item_id=f"paper-{index}",
        url=f"https://arxiv.org/abs/2608.{index:05d}",
        title=f"Incremental benchmark paper number {index}",
        summary="A routine benchmark paper with limited immediate product impact.",
        published_at=datetime(2026, 8, 12, tzinfo=UTC),
        discovered_at=datetime(2026, 8, 13, tzinfo=UTC),
    )


def test_editorial_preview_covers_major_news_without_paper_overload(tmp_path: Path) -> None:
    fixture = Path("tests/fixtures/editorial-preview.json")
    value = json.loads(fixture.read_text())
    stories = value["stories"]
    headlines = "\n".join(item["headline"] for item in stories)

    assert sum(item["tier"] == "lead" for item in stories) == 4
    assert sum(item["tier"] == "follow" for item in stories) == 5
    assert sum(item["tier"] == "brief" for item in stories) == 8
    assert all(term in headlines for term in ("Grok 4.6", "Linux", "Manus", "Pydantic AI"))
    assert sum(item["category"] == "前沿研究" for item in stories[:9]) <= 2

    output = tmp_path / "preview.md"
    subprocess.run(
        [
            sys.executable,
            "scripts/render_fixture.py",
            "--fixture",
            str(fixture),
            "--output",
            str(output),
        ],
        check=True,
    )
    body = output.read_text()
    assert len(body.encode("utf-8")) < 25_000
    assert body.count('<li id="story-') == 8


def test_prefilter_keeps_all_golden_news_ahead_of_research_noise() -> None:
    value = json.loads(Path("tests/fixtures/editorial-preview.json").read_text())
    golden = [_candidate(story) for story in value["stories"]]
    inputs = [*golden, *[_paper(index) for index in range(90)]]

    scored = score_events(
        cluster_items([item for item in inputs if is_ai_related(item)]),
        datetime(2026, 8, 13, tzinfo=UTC),
    )
    candidates = select_candidate_pool(scored, 80, research_limit=16, release_limit=12)

    candidate_urls = {str(event.canonical_url) for event in candidates}
    assert {canonicalize_url(str(item.url)) for item in golden} <= candidate_urls
