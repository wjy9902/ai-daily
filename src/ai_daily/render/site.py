"""Hand-built HTML and RSS renderers for the self-hosted daily site.

There is no template engine here on purpose. Every string that originates from
a source, a model or a persisted publication field is hostile input, so it is
funnelled through :func:`_t` (or :func:`_x` for XML) at the point of
interpolation. Raw markup only ever comes from the module-level constants and
the literal fragments in this file.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import format_datetime
from html import escape
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape
from zoneinfo import ZoneInfo

from ai_daily.models import EditorialTier, SourceTimeKind
from ai_daily.publication import (
    LEVEL_NOTICE,
    BriefCard,
    Claim,
    DailyPublication,
    PublicationLevel,
    SourceRef,
    StoryCard,
    Viewpoint,
)

BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
WEEKDAYS = "一二三四五六日"

SITE_TITLE = "AI 日报"
SITE_DESCRIPTION = "每日 AI 要闻，证据优先，每条都可回溯到一手来源。"
FOOTER_NOTE = "本刊由自动化编辑流水线整理；事实与数据请以链接中的原始来源为准。"

LEAD_TITLE = "今日重点"
FOLLOW_TITLE = "值得关注"
BRIEF_TITLE = "快讯"
VIEWPOINT_TITLE = "编辑观点"
OVERVIEW_TITLE = "概览"
ARCHIVE_TITLE = "往期归档"
RECENT_TITLE = "近期刊期"
MISSING_LABEL = "未出刊"

#: Overview grouping order. Categories are listed in the order a reader scans
#: for them rather than alphabetically, and any category not named here is
#: appended in first-appearance order so a new one cannot vanish from the list.
OVERVIEW_CATEGORY_ORDER = (
    "模型与平台",
    "国内 AI",
    "值得试的项目",
    "前沿研究",
    "行业动态",
    "前瞻与传闻",
)

PUBLISHED_TIME_LABEL = "来源发布"
UPDATED_TIME_LABEL = "来源更新"

LEVEL_LABEL: dict[PublicationLevel, str] = {
    PublicationLevel.L0: "完整刊期",
    PublicationLevel.L1: "部分降级",
    PublicationLevel.L2A: "快讯模式",
    PublicationLevel.L2B: "自动快讯",
    PublicationLevel.L3: MISSING_LABEL,
}

BRIEF_ONLY_LEVELS = frozenset({PublicationLevel.L2A, PublicationLevel.L2B})

_ROOT_PREFIX = ""
_DAILY_PREFIX = "../../"


@dataclass(frozen=True)
class ArchiveEntry:
    """One row of the archive index, as recorded by the publisher."""

    target_date: date
    level: PublicationLevel
    headline: str
    story_count: int
    published: bool


# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------


def _t(value: str) -> str:
    """Escape any untrusted text for use in HTML text or an attribute value."""

    return escape(value, quote=True)


def _url(value: str) -> str:
    """Escape a link, rejecting anything that is not an http(s) URL."""

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("only http and https links may be rendered")
    return escape(value, quote=True)


def _x(value: str) -> str:
    """Escape untrusted text for XML character data."""

    return xml_escape(value)


def _site_base(site_base_url: str) -> str:
    """Validate and normalise the site root, e.g. ``https://daily.example``."""

    base = site_base_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("site base url must be an absolute http or https url")
    return base


def _daily_url(base: str, target_date: date) -> str:
    return f"{base}/daily/{target_date.isoformat()}/"


def _daily_href(prefix: str, target_date: date) -> str:
    """A relative link to an issue page that also works from ``file://``."""

    return f"{prefix}daily/{target_date.isoformat()}/index.html"


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------


