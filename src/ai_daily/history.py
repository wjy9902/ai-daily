from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from ai_daily.site_trust import (
    is_trusted_issue_payload,
    marked_story_titles,
)

URL_RE = re.compile(r'https?://[^\s)>"\]]+')
HEADING_RE = re.compile(r"^#{2,3}\s+(.+)$", re.MULTILINE)
HTML_RE = re.compile(r"<[^>]+>")
DATE_TAG_RE = re.compile(r"`\d{4}-\d{2}-\d{2}`")
NON_STORY_HEADINGS = {
    "今日重点",
    "值得关注",
    "快讯",
    "速览",
    "速览目录",
    "今日必读",
    "编辑观点",
    "模型与平台",
    "前沿研究",
    "值得试的项目",
    "行业动态",
    "国内 AI",
}


@dataclass(frozen=True)
class HistoricalIndex:
    urls: set[str]
    titles: set[str]


def _story_titles(body: str) -> set[str]:
    marked = marked_story_titles(body)
    if marked:
        return marked
    titles = set()
    for raw in HEADING_RE.findall(body):
        value = HTML_RE.sub("", raw)
        value = DATE_TAG_RE.sub("", value).strip(" #🔥📌\t")
        value = re.sub(r"^(?:今日重点|值得关注)\s*", "", value)
        if value and value not in NON_STORY_HEADINGS:
            titles.add(value)
    return titles


async def fetch_historical_index(
    client: httpx.AsyncClient,
    repository: str,
    token: str | None,
    days: int,
    before_date: date,
) -> HistoricalIndex:
    first_date = before_date - timedelta(days=days)
    since = (
        datetime(
            first_date.year,
            first_date.month,
            first_date.day,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        .astimezone(UTC)
        .isoformat()
    )
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    issues = await _fetch_issues(client, repository, since, headers)
    urls: set[str] = set()
    titles: set[str] = set()
    for issue in issues:
        if "pull_request" in issue:
            continue
        issue_date = _issue_date(issue)
        if issue_date is None or not first_date <= issue_date < before_date:
            continue
        if not _trusted_history_issue(issue, repository, issue_date):
            continue
        raw_body = issue.get("body")
        body = raw_body if isinstance(raw_body, str) else ""
        urls.update(URL_RE.findall(body))
        titles.update(_story_titles(body))
    return HistoricalIndex(urls=urls, titles=titles)


def _issue_date(issue: dict[str, object]) -> date | None:
    raw_title = issue.get("title")
    if not isinstance(raw_title, str):
        return None
    try:
        return date.fromisoformat(raw_title)
    except ValueError:
        return None


def _trusted_history_issue(issue: dict[str, object], repository: str, issue_date: date) -> bool:
    owner = repository.split("/", 1)[0]
    return is_trusted_issue_payload(issue, owner, issue_date.isoformat())


async def _fetch_issues(
    client: httpx.AsyncClient,
    repository: str,
    since: str,
    headers: dict[str, str],
) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    page = 1
    while True:
        response = await client.get(
            f"https://api.github.com/repos/{repository}/issues",
            headers=headers,
            params={"state": "all", "since": since, "per_page": 100, "page": page},
            timeout=20,
        )
        response.raise_for_status()
        values = response.json()
        issues.extend(values)
        if len(values) < 100:
            return issues
        page += 1
