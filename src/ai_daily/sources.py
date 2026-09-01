from __future__ import annotations

import asyncio
import calendar
import ipaddress
import json
import re
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, TypedDict, cast
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import feedparser
import httpcore
import httpx
from lxml import etree, html  # type: ignore[import-untyped]
from pydantic import HttpUrl

from ai_daily.models import (
    Event,
    RawItem,
    SourceChannel,
    SourceConfig,
    SourceHealth,
    SourceRegion,
    SourceTier,
    SourceTimeKind,
)


class SourceCollectionError(RuntimeError):
    pass


class SourceNotModified(RuntimeError):
    pass


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ARTICLE_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ARTICLE_TEXT_LIMIT = 10_000
ARTICLE_FETCH_THRESHOLD = 2_000
ARTICLE_TEXT_MINIMUM = 500
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36 "
    "ai-daily/0.2 (+https://wjy9902.github.io/ai-daily/)"
)


@dataclass(frozen=True)
class PartialSourceResult:
    items: list[RawItem]
    failed_items: int


@dataclass(frozen=True)
class ArticleTarget:
    event: Event
    item: RawItem
    timeout: float


class SourceFields(TypedDict):
    source: str
    source_tier: SourceTier
    source_label: str
    source_channel: SourceChannel
    source_region: SourceRegion
    source_ai_focused: bool


def _source_fields(source: SourceConfig) -> SourceFields:
    return {
        "source": source.name,
        "source_tier": source.tier,
        "source_label": source.display_name or source.name,
        "source_channel": source.channel,
        "source_region": source.region,
        "source_ai_focused": source.ai_focused,
    }


def _published(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(UTC)
            except ValueError:
                return None
    if hasattr(value, "tm_year"):
        return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    return None


# Listing text comes from text_content(), which glues adjacent elements
# together: anthropic.com/news renders a card as "AnnouncementsSep 1, 2026Our
# most advanced…", and \b on either side of the date never matches there. The
# launch page behind it carries no date meta, so this text was its only date -
# and without one the freshness gate rejected the first-party Fable 5.1 post.
# No boundary before the month, then: "<month> <day>, <year>" is specific
# enough on its own. The year still may not run on into more digits.
DATE_RE = re.compile(
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+\d{1,2},\s+\d{4})(?!\d)",
    re.IGNORECASE,
)
NUMERIC_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}:\d{2}))?\b")
PUBLISHED_META_KEYS = {
    "article:published_time",
    "citation_publication_date",
    "date",
    "datepublished",
    "dc.date",
    "dc.date.issued",
    "og:published_time",
    "parsely-pub-date",
    "pubdate",
    "publish-date",
}
ARTICLE_JSONLD_TYPES = {
    "article",
    "blogposting",
    "newsarticle",
    "report",
    "scholarlyarticle",
    "techarticle",
}
#: A date on its own, in the forms Chinese publishers actually emit.
DATE_TEXT_RE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}")
#: A wall clock and nothing else, so a bare "13:10:50" is not read as a date.
CLOCK_TEXT_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")


def _published_from_text(value: str, default_timezone: tzinfo = UTC) -> datetime | None:
    match = DATE_RE.search(value)
    if match:
        normalized = re.sub(r"(?i)^([a-z]+)\.", r"\1", match.group(1))
        for date_format in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return (
                    datetime.strptime(normalized, date_format)
                    .replace(tzinfo=default_timezone)
                    .astimezone(UTC)
                )
            except ValueError:
                continue
    if numeric := NUMERIC_DATE_RE.search(value):
        clock = numeric.group(2) or "00:00:00"
        return (
            datetime.fromisoformat(f"{numeric.group(1)}T{clock}")
            .replace(tzinfo=default_timezone)
            .astimezone(UTC)
        )
    return None


def _plain_text(value: str) -> str:
    if not value:
        return ""
    try:
        text = html.fromstring(value).text_content()
    except (etree.ParserError, ValueError, TypeError):
        text = value
    return " ".join(text.split())