def _beijing(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("publication time must carry a timezone")
    return value.astimezone(BEIJING_TIMEZONE)


def _time_label(sources: Sequence[SourceRef]) -> str:
    """The label for a story's publication time.

    A community submission time is when somebody posted a link, not when the
    thing happened, so it is never presented as a publication time.
    """

    for ref in sources:
        if ref.time_kind is SourceTimeKind.REPOSITORY_UPDATED:
            return UPDATED_TIME_LABEL
        if ref.time_kind is SourceTimeKind.PUBLISHED:
            return PUBLISHED_TIME_LABEL
    raise ValueError("community submission time cannot be used as publication time")


def _time_html(published_at: datetime, sources: Sequence[SourceRef]) -> str:
    label = _time_label(sources)
    moment = _beijing(published_at)
    shown = f"{label}：{moment.strftime('%Y-%m-%d %H:%M')} 北京时间"
    return f'<time datetime="{_t(moment.isoformat())}">{_t(shown)}</time>'


def _weekday(target_date: date) -> str:
    return f"周{WEEKDAYS[target_date.weekday()]}"


# --------------------------------------------------------------------------
# shared fragments
# --------------------------------------------------------------------------


def _summary(value: str, limit: int = 160) -> str:
    single_line = " ".join(value.split())
    if len(single_line) <= limit:
        return single_line
    return f"{single_line[:limit]}…"


def _header(prefix: str) -> list[str]:
    return [
        '<header class="site-header">',
        f'<a class="site-title" href="{_t(prefix)}index.html">{_t(SITE_TITLE)}</a>',
        '<nav class="site-nav">',
        f'<a href="{_t(prefix)}index.html">今日</a>',
        f'<a href="{_t(prefix)}archive.html">归档</a>',
        f'<a href="{_t(prefix)}papers/index.html">论文</a>',
        f'<a href="{_t(prefix)}rss.xml">RSS</a>',
        "</nav>",
        "</header>",
    ]


def _footer(base: str) -> list[str]:
    home = _url(f"{base}/")
    return [
        '<footer class="site-footer">',
        f"<p>{_t(FOOTER_NOTE)}</p>",
        f'<p class="site-footer__home"><a href="{home}">{_t(base)}</a></p>',
        "</footer>",
    ]


def _page(
    *,
    title: str,
    description: str,
    canonical: str,
    prefix: str,
    base: str,
    body: str,
) -> str:
    rss_href = f"{_t(prefix)}rss.xml"
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-Hans">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{_t(title)}</title>",
            f'<meta name="description" content="{_t(description)}">',
            f'<link rel="canonical" href="{_url(canonical)}">',
            f'<link rel="stylesheet" href="{_t(prefix)}assets/site.css">',
            f'<link rel="alternate" type="application/rss+xml" '
            f'title="{_t(SITE_TITLE)}" href="{rss_href}">',
            "</head>",
            "<body>",
            *_header(prefix),
            '<main class="site-main">',
            body,
            "</main>",
            *_footer(base),
            "</body>",
            "</html>",
            "",
        ]
    )


def _notice_banner(text: str, reasons: Sequence[str]) -> list[str]:
    lines = [
        '<aside class="notice" role="note">',
        f'<p class="notice__text">{_t(text)}</p>',
    ]
    if reasons:
        lines.append('<ul class="notice__reasons">')
        lines.extend(f"<li>{_t(reason)}</li>" for reason in reasons)
        lines.append("</ul>")
    lines.append("</aside>")
    return lines


def _source_links(sources: Sequence[SourceRef]) -> str:
    unique = list(dict.fromkeys((ref.source, ref.title, str(ref.url)) for ref in sources))
    totals = Counter(source for source, _, _ in unique)
    seen: Counter[str] = Counter()
    links: list[str] = []
    for source, title, url in unique:
        seen[source] += 1
        label = source if totals[source] == 1 else f"{source} {seen[source]}/{totals[source]}"
        links.append(
            f'<a class="source-link" href="{_url(url)}" title="{_t(title)}" '
            f'rel="nofollow noopener" target="_blank">{_t(label)}</a>'
        )
    return '<span class="source-sep"> · </span>'.join(links)


def _fact_item(claim: Claim) -> str:
    return f'<li class="story-fact" title="{_t(claim.quote)}">{_t(claim.text)}</li>'


