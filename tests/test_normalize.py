from datetime import UTC, datetime
from itertools import permutations

from ai_daily.history import HistoricalIndex
from ai_daily.models import RawItem, SourceChannel, SourceTier
from ai_daily.normalize import canonicalize_url, cluster_items, is_ai_related, remove_historical


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
    history = HistoricalIndex(urls={"https://www.example.com/a"}, titles=set())
    assert remove_historical([event], history) == []


def test_history_filter_matches_a_rephrased_story_title() -> None:
    event = cluster_items([item("1", "https://example.com/a", "阿里正式发布全新推理模型")])[0]
    history = HistoricalIndex(urls=set(), titles={"阿里发布全新推理模型"})
    assert remove_historical([event], history) == []


def test_cluster_matches_chinese_reports_of_the_same_event() -> None:
    events = cluster_items(
        [
            item("1", "https://example.com/a", "阿里发布全新推理模型"),
            item("2", "https://other.com/b", "全新推理模型由阿里正式发布"),
        ]
    )
    assert len(events) == 1


def test_cluster_is_order_independent_for_transitive_matches() -> None:
    first = item("1", "https://example.com/a", "Alpha Beta launch")
    bridge = item("2", "https://example.com/b", "Alpha Beta Gamma launch")
    third = item("3", "https://example.com/c", "Beta Gamma launch")

    assert {len(cluster_items(list(order))) for order in permutations([first, bridge, third])} == {
        1
    }


def test_security_identifier_merges_multi_version_release_notices() -> None:
    first = item("1", "https://github.com/example/releases/v1", "Pydantic AI v1 security fix")
    second = item("2", "https://github.com/example/releases/v2", "Pydantic AI v2 security fix")
    first = first.model_copy(
        update={"source_channel": SourceChannel.RELEASE, "summary": "Fixes GHSA-abcd-1234-zzzz"}
    )
    second = second.model_copy(
        update={"source_channel": SourceChannel.RELEASE, "summary": "Fixes GHSA-abcd-1234-zzzz"}
    )
    assert len(cluster_items([first, second])) == 1


def test_unrelated_versions_from_one_release_feed_stay_separate() -> None:
    first = item("1", "https://github.com/example/releases/v1", "Framework v1.2.0")
    second = item("2", "https://github.com/example/releases/v2", "Framework v1.3.0")
    values = [
        value.model_copy(update={"source_channel": SourceChannel.RELEASE})
        for value in (first, second)
    ]
    assert len(cluster_items(values)) == 2


def test_same_model_name_does_not_merge_distinct_events_without_a_strong_identifier() -> None:
    launch = item("1", "https://example.com/launch", "GPT-5.6 API 正式发布")
    policy = item("2", "https://other.com/policy", "GPT-5.6 更新数据保留政策")
    assert len(cluster_items([launch, policy])) == 2


def test_mixed_topic_feed_requires_an_ai_signal() -> None:
    sports = item("1", "https://example.com/sports", "Championship final result").model_copy(
        update={"source_ai_focused": False}
    )
    model = item("2", "https://example.com/model", "New multimodal model launched").model_copy(
        update={"source_ai_focused": False}
    )
    assert is_ai_related(sports) is False
    assert is_ai_related(model) is True


def test_popular_community_story_reaches_the_model_even_without_known_ai_terms() -> None:
    novel_tool = item("1", "https://example.com/tool", "Mojo 1.0 is here").model_copy(
        update={
            "source_ai_focused": False,
            "source_channel": SourceChannel.COMMUNITY,
            "metrics": {"score": 250},
        }
    )
    assert is_ai_related(novel_tool) is True


def test_named_ai_products_are_kept_from_broad_feeds() -> None:
    product = item("1", "https://example.com/model", "NVIDIA unveils Nemotron 4").model_copy(
        update={"source_ai_focused": False}
    )
    assert is_ai_related(product) is True


def test_history_filter_keeps_a_new_product_version() -> None:
    event = cluster_items([item("1", "https://example.com/new", "OpenAI 发布 GPT-5.6")])[0]
    history = HistoricalIndex(urls=set(), titles={"OpenAI 发布 GPT-5.5"})
    assert remove_historical([event], history) == [event]


def test_history_filter_keeps_a_distinct_followup_for_the_same_model() -> None:
    event = cluster_items([item("1", "https://example.com/pricing", "OpenAI 调整 GPT-5 API 定价")])[
        0
    ]
    history = HistoricalIndex(urls=set(), titles={"OpenAI 发布 GPT-5"})
    assert remove_historical([event], history) == [event]
