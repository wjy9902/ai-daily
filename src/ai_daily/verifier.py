from __future__ import annotations

from datetime import date

import feedparser
import httpx
from pydantic import HttpUrl

from ai_daily.models import Publication


class PublicationNotVisible(RuntimeError):
    pass


async def verify_publication(
    target_date: date,
    site_base_url: str,
    issue_number: int,
    expected_marker: str,
    client: httpx.AsyncClient | None = None,
) -> Publication:
    http = client or httpx.AsyncClient(follow_redirects=True)
    page_url = f"{site_base_url.rstrip('/')}/issue-{issue_number}/"
    rss_url = f"{site_base_url.rstrip('/')}/rss.xml"
    page, rss = await http.get(page_url, timeout=20), await http.get(rss_url, timeout=20)
    if page.status_code != 200:
        raise PublicationNotVisible(f"page returned {page.status_code}")
    rss.raise_for_status()
    if expected_marker not in page.text:
        raise PublicationNotVisible("page does not contain the expected content marker")
    feed = feedparser.parse(rss.content)
    if not feed.entries:
        raise PublicationNotVisible("RSS has no entries")
    latest = feed.entries[0]
    latest_content = "\n".join(
        str(value)
        for value in (
            latest.get("summary", ""),
            *(part.get("value", "") for part in latest.get("content", [])),
        )
    )
    if expected_marker not in latest_content:
        raise PublicationNotVisible("RSS latest entry does not contain the expected content marker")
    if latest.get("link", "").rstrip("/") != page_url.rstrip("/"):
        raise PublicationNotVisible("RSS latest entry does not point to today's page")
    if target_date.isoformat() not in latest.get("title", ""):
        raise PublicationNotVisible("RSS latest title does not contain the target date")
    return Publication(
        target_date=target_date,
        issue_number=issue_number,
        page_url=HttpUrl(page_url),
        rss_url=HttpUrl(rss_url),
        status="verified",
        marker=expected_marker,
    )
