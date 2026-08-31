from datetime import UTC, date, datetime
from itertools import permutations

from ai_daily.history import HistoricalIndex, HistoricalStory
from ai_daily.models import Event, RawItem, SourceChannel, SourceTier, SourceTimeKind
from ai_daily.normalize import (
    _channel_score,
    _corroboration,
    _is_actionable,
    canonicalize_url,
    cluster_items,
    is_ai_related,
    remove_historical,
)


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


def test_history_filter_matches_a_cross_language_report_from_another_outlet() -> None:
    event = cluster_items(
        [
            item(
                "court-followup",
                "https://techcrunch.com/anthropic-court-win",
                "Anthropic gets its first court win over the Pentagon's supply-chain risk label",
            ).model_copy(
                update={
                    "summary": (
                        "A federal judge ruled the Trump administration illegally labeled "
                        "Anthropic a supply-chain risk."
                    )
                }
            )
        ]
    )[0]
    history = HistoricalIndex(
        urls=set(),
        titles={"美法院裁定五角大楼将 Anthropic 列为供应链风险违法"},
        stories=(
            HistoricalStory(
                event_id="previous-court-story",
                issue_date=date(2026, 8, 29),
                texts=(
                    "Trump blacklisting of Anthropic deemed illegal by federal judge",
                    (
                        "The Trump administration's blacklisting of Anthropic was illegal, "
                        "a federal judge ruled."
                    ),
                ),
            ),
        ),
    )

    assert remove_historical([event], history) == []


def test_history_filter_keeps_a_different_story_about_the_same_company() -> None:
    event = cluster_items(
        [
            item(
                "hardware-standard",
                "https://example.com/anthropic-mhs",
                "Anthropic launches a hardware standard for AI agents",
            ).model_copy(
                update={
                    "summary": "MHS gives agents one interface for microscopes and robotic arms."
                }
            )
        ]
    )[0]
    history = HistoricalIndex(
        urls=set(),
        titles={"美法院裁定五角大楼将 Anthropic 列为供应链风险违法"},
        stories=(
            HistoricalStory(
                event_id="previous-court-story",
                issue_date=date(2026, 8, 29),
                texts=(
                    "Trump blacklisting of Anthropic deemed illegal by federal judge",
                    "A federal judge ruled the administration's Anthropic blacklist illegal.",
                ),
            ),
        ),
    )

    assert remove_historical([event], history) == [event]


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


def test_history_filter_drops_a_foreign_language_rerun_of_yesterdays_release() -> None:
    """Our headline and the outlet's title are different kinds of string.

    2026-08-31 led with Tencent's Hy4 release a day after 2026-08-30 led with
    it. The candidate came from a different write-up, so the URL index could not
    help, and symmetric token overlap scored jaccard 0.12 between "Introducing
    Hy4 Preview" and our own spec-laden Chinese headline.
    """

    event = cluster_items([item("1", "https://example.com/hy4", "Introducing Hy4 Preview")])[0]
    history = HistoricalIndex(
        urls=set(),
        titles={"腾讯发布开源旗舰 Hy4 preview：总参数 770B、激活 49B、上下文 1M"},
    )
    assert remove_historical([event], history) == []


def test_history_filter_keeps_a_followup_about_a_product_already_covered() -> None:
    """Same product, new development: the planner is told to run these."""

    event = cluster_items(
        [item("1", "https://example.com/router", "Hy4 上线 OpenRouter 与腾讯云")]
    )[0]
    history = HistoricalIndex(
        urls=set(),
        titles={"腾讯发布开源旗舰 Hy4 preview：总参数 770B、激活 49B、上下文 1M"},
    )
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


def test_a_latin_action_term_does_not_fire_inside_a_longer_word() -> None:
    """``api`` inside capital and rapid used to mark funding news as actionable."""

    assert not _is_actionable("anthropic raises $30b and the capital funds compute")
    assert not _is_actionable("meta reorganizes its lab after rapid growth")
    assert not _is_actionable("a study of therapies assisted by machine learning")
    assert not _is_actionable("freedom of speech online")


