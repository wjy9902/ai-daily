"""HTML and RSS renderers for the papers publication records."""

from __future__ import annotations

from collections.abc import Sequence
from email.utils import format_datetime

from ai_daily.papers_models import PaperCard, PapersPublication

from .site import BEIJING_TIMEZONE, _site_base, _t, _url, _x

PAPERS_TITLE = "AI 论文深读"
PAPERS_DESCRIPTION = "每日筛选 agent、模型、推理与对齐方向论文，基于 arXiv 全文深读。"


def _papers_header(prefix: str) -> list[str]:
    return [
        '<header class="site-header">',
        f'<a class="site-title" href="{_t(prefix)}papers/index.html">{PAPERS_TITLE}</a>',
        '<nav class="site-nav">',
        f'<a href="{_t(prefix)}index.html">日报</a>',
        f'<a href="{_t(prefix)}papers/index.html">论文</a>',
        f'<a href="{_t(prefix)}papers/rss.xml">RSS</a>',
        "</nav>",
        "</header>",
    ]


def _papers_page(
    *, title: str, description: str, canonical: str, prefix: str, base: str, body: str
) -> str:
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
            '<link rel="alternate" type="application/rss+xml" '
            f'title="{PAPERS_TITLE}" href="{_t(prefix)}papers/rss.xml">',
            "</head>",
            "<body>",
            *_papers_header(prefix),
            '<main class="site-main papers-main">',
            body,
            "</main>",
            '<footer class="site-footer">',
            "<p>自动化深读不能替代原文与同行评审；实验数字均附可回查原文片段。</p>",
            f'<p class="site-footer__home"><a href="{_url(base)}/">{_t(base)}</a></p>',
            "</footer>",
            "</body>",
            "</html>",
            "",
        ]
    )


def _badges(paper: PaperCard) -> str:
    signals = paper.signals
    values = []
    if signals.organization:
        values.append(signals.organization)
    if signals.hf_listed:
        values.append(f"HF ↑{signals.upvotes}")
    if signals.github_repo:
        values.append(f"GitHub ★{signals.github_stars}")
    if signals.cross_mentions:
        values.append(f"X/HN x{signals.cross_mentions}")
    values.append(paper.topic)
    return "".join(f'<span class="paper-badge">{_t(value)}</span>' for value in values)


def _links(paper: PaperCard) -> str:
    links = [f'<a href="{_url(str(paper.arxiv_url))}">arXiv</a>']
    if paper.hf_url:
        links.append(f'<a href="{_url(str(paper.hf_url))}">HF 讨论</a>')
    if paper.alphaxiv_url:
        links.append(f'<a href="{_url(str(paper.alphaxiv_url))}">alphaXiv</a>')
    return '<span class="paper-links">' + " · ".join(links) + "</span>"


def _details(title: str, body: str, *, opened: bool = False) -> str:
    open_attr = " open" if opened else ""
    return (
        f'<details class="paper-section"{open_attr}><summary>{_t(title)}</summary>'
        f'<div class="paper-section__body"><p>{_t(body)}</p></div></details>'
    )


def _experiment_section(paper: PaperCard) -> str:
    assert paper.deep_read is not None
    deep = paper.deep_read
    rows = [
        '<details class="paper-section"><summary>④ 实验与证据</summary>',
        '<div class="paper-section__body">',
        f"<p>{_t(deep.experiment_summary)}</p>",
        '<ol class="paper-evidence">',
    ]
    for pair in deep.experiments:
        rows.extend(
            [
                "<li>",
                f'<p class="paper-claim">{_t(pair.claim)}</p>',
                f"<blockquote>{_t(pair.quote)}</blockquote>",
                "</li>",
            ]
        )
    rows.extend(["</ol>", "</div>", "</details>"])
    return "\n".join(rows)


