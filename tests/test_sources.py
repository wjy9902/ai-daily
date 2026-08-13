import httpx
import pytest

from ai_daily.models import SourceConfig, SourceTier
from ai_daily.sources import Collector


def test_collector_rejects_zero_per_host_concurrency() -> None:
    with pytest.raises(ValueError, match="per_host"):
        Collector(per_host=0)


async def test_rss_adapter_parses_valid_entries() -> None:
    body = b"""<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>
    <item><guid>1</guid><title>New model</title><link>https://example.com/a</link>
    <description>Details</description><pubDate>Wed, 12 Aug 2026 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    source = SourceConfig(
        name="feed",
        display_name="Official Feed",
        kind="rss",
        url="https://example.com/rss",
        tier=SourceTier.A,
    )
    items, health = await Collector(client).collect([source])
    assert [item.title for item in items] == ["New model"]
    assert items[0].source_label == "Official Feed"
    assert health[0].status == "ok"


async def test_source_failure_is_visible_not_silent() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(503, headers={"Retry-After": "0"})
        )
    )
    source = SourceConfig(name="feed", kind="rss", url="https://example.com/rss", tier=SourceTier.A)
    items, health = await Collector(client).collect([source])
    assert items == []
    assert health[0].status == "failed"
    assert health[0].error == "HTTPStatusError"


async def test_empty_source_is_reported_as_failed() -> None:
    body = b"<?xml version='1.0'?><rss version='2.0'><channel><title>T</title></channel></rss>"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    source = SourceConfig(name="feed", kind="rss", url="https://example.com/rss", tier=SourceTier.A)

    items, health = await Collector(client).collect([source])

    assert items == []
    assert health[0].status == "failed"
    assert health[0].error == "SourceCollectionError"


async def test_conditional_request_reports_not_modified() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"ETag": '"v1"'},
                content=b"<rss version='2.0'><channel><title>T</title></channel></rss>",
            )
        assert request.headers["If-None-Match"] == '"v1"'
        return httpx.Response(304)

    collector = Collector(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    source = SourceConfig(name="feed", kind="rss", url="https://example.com/rss", tier=SourceTier.A)
    await collector.collect([source])
    _, health = await collector.collect([source])
    assert health[0].status == "not_modified"


async def test_hackernews_adapter_uses_public_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("topstories.json"):
            return httpx.Response(200, json=[1])
        return httpx.Response(
            200,
            json={"id": 1, "type": "story", "title": "AI tool", "time": 1786492800, "score": 100},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    source = SourceConfig(
        name="hn", kind="hackernews", url="https://hacker-news.firebaseio.com/v0", tier=SourceTier.B
    )
    items, _ = await Collector(client).collect([source])
    assert str(items[0].url).startswith("https://news.ycombinator.com/item?id=1")


async def test_html_index_fetches_first_party_article_metadata() -> None:
    listing = b"""<html><body><a href='/news/new-model'>
    Product Aug 12, 2026 Introducing Model Five A major model update.</a></body></html>"""
    article = b"""<html><head>
    <meta property='og:title' content='Introducing Model Five'>
    <meta property='og:description' content='A major model update for coding agents.'>
    </head><body></body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=article if request.url.path != "/news" else listing)

    source = SourceConfig(
        name="lab-news",
        display_name="Official Lab",
        kind="html_index",
        url="https://example.com/news",
        link_pattern=r"^https://example\.com/news/[^/]+$",
        tier=SourceTier.A,
    )
    items, health = await Collector(
        httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ).collect([source])

    assert health[0].status == "ok"
    assert items[0].title == "Introducing Model Five"
    assert items[0].summary == "A major model update for coding agents."
    assert items[0].published_at is not None


async def test_html_index_rejects_cross_origin_redirects() -> None:
    listing = b"<a href='/news/new-model'>Aug 12, 2026 New model</a>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/news":
            return httpx.Response(200, content=listing)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})

    source = SourceConfig(
        name="lab-news",
        kind="html_index",
        url="https://example.com/news",
        link_pattern=r"^https://example\.com/news/[^/]+$",
        tier=SourceTier.A,
    )

    items, health = await Collector(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    ).collect([source])

    assert items == []
    assert health[0].status == "failed"
    assert health[0].error == "SourceCollectionError"


async def test_html_index_reports_partial_success_when_one_article_fails() -> None:
    listing = b"""<a href='/news/working'>Working article</a>
    <a href='/news/broken'>Broken article</a>"""
    article = b"""<html><head><meta property='og:title' content='Working AI model'>
    <meta property='og:description' content='A valid article.'></head></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/news":
            return httpx.Response(200, content=listing)
        if request.url.path == "/news/broken":
            return httpx.Response(503)
        return httpx.Response(200, content=article)

    source = SourceConfig(
        name="lab-news",
        kind="html_index",
        url="https://example.com/news",
        link_pattern=r"^https://example\.com/news/[^/]+$",
        tier=SourceTier.A,
    )
    items, health = await Collector(
        httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ).collect([source])

    assert [item.title for item in items] == ["Working AI model"]
    assert health[0].status == "partial"
    assert health[0].error == "1 linked article(s) failed"