def _story_card(card: StoryCard) -> list[str]:
    tier = "lead" if card.tier is EditorialTier.LEAD else "follow"
    anchor = _t(f"story-{card.event_id}")
    lines = [
        f'<article class="story story--{tier}" id="{anchor}">',
        f'<h3 class="story-headline">{_t(card.headline)}</h3>',
        f'<p class="story-tldr"><strong>TL;DR：</strong>{_t(card.tldr)}</p>',
        '<div class="story-facts">',
        '<p class="story-label">核心事实</p>',
        "<ul>",
        *[_fact_item(claim) for claim in card.facts],
        "</ul>",
        "</div>",
        f'<p class="story-importance"><strong>为什么重要：</strong>{_t(card.why_it_matters)}</p>',
    ]
    if card.action:
        lines.append(f'<p class="story-action"><strong>可以怎么用：</strong>{_t(card.action)}</p>')
    if card.caveat:
        lines.append(f'<p class="story-caveat"><strong>局限/争议：</strong>{_t(card.caveat)}</p>')
    lines.extend(
        [
            f'<p class="story-sources"><strong>来源：</strong>{_source_links(card.sources)}</p>',
            f'<p class="story-meta">{_time_html(card.published_at, card.sources)}'
            f'<span class="story-sep"> · </span>'
            f'<span class="story-category">{_t(card.category)}</span></p>',
            "</article>",
        ]
    )
    return lines


def _brief_item(card: BriefCard) -> list[str]:
    anchor = _t(f"story-{card.event_id}")
    return [
        f'<li class="brief" id="{anchor}">',
        f'<p class="brief-headline">{_t(card.headline)}'
        f'<span class="brief-category">{_t(card.category)}</span></p>',
        f'<p class="brief-text">{_t(card.brief)}</p>',
        f'<p class="brief-meta">{_source_links(card.sources)}'
        f'<span class="brief-sep"> · </span>'
        f"{_time_html(card.published_at, card.sources)}</p>",
        "</li>",
    ]


def _viewpoint_item(viewpoint: Viewpoint) -> list[str]:
    lines = [
        '<li class="viewpoint">',
        f'<p class="viewpoint-text">{_t(viewpoint.text)}</p>',
    ]
    if viewpoint.sources:
        lines.append(f'<p class="viewpoint-sources">{_source_links(viewpoint.sources)}</p>')
    lines.append("</li>")
    return lines


def _overview_section(publication: DailyPublication) -> list[str]:
    """A grouped index of every headline in the issue, linked to its story.

    The issue is organised by importance, which answers "what matters most"
    but not "did today have anything about X". The benchmark digest opens with
    exactly this list and it is the fastest way to see the shape of a day, so
    it is built here from the cards themselves rather than asked of the model.
    """

    entries: list[tuple[str, str, str]] = [
        (card.category, card.event_id, card.headline) for card in publication.details
    ]
    entries.extend(
        (str(card.category), card.event_id, card.headline) for card in publication.briefs
    )
    if not entries:
        return []

    grouped: dict[str, list[tuple[str, str]]] = {}
    for category, event_id, headline in entries:
        grouped.setdefault(category, []).append((event_id, headline))

    ordered = [name for name in OVERVIEW_CATEGORY_ORDER if name in grouped]
    ordered.extend(name for name in grouped if name not in ordered)

    body: list[str] = []
    for category in ordered:
        body.append('<div class="overview-group">')
        body.append(f'<p class="overview-category">{_t(category)}</p>')
        body.append('<ul class="overview-list">')
        for event_id, headline in grouped[category]:
            anchor = _t(f"story-{event_id}")
            body.append(f'<li class="overview-item"><a href="#{anchor}">{_t(headline)}</a></li>')
        body.append("</ul>")
        body.append("</div>")
    return _section(OVERVIEW_TITLE, "overview", body)


def _section(title: str, modifier: str, body: Iterable[str]) -> list[str]:
    return [
        f'<section class="section section--{modifier}">',
        f'<h2 class="section-title">{_t(title)}</h2>',
        *body,
        "</section>",
    ]