def _card(paper: PaperCard) -> str:
    rows = [
        '<article class="paper-card">',
        f'<div class="paper-badges">{_badges(paper)}</div>',
        f'<h2 class="paper-title">{_t(paper.title)}</h2>',
    ]
    if paper.authors:
        rows.append(f'<p class="paper-authors">{_t(paper.authors)}</p>')
    rows.append(_links(paper))
    if paper.deep_read is None:
        rows.extend(
            [
                '<p class="paper-fallback">未深读 · 全文抓取或深读生成失败</p>',
                _details("① 一句话定位 (简读)", paper.abstract[:1000] or paper.title, opened=True),
                _details(
                    "⑤ 审稿人视角 (简读)",
                    "仅依据摘要，无法可靠判断方法 soundness 与实验充分性，请以原文为准。",
                    opened=True,
                ),
            ]
        )
    else:
        deep = paper.deep_read
        rows.extend(
            [
                _details("① 一句话定位", deep.positioning, opened=True),
                _details("② 背景与动机", deep.background),
                _details("③ 方法机制", deep.mechanism),
                _experiment_section(paper),
                _details(
                    "⑤ 审稿人视角",
                    f"Novelty：{deep.novelty}\nSoundness：{deep.soundness}\n"
                    f"Significance：{deep.significance}",
                    opened=True,
                ),
                _details("⑥ 局限与保留意见", deep.limitations),
                _details("⑦ 值得跟进", deep.follow_up),
            ]
        )
    rows.extend(["</article>"])
    return "\n".join(rows)


def render_papers_issue(publication: PapersPublication, site_base_url: str) -> str:
    base = _site_base(site_base_url)
    cards = "\n".join(_card(paper) for paper in publication.papers)
    body = "\n".join(
        [
            '<header class="papers-edition">',
            f'<p class="papers-kicker">{PAPERS_TITLE}</p>',
            f"<h1>{_t(publication.target_date.isoformat())}</h1>",
            f"<p>{publication.deep_read_count} 篇深读 · "
            f"{len(publication.papers) - publication.deep_read_count} 篇简读</p>",
            "</header>",
            cards,
            f"<!-- papers-publication:{_t(publication.marker)} -->",
        ]
    )
    return _papers_page(
        title=f"{PAPERS_TITLE} · {publication.target_date.isoformat()}",
        description=PAPERS_DESCRIPTION,
        canonical=f"{base}/papers/{publication.target_date.isoformat()}/",
        prefix="../../",
        base=base,
        body=body,
    )


def render_papers_index(
    latest: PapersPublication, history: Sequence[PapersPublication], site_base_url: str
) -> str:
    base = _site_base(site_base_url)
    archive = "".join(
        '<li><a href="'
        f'{_t(publication.target_date.isoformat())}/index.html">'
        f"{_t(publication.target_date.isoformat())}</a> · {len(publication.papers)} 篇</li>"
        for publication in history
    )
    body = "\n".join(
        [
            '<header class="papers-edition">',
            f'<p class="papers-kicker">{PAPERS_TITLE}</p>',
            f"<h1>{_t(latest.target_date.isoformat())}</h1>",
            "</header>",
            *(_card(paper) for paper in latest.papers),
            '<section class="papers-archive"><h2>往期</h2>',
            f"<ul>{archive}</ul></section>",
            f"<!-- papers-publication:{_t(latest.marker)} -->",
        ]
    )
    return _papers_page(
        title=PAPERS_TITLE,
        description=PAPERS_DESCRIPTION,
        canonical=f"{base}/papers/",
        prefix="../",
        base=base,
        body=body,
    )


def render_papers_rss(publications: Sequence[PapersPublication], site_base_url: str) -> str:
    base = _site_base(site_base_url)
    ordered = sorted(publications, key=lambda item: item.target_date, reverse=True)
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{_x(PAPERS_TITLE)}</title>",
        f"<link>{_x(base)}/papers/</link>",
        f"<description>{_x(PAPERS_DESCRIPTION)}</description>",
        "<language>zh-cn</language>",
        f'<atom:link href="{_x(base)}/papers/rss.xml" rel="self" '
        'type="application/rss+xml"></atom:link>',
    ]
    if ordered:
        generated = ordered[0].generated_at.astimezone(BEIJING_TIMEZONE)
        lines.append(f"<lastBuildDate>{_x(format_datetime(generated))}</lastBuildDate>")
    for publication in ordered:
        guid = f"{base}/papers/{publication.target_date.isoformat()}/"
        title = f"{PAPERS_TITLE} {publication.target_date.isoformat()}"
        description = "；".join(paper.title for paper in publication.papers)
        lines.extend(
            [
                "<item>",
                f"<title>{_x(title)}</title>",
                f"<link>{_x(guid)}</link>",
                f'<guid isPermaLink="true">{_x(guid)}</guid>',
                f"<pubDate>{_x(format_datetime(publication.generated_at.astimezone(BEIJING_TIMEZONE)))}</pubDate>",
                f"<description>{_x(description)}</description>",
                "</item>",
            ]
        )
    lines.extend(["</channel>", "</rss>", ""])
    return "\n".join(lines)
