"""Verification answers "what level is live", not "did it work".

The pages served in these tests come from the real renderer, so a change that
stops the marker reaching the page fails here rather than in production.
"""

from __future__ import annotations

from datetime import date

import factories
import httpx
import pytest

from ai_daily.publication import DailyPublication, PublicationLevel
from ai_daily.render import render_daily, render_rss
from ai_daily.verifier import (
    PublicationNotVisible,
    daily_page_url,
    verify_publication,
)

SITE = "https://site.test"


def _client(
    *,
    page: str,
    feed: str,
    seen: list[httpx.URL] | None = None,
    page_status: int = 200,
    feed_status: int = 200,
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url)
        if request.url.path.endswith("rss.xml"):
            return httpx.Response(feed_status, content=feed.encode("utf-8"))
        return httpx.Response(page_status, text=page)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _live(
    publication: DailyPublication,
    *,
    feed_from: list[DailyPublication] | None = None,
    **kwargs: object,
) -> httpx.AsyncClient:
    """A site that serves exactly what the renderer produced."""

    return _client(
        page=render_daily(publication, SITE),
        feed=render_rss(feed_from or [publication], SITE),
        **kwargs,  # type: ignore[arg-type]
    )


async def test_verify_reports_the_level_the_live_site_serves() -> None:
    publication = factories.publication()

    result = await verify_publication(publication, SITE, _live(publication))

    assert result.level is PublicationLevel.L0
    assert result.target_date == publication.target_date
    assert result.marker == publication.marker
    assert result.page_url == daily_page_url(SITE, publication.target_date)
    assert result.rss_url == f"{SITE}/rss.xml"


@pytest.mark.parametrize(
    "level", [PublicationLevel.L1, PublicationLevel.L2A, PublicationLevel.L2B]
)
async def test_a_degraded_issue_verifies_at_its_own_level(level: PublicationLevel) -> None:
    publication = factories.publication(level=level)

    result = await verify_publication(publication, SITE, _live(publication))

    assert result.level is level


async def test_verify_rejects_a_page_without_the_expected_marker() -> None:
    publication = factories.publication()
    client = _client(page="stale digest", feed=render_rss([publication], SITE))

    with pytest.raises(PublicationNotVisible, match="expected content marker"):
        await verify_publication(publication, SITE, client)


async def test_verify_rejects_a_page_that_is_not_being_served() -> None:
    publication = factories.publication()

    with pytest.raises(PublicationNotVisible, match="page returned 404"):
        await verify_publication(publication, SITE, _live(publication, page_status=404))


async def test_verify_rejects_a_feed_whose_newest_entry_is_yesterdays_issue() -> None:
    yesterday = factories.publication(target_date=date(2026, 8, 12))
    today = factories.publication(target_date=date(2026, 8, 13))
    client = _client(page=render_daily(today, SITE), feed=render_rss([yesterday], SITE))

    with pytest.raises(PublicationNotVisible, match="RSS latest entry does not carry"):
        await verify_publication(today, SITE, client)


async def test_verify_rejects_a_feed_entry_pointing_at_another_page() -> None:
    publication = factories.publication()
    feed = render_rss([publication], SITE).replace(
        daily_page_url(SITE, publication.target_date), f"{SITE}/daily/2026-08-01/"
    )
    client = _client(page=render_daily(publication, SITE), feed=feed)

    with pytest.raises(PublicationNotVisible, match="does not point to today's page"):
        await verify_publication(publication, SITE, client)


async def test_verify_rejects_a_feed_entry_titled_for_another_day() -> None:
    publication = factories.publication()
    feed = render_rss([publication], SITE).replace(
        f"<title>AI 日报 {publication.target_date.isoformat()}</title>",
        "<title>AI 日报</title>",
    )
    client = _client(page=render_daily(publication, SITE), feed=feed)

    with pytest.raises(PublicationNotVisible, match="title does not contain the target date"):
        await verify_publication(publication, SITE, client)


async def test_verify_rejects_an_empty_feed() -> None:
    publication = factories.publication()
    client = _client(page=render_daily(publication, SITE), feed=render_rss([], SITE))

    with pytest.raises(PublicationNotVisible, match="RSS has no entries"):
        await verify_publication(publication, SITE, client)


async def test_verify_does_not_accept_a_broken_feed() -> None:
    publication = factories.publication()

    with pytest.raises(httpx.HTTPStatusError):
        await verify_publication(publication, SITE, _live(publication, feed_status=503))


async def test_verify_recomputes_the_marker_and_refuses_a_tampered_record() -> None:
    tampered = factories.publication().model_copy(update={"highlight": "被改写过的亮点"})
    seen: list[httpx.URL] = []
    client = _client(page="anything", feed="anything", seen=seen)

    with pytest.raises(PublicationNotVisible, match="does not match its own content"):
        await verify_publication(tampered, SITE, client)

    assert seen == []


async def test_every_request_is_cache_busted_with_a_fresh_token() -> None:
    publication = factories.publication()
    seen: list[httpx.URL] = []
    client = _live(publication, seen=seen)

    await verify_publication(publication, SITE, client)
    await verify_publication(publication, SITE, client)

    assert len(seen) == 4
    tokens = [url.params["_"] for url in seen]
    assert all(tokens)
    assert len(set(tokens)) == 4
    assert {url.path for url in seen} == {
        f"/daily/{publication.target_date.isoformat()}/",
        "/rss.xml",
    }
