"""Check that what is on disk is actually being served, and at what quality.

Verification returns the *level* that is live, not a yes/no. A degraded issue
is a real publication, so a boolean would report success and stop the retry
windows from ever upgrading it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

import feedparser
import httpx

from ai_daily.publication import DailyPublication, PublicationLevel


class PublicationNotVisible(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedPublication:
    target_date: date
    level: PublicationLevel
    marker: str
    page_url: str
    rss_url: str


def daily_page_url(site_base_url: str, target_date: date) -> str:
    return f"{site_base_url.rstrip('/')}/daily/{target_date.isoformat()}/"


def _cache_busted(url: str) -> str:
    """Defeat the site's five-minute cache header.

    Without this a check right after a swap can be answered from cache with the
    previous release, which reads as a failed publish and triggers a pointless
    rebuild.
    """

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}_={uuid.uuid4().hex}"


async def verify_publication(
    publication: DailyPublication,
    site_base_url: str,
    client: httpx.AsyncClient | None = None,
) -> VerifiedPublication:
    """Confirm the live site serves exactly this publication."""

    expected_marker = publication.compute_marker()
    if expected_marker != publication.marker:
        raise PublicationNotVisible("publication marker does not match its own content")

    owns_client = client is None
    http = client or httpx.AsyncClient(follow_redirects=True)
    page_url = daily_page_url(site_base_url, publication.target_date)
    rss_url = f"{site_base_url.rstrip('/')}/rss.xml"
    try:
        page = await http.get(_cache_busted(page_url), timeout=20)
        rss = await http.get(_cache_busted(rss_url), timeout=20)
    finally:
        if owns_client:
            await http.aclose()

    if page.status_code != 200:
        raise PublicationNotVisible(f"page returned {page.status_code}")
    rss.raise_for_status()
    if expected_marker not in page.text:
        raise PublicationNotVisible("page does not carry the expected content marker")

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
    # Ordering is checked, not just membership: an entry that exists but is not
    # first means subscribers are being shown a stale issue at the top.
    if expected_marker not in latest_content:
        raise PublicationNotVisible("RSS latest entry does not carry the expected marker")
    if latest.get("link", "").rstrip("/") != page_url.rstrip("/"):
        raise PublicationNotVisible("RSS latest entry does not point to today's page")
    if publication.target_date.isoformat() not in latest.get("title", ""):
        raise PublicationNotVisible("RSS latest title does not contain the target date")

    return VerifiedPublication(
        target_date=publication.target_date,
        level=publication.level,
        marker=expected_marker,
        page_url=page_url,
        rss_url=rss_url,
    )
