from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx

URL_RE = re.compile(r'https?://[^\s)>"\]]+')


async def fetch_historical_urls(
    client: httpx.AsyncClient,
    repository: str,
    token: str | None,
    days: int,
) -> set[str]:
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await client.get(
        f"https://api.github.com/repos/{repository}/issues",
        headers=headers,
        params={"state": "all", "since": since, "per_page": 100},
        timeout=20,
    )
    response.raise_for_status()
    urls: set[str] = set()
    for issue in response.json():
        if "pull_request" in issue:
            continue
        urls.update(URL_RE.findall(issue.get("body") or ""))
    return urls