def _entry_body(entry: Any) -> str:
    content_values = [
        _plain_text(str(value.get("value") or ""))
        for value in entry.get("content", [])
        if isinstance(value, dict)
    ]
    content = max((value for value in content_values if value), key=len, default="")
    fallback = _plain_text(str(entry.get("summary") or entry.get("description") or ""))
    return (content or fallback)[:ARTICLE_TEXT_LIMIT]


def _meta(document: html.HtmlElement, property_name: str) -> str:
    values = document.xpath(f'//meta[@property="{property_name}"]/@content')
    return str(values[0]).strip() if values else ""


def _published_from_document(
    document: html.HtmlElement, default_timezone: tzinfo = UTC
) -> datetime | None:
    for element in document.xpath("//meta[@content]"):
        key = str(
            element.get("property") or element.get("name") or element.get("itemprop") or ""
        ).lower()
        if key in PUBLISHED_META_KEYS and (parsed := _published(element.get("content"))):
            return parsed
    for raw_value in document.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            structured = json.loads(str(raw_value))
        except (json.JSONDecodeError, TypeError):
            continue
        for value in _jsonld_published_values(structured):
            if parsed := _published(value):
                return parsed
    containers = document.xpath("(//main | //article)[1]")
    if containers:
        for value in containers[0].xpath(".//time[@datetime]/@datetime"):
            if parsed := _published(value):
                return parsed
    if parsed := _published_from_date_classes(document, default_timezone):
        return parsed
    if not containers:
        return None
    text = " ".join(" ".join(containers[0].itertext()).split())[:2000]
    return _published_from_text(text, default_timezone)


def _published_from_date_classes(
    document: html.HtmlElement, default_timezone: tzinfo
) -> datetime | None:
    """Read a byline that marks its date with a CSS class instead of markup.

    Several Chinese publishers (量子位 among them) emit no date meta tag, no
    JSON-LD and no <time> element, only `<span class="date">2026-08-13</span>`
    followed by `<span class="time">13:10:50</span>`. Without this the source
    fetches fine and then loses every item to the has-a-publication-time gate.

    Only the first date-like value is used, so a "related articles" sidebar
    further down the page cannot masquerade as the byline.
    """

    date_text: str | None = None
    clock_text: str | None = None
    for element in document.xpath(
        "//*[contains(@class, 'date') or contains(@class, 'time')"
        " or contains(@class, 'publish')][position() < 30]"
    ):
        value = " ".join(" ".join(element.itertext()).split())[:60]
        if not value:
            continue
        if date_text is None and DATE_TEXT_RE.search(value):
            date_text = value
            continue
        if clock_text is None and CLOCK_TEXT_RE.fullmatch(value):
            clock_text = value
        if date_text is not None and clock_text is not None:
            break
    if date_text is None:
        return None
    combined = f"{date_text} {clock_text}" if clock_text else date_text
    return _published_from_text(combined, default_timezone)