def _issue_notice(publication: DailyPublication) -> list[str]:
    text = publication.notice or LEVEL_NOTICE[publication.level]
    if text is None:
        return []
    return _notice_banner(text, publication.degradation_reasons)


def _issue(publication: DailyPublication, *, prefix: str, permalink: bool) -> list[str]:
    """The issue article, shared by the daily page and the home page."""

    level = publication.level
    target_date = publication.target_date
    lines = [
        f"<!-- ai-daily-publication:{_t(target_date.isoformat())}:"
        f"sha256={_t(publication.marker)} -->",
        f'<article class="issue issue--{_t(level.value.lower())}" '
        f'id="issue-{_t(target_date.isoformat())}">',
        '<div class="issue-head">',
        f'<p class="issue-kicker">{_t(SITE_TITLE)}'
        f'<span class="issue-sep"> · </span>'
        f'<time datetime="{_t(target_date.isoformat())}">{_t(target_date.isoformat())}</time>'
        f'<span class="issue-sep"> · </span>{_t(_weekday(target_date))}</p>',
        f'<p class="issue-level issue-level--{_t(level.value.lower())}">'
        f"{_t(LEVEL_LABEL[level])}</p>",
        "</div>",
    ]
    lines.extend(_issue_notice(publication))
    if publication.highlight:
        lines.append(
            f'<p class="issue-highlight"><strong>今日亮点：</strong>{_t(publication.highlight)}</p>'
        )
    lines.extend(_overview_section(publication))
    if level in BRIEF_ONLY_LEVELS:
        lines.extend(_brief_section(publication.briefs))
    else:
        lines.extend(_full_sections(publication))
    if permalink:
        lines.append(
            f'<p class="issue-permalink">'
            f'<a href="{_t(_daily_href(prefix, target_date))}">阅读本期完整页面</a></p>'
        )
    lines.append("</article>")
    return lines


def _full_sections(publication: DailyPublication) -> list[str]:
    leads = [card for card in publication.details if card.tier is EditorialTier.LEAD]
    follows = [card for card in publication.details if card.tier is not EditorialTier.LEAD]
    lines: list[str] = []
    if leads:
        body: list[str] = []
        for card in leads:
            body.extend(_story_card(card))
        lines.extend(_section(LEAD_TITLE, "lead", body))
    if follows:
        body = []
        for card in follows:
            body.extend(_story_card(card))
        lines.extend(_section(FOLLOW_TITLE, "follow", body))
    lines.extend(_brief_section(publication.briefs))
    if publication.viewpoints:
        body = ['<ul class="viewpoint-list">']
        for viewpoint in publication.viewpoints:
            body.extend(_viewpoint_item(viewpoint))
        body.append("</ul>")
        lines.extend(_section(VIEWPOINT_TITLE, "viewpoint", body))
    return lines


def _brief_section(briefs: Sequence[BriefCard]) -> list[str]:
    if not briefs:
        return []
    body = ['<ol class="brief-list">']
    for card in briefs:
        body.extend(_brief_item(card))
    body.append("</ol>")
    return _section(BRIEF_TITLE, "brief", body)


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------


def render_daily(publication: DailyPublication, site_base_url: str) -> str:
    """The page for a single issue, served at ``/daily/<date>/``.

    L3 means there is no issue: the site keeps the previous release, so asking
    for an L3 page is a bug rather than something to paper over.
    """

    base = _site_base(site_base_url)
    body = render_issue(publication)
    description = publication.highlight or SITE_DESCRIPTION
    return _page(
        title=f"{SITE_TITLE} {publication.target_date.isoformat()}",
        description=_summary(description),
        canonical=_daily_url(base, publication.target_date),
        prefix=_DAILY_PREFIX,
        base=base,
        body=body,
    )


def render_issue(publication: DailyPublication) -> str:
    """Render the original issue body shared by the website and WeChat draft."""

    if publication.level is PublicationLevel.L3:
        raise ValueError("L3 has no issue page; keep the previous release instead")
    return "\n".join(_issue(publication, prefix=_DAILY_PREFIX, permalink=False))


