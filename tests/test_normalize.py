from datetime import UTC, datetime

from ai_daily.models import RawItem, SourceTier
from ai_daily.normalize import canonicalize_url, cluster_items, remove_historical


def item(item_id: str, url: str, title: str) -> RawItem:
    return RawItem(
        source="test",
        source_tier=SourceTier.A,
        source_item_id=item_id,
        url=url,
        title=title,
        discovered_at=datetime(2026, 8, 12, tzinfo=UTC),
    )


def test_canonicalize_removes_tracking_and_fragment() -> None:
    assert (
        canonicalize_url("HTTPS://www.Example.com//post/?utm_source=x&a=1#part")
        == "https://example.com/post?a=1"
    )


def test_cluster_exact_url_and_similar_title() -> None:
    events = cluster_items(
        [
            item("1", "https://example.com/a?utm_source=x", "Qwen model release today"),
            item("2", "https://example.com/a", "Qwen model release today"),
            item("3", "https://other.com/b", "Qwen model release today announced"),
        ]
    )
    assert len(events) == 1
    assert len(events[0].items) == 3


def test_history_filter_uses_canonical_url() -> None:
    event = cluster_items([item("1", "https://example.com/a?utm_source=x", "Release")])[0]
    assert remove_historical([event], {"https://www.example.com/a"}) == []
