import httpx

from ai_daily.models import SourceConfig, SourceTier
from ai_daily.sources import Collector


async def test_rss_adapter_parses_valid_entries() -> None:
    body = b"""<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>
    <item><guid>1</guid><title>New model</title><link>https://example.com/a</link>
    <description>Details</description><pubDate>Wed, 12 Aug 2026 00:00:00 GMT</pubDate></item>
    </channel></rss>"""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    source = SourceConfig(name="feed", kind="rss", url="https://example.com/rss", tier=SourceTier.A)
    items, health = await Collector(client).collect([source])
    assert [item.title for item in items] == ["New model"]
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
