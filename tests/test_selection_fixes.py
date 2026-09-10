"""The 2026-09-10 review of the selection stages, one test per finding.

Digests merged into whichever story they opened with; the candidate cap cut
reported news and personal tweets alike; a cluster that repeated yesterday's
story took today's follow-ups down with it; the judge saw three of a ten-item
cluster; the relevance vocabulary knew no Chinese vendor.
"""

from datetime import UTC, date, datetime

from ai_daily.content import evidence_bundle
from ai_daily.history import HistoricalIndex, HistoricalStory
from ai_daily.models import Event, RawItem, SourceChannel, SourceTier
from ai_daily.normalize import (
    PERSONAL_POST_PENALTY,
    _is_actionable,
    cluster_items,
    is_ai_related,
    is_digest,
    remove_historical,
    score_events,
)

NOW = datetime(2026, 9, 10, 0, 30, tzinfo=UTC)


def _item(
    url: str,
    title: str,
    summary: str = "",
    source: str = "test",
    channel: SourceChannel = SourceChannel.NEWS,
    tier: SourceTier = SourceTier.B,
    ai_focused: bool = True,
) -> RawItem:
    return RawItem(
        source=source,
        source_tier=tier,
        source_channel=channel,
        source_ai_focused=ai_focused,
        source_item_id=url,
        url=url,
        title=title,
        summary=summary,
        discovered_at=NOW,
        published_at=NOW,
    )


# --- 1. digests stand alone -------------------------------------------------


def test_a_digest_does_not_merge_into_the_story_it_opens_with() -> None:
    digest = _item(
        "https://www.ithome.com/1/000/567.htm",
        "IT早报 0910：苹果发首款折叠屏 iPhone Duo、iPhone 18 Pro 系列；老款 iPhone 集体涨价；"
        "曝 DeepSeek 备战科创板 IPO",
        "1. 苹果发布首款折叠屏 iPhone Duo，iPhone 18 Pro 系列同步登场。2. 老款 iPhone 集体涨价。"
        "13. DeepSeek 计划 9 月 10 日前后发布 V4.1 Flash 模型。",
    )
    apple = _item(
        "https://www.ithome.com/0/999/900.htm",
        "苹果发布首款折叠屏 iPhone Duo、iPhone 18 Pro 系列",
        "苹果今天发布了首款折叠屏 iPhone Duo 和 iPhone 18 Pro 系列。",
    )
    assert is_digest(digest)
    assert not is_digest(apple)
    assert len(cluster_items([digest, apple])) == 2


def test_digest_titles_are_recognised_by_marker_or_by_separators() -> None:
    assert is_digest(_item("https://a.example/1", "ICYMI: What landed for AI builders in August"))
    assert is_digest(_item("https://a.example/2", "A 发布新品；B 上线功能；C 宣布融资"))
    assert not is_digest(_item("https://a.example/3", "Introducing ChatGPT Images 2.5"))


def test_a_long_headline_no_longer_merges_on_its_summary_alone() -> None:
    """A short post still borrows its summary; two real headlines compare as headlines."""

    launch = _item(
        "https://openai.com/index/images",
        "Introducing ChatGPT Images 2.5",
        "ChatGPT Images 2.5 turns sketches into images faster than before.",
    )
    fish = _item(
        "https://x.com/gdb/status/1",
        "Look! A fish!",
        "Made with ChatGPT Images 2.5 in one shot, introducing my new wallpaper.",
        source="x-gdb",
    )
    report = _item(
        "https://theverge.com/apple-photos",
        "Apple has a new way to prove your iPhone photos are not AI",
        "The camera mode was shown alongside Images 2.5 comparisons; introducing ChatGPT "
        "Images 2.5 changed how fakes look, Apple said.",
    )
    assert len(cluster_items([launch, report])) == 2
    assert len(cluster_items([launch, fish])) == 2


# --- 2. scoring: personal posts and the action vocabulary -------------------


