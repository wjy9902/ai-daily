from __future__ import annotations

import asyncio
import calendar
import hashlib
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast

import feedparser
import httpx
from pydantic import HttpUrl

from ai_daily.models import RawItem, SourceConfig, SourceHealth


class SourceCollectionError(RuntimeError):
    pass


class SourceNotModified(RuntimeError):
    pass


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


class Collector:
    def __init__(self, client: httpx.AsyncClient | None = None, per_host: int = 5) -> None:
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": "ai-daily/0.2 (+https://wjy9902.github.io/ai-daily/)"},
            follow_redirects=True,
        )
        self._semaphore = asyncio.Semaphore(per_host)
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
            async with self._semaphore:
                handler = getattr(self, f"_fetch_{source.kind}")
                items = await handler(source)
            health = SourceHealth(
                source=source.name,
                tier=source.tier,
                status="ok",
                item_count=len(items),
                latency_ms=int((time.monotonic() - started) * 1000),
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
            response = await self.client.get(
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
                    source=source.name,
                    source_tier=source.tier,
                    source_item_id=identifier,
                    url=url,
                    title=str(title).strip(),
                    summary=str(entry.get("summary") or entry.get("description") or ""),
                    published_at=_published(
                        entry.get("published_parsed") or entry.get("published")
                    ),
                    discovered_at=now,
                    author=entry.get("author"),
                )
            )
        return items

    async def _fetch_hackernews(self, source: SourceConfig) -> list[RawItem]:
        base = str(source.url).rstrip("/")
        ids_response = await self.client.get(
            f"{base}/topstories.json", timeout=source.timeout_seconds
        )
        ids_response.raise_for_status()
        ids = ids_response.json()[: source.limit]

        async def fetch_item(item_id: int) -> dict[str, Any]:
            response = await self.client.get(
                f"{base}/item/{item_id}.json", timeout=source.timeout_seconds
            )
            response.raise_for_status()
            return cast(dict[str, Any], response.json())

        values = await asyncio.gather(*(fetch_item(item_id) for item_id in ids))
        now = datetime.now(UTC)
        return [
            RawItem(
                source=source.name,
                source_tier=source.tier,
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
                source=source.name,
                source_tier=source.tier,
                source_item_id=str(release["id"]),
                url=release["html_url"],
                title=release.get("name") or release["tag_name"],
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
                source=source.name,
                source_tier=source.tier,
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
                    source=source.name,
                    source_tier=source.tier,
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

    async def _fetch_html(self, source: SourceConfig) -> list[RawItem]:
        response = await self._get(source)
        text = " ".join(response.text.split())[:10000]
        digest = hashlib.sha256(response.content).hexdigest()
        return [
            RawItem(
                source=source.name,
                source_tier=source.tier,
                source_item_id=digest,
                url=source.url,
                title=source.name,
                summary=text,
                discovered_at=datetime.now(UTC),
            )
        ]
