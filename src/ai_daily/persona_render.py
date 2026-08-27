from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html import escape

from ai_daily.persona_models import AnalysisItem, PersonaEdition, RenderReceipt

RENDERER_VERSION = "persona-renderer-2"
TEMPLATE_VERSION = "jiayu-editorial-3"

ARTICLE_STYLE = "max-width:680px;margin:0 auto;color:#20201e;font-size:16px;line-height:1.85"
KICKER_STYLE = "margin:0 0 12px;color:#8a5b35;font-size:13px;letter-spacing:.08em"
TITLE_STYLE = "margin:0 0 18px;color:#171715;font-size:28px;line-height:1.3"
DIGEST_STYLE = "margin:0 0 22px;padding:14px 16px;background:#f4efe7;color:#443d35"
THESIS_STYLE = "margin:0 0 28px;font-size:17px;line-height:1.9"
SECTION_STYLE = "margin:30px 0 0;padding-top:22px;border-top:1px solid #ded7cc"
HEADING_STYLE = "margin:0 0 16px;color:#171715;font-size:21px;line-height:1.45"
PARAGRAPH_STYLE = "margin:12px 0"
DISCLOSURE_STYLE = "margin:28px 0 0;color:#777;font-size:13px"


@dataclass(frozen=True)
class RenderedPersona:
    markdown: str
    html: str
    web_html: str
    receipt: RenderReceipt


def render_persona(edition: PersonaEdition, site_base_url: str) -> RenderedPersona:
    if not edition.hash_is_valid():
        raise ValueError("cannot render persona edition with invalid hash")
    markdown = _markdown(edition)
    body = _article_html(edition, inline_styles=True)
    html = "\n".join([body, f"<!-- persona-marker:{edition.payload_sha256} -->"])
    web_html = _web_html(edition, site_base_url)
    receipt = RenderReceipt(
        edition_payload_sha256=edition.payload_sha256,
        markdown_sha256=_digest(markdown),
        html_sha256=_digest(html),
        renderer_version=RENDERER_VERSION,
        template_version=TEMPLATE_VERSION,
    )
    return RenderedPersona(
        markdown=markdown,
        html=html,
        web_html=web_html,
        receipt=receipt,
    )


def _markdown(edition: PersonaEdition) -> str:
    lines = [
        f"# {edition.title_block.text}",
        "",
        f"> {edition.digest_block.text}",
        "",
        edition.thesis_block.text,
    ]
    for item in edition.items:
        lines.extend(
            [
                "",
                f"## {item.headline_block.text}",
                "",
                f"**确认变化**：{_confirmed_change_text(item.confirmed_change_block.text)}",
            ]
        )
        if item.delta_from_before_block:
            lines.extend(["", f"**相较此前**：{item.delta_from_before_block.text}"])
        lines.extend(
            [
                "",
                f"**为什么重要**：{item.importance_block.text}",
                "",
                f"**对 AI 产品的影响**：{item.product_implication_block.text}",
            ]
        )
        if item.recommended_action_block:
            lines.extend(["", f"**可以怎么做**：{item.recommended_action_block.text}"])
        lines.extend(
            [
                "",
                f"**反面条件**：{item.counter_case_block.text}",
                "",
                f"**继续观察**：{item.watch_signal_block.text}",
            ]
        )
    if edition.watchlist_blocks:
        lines.extend(["", "## 观察清单", ""])
        lines.extend(f"- {block.text}" for block in edition.watchlist_blocks)
    lines.extend(["", "## 来源", ""])
    lines.extend(f"- <{url}>" for url in edition.source_links)
    lines.extend(["", f"_{edition.ai_disclosure}_", ""])
    return "\n".join(lines)


def _article_html(edition: PersonaEdition, *, inline_styles: bool) -> str:
    title = escape(edition.title_block.text)
    body = [
        f'<article class="persona-edition"{_style(ARTICLE_STYLE, inline_styles)}>',
        f'<p class="persona-kicker"{_style(KICKER_STYLE, inline_styles)}>'
        "甲鱼主编版 · AI 产品与行业判断</p>",
        f'<p class="persona-date"><time datetime="{edition.target_date.isoformat()}">'
        f"{edition.target_date.isoformat()}</time></p>",
        f"<h1{_style(TITLE_STYLE, inline_styles)}>{title}</h1>",
        f'<p class="persona-digest"{_style(DIGEST_STYLE, inline_styles)}>'
        f"{escape(edition.digest_block.text)}</p>",
        f'<p class="persona-thesis"{_style(THESIS_STYLE, inline_styles)}>'
        f"{escape(edition.thesis_block.text)}</p>",
    ]
    for item in edition.items:
        body.extend(_item_html(item, inline_styles))
    if edition.watchlist_blocks:
        body.extend(
            [
                f'<section class="persona-watch"{_style(SECTION_STYLE, inline_styles)}>',
                f"<h2{_style(HEADING_STYLE, inline_styles)}>观察清单</h2>",
                f"<ul{_style('margin:0;padding-left:1.4em', inline_styles)}>",
            ]
        )
        body.extend(f"<li>{escape(block.text)}</li>" for block in edition.watchlist_blocks)
        body.extend(["</ul></section>"])
    body.extend(
        [
            f'<section class="persona-sources"{_style(SECTION_STYLE, inline_styles)}>',
            f"<h2{_style(HEADING_STYLE, inline_styles)}>来源</h2>",
            f"<ul{_style('margin:0;padding-left:1.4em;word-break:break-all', inline_styles)}>",
        ]
    )
    body.extend(
        f'<li><a href="{escape(str(url), quote=True)}" rel="noopener noreferrer">'
        f"{escape(str(url))}</a></li>"
        for url in edition.source_links
    )
    body.extend(
        [
            "</ul></section>",
            f'<p class="persona-disclosure"'
            f"{_style(DISCLOSURE_STYLE, inline_styles)}>"
            f"{escape(edition.ai_disclosure)}</p>",
            "</article>",
        ]
    )
    return "\n".join(body)