def test_a_lone_personal_x_post_scores_below_the_same_story_from_an_outlet() -> None:
    tweet = _item(
        "https://x.com/sama/status/9",
        "ocarina of time remake is the best news",
        "推文。",
        source="x-sama",
    )
    article = _item(
        "https://techcrunch.com/superintelligence",
        "Superintelligence is coming. Should we let it?",
        "报道正文。",
        source="techcrunch-ai",
    )
    scored = {
        event.primary_item.source: event.score
        for event in score_events(cluster_items([tweet, article]), NOW)
    }
    assert scored["techcrunch-ai"] - scored["x-sama"] == PERSONAL_POST_PENALTY


def test_an_official_account_or_an_outlet_in_the_cluster_lifts_the_penalty() -> None:
    tweet = _item(
        "https://x.com/sama/status/9", "GPT-6 Astra is rolling out today", source="x-sama"
    )
    official = _item(
        "https://x.com/OpenAI/status/1",
        "GPT-6 Astra is rolling out today to everyone",
        source="x-openai",
        channel=SourceChannel.OFFICIAL,
        tier=SourceTier.A,
    )
    (event,) = cluster_items([tweet, official])
    (alone,) = cluster_items([tweet])
    assert (
        score_events([event], NOW)[0].score
        > score_events([alone], NOW)[0].score + PERSONAL_POST_PENALTY
    )


def test_mentioning_a_model_or_an_api_is_no_longer_actionable_on_its_own() -> None:
    assert not _is_actionable("a new model from openai")
    assert not _is_actionable("the api and the model behind it")
    assert not _is_actionable("这个模型的评测")
    assert _is_actionable("openai 发布新模型")
    assert _is_actionable("now available in the api")
    assert _is_actionable("prices drop 40%")


# --- 3. history removes items, not clusters ---------------------------------


def _history_with(*texts: str, urls: set[str] | None = None) -> HistoricalIndex:
    return HistoricalIndex(
        urls=urls or set(),
        titles=set(),
        stories=(HistoricalStory(event_id="old", issue_date=date(2026, 9, 9), texts=texts),),
    )


def test_yesterdays_rollout_leaves_and_todays_follow_up_in_the_same_cluster_stays() -> None:
    rollout = _item(
        "https://x.com/OpenAI/status/1",
        "Astra is fully rolled out to Plus, Pro, Business, and Enterprise",
        source="x-openai",
        channel=SourceChannel.OFFICIAL,
        tier=SourceTier.A,
    )
    quantum = _item(
        "https://openai.com/index/sol-quantum",
        "How GPT-5.6 Sol helps run quantum computers",
        "Astra is fully rolled out, and Sol now steers quantum error correction.",
        source="openai-news",
        channel=SourceChannel.OFFICIAL,
        tier=SourceTier.A,
    )
    event = cluster_items([rollout])[0].model_copy(update={"items": [rollout, quantum]})
    history = _history_with(rollout.title, urls={str(rollout.url)})

    kept = remove_historical([event], history)

    assert [e.title for e in kept] == ["How GPT-5.6 Sol helps run quantum computers"]
    assert [i.source for i in kept[0].items] == ["openai-news"]


def test_a_cluster_made_only_of_yesterdays_items_is_still_dropped() -> None:
    old = _item(
        "https://openai.com/index/images", "Introducing ChatGPT Images 2.5", source="openai-news"
    )
    event = cluster_items([old])[0]
    assert remove_historical([event], _history_with(old.title, urls={str(old.url)})) == []


def test_an_exact_repeat_of_a_cited_source_title_is_historical_even_from_a_new_url() -> None:
    """09-10 08:30: the same OpenAI post came back through a blog link and scored 92."""

    rerun = _item(
        "https://simonwillison.net/images-2-5",
        "Introducing ChatGPT Images 2.5",
        source="simon-willison",
    )
    event = cluster_items([rerun])[0]
    assert remove_historical([event], _history_with("Introducing ChatGPT Images 2.5")) == []
    assert remove_historical([event], _history_with("ChatGPT Images 2.5 pricing update")) == [event]


