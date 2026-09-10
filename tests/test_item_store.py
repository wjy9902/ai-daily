"""The item store keeps what the collector saw and merges later sightings field by field."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_daily.item_store import (
    ItemStore,
    ItemStoreError,
    merge_payload,
    merge_sightings,
)
from ai_daily.models import (
    RawItem,
    SourceChannel,
    SourceConfig,
    SourceHealth,
    SourceRegion,
    SourceTier,
    SourceTimeKind,
)

NOW = datetime(2026, 9, 10, 4, 0, tzinfo=UTC)


def _item(
    source: str = "feed",
    item_id: str = "a",
    url: str = "https://example.com/a",
    title: str = "Story A",
    summary: str = "short",
    published_at: datetime | None = NOW - timedelta(hours=2),
    discovered_at: datetime = NOW - timedelta(hours=2),
    tier: SourceTier = SourceTier.B,
    channel: SourceChannel = SourceChannel.NEWS,
    metrics: dict[str, int | float | str] | None = None,
) -> RawItem:
    return RawItem(
        source=source,
        source_tier=tier,
        source_channel=channel,
        source_item_id=item_id,
        url=url,
        title=title,
        summary=summary,
        published_at=published_at,
        discovered_at=discovered_at,
        metrics=metrics or {},
    )


def _source(
    name: str = "feed", enabled: bool = True, tier: SourceTier = SourceTier.A
) -> SourceConfig:
    return SourceConfig(
        name=name,
        display_name=f"{name} label",
        kind="rss",
        url="https://example.com/rss",
        tier=tier,
        channel=SourceChannel.OFFICIAL,
        region=SourceRegion.CHINA,
        ai_focused=False,
        enabled=enabled,
    )


@pytest.fixture
def store(tmp_path: Path) -> ItemStore:
    return ItemStore(tmp_path / "items.sqlite")


# ---------------------------------------------------------------- merge rules


def test_the_first_reported_publication_time_is_kept() -> None:
    first = _item(published_at=NOW - timedelta(hours=5))
    later = _item(published_at=NOW - timedelta(hours=1))
    assert merge_payload(first, later).published_at == first.published_at


def test_a_missing_publication_time_is_filled_in_with_its_time_kind() -> None:
    undated = _item(published_at=None)
    dated = _item(published_at=NOW - timedelta(hours=1)).model_copy(
        update={"source_time_kind": SourceTimeKind.REPOSITORY_UPDATED}
    )
    merged = merge_payload(undated, dated)
    assert merged.published_at == dated.published_at
    assert merged.source_time_kind is SourceTimeKind.REPOSITORY_UPDATED


def test_the_longer_summary_wins_regardless_of_order() -> None:
    long = _item(summary="a much longer body of text")
    short = _item(summary="teaser")
    assert merge_payload(long, short).summary == long.summary
    assert merge_payload(short, long).summary == long.summary


def test_title_and_metrics_follow_the_newest_sighting() -> None:
    old = _item(title="Typo in titel", metrics={"score": 10})
    new = _item(title="Typo in title", metrics={"score": 40})
    merged = merge_payload(old, new)
    assert merged.title == "Typo in title"
    assert merged.metrics == {"score": 40}


def test_discovered_at_is_the_sources_own_time_and_never_moves() -> None:
    """Hacker News writes the submission time there; the freshness gate reads it."""

    submitted = _item(published_at=None, discovered_at=NOW - timedelta(days=3))
    seen_again = _item(published_at=None, discovered_at=NOW)
    assert merge_payload(submitted, seen_again).discovered_at == submitted.discovered_at


# ---------------------------------------------------------------- persistence


def test_first_seen_is_immutable_and_sightings_are_counted(store: ItemStore) -> None:
    with store:
        first = store.merge_many([_item()], NOW)
        second = store.merge_many(
            [_item(summary="a longer summary this time")], NOW + timedelta(hours=3)
        )
        row = store.connection.execute(
            "SELECT first_seen, last_seen, seen_count FROM items"
        ).fetchone()
    assert (first.inserted, first.updated) == (1, 0)
    assert (second.inserted, second.updated) == (0, 1)
    assert row[0] == NOW.isoformat()
    assert row[1] == (NOW + timedelta(hours=3)).isoformat()
    assert row[2] == 2


def test_the_same_url_from_two_sources_is_two_rows(store: ItemStore) -> None:
    with store:
        store.merge_many([_item(source="x-openai"), _item(source="openai-news")], NOW)
        assert store.counts(NOW - timedelta(days=1))[0] == 2


def test_two_updates_of_one_repository_page_are_two_rows(store: ItemStore) -> None:
    """Hugging Face change-watch keeps the URL and changes the item id per update."""

    with store:
        store.merge_many(
            [
                _item(
                    source="hf",
                    item_id="deepseek-ai/V4@aaa",
                    url="https://huggingface.co/deepseek-ai/V4",
                ),
                _item(
                    source="hf",
                    item_id="deepseek-ai/V4@bbb",
                    url="https://huggingface.co/deepseek-ai/V4",
                ),
            ],
            NOW,
        )
        assert store.counts(NOW - timedelta(days=1))[0] == 2


def test_read_window_applies_the_current_config_and_drops_disabled_sources(
    store: ItemStore,
) -> None:
    stored_as_tier_b = _item(source="feed", tier=SourceTier.B)
    gone = _item(source="retired", item_id="r")
    with store:
        store.merge_many([stored_as_tier_b, gone], NOW)
        items = store.read_window(
            NOW - timedelta(hours=36),
            {
                "feed": _source("feed", tier=SourceTier.A),
                "retired": _source("retired", enabled=False),
            },
        )
    assert [item.source for item in items] == ["feed"]
    assert items[0].source_tier is SourceTier.A
    assert items[0].source_channel is SourceChannel.OFFICIAL
    assert items[0].source_label == "feed label"
    assert items[0].source_ai_focused is False
    assert items[0].metrics["first_seen"] == NOW.isoformat()


def test_read_window_uses_publication_time_and_falls_back_to_discovery_for_undated(
    store: ItemStore,
) -> None:
    cutoff = NOW - timedelta(hours=36)
    with store:
        store.merge_many(
            [
                _item(item_id="fresh", published_at=NOW - timedelta(hours=1)),
                _item(item_id="old", published_at=NOW - timedelta(hours=40)),
                _item(
                    item_id="undated-fresh",
                    published_at=None,
                    discovered_at=NOW - timedelta(hours=2),
                ),
                _item(
                    item_id="undated-old",
                    published_at=None,
                    discovered_at=NOW - timedelta(hours=50),
                ),
            ],
            NOW,
        )
        ids = sorted(item.source_item_id for item in store.read_window(cutoff, {"feed": _source()}))
    assert ids == ["fresh", "undated-fresh"]


def test_prune_drops_only_rows_that_are_both_stale_and_old(store: ItemStore) -> None:
    with store:
        store.merge_many(
            [_item(item_id="old", published_at=NOW - timedelta(days=10))], NOW - timedelta(days=9)
        )
        store.merge_many(
            [_item(item_id="seen-recently", published_at=NOW - timedelta(days=10))], NOW
        )
        store.merge_many([_item(item_id="new", published_at=NOW)], NOW - timedelta(days=9))
        pruned = store.prune(NOW)
        remaining = sorted(
            row[0] for row in store.connection.execute("SELECT source_item_id FROM items")
        )
    assert pruned == 1
    assert remaining == ["new", "seen-recently"]


def test_rounds_are_recorded_with_their_source_tally(store: ItemStore) -> None:
    health = [
        SourceHealth(source="a", tier=SourceTier.A, status="ok", item_count=3, latency_ms=10),
        SourceHealth(
            source="b", tier=SourceTier.B, status="failed", item_count=0, latency_ms=10, error="x"
        ),
        SourceHealth(source="c", tier=SourceTier.B, status="partial", item_count=1, latency_ms=10),
    ]
    with store:
        record = store.record_round("collect", NOW, NOW + timedelta(minutes=1), health)
        latest = store.latest_round()
    assert (record.ok_sources, record.failed_sources) == (2, 1)
    assert latest is not None and latest.round_id == record.round_id
    assert latest.kind == "collect"


def test_an_unopenable_store_raises_the_stores_own_error(tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory" / "items.sqlite"
    (tmp_path / "not-a-directory").write_text("file, not directory", encoding="utf-8")
    with pytest.raises(ItemStoreError, match="cannot open item store"):
        with ItemStore(blocked):
            pass


# ------------------------------------------------------------- merging live


def test_live_and_stored_sightings_merge_by_source_and_id_without_priority() -> None:
    stored = _item(
        item_id="a", summary="the full stored body", published_at=NOW - timedelta(hours=3)
    )
    live = _item(item_id="a", summary="teaser", published_at=None, title="Corrected title")
    only_stored = _item(item_id="b")
    only_live = _item(item_id="c")

    merged = {
        item.source_item_id: item
        for item in merge_sightings([live, only_live], [stored, only_stored])
    }

    assert set(merged) == {"a", "b", "c"}
    assert merged["a"].summary == "the full stored body"
    assert merged["a"].published_at == stored.published_at
    assert merged["a"].title == "Corrected title"