def _web_html(edition: PersonaEdition, site_base_url: str) -> str:
    title = escape(edition.title_block.text)
    base = site_base_url.rstrip("/")
    canonical = f"{base}/jiayu/{edition.target_date.isoformat()}.html"
    article = _article_html(edition, inline_styles=False)
    article += (
        '\n<p class="persona-back"><a href="index.html">返回栏目归档</a></p>'
        f"\n<!-- persona-marker:{edition.payload_sha256} -->"
    )
    return _web_shell(f"{title} · 甲鱼 AI 编辑部", canonical, article)


def render_persona_index(
    latest: PersonaEdition, editions: list[PersonaEdition], site_base_url: str
) -> str:
    base = site_base_url.rstrip("/")
    archive = ['<section class="persona-archive"><h2>往期主编版</h2><ol>']
    archive.extend(
        f'<li><a href="{item.target_date.isoformat()}.html">'
        f'<time datetime="{item.target_date.isoformat()}">{item.target_date.isoformat()}</time>'
        f" · {escape(item.title_block.text)}</a></li>"
        for item in editions
    )
    archive.append("</ol></section>")
    content = "\n".join(
        [
            _article_html(latest, inline_styles=False),
            f"<!-- persona-marker:{latest.payload_sha256} -->",
            *archive,
        ]
    )
    return _web_shell("甲鱼主编版 · AI 日报", f"{base}/jiayu/", content)


def render_persona_placeholder(site_base_url: str, held: bool) -> str:
    base = site_base_url.rstrip("/")
    message = "最新一期正在重新生成。" if held else "首期正在准备中。"
    content = "\n".join(
        [
            '<article class="persona-edition persona-empty">',
            '<p class="persona-kicker">甲鱼主编版 · AI 产品与行业判断</p>',
            f"<h1>{message}</h1>",
            "<p>证据与审稿状态通过后，这里会自动更新。</p>",
            "</article>",
        ]
    )
    return _web_shell("甲鱼主编版 · AI 日报", f"{base}/jiayu/", content)


def _web_shell(title: str, canonical: str, content: str) -> str:
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-Hans"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            f"<title>{escape(title)}</title>",
            f'<link rel="canonical" href="{escape(canonical, quote=True)}">',
            '<link rel="stylesheet" href="../assets/site.css">',
            "</head><body>",
            '<header class="site-header"><a class="site-title" href="../index.html">AI 日报</a>',
            '<nav class="site-nav"><a href="index.html">甲鱼主编版</a>'
            '<a href="../archive.html">基础日报</a></nav></header>',
            '<main class="site-main">',
            content,
            "</main>",
            '<footer class="site-footer"><p class="site-footer__home">'
            '<a href="../index.html">返回 AI 日报首页</a></p></footer>',
            "</body></html>",
        ]
    )


def _label(label: str, text: str, inline_styles: bool) -> str:
    return (
        f"<p{_style(PARAGRAPH_STYLE, inline_styles)}><strong"
        f"{_style('color:#6e4325', inline_styles)}>"
        f"{escape(label)}</strong>：{escape(text)}</p>"
    )


def _item_html(item: AnalysisItem, inline_styles: bool) -> list[str]:
    blocks = [
        f'<section class="persona-item"{_style(SECTION_STYLE, inline_styles)}>',
        f"<h2{_style(HEADING_STYLE, inline_styles)}>{escape(item.headline_block.text)}</h2>",
        _label("确认变化", _confirmed_change_text(item.confirmed_change_block.text), inline_styles),
    ]
    if item.delta_from_before_block:
        blocks.append(_label("相较此前", item.delta_from_before_block.text, inline_styles))
    blocks.extend(
        [
            _label("为什么重要", item.importance_block.text, inline_styles),
            _label("对 AI 产品的影响", item.product_implication_block.text, inline_styles),
        ]
    )
    if item.recommended_action_block:
        blocks.append(_label("可以怎么做", item.recommended_action_block.text, inline_styles))
    blocks.extend(
        [
            _label("反面条件", item.counter_case_block.text, inline_styles),
            _label("继续观察", item.watch_signal_block.text, inline_styles),
            "</section>",
        ]
    )
    return blocks


def _style(value: str, enabled: bool) -> str:
    return f' style="{value}"' if enabled else ""


def _confirmed_change_text(value: str) -> str:
    return value.replace(
        "is generally available Give Claude",
        "is generally available. Give Claude",
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