def test_a_short_exact_title_is_not_enough_to_call_a_story_old() -> None:
    event = cluster_items([_item("https://a.example/release", "Release")])[0]
    assert remove_historical([event], _history_with("Release")) == [event]


def test_survivors_of_a_historical_cluster_are_clustered_again_on_their_own() -> None:
    old = _item(
        "https://openai.com/index/astra",
        "Astra is fully rolled out to everyone",
        source="openai-news",
    )
    voice_a = _item(
        "https://x.com/OpenAI/status/2",
        "ChatGPT Voice now supports GPT-6 Astra and GPT-5.6 Sol",
        source="x-openai",
    )
    voice_b = _item(
        "https://the-decoder.com/voice",
        "ChatGPT Voice now supports GPT-6 Astra and GPT-5.6 Sol models",
        source="the-decoder",
    )
    unrelated = _item(
        "https://x.com/sama/status/3", "i want one!", "Astra hardware is the best.", source="x-sama"
    )
    event = cluster_items([old])[0].model_copy(update={"items": [old, voice_a, voice_b, unrelated]})

    kept = remove_historical([event], _history_with(old.title, urls={str(old.url)}))

    titles = sorted(len(e.items) for e in kept)
    assert titles == [1, 2]


# --- 4. evidence shows the cluster as it is ---------------------------------


def test_evidence_takes_one_item_per_publisher_before_repeating_one() -> None:
    items = [
        _item(
            "https://x.com/OpenAI/status/1",
            "Astra rollout tweet 1",
            source="x-openai",
            channel=SourceChannel.OFFICIAL,
            tier=SourceTier.A,
        ),
        _item(
            "https://x.com/OpenAIDevs/status/2",
            "Astra rollout tweet 2",
            source="x-openai-devs",
            channel=SourceChannel.OFFICIAL,
            tier=SourceTier.A,
        ),
        _item("https://x.com/sama/status/3", "Astra rollout tweet 3", source="x-sama"),
        _item(
            "https://techcrunch.com/astra",
            "OpenAI rolls out Astra with new pricing",
            source="techcrunch-ai",
        ),
        _item("https://the-decoder.com/astra", "Astra benchmark report", source="the-decoder"),
        _item(
            "https://openai.com/index/astra",
            "Astra is fully rolled out",
            source="openai-news",
            channel=SourceChannel.OFFICIAL,
            tier=SourceTier.A,
        ),
        _item("https://x.com/gdb/status/4", "team has been cooking", source="x-gdb"),
    ]
    event = Event(
        event_id="e", canonical_url=str(items[0].url), title=items[0].title, summary="", items=items
    )

    bundle = evidence_bundle(event)

    assert [e.source for e in bundle.evidence] == [
        "x-openai",
        "techcrunch-ai",
        "the-decoder",
        "openai-news",
        "x-openai-devs",
    ]
    assert [e.evidence_id for e in bundle.evidence] == [f"e-{n}" for n in range(1, 6)]
    assert bundle.also_reported == ["Astra rollout tweet 3", "team has been cooking"]


def test_a_small_cluster_keeps_every_item_and_reports_nothing_else() -> None:
    a = _item("https://a.example/1", "Story A")
    b = _item("https://a.example/2", "Story A follow-up")
    event = Event(event_id="e", canonical_url=str(a.url), title=a.title, summary="", items=[a, b])
    bundle = evidence_bundle(event)
    assert len(bundle.evidence) == 2
    assert bundle.also_reported == []


# --- 5. the relevance vocabulary knows Chinese vendors -----------------------


def test_chinese_vendor_names_count_as_ai_signal_in_general_media() -> None:
    for title in (
        "豆包上线新的工作模式",
        "腾讯混元发布新版本",
        "百度文心一言降价",
        "阿里千问眼镜曝光",
        "英伟达财报超预期",
    ):
        assert is_ai_related(_item("https://cls.cn/x", title, ai_focused=False)), title
    assert not is_ai_related(_item("https://cls.cn/y", "苹果上调 iPhone 售价", ai_focused=False))
