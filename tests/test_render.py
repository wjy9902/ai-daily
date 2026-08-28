from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ai_daily.models import EditorialTier, SourceTimeKind
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

SITE = "https://daily.jiayutool.cn"
XSS_TAG = "<script>alert(1)</script>"
XSS_ATTR = '"><img src=x onerror=y>'


def _source(title: str = "Grok 4.6 pricing", source: str = "The Decoder") -> SourceRef:
    return SourceRef(
        title=title,
        url="https://the-decoder.com/grok-4-6/",
        source=source,
        published_at=datetime(2026, 8, 12, 18, 33, tzinfo=UTC),
        time_kind=SourceTimeKind.PUBLISHED,
    )


def _story(
    event_id: str = "grok-46",
    tier: EditorialTier = EditorialTier.LEAD,
    headline: str = "Grok 4.6 进入第一梯队",
    fact: str = "综合指数 61 分。",
    quote: str = "Grok 4.6 scores 61 on the index.",
    sources: list[SourceRef] | None = None,
) -> StoryCard:
    return StoryCard(
        event_id=event_id,
        tier=tier,
        category="模型与平台",
        headline=headline,
        tldr="价格不变，能力追平第一梯队。",
        facts=[Claim(text=fact, evidence_id=f"{event_id}-1", quote=quote)],
        why_it_matters="模型路由和供应商议价都需要重新测算。",
        action="把它加入现有真实任务评测。",
        caveat="第三方基准不能代替自建评测。",
        sources=sources or [_source()],
        published_at=datetime(2026, 8, 12, 18, 33, tzinfo=UTC),
    )


def _brief(event_id: str = "manus", headline: str = "Manus 独立运营") -> BriefCard:
    return BriefCard(
        event_id=event_id,
        category="行业动态",
        headline=headline,
        brief="团队拆分为独立公司，产品线保持不变。",
        sources=[_source(title="Manus spins out", source="Manus")],
        published_at=datetime(2026, 8, 12, 9, 5, tzinfo=UTC),
    )


def _publication(
    level: PublicationLevel = PublicationLevel.L0,
    *,
    target_date: date = date(2026, 8, 13),
    details: list[StoryCard] | None = None,
    briefs: list[BriefCard] | None = None,
    notice: str | None = None,
    highlight: str = "前沿模型价格战继续下压。",
) -> DailyPublication:
    brief_only = level in (PublicationLevel.L2A, PublicationLevel.L2B)
    follow = _story("openai-linux", EditorialTier.FOLLOW, "OpenAI 推出 Linux 桌面端")
    if details is None:
        details = [] if brief_only else [_story(), follow]
    viewpoints = [] if brief_only else [Viewpoint(text="竞争转向价格与分发。", sources=[_source()])]
    return DailyPublication(
        target_date=target_date,
        level=level,
        generated_at=datetime(2026, 8, 13, 1, 30, tzinfo=UTC),
        highlight=highlight,
        details=details,
        briefs=[_brief()] if briefs is None else briefs,
        viewpoints=viewpoints,
        notice=notice,
        degradation_reasons=[] if level is PublicationLevel.L0 else ["部分详报起草失败"],
    ).signed()


@pytest.mark.parametrize(
    "level",
    [
        PublicationLevel.L0,
        PublicationLevel.L1,
        PublicationLevel.L2A,
        PublicationLevel.L2B,
    ],
)
def test_every_publishable_level_renders(level: PublicationLevel) -> None:
    page = render_daily(_publication(level), SITE)

    assert page.startswith("<!doctype html>")
    assert "快讯" in page
    assert _publication(level).marker in page


def test_full_issue_carries_every_section() -> None:
    page = render_daily(_publication(PublicationLevel.L0), SITE)

    for label in ("今日亮点", "今日重点", "值得关注", "快讯", "编辑观点", "为什么重要"):
        assert label in page
    assert 'class="notice"' not in page
    assert "来源发布：2026-08-13 02:33 北京时间" in page


def test_overview_indexes_every_item_grouped_by_category() -> None:
    """The overview is the reader's map of the day, so nothing may be missing."""

    publication = _publication(PublicationLevel.L0)
    page = render_daily(publication, SITE)

    overview = page[page.index('class="section section--overview"') : page.index("今日重点")]
    for card in publication.details:
        assert f'href="#story-{card.event_id}"' in overview
        assert card.headline in overview
    for card in publication.briefs:
        assert f'href="#story-{card.event_id}"' in overview
        assert card.headline in overview
    assert "模型与平台" in overview
    assert "行业动态" in overview


def test_overview_orders_categories_and_appends_unknown_ones() -> None:
    publication = _publication(
        PublicationLevel.L0,
        details=[_story()],
        briefs=[_brief(), _brief("rumor-1", "消息称某厂商洽谈收购")],
    )
    object.__setattr__(publication.briefs[1], "category", "前瞻与传闻")
    page = render_daily(publication.signed(), SITE)

    overview = page[page.index('class="section section--overview"') : page.index("今日重点")]
    assert overview.index("模型与平台") < overview.index("行业动态")
    assert overview.index("行业动态") < overview.index("前瞻与传闻")


def test_brief_only_issue_still_gets_an_overview() -> None:
    """A degraded issue is the one a reader most needs a map of."""

    page = render_daily(_publication(PublicationLevel.L2B), SITE)

    assert "概览" in page
    assert 'href="#story-manus"' in page


def test_overview_headlines_never_become_live_markup() -> None:
    """The overview repeats every headline, so it repeats every attack too."""

    publication = _publication(
        PublicationLevel.L0,
        details=[_story()],
        briefs=[_brief("evil", XSS_TAG)],
    )
    page = render_daily(publication.signed(), SITE)

    overview = page[page.index('class="section section--overview"') : page.index("今日重点")]
    assert "<script>" not in overview
    assert XSS_ATTR not in overview
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in overview