def test_action_terms_still_match_the_forms_a_headline_uses() -> None:
    for text in (
        "now available in the api",
        "openai launches gpt-5.6",
        "new models are available",
        "the endpoint is deprecated",
        "scheduled for deprecation",
        "prices drop 40%",
        "meta open sources the weights",
    ):
        assert _is_actionable(text), text


def test_cjk_action_terms_keep_matching_without_word_boundaries() -> None:
    assert _is_actionable("openai 发布新模型")
    assert _is_actionable("额度重置")
    assert not _is_actionable("某公司裁员")


def _sourced(channel: SourceChannel, url: str, name: str) -> RawItem:
    return RawItem(
        source=name,
        source_tier=SourceTier.A,
        source_item_id=name,
        url=url,
        title="t",
        discovered_at=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
        published_at=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
        source_channel=channel,
    )


def _event(*items: RawItem) -> Event:
    return Event(
        event_id="e",
        canonical_url=str(items[0].url),
        title="t",
        summary="s",
        items=list(items),
        published_at=items[0].published_at,
    )


def test_one_party_talking_about_itself_is_not_corroboration() -> None:
    """Seven OpenAI feeds used to score the same as four independent outlets."""

    event = _event(
        _sourced(SourceChannel.OFFICIAL, "https://openai.com/a", "openai-news"),
        _sourced(SourceChannel.OFFICIAL, "https://learn.chatgpt.com/a", "openai-changelog"),
        _sourced(SourceChannel.OFFICIAL, "https://x.com/OpenAI/1", "x-openai"),
        _sourced(SourceChannel.OFFICIAL, "https://x.com/OpenAIDevs/2", "x-openai-devs"),
        _sourced(SourceChannel.NEWS, "https://x.com/sama/3", "x-sama"),
    )
    assert _corroboration(event) == 0


def test_a_lone_announcement_scores_the_same_however_many_feeds_carry_it() -> None:
    """Otherwise the bonus measures how many feeds we point at a company."""

    big = _event(
        _sourced(SourceChannel.OFFICIAL, "https://anthropic.com/a", "anthropic-news"),
        _sourced(SourceChannel.OFFICIAL, "https://anthropic.com/eng/a", "anthropic-eng"),
        _sourced(SourceChannel.OFFICIAL, "https://x.com/AnthropicAI/1", "x-anthropic"),
    )
    small = _event(_sourced(SourceChannel.OFFICIAL, "https://mistral.ai/a", "mistral"))
    assert _corroboration(big) == _corroboration(small) == 0


def test_independent_publishers_still_earn_the_bonus() -> None:
    two = _event(
        _sourced(SourceChannel.OFFICIAL, "https://openai.com/a", "openai-news"),
        _sourced(SourceChannel.NEWS, "https://reuters.com/a", "reuters"),
        _sourced(SourceChannel.NEWS, "https://theverge.com/a", "verge"),
    )
    three = _event(
        _sourced(SourceChannel.NEWS, "https://reuters.com/a", "reuters"),
        _sourced(SourceChannel.NEWS, "https://theverge.com/b", "verge"),
        _sourced(SourceChannel.NEWS, "https://techcrunch.com/c", "tc"),
    )
    assert _corroboration(two) == 7
    assert _corroboration(three) == 14


def test_channel_scores_still_suppress_research_and_release_as_before() -> None:
    """The folded-in values must equal what the removed penalties produced."""

    assert _channel_score(SourceChannel.RESEARCH) == 2
    assert _channel_score(SourceChannel.RELEASE) == 14
    assert _channel_score(SourceChannel.OFFICIAL) == 30
    assert _channel_score(SourceChannel.NEWS) == 24
    assert _channel_score(SourceChannel.COMMUNITY) == 16
