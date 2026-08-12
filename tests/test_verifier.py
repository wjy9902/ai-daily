from datetime import date

import httpx
import pytest

from ai_daily.verifier import PublicationNotVisible, verify_publication


def rss(link: str, title: str = "2026-08-12") -> bytes:
    return f"""<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>
    <item><title>{title}</title><link>{link}</link><guid>{link}</guid></item>
    </channel></rss>""".encode()


async def test_verify_requires_page_and_matching_latest_rss() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("rss.xml"):
            return httpx.Response(200, content=rss("https://site.test/issue-12/"))
        return httpx.Response(200, text="digest")

    result = await verify_publication(
        date(2026, 8, 12),
        "https://site.test",
        12,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert result.status == "verified"


async def test_verify_rejects_stale_rss() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("rss.xml"):
            return httpx.Response(200, content=rss("https://site.test/issue-11/"))
        return httpx.Response(200)

    with pytest.raises(PublicationNotVisible, match="latest"):
        await verify_publication(
            date(2026, 8, 12),
            "https://site.test",
            12,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