def render_index(
    latest: DailyPublication,
    archive: Sequence[ArchiveEntry],
    site_base_url: str,
    notice: str | None = None,
) -> str:
    """The home page: the latest issue inline plus links to recent issues.

    ``notice`` is the operator's override, used on an L3 day to explain that
    the page below is the previous release.
    """

    base = _site_base(site_base_url)
    lines: list[str] = []
    if notice:
        lines.extend(_notice_banner(notice, ()))
    lines.extend(_issue(latest, prefix=_ROOT_PREFIX, permalink=True))
    lines.extend(_recent_section(archive, latest.target_date))
    description = latest.highlight or SITE_DESCRIPTION
    return _page(
        title=SITE_TITLE,
        description=_summary(description),
        canonical=f"{base}/",
        prefix=_ROOT_PREFIX,
        base=base,
        body="\n".join(lines),
    )


def _recent_section(archive: Sequence[ArchiveEntry], exclude: date) -> list[str]:
    entries = [
        entry
        for entry in sorted(archive, key=lambda item: item.target_date, reverse=True)
        if entry.published and entry.target_date != exclude
    ][:10]
    if not entries:
        return []
    body = ['<ul class="recent-list">']
    for entry in entries:
        body.extend(
            [
                '<li class="recent">',
                f'<a href="{_t(_daily_href(_ROOT_PREFIX, entry.target_date))}">'
                f'<time datetime="{_t(entry.target_date.isoformat())}" class="recent-date">'
                f"{_t(entry.target_date.isoformat())}</time>"
                f'<span class="recent-headline">{_t(entry.headline)}</span></a>',
                "</li>",
            ]
        )
    body.append("</ul>")
    body.append(f'<p class="recent-more"><a href="archive.html">{_t(ARCHIVE_TITLE)}</a></p>')
    return _section(RECENT_TITLE, "recent", body)


def render_archive(archive: Sequence[ArchiveEntry], site_base_url: str) -> str:
    """The full archive, with every day between the first and last issue.

    A day without an issue is shown as 未出刊 rather than silently skipped, so
    the gaps in the record stay visible.
    """

    base = _site_base(site_base_url)
    body = "\n".join(_archive_body(archive))
    return _page(
        title=f"{ARCHIVE_TITLE} · {SITE_TITLE}",
        description=_summary(f"{SITE_TITLE}{ARCHIVE_TITLE}，含未出刊日期。"),
        canonical=f"{base}/archive.html",
        prefix=_ROOT_PREFIX,
        base=base,
        body=body,
    )


def _archive_body(archive: Sequence[ArchiveEntry]) -> list[str]:
    lines = [
        '<article class="archive">',
        f'<h1 class="page-title">{_t(ARCHIVE_TITLE)}</h1>',
    ]
    published = {entry.target_date: entry for entry in archive if entry.published}
    if not archive:
        lines.extend([f'<p class="archive-empty">{_t("暂无往期内容。")}</p>', "</article>"])
        return lines
    newest = max(entry.target_date for entry in archive)
    oldest = min(entry.target_date for entry in archive)
    lines.append('<ol class="archive-list">')
    current = newest
    while current >= oldest:
        entry = published.get(current)
        lines.extend(_archive_row(current, entry))
        current -= timedelta(days=1)
    lines.extend(["</ol>", "</article>"])
    return lines


def _archive_row(target_date: date, entry: ArchiveEntry | None) -> list[str]:
    stamp = (
        f'<time datetime="{_t(target_date.isoformat())}" class="archive-date">'
        f"{_t(target_date.isoformat())}</time>"
        f'<span class="archive-weekday">{_t(_weekday(target_date))}</span>'
    )
    if entry is None:
        return [
            '<li class="archive-row archive-row--missing">',
            stamp,
            f'<span class="archive-missing">{_t(MISSING_LABEL)}</span>',
            "</li>",
        ]
    return [
        '<li class="archive-row">',
        f'<a class="archive-link" href="{_t(_daily_href(_ROOT_PREFIX, entry.target_date))}">',
        stamp,
        f'<span class="archive-headline">{_t(entry.headline)}</span>',
        "</a>",
        f'<span class="archive-meta">'
        f'<span class="archive-level archive-level--{_t(entry.level.value.lower())}">'
        f"{_t(LEVEL_LABEL[entry.level])}</span>"
        f'<span class="archive-count">{_t(str(entry.story_count))} 条</span></span>',
        "</li>",
    ]