def _iter_jsonld_articles(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        raw_types = value.get("@type", [])
        types = raw_types if isinstance(raw_types, list) else [raw_types]
        is_article = any(str(item).lower() in ARTICLE_JSONLD_TYPES for item in types)
        if is_article:
            yield value
        for child in value.values():
            yield from _iter_jsonld_articles(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_jsonld_articles(child)


def _jsonld_published_values(value: Any) -> list[str]:
    return [
        str(article["datePublished"])
        for article in _iter_jsonld_articles(value)
        if article.get("datePublished")
    ]


def _article_text_from_document(document: html.HtmlElement) -> str:
    structured = _jsonld_article_bodies(document)
    for element in document.xpath(
        "//script | //style | //noscript | //nav | //header | //footer | //aside | //form | //svg"
    ):
        element.drop_tree()
    selectors = (
        '//*[@itemprop="articleBody"]',
        "//article",
        '//*[@role="main"]',
        "//main",
        '//*[contains(concat(" ", normalize-space(@class), " "), " article-body ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " article-content ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " entry-content ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " entry ")]',
        '//*[contains(concat(" ", normalize-space(@class), " "), " post-content ")]',
    )
    candidates = structured
    for selector in selectors:
        values = [
            " ".join(" ".join(element.itertext()).split()) for element in document.xpath(selector)
        ]
        values = [value for value in values if value]
        if values:
            candidates.extend(values)
            break
    if not candidates:
        return ""
    return max(candidates, key=len)[:ARTICLE_TEXT_LIMIT]


def _article_text_from_content(content: bytes, url: str) -> str:
    document = html.fromstring(content, base_url=url)
    return _article_text_from_document(document)


def _article_metadata_from_content(content: bytes, url: str) -> tuple[datetime | None, str]:
    document = html.fromstring(content, base_url=url)
    return _published_from_document(document), _article_text_from_document(document)


def _jsonld_article_bodies(document: html.HtmlElement) -> list[str]:
    found: list[str] = []
    for raw_value in document.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            structured = json.loads(str(raw_value))
        except (json.JSONDecodeError, TypeError):
            continue
        found.extend(_jsonld_body_values(structured))
    return found


def _jsonld_body_values(value: Any) -> list[str]:
    return [
        _plain_text(str(article["articleBody"]))
        for article in _iter_jsonld_articles(value)
        if article.get("articleBody")
    ]


def _normalized_host(value: str) -> str:
    host = (urlsplit(value).hostname or "").lower()
    return host.removeprefix("www.")


def _article_url_allowed(source: SourceConfig, item: RawItem) -> bool:
    if source.kind == "huggingface_models":
        path_parts = [part for part in urlsplit(str(item.url)).path.split("/") if part]
        return (
            item.source == source.name
            and _normalized_host(str(item.url)) == _normalized_host(str(source.url))
            and len(path_parts) == 2
            and path_parts[0] == source.namespace
        )
    if source.kind not in {"rss", "html_index"}:
        return False
    item_parts = urlsplit(str(item.url))
    return (
        item.source == source.name
        and item_parts.scheme == "https"
        and _normalized_host(str(item.url)) == _normalized_host(str(source.url))
    )


class Collector:
    """Owns the HTTP client so every source request uses the browser UA and pinned DNS.

    `transport` exists for tests only; production must use the default pinned transport.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        per_host: int = 5,
        article_concurrency: int = 8,
    ) -> None:
        if per_host < 1:
            raise ValueError("per_host must be at least 1")
        if article_concurrency < 1:
            raise ValueError("article_concurrency must be at least 1")
        self.client = httpx.AsyncClient(
            headers={"User-Agent": DEFAULT_USER_AGENT},
            follow_redirects=False,
            transport=transport or PublicAsyncHTTPTransport(),
        )
        self._uses_pinned_dns = transport is None
        self._per_host = per_host
        self._article_semaphore = asyncio.Semaphore(article_concurrency)
        self._article_text_cache: dict[str, str] = {}
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}
        self._validators: dict[str, dict[str, str]] = {}

    async def __aenter__(self) -> Collector:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.client.aclose()

    async def collect(
        self, sources: list[SourceConfig]
    ) -> tuple[list[RawItem], list[SourceHealth]]:
        enabled = [source for source in sources if source.enabled]
        results = await asyncio.gather(*(self._collect_one(source) for source in enabled))
        items: list[RawItem] = []
        health: list[SourceHealth] = []
        for source_items, source_health in results:
            items.extend(source_items)
            health.append(source_health)
        return items, health

    async def enrich_event_content(
        self,
        events: list[Event],
        sources: list[SourceConfig],
        event_ids: set[str],
    ) -> list[dict[str, object]]:
        enabled = [source for source in sources if source.enabled]
        sources_by_name = {source.name: source for source in enabled}
        targets, audit = self._enrichment_targets(events, event_ids, sources_by_name)
        results = await asyncio.gather(
            *(self._fetch_article_text(str(target.item.url), target.timeout) for target in targets),
            return_exceptions=True,
        )
        audit.extend(
            self._apply_enrichment_result(target, result)
            for target, result in zip(targets, results, strict=True)
        )
        return audit

    def _enrichment_targets(
        self,
        events: list[Event],
        event_ids: set[str],
        sources_by_name: dict[str, SourceConfig],
    ) -> tuple[list[ArticleTarget], list[dict[str, object]]]:
        targets: list[ArticleTarget] = []
        audit: list[dict[str, object]] = []
        for event in events:
            if event.event_id not in event_ids:
                continue
            for item in event.items[:3]:
                base = self._enrichment_audit(event, item)
                if len(item.summary) >= ARTICLE_FETCH_THRESHOLD:
                    audit.append({**base, "status": "existing_full_text"})
                    continue
                source = sources_by_name.get(item.source)
                if source is None or not _article_url_allowed(source, item):
                    audit.append({**base, "status": "skipped_unconfigured_host"})
                    continue
                targets.append(ArticleTarget(event, item, source.timeout_seconds))
        return targets, audit

    def _apply_enrichment_result(
        self,
        target: ArticleTarget,
        result: str | BaseException,
    ) -> dict[str, object]:
        event, item = target.event, target.item
        base = self._enrichment_audit(event, item)
        if isinstance(result, BaseException):
            return {**base, "status": "fetch_failed", "error": type(result).__name__}
        if len(result) < ARTICLE_TEXT_MINIMUM:
            return {**base, "status": "insufficient_text", "extracted_chars": len(result)}
        if len(result) <= len(item.summary):
            return {**base, "status": "existing_text_richer", "extracted_chars": len(result)}
        item.summary = result
        if item is event.primary_item:
            event.summary = result
        return {**base, "status": "enriched", "extracted_chars": len(result)}

    async def _fetch_article_text(self, url: str, timeout: float) -> str:
        if cached := self._article_text_cache.get(url):
            return cached
        async with self._article_semaphore:
            if cached := self._article_text_cache.get(url):
                return cached
            response = await self._request(
                url,
                timeout=timeout,
                max_response_bytes=ARTICLE_RESPONSE_BYTES,
            )
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type and "html" not in content_type:
                raise SourceCollectionError("article response is not HTML")
            text = await asyncio.to_thread(_article_text_from_content, response.content, url)
            if text:
                self._article_text_cache[url] = text
            return text

    @staticmethod
    def _enrichment_audit(event: Event, item: RawItem) -> dict[str, object]:
        return {
            "event_id": event.event_id,
            "source": item.source,
            "url": str(item.url),
            "existing_chars": len(item.summary),
        }

    async def _collect_one(self, source: SourceConfig) -> tuple[list[RawItem], SourceHealth]:
        started = time.monotonic()
        try:
            handler = getattr(self, f"_fetch_{source.kind}")
            result = await handler(source)
            items = result.items if isinstance(result, PartialSourceResult) else result
            if not items:
                raise SourceCollectionError("source returned no usable items")
            if source.channel != SourceChannel.COMMUNITY and not any(
                item.published_at is not None for item in items
            ):
                raise SourceCollectionError("source returned no verified publication dates")
            health = SourceHealth(
                source=source.name,
                tier=source.tier,
                status="partial" if isinstance(result, PartialSourceResult) else "ok",
                item_count=len(items),
                latency_ms=int((time.monotonic() - started) * 1000),
                error=(
                    f"{result.failed_items} linked article(s) failed"
                    if isinstance(result, PartialSourceResult)
                    else None
                ),
                newest_item_at=max(
                    (item.published_at for item in items if item.published_at is not None),
                    default=None,
                ),
            )
            return items, health
        except SourceNotModified:
            health = SourceHealth(
                source=source.name,
                tier=source.tier,
                status="not_modified",
                item_count=0,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            return [], health
        except Exception as error:
            health = SourceHealth(
                source=source.name,
                tier=source.tier,
                status="failed",
                item_count=0,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=type(error).__name__,
            )
            return [], health

    async def _get(self, source: SourceConfig, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self._validators.get(source.name, {}))
        for attempt in range(3):
            response = await self._request(
                str(source.url),
                timeout=source.timeout_seconds,
                headers=headers,
                **kwargs,
            )
            if response.status_code == 304:
                raise SourceNotModified(source.name)
            if response.status_code != 429 and response.status_code < 500:
                response.raise_for_status()
                self._remember_validators(source.name, response)
                return response
            if attempt == 2:
                response.raise_for_status()
            delay = min(float(response.headers.get("Retry-After", attempt + 1)), 10)
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def _request(self, url: str, **kwargs: Any) -> httpx.Response:
        allowed_origin = kwargs.pop("allowed_origin", _origin(url))
        max_response_bytes = kwargs.pop("max_response_bytes", MAX_RESPONSE_BYTES)
        kwargs.pop("follow_redirects", None)
        current = url
        request_kwargs = kwargs
        for redirect_count in range(MAX_REDIRECTS + 1):
            _validate_request_url(current, allowed_origin)
            if not self._uses_pinned_dns:
                await _validate_public_dns(current)
            request = self.client.build_request("GET", current, **request_kwargs)
            response = await self._send_limited(request, max_response_bytes)
            if response.status_code not in REDIRECT_STATUSES:
                return response
            location = response.headers.get("Location")
            if not location:
                raise SourceCollectionError("source redirect has no location")
            if redirect_count == MAX_REDIRECTS:
                raise SourceCollectionError("source exceeded redirect limit")
            current = urljoin(str(request.url), location)
            request_kwargs = {
                key: value for key, value in kwargs.items() if key in {"headers", "timeout"}
            }
        raise AssertionError("unreachable")

    async def _send_limited(
        self, request: httpx.Request, max_response_bytes: int = MAX_RESPONSE_BYTES
    ) -> httpx.Response:
        host = request.url.host
        semaphore = self._host_semaphores.setdefault(host, asyncio.Semaphore(self._per_host))
        async with semaphore:
            async with asyncio.timeout(_total_timeout(request)):
                response = await self.client.send(request, stream=True, follow_redirects=False)
                try:
                    if response.status_code in REDIRECT_STATUSES:
                        return _buffered_response(response, b"")
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > max_response_bytes:
                        raise SourceCollectionError("source response exceeds size limit")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_response_bytes:
                            raise SourceCollectionError("source response exceeds size limit")
                    return _buffered_response(response, bytes(body))
                finally:
                    await response.aclose()

    def _remember_validators(self, name: str, response: httpx.Response) -> None:
        validators = {}
        if response.headers.get("ETag"):
            validators["If-None-Match"] = response.headers["ETag"]
        if response.headers.get("Last-Modified"):
            validators["If-Modified-Since"] = response.headers["Last-Modified"]
        if validators:
            self._validators[name] = validators

    async def _fetch_rss(self, source: SourceConfig) -> list[RawItem] | PartialSourceResult:
        response = await self._get(source)
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise SourceCollectionError("invalid feed")
        now = datetime.now(UTC)
        items = []
        for entry in feed.entries[: source.limit]:
            url = entry.get("link")
            title = entry.get("title")
            if not url or not title:
                continue
            identifier = str(entry.get("id") or url)
            items.append(
                RawItem(
                    **_source_fields(source),
                    source_item_id=identifier,
                    url=url,
                    title=str(title).strip(),
                    summary=_entry_body(entry),
                    published_at=_published(
                        entry.get("published_parsed") or entry.get("published")
                    ),
                    discovered_at=now,
                    author=entry.get("author"),
                )
            )
        return await self._enrich_rss_dates(source, items)

    async def _enrich_rss_dates(
        self, source: SourceConfig, items: list[RawItem]
    ) -> list[RawItem] | PartialSourceResult:
        source_origin = _origin(str(source.url))
        targets = [
            item
            for item in items
            if item.published_at is None and _origin(str(item.url)) == source_origin
        ]
        results = await asyncio.gather(
            *(self._fetch_article_date(source, str(item.url)) for item in targets),
            return_exceptions=True,
        )
        failed_items = 0
        for item, result in zip(targets, results, strict=True):
            if isinstance(result, BaseException) or result is None:
                failed_items += 1
            else:
                item.published_at = result
        if failed_items:
            return PartialSourceResult(items=items, failed_items=failed_items)
        return items

    async def _fetch_article_date(self, source: SourceConfig, url: str) -> datetime | None:
        async with self._article_semaphore:
            response = await self._same_origin_get(
                source, url, max_response_bytes=ARTICLE_RESPONSE_BYTES
            )
            published, article_text = await asyncio.to_thread(
                _article_metadata_from_content, response.content, url
            )
            if article_text:
                self._article_text_cache[url] = article_text
            return published

    async def _fetch_hackernews(self, source: SourceConfig) -> list[RawItem]:
        base = str(source.url).rstrip("/")
        ids_response = await self._request(
            f"{base}/topstories.json", timeout=source.timeout_seconds
        )
        ids_response.raise_for_status()
        ids = ids_response.json()[: source.limit]

        async def fetch_item(item_id: int) -> dict[str, Any]:
            response = await self._request(
                f"{base}/item/{item_id}.json", timeout=source.timeout_seconds
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

        values = await asyncio.gather(*(fetch_item(item_id) for item_id in ids))
        return [
            RawItem(
                **_source_fields(source),
                source_item_id=str(item["id"]),
                url=HttpUrl(
                    str(item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}")
                ),
                title=item["title"],
                summary="",
                published_at=None,
                source_time_kind=SourceTimeKind.COMMUNITY_SUBMITTED,
                discovered_at=datetime.fromtimestamp(item["time"], tz=UTC),
                author=item.get("by"),
                metrics={"score": item.get("score", 0), "comments": item.get("descendants", 0)},
            )
            for item in values
            if item and item.get("type") == "story" and item.get("title")
        ]

    async def _fetch_github_releases(self, source: SourceConfig) -> list[RawItem]:
        response = await self._get(source, params={"per_page": source.limit})
        now = datetime.now(UTC)
        return [
            RawItem(
                **_source_fields(source),
                source_item_id=str(release["id"]),
                url=release["html_url"],
                title=f"{source.display_name or source.name}: "
                f"{release.get('name') or release['tag_name']}",
                summary=(release.get("body") or "")[:ARTICLE_TEXT_LIMIT],
                published_at=_published(release.get("published_at")),
                discovered_at=now,
                author=(release.get("author") or {}).get("login"),
            )
            for release in response.json()
            if not release.get("draft")
        ]

    async def _fetch_arxiv(self, source: SourceConfig) -> list[RawItem]:
        categories = source.arxiv_categories or ["cs.AI", "cs.LG", "cs.CL"]
        response = await self._get(
            source,
            params={
                "search_query": " OR ".join(f"cat:{category}" for category in categories),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": source.limit,
            },
        )
        feed = feedparser.parse(response.content)
        now = datetime.now(UTC)
        return [
            RawItem(
                **_source_fields(source),
                source_item_id=str(entry.id),
                url=HttpUrl(str(entry.link)),
                title=" ".join(str(entry.title).split()),
                summary=" ".join(str(entry.get("summary", "")).split()),
                published_at=_published(entry.get("published")),
                discovered_at=now,
                author=", ".join(author.name for author in entry.get("authors", [])),
            )
            for entry in feed.entries[: source.limit]
        ]

    async def _fetch_huggingface(self, source: SourceConfig) -> list[RawItem]:
        response = await self._get(source, params={"limit": source.limit})
        now = datetime.now(UTC)
        items = []
        for value in response.json()[: source.limit]:
            paper = value.get("paper") or value
            paper_id = paper.get("id") or paper.get("paperId")
            title = paper.get("title")
            if not paper_id or not title:
                continue
            raw_organization = paper.get("organization")
            organization = (
                raw_organization.get("name")
                if isinstance(raw_organization, dict)
                else raw_organization
            )
            authors = paper.get("authors") or []
            items.append(
                RawItem(
                    **_source_fields(source),
                    source_item_id=str(paper_id),
                    url=HttpUrl(f"https://huggingface.co/papers/{paper_id}"),
                    title=title,
                    summary=paper.get("summary") or paper.get("abstract") or "",
                    published_at=_published(paper.get("publishedAt")),
                    discovered_at=now,
                    author=", ".join(
                        str(author.get("name") or author)
                        if isinstance(author, dict)
                        else str(author)
                        for author in authors
                    )
                    or None,
                    metrics={
                        "upvotes": value.get("upvotes", value.get("numUpvotes", 0)) or 0,
                        "organization": organization or "",
                        "github_repo": paper.get("githubRepo") or "",
                        "github_stars": paper.get("githubStars") or 0,
                        "comments": paper.get("numComments") or 0,
                    },
                )
            )
        return items

    async def _fetch_huggingface_models(self, source: SourceConfig) -> list[RawItem]:
        response = await self._get(
            source,
            params={
                "author": source.namespace,
                "sort": "lastModified",
                "direction": "-1",
                "limit": source.limit,
            },
        )
        now = datetime.now(UTC)
        items: list[RawItem] = []
        for value in response.json():
            model_id = value.get("modelId") or value.get("id")
            tags = [str(tag) for tag in value.get("tags", [])]
            if (
                not model_id
                or not str(model_id).startswith(f"{source.namespace}/")
                or value.get("private")
                or any(tag.startswith("base_model:quantized:") for tag in tags)
            ):
                continue
            updated = _published(value.get("lastModified"))
            if updated is None:
                continue
            name = str(model_id).split("/", 1)[1]
            summary = (
                f"Official {source.namespace} model repository changed at "
                f"{updated.isoformat()}. Model: {model_id}. "
                f"Task: {value.get('pipeline_tag') or 'unspecified'}. "
                f"Tags: {', '.join(tags[:20])}."
            )
            items.append(
                RawItem(
                    **_source_fields(source),
                    source_item_id=f"{model_id}@{value.get('sha') or updated.isoformat()}",
                    url=HttpUrl(f"https://huggingface.co/{model_id}"),
                    title=f"{source.display_name or source.namespace}: {name} repository update",
                    summary=summary[:ARTICLE_TEXT_LIMIT],
                    published_at=updated,
                    source_time_kind=SourceTimeKind.REPOSITORY_UPDATED,
                    discovered_at=now,
                    metrics={
                        "likes": value.get("likes", 0),
                        "downloads": value.get("downloads", 0),
                        "timestamp_kind": "repository_last_modified",
                    },
                )
            )
        return items

    async def _fetch_html_index(self, source: SourceConfig) -> list[RawItem] | PartialSourceResult:
        response = await self._get(source)
        document = html.fromstring(response.content, base_url=str(source.url))
        links: list[tuple[str, str]] = []
        seen: set[str] = set()
        pattern = re.compile(source.link_pattern or "")
        for anchor in document.xpath("//a[@href]"):
            url = urljoin(str(source.url), str(anchor.get("href")))
            if url in seen or not pattern.search(url):
                continue
            seen.add(url)
            links.append((url, _plain_text(anchor.text_content())))
            if len(links) >= source.limit:
                break
        if not links:
            raise SourceCollectionError("html index yielded no matching links")
        results = await asyncio.gather(
            *(self._fetch_index_article(source, url, listing_text) for url, listing_text in links),
            return_exceptions=True,
        )
        items = [result for result in results if isinstance(result, RawItem)]
        failed_items = len(results) - len(items)
        if not items:
            raise SourceCollectionError("all linked articles failed")
        if failed_items:
            return PartialSourceResult(items=items, failed_items=failed_items)
        return items

    async def _fetch_index_article(
        self, source: SourceConfig, url: str, listing_text: str
    ) -> RawItem:
        response = await self._same_origin_get(source, url)
        document = html.fromstring(response.content, base_url=url)
        title = _meta(document, "og:title") or _plain_text(document.findtext(".//title") or "")
        summary = _meta(document, "og:description")
        default_timezone = ZoneInfo("Asia/Shanghai") if source.region == SourceRegion.CHINA else UTC
        return RawItem(
            **_source_fields(source),
            source_item_id=url,
            url=HttpUrl(url),
            title=title,
            summary=_plain_text(summary or listing_text)[:ARTICLE_TEXT_LIMIT],
            published_at=_published_from_document(document, default_timezone)
            or _published_from_text(listing_text, default_timezone),
            discovered_at=datetime.now(UTC),
        )

    async def _same_origin_get(
        self,
        source: SourceConfig,
        url: str,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> httpx.Response:
        response = await self._request(
            url,
            timeout=source.timeout_seconds,
            allowed_origin=_origin(str(source.url)),
            max_response_bytes=max_response_bytes,
        )
        response.raise_for_status()
        return response


def _buffered_response(response: httpx.Response, content: bytes) -> httpx.Response:
    consumed_headers = {"content-encoding", "content-length", "transfer-encoding"}
    headers = [
        (name, value)
        for name, value in response.headers.multi_items()
        if name.lower() not in consumed_headers
    ]
    return httpx.Response(
        status_code=response.status_code,
        headers=headers,
        content=content,
        request=response.request,
        extensions=response.extensions,
    )


def _total_timeout(request: httpx.Request) -> float | None:
    settings = request.extensions.get("timeout")
    if not isinstance(settings, dict):
        return None
    value = settings.get("read")
    return float(value) if isinstance(value, int | float) else None


def _validate_request_url(value: str, allowed_origin: tuple[str, str, int]) -> None:
    parts = urlsplit(value)
    if parts.username or parts.password:
        raise SourceCollectionError("source URL cannot contain credentials")
    if _origin(value) != allowed_origin or parts.scheme.lower() != "https":
        raise SourceCollectionError("source redirect left its configured HTTPS origin")
    host = parts.hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        raise SourceCollectionError("source URL cannot target localhost")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise SourceCollectionError("source URL cannot target a non-public address")


async def _validate_public_dns(value: str) -> None:
    parts = urlsplit(value)
    host = parts.hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        port = parts.port or 443
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            type=socket.SOCK_STREAM,
        )
        addresses = {ipaddress.ip_address(record[4][0]) for record in records}
        if not addresses or any(not value.is_global for value in addresses):
            raise SourceCollectionError(
                "source hostname resolved to a non-public address"
            ) from None
        return
    if not address.is_global:
        raise SourceCollectionError("source URL cannot target a non-public address")


async def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    records = await asyncio.to_thread(
        socket.getaddrinfo,
        host,
        port,
        type=socket.SOCK_STREAM,
    )
    addresses = tuple(dict.fromkeys(str(record[4][0]) for record in records))
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise SourceCollectionError("source hostname resolved to a non-public address")
    return addresses


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, validate, and connect to that exact public address."""

    def __init__(self) -> None:
        self._backend = cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await _public_addresses(host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address, port, timeout, local_address, socket_options
                )
            except httpcore.ConnectError as error:
                last_error = error
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        raise SourceCollectionError("Unix sockets are not allowed for sources")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PublicAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """httpx transport that keeps httpx's TLS, limits and proxy setup but pins DNS."""

    def __init__(self) -> None:
        super().__init__()
        self._pool._network_backend = PublicNetworkBackend()


def _origin(value: str) -> tuple[str, str, int]:
    parts = urlsplit(value)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme.lower(), (parts.hostname or "").lower(), port
