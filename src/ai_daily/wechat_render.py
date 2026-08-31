from __future__ import annotations

import hashlib
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ai_daily.persona_models import DailyWechatEdition, RenderReceipt
from ai_daily.render import render_issue

RENDERER_VERSION = "daily-wechat-renderer-1"
TEMPLATE_VERSION = "ai-daily-original-1"

_KEPT_TAGS = frozenset({"a", "h2", "h3", "li", "ol", "p", "strong", "time", "ul"})
_UNWRAPPED_TAGS = frozenset({"article", "aside", "div", "section", "span"})
_WECHAT_ROOT_STYLE = "color:#20201e;font-size:16px;line-height:1.8"


@dataclass(frozen=True)
class RenderedWechatDaily:
    html: str
    receipt: RenderReceipt


def render_daily_wechat(edition: DailyWechatEdition) -> RenderedWechatDaily:
    """Render the published daily text unchanged in compact WeChat-safe markup."""

    if not edition.hash_is_valid():
        raise ValueError("cannot render daily WeChat edition with invalid hash")
    source = edition.publication.model_dump_json(indent=2)
    original_html = render_issue(edition.publication)
    html = _compact_issue_html(original_html)
    if _visible_text(html) != _visible_text(original_html):
        raise ValueError("WeChat HTML adaptation changed the published daily text")
    receipt = RenderReceipt(
        edition_payload_sha256=edition.payload_sha256,
        markdown_sha256=_digest(source),
        html_sha256=_digest(html),
        renderer_version=RENDERER_VERSION,
        template_version=TEMPLATE_VERSION,
    )
    return RenderedWechatDaily(html=html, receipt=receipt)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _WechatHTMLAdapter(HTMLParser):
    """Strip website-only markup while preserving every visible text node."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output = [f'<section style="{_WECHAT_ROOT_STYLE}">']
        self.stack: list[str] = []
        self.anchor_kept: list[bool] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._validate_tag(tag)
        self.stack.append(tag)
        if tag == "a":
            href = _link_href(attrs)
            self.anchor_kept.append(href is not None)
            if href is not None:
                self.output.append(f'<a href="{escape(href, quote=True)}">')
        elif tag in _KEPT_TAGS:
            self.output.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        self._validate_tag(tag)
        if not self.stack or self.stack.pop() != tag:
            raise ValueError(f"unexpected closing tag in daily HTML: {tag}")
        if tag == "a":
            if self.anchor_kept.pop():
                self.output.append("</a>")
        elif tag in _KEPT_TAGS:
            self.output.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raise ValueError(f"unexpected self-closing tag in daily HTML: {tag}")

    def handle_data(self, data: str) -> None:
        if _is_visible_text_node(data):
            self.output.append(escape(data, quote=False))

    def handle_decl(self, decl: str) -> None:
        raise ValueError(f"unexpected declaration in daily HTML: {decl}")

    def unknown_decl(self, data: str) -> None:
        raise ValueError(f"unexpected declaration in daily HTML: {data}")

    def finish(self) -> str:
        self.close()
        if self.stack:
            raise ValueError(f"unclosed tag in daily HTML: {self.stack[-1]}")
        self.output.append("</section>")
        return "".join(self.output)

    @staticmethod
    def _validate_tag(tag: str) -> None:
        if tag not in _KEPT_TAGS and tag not in _UNWRAPPED_TAGS:
            raise ValueError(f"unexpected tag in daily HTML: {tag}")


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if _is_visible_text_node(data):
            self.parts.append(data)


def _compact_issue_html(source_html: str) -> str:
    parser = _WechatHTMLAdapter()
    parser.feed(source_html)
    return parser.finish()


def _visible_text(source_html: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(source_html)
    parser.close()
    return "".join(parser.parts)


def _is_visible_text_node(data: str) -> bool:
    return bool(data) and (bool(data.strip()) or ("\n" not in data and "\r" not in data))


def _link_href(attrs: list[tuple[str, str | None]]) -> str | None:
    """Return the link target, or ``None`` for an in-page anchor to unwrap.

    The website's overview list navigates within the page. WeChat has no
    in-page anchors, so those links become plain text; every other link must
    still resolve on its own, away from our site.
    """

    hrefs = [value for name, value in attrs if name == "href"]
    if len(hrefs) != 1 or hrefs[0] is None:
        raise ValueError("daily source link must contain exactly one href")
    href = hrefs[0]
    if href.startswith("#"):
        return None
    parsed = urlsplit(href)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("daily source link must use an absolute http(s) URL")
    return href