def test_reduced_issue_shows_the_notice_banner() -> None:
    page = render_daily(_publication(PublicationLevel.L1), SITE)

    assert 'class="notice"' in page
    assert "本期部分详报因证据不足降为快讯。" in page
    assert "部分详报起草失败" in page
    assert "今日重点" in page


@pytest.mark.parametrize("level", [PublicationLevel.L2A, PublicationLevel.L2B])
def test_brief_only_levels_drop_stories_and_viewpoints(level: PublicationLevel) -> None:
    page = render_daily(_publication(level), SITE)

    assert 'class="notice"' in page
    assert 'class="story ' not in page
    assert "编辑观点" not in page
    assert "今日重点" not in page
    assert 'class="brief-list"' in page


def test_l3_has_no_issue_page() -> None:
    publication = _publication(PublicationLevel.L3)

    with pytest.raises(ValueError, match="L3"):
        render_daily(publication, SITE)


def test_index_accepts_an_operator_notice_for_a_blocked_day() -> None:
    page = render_index(
        _publication(),
        [ArchiveEntry(date(2026, 8, 12), PublicationLevel.L0, "上一期头条", 9, True)],
        SITE,
        notice="今日采集受阻",
    )

    assert "今日采集受阻" in page
    assert "上一期头条" in page
    assert "daily/2026-08-12/index.html" in page


def test_facts_keep_their_quote_in_a_title_attribute() -> None:
    page = render_daily(_publication(), SITE)

    expected = '<li class="story-fact" title="Grok 4.6 scores 61 on the index.">'
    assert f"{expected}综合指数 61 分。</li>" in page


def test_hostile_text_never_becomes_live_markup() -> None:
    hostile = _publication(
        details=[
            _story(
                headline=XSS_TAG,
                fact=XSS_ATTR,
                quote=XSS_TAG,
                sources=[_source(title=XSS_ATTR, source=XSS_TAG)],
            )
        ],
        briefs=[_brief(headline=XSS_ATTR)],
        highlight=XSS_TAG,
    )

    for page in (
        render_daily(hostile, SITE),
        render_index(hostile, [], SITE),
        render_rss([hostile], SITE),
    ):
        assert "<script>" not in page
        assert "</script>" not in page
        assert "<img" not in page
        assert XSS_ATTR not in page
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_hostile_source_title_cannot_break_out_of_its_attribute() -> None:
    hostile = _publication(details=[_story(sources=[_source(title=XSS_ATTR)])])

    page = render_daily(hostile, SITE)

    assert 'title="&quot;&gt;&lt;img src=x onerror=y&gt;"' in page


def test_rss_guids_are_stable_across_renders() -> None:
    publication = _publication()

    first = render_rss([publication], SITE)
    second = render_rss([publication], SITE)

    assert first == second
    assert '<guid isPermaLink="true">https://daily.jiayutool.cn/daily/2026-08-13/</guid>' in first


def test_rss_is_newest_first_and_carries_the_marker() -> None:
    older = _publication(target_date=date(2026, 8, 11))
    newer = _publication(target_date=date(2026, 8, 13))

    feed = render_rss([older, newer], SITE)

    assert feed.index("2026-08-13") < feed.index("2026-08-11")
    assert f"ai-daily-marker:{newer.marker}" in feed
    assert f"AI 日报 {newer.target_date.isoformat()}" in feed
    assert feed.count("<item>") == 2


def test_rss_refuses_a_blocked_day() -> None:
    with pytest.raises(ValueError, match="L3"):
        render_rss([_publication(PublicationLevel.L3)], SITE)


def test_archive_shows_gap_days_as_missing() -> None:
    archive = [
        ArchiveEntry(date(2026, 8, 13), PublicationLevel.L0, "今天的头条", 9, True),
        ArchiveEntry(date(2026, 8, 10), PublicationLevel.L2A, "三天前的头条", 4, True),
    ]

    page = render_archive(archive, SITE)

    assert page.count("archive-row--missing") == 2
    assert page.count(">未出刊</span>") == 2
    assert page.index("2026-08-12") < page.index("2026-08-11")
    assert "今天的头条" in page
    assert "三天前的头条" in page


def test_archive_marks_an_unpublished_entry_as_missing() -> None:
    archive = [
        ArchiveEntry(date(2026, 8, 13), PublicationLevel.L0, "今天的头条", 9, True),
        ArchiveEntry(date(2026, 8, 12), PublicationLevel.L3, "", 0, False),
    ]

    page = render_archive(archive, SITE)

    assert page.count("archive-row--missing") == 1
    assert page.count(">未出刊</span>") == 1
    assert "2026-08-12" in page


def test_empty_archive_still_renders() -> None:
    page = render_archive([], SITE)

    assert "暂无往期内容。" in page


def test_fallback_needs_no_publication_data() -> None:
    page = render_fallback(SITE)

    assert page.startswith("<!doctype html>")
    assert "站点暂时没有可展示的刊期。" in page
    assert "rss.xml" in page


def test_community_submitted_time_is_never_shown_as_a_publication_time() -> None:
    community = _source()
    community = community.model_copy(update={"time_kind": SourceTimeKind.COMMUNITY_SUBMITTED})
    publication = _publication(details=[_story(sources=[community])])

    with pytest.raises(ValueError, match="community submission"):
        render_daily(publication, SITE)


def test_site_base_url_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="site base url"):
        render_fallback("javascript:alert(1)")
