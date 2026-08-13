from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from ai_daily.models import RawItem, SourceConfig, SourceHealth
from ai_daily.normalize import is_ai_related
from ai_daily.pipeline import collection_window, filter_fresh_items
from ai_daily.sources import Collector


async def probe_sources(
    sources: list[SourceConfig],
    target_date: date,
    timezone_name: str,
    window_hours: int,
) -> list[dict[str, object]]:
    """Run the production collector against each source and report where its items die.

    Every number comes from the same code the pipeline runs: the real `Collector`
    fetches, `is_ai_related` filters, and `filter_fresh_items` applies the freshness
    policy. A source that looks healthy in curl but contributes nothing shows up here
    as the stage that discards it, not as a green HTTP status.
    """
    cutoff, run_time = collection_window(target_date, timezone_name, window_hours)
    timezone = ZoneInfo(timezone_name)
    async with Collector() as collector:
        results = await asyncio.gather(*(collector.collect([source]) for source in sources))
    return [
        _source_report(source, items, health, cutoff, run_time, timezone)
        for source, (items, health) in zip(sources, results, strict=True)
    ]


def _source_report(
    source: SourceConfig,
    items: list[RawItem],
    health: list[SourceHealth],
    cutoff: datetime,
    run_time: datetime,
    timezone: ZoneInfo,
) -> dict[str, object]:
    relevant = [item for item in items if is_ai_related(item)]
    fresh, audit = filter_fresh_items(items, cutoff, run_time, timezone)
    rejections = {
        "not_ai_related": len(items) - len(relevant),
        "no_publication_time": len(cast(list[object], audit["rejected_undated"])),
        "outside_window": len(cast(list[object], audit["rejected_outside_window"])),
    }
    ranked = sorted(rejections.items(), key=lambda entry: entry[1], reverse=True)
    return {
        **_identity(source),
        **_status(health),
        "items_fetched": len(items),
        "ai_related": len(relevant),
        "with_publication_time": sum(item.published_at is not None for item in relevant),
        "in_window": len(fresh),
        "rejections": rejections,
        "top_rejection": ranked[0][0] if ranked[0][1] else None,
    }


def _identity(source: SourceConfig) -> dict[str, object]:
    return {
        "source": source.name,
        "display_name": source.display_name or source.name,
        "kind": source.kind,
        "url": str(source.url),
        "tier": source.tier.value,
        "channel": source.channel.value,
        "region": source.region.value,
    }


def _status(health: list[SourceHealth]) -> dict[str, object]:
    # `Collector.collect` skips disabled sources, so it reports no health for them.
    if not health:
        return {"status": "disabled", "latency_ms": 0, "error": None}
    return {
        "status": health[0].status,
        "latency_ms": health[0].latency_ms,
        "error": health[0].error,
    }
