from __future__ import annotations

import asyncio
import calendar
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict, cast
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
from lxml import html  # type: ignore[import-untyped]
from pydantic import HttpUrl

from ai_daily.models import (
    RawItem,
    SourceChannel,
    SourceConfig,
    SourceHealth,
    SourceRegion,
    SourceTier,
)


class SourceCollectionError(RuntimeError):
    pass


class SourceNotModified(RuntimeError):
    pass


@dataclass(frozen=True)
class PartialSourceResult:
    items: list[RawItem]
    failed_items: int


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
            return parsed.astimezone(UTC)
        except (TypeError, ValueError):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                return None
    if hasattr(value, "tm_year"):
        return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    return None


DATE_RE = re.compile(r"\b([A-Z][a-z]{2} \d{1,2}, \d{4})\b")


def _published_from_text(value: str) -> datetime | None:
    match = DATE_RE.search(value)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%b %d, %Y").replace(tzinfo=UTC)


def _plain_text(value: str) -> str:
    if not value:
        return ""
    try:
        text = html.fromstring(value).text_content()
    except (ValueError, TypeError):
        text = value
    return " ".join(text.split())


def _meta(document: html.HtmlElement, property_name: str) -> str:
    values = document.xpath(f'//meta[@property="{property_name}"]/@content')
    return str(values[0]).strip() if values else ""


class Collector:
    def __init__(self, client: httpx.AsyncClient | None = None, per_host: int = 5) -> None:
        if per_host < 1:
            raise ValueError("per_host must be at least 1")
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": "ai-daily/0.2 (+https://wjy9902.github.io/ai-daily/)"},
            follow_redirects=True,
        )
        self._per_host = per_host
        self._host_semaphores: dict[str, asyncio.Semaphore] = {}
        self._validators: dict[str, dict[str, str]] = {}

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

    async def _collect_one(self, source: SourceConfig) -> tuple[list[RawItem], SourceHealth]:
        started = time.monotonic()
        try:
            handler = getattr(self, f"_fetch_{source.kind}")
            result = await handler(source)
            items = result.items if isinstance(result, PartialSourceResult) else result
            if not items:
                raise SourceCollectionError("source returned no usable items")
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
        host = urlsplit(url).hostname or ""
        semaphore = self._host_semaphores.setdefault(host, asyncio.Semaphore(self._per_host))
        async with semaphore:
            return await self.client.get(url, **kwargs)

    def _remember_validators(self, name: str, response: httpx.Response) -> None:
        validators = {}
        if response.headers.get("ETag"):
            validators["If-None-Match"] = response.headers["ETag"]
        if response.headers.get("Last-Modified"):
            validators["If-Modified-Since"] = response.headers["Last-Modified"]
        if validators:
            self._validators[name] = validators

    async def _fetch_rss(self, source: SourceConfig) -> list[RawItem]:
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
                    summary=_plain_text(
                        str(entry.get("summary") or entry.get("description") or "")
                    )[:10000],
                    published_at=_published(
                        entry.get("published_parsed")
                        or entry.get("published")
                        or entry.get("updated_parsed")
                        or entry.get("updated")
                    ),
                    discovered_at=now,
                    author=entry.get("author"),
                )
            )
        return items

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
        now = datetime.now(UTC)
        return [
            RawItem(
                **_source_fields(source),
                source_item_id=str(item["id"]),
                url=HttpUrl(
                    str(item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}")
                ),
                title=item["title"],
                summary="",
                published_at=datetime.fromtimestamp(item["time"], tz=UTC),
                discovered_at=now,
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
                summary=(release.get("body") or "")[:10000],
                published_at=_published(release.get("published_at")),
                discovered_at=now,
                author=(release.get("author") or {}).get("login"),
            )
            for release in response.json()
            if not release.get("draft")
        ]

    async def _fetch_arxiv(self, source: SourceConfig) -> list[RawItem]:
        response = await self._get(
            source,
            params={
                "search_query": "cat:cs.AI OR cat:cs.LG OR cat:cs.CL",
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
            items.append(
                RawItem(
                    **_source_fields(source),
                    source_item_id=str(paper_id),
                    url=HttpUrl(f"https://huggingface.co/papers/{paper_id}"),
                    title=title,
                    summary=paper.get("summary") or paper.get("abstract") or "",
                    published_at=_published(paper.get("publishedAt")),
                    discovered_at=now,
                    metrics={"upvotes": value.get("numUpvotes", 0)},
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
        published = _published(_meta(document, "article:published_time"))
        return RawItem(
            **_source_fields(source),
            source_item_id=url,
            url=HttpUrl(url),
            title=title,
            summary=_plain_text(summary or listing_text)[:10000],
            published_at=published or _published_from_text(listing_text),
            discovered_at=datetime.now(UTC),
        )

    async def _same_origin_get(self, source: SourceConfig, url: str) -> httpx.Response:
        source_origin = _origin(str(source.url))
        current = url
        for _ in range(6):
            if _origin(current) != source_origin:
                raise SourceCollectionError("html article left its configured HTTPS origin")
            response = await self._request(
                current,
                timeout=source.timeout_seconds,
                follow_redirects=False,
            )
            if not response.is_redirect:
                response.raise_for_status()
                return response
            location = response.headers.get("Location")
            if not location:
                raise SourceCollectionError("html article redirect has no location")
            current = urljoin(current, location)
        raise SourceCollectionError("html article exceeded redirect limit")


def _origin(value: str) -> tuple[str, str, int]:
    parts = urlsplit(value)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme.lower(), (parts.hostname or "").lower(), port
