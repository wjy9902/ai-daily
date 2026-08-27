from datetime import UTC, datetime
from itertools import permutations

from ai_daily.history import HistoricalIndex
from ai_daily.models import RawItem, SourceChannel, SourceTier, SourceTimeKind
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


def test_cluster_preserves_the_time_kind_of_latest_verified_source_time() -> None:
    published = item("1", "https://example.com/a", "Qwen model release today").model_copy(
        update={"published_at": datetime(2026, 8, 12, 8, tzinfo=UTC)}
    )
    repository_update = item("2", "https://example.com/a", "Qwen model release today").model_copy(
        update={
            "published_at": datetime(2026, 8, 12, 10, tzinfo=UTC),
            "source_time_kind": SourceTimeKind.REPOSITORY_UPDATED,
        }
    )

    event = cluster_items([published, repository_update])[0]

    assert event.published_at == repository_update.published_at
    assert event.source_time_kind == SourceTimeKind.REPOSITORY_UPDATED


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


# ------------------------------------------------- cross-outlet product merge


def test_cluster_merges_cross_language_reports_sharing_a_product_identifier() -> None:
    events = cluster_items(
        [
            item("1", "https://z.ai/blog/glm", "GLM-5.3-Flash: frontier intelligence"),
            item("2", "https://qbitai.com/glm", "智谱正式发布并开源GLM-5.3-Flash"),
        ]
    )
    assert len(events) == 1
    assert len(events[0].items) == 2


def test_cluster_joins_a_bare_name_with_its_version_number() -> None:
    events = cluster_items(
        [
            item("1", "https://moonshot.cn/k3", "月之暗面正式发布 Kimi K3，总参数 2.8T"),
            item("2", "https://the-decoder.com/k3", "Moonshot AI releases Kimi K3 open weights"),
        ]
    )
    assert len(events) == 1


def test_cluster_ignores_event_year_identifiers() -> None:
    events = cluster_items(
        [
            item("1", "https://qbitai.com/wrc-a", "WRC 2026 生数科技发布世界模型路线"),
            item("2", "https://leiphone.com/wrc-b", "WRC 2026 现场：人形机器人厂商密集亮相"),
        ]
    )
    assert len(events) == 2


def test_cluster_ignores_identifiers_from_roundup_titles() -> None:
    roundup = item(
        "1",
        "https://digest.example.com/daily",
        "新模型狂潮：Claude Sonnet-4.5、DeepSeek-V3.2、GLM-4.6、Ring-1T 同日发布",
    )
    single = item("2", "https://z.ai/blog/glm46", "GLM-4.6 is now generally available")
    events = cluster_items([roundup, single])
    assert len(events) == 2


def test_title_product_identifiers_normalize_punctuation_variants() -> None:
    from ai_daily.normalize import title_product_identifiers

    spaced = title_product_identifiers("Google announces Gemini 3.5 for speech")
    hyphenated = title_product_identifiers("gemini-3.5 hands-on")
    assert spaced & hyphenated
    assert not title_product_identifiers("K3")  # a bare short id alone proves nothing


def test_same_script_titles_need_agreement_beyond_the_product_name() -> None:
    # Same model, two different English stories: a partner rollout and a
    # platform integration share only the model name and must stay apart.
    events = cluster_items(
        [
            item("1", "https://openai.com/replit", "Replit expands creation with GPT-5.6 Luna"),
            item("2", "https://aws.amazon.com/br", "Cross-Region inference for GPT-5.6 on Bedrock"),
        ]
    )
    assert len(events) == 2


def test_a_bare_link_title_merges_with_the_story_it_names() -> None:
    events = cluster_items(
        [
            item("1", "https://news.ycombinator.com/item?id=1", "GLM-5.3-Flash"),
            item("2", "https://testingcatalog.com/glm", "Z.ai launches GLM-5.3-Flash under MIT"),
        ]
    )
    assert len(events) == 1