def render_fallback(site_base_url: str) -> str:
    """The page shown when nothing has ever been published, or a render failed.

    It deliberately reads no publication data so it can always be produced.
    """

    base = _site_base(site_base_url)
    body = "\n".join(
        [
            '<article class="fallback">',
            f'<h1 class="page-title">{_t(SITE_TITLE)}</h1>',
            *_notice_banner("站点暂时没有可展示的刊期。流水线恢复后会自动补上。", ()),
            f'<p class="fallback-text">{_t(SITE_DESCRIPTION)}</p>',
            f'<p class="fallback-links"><a href="archive.html">{_t(ARCHIVE_TITLE)}</a>'
            f'<span class="fallback-sep"> · </span><a href="rss.xml">RSS</a></p>',
            "</article>",
        ]
    )
    return _page(
        title=SITE_TITLE,
        description=_summary(SITE_DESCRIPTION),
        canonical=f"{base}/",
        prefix=_ROOT_PREFIX,
        base=base,
        body=body,
    )


# --------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------


def render_rss(publications: Sequence[DailyPublication], site_base_url: str) -> str:
    """RSS 2.0, newest first, with a permanent guid per issue.

    The guid is ``<base>/daily/<date>/`` and never changes, so a reader that
    has seen an issue will not see it again after a re-render. The feed is a
    pure function of its inputs: nothing here reads the clock.
    """

    base = _site_base(site_base_url)
    ordered = sorted(publications, key=lambda item: item.target_date, reverse=True)
    for publication in ordered:
        if publication.level is PublicationLevel.L3:
            raise ValueError("L3 has no issue and must not enter the feed")
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{_x(SITE_TITLE)}</title>",
        f"<link>{_x(base)}/</link>",
        f"<description>{_x(SITE_DESCRIPTION)}</description>",
        "<language>zh-cn</language>",
        f'<atom:link href="{_x(base)}/rss.xml" rel="self" type="application/rss+xml"></atom:link>',
    ]
    if ordered:
        lines.append(f"<lastBuildDate>{_x(_rfc822(ordered[0].generated_at))}</lastBuildDate>")
    for publication in ordered:
        lines.extend(_rss_item(publication, base))
    lines.extend(["</channel>", "</rss>", ""])
    return "\n".join(lines)


def _rfc822(value: datetime) -> str:
    return format_datetime(_beijing(value))


def _rss_item(publication: DailyPublication, base: str) -> list[str]:
    guid = _daily_url(base, publication.target_date)
    title = f"{SITE_TITLE} {publication.target_date.isoformat()}"
    return [
        "<item>",
        f"<title>{_x(title)}</title>",
        f"<link>{_x(guid)}</link>",
        f'<guid isPermaLink="true">{_x(guid)}</guid>',
        f"<pubDate>{_x(_rfc822(publication.generated_at))}</pubDate>",
        f"<description>{_x(_rss_description(publication))}</description>",
        "</item>",
    ]


def _rss_description(publication: DailyPublication) -> str:
    parts = [f"{publication.target_date.isoformat()} {LEVEL_LABEL[publication.level]}"]
    if publication.notice:
        parts.append(publication.notice)
    if publication.highlight:
        parts.append(f"今日亮点：{publication.highlight}")
    parts.extend(f"- {card.headline}" for card in publication.details)
    parts.extend(f"- {card.headline}" for card in publication.briefs)
    parts.append(f"ai-daily-marker:{publication.marker}")
    return "\n".join(parts)
