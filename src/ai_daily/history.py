from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
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


#: How far back a *headline* blocks a story, as opposed to a URL.
#:
#: URL dedupe can safely span the whole history window: the same article is
#: the same article a month later. Headline dedupe cannot, because running
#: stories legitimately recur under near-identical headlines — a quota reset,
#: a staged rollout reaching a new tier, a price changing again. The benchmark
#: digest follows those threads for days and that is much of why it reads as
#: current; a 45-day headline block silently made every follow-up
#: unpublishable. Three days still catches the real duplicate: the same story
#: re-reported by a slower outlet a day or two behind.
TITLE_WINDOW_DAYS = 3


@dataclass(frozen=True)
class HistoricalIndex:
    urls: set[str]
    titles: set[str]


@dataclass(frozen=True)
class PublishedItem:
    """One story we already ran, as the planner needs to see it."""

    target_date: date
    category: str
    headline: str


def recent_published_items(
    published_dir: Path,
    before_date: date,
    days: int = TITLE_WINDOW_DAYS,
    limit: int = 80,
) -> list[PublishedItem]:
    """What we published over the last few days, newest first.

    remove_historical can only delete candidates. It cannot stop the planner
    reaching for a different write-up of the same announcement, because the
    planner has never been shown what already ran - it sees a pile of
    candidates and chooses. That is why 2026-08-31 led with Tencent's Hy4
    release a day after 2026-08-30 did, and why deleting that one candidate
    just moved the repeat onto another story from the same cluster.

    Whether a recurring story is a duplicate or a new development is a
    judgement ("此前的传闻被官方确认" is a new item, the same release announced
    twice is not), and the planner is already instructed to make it. It just
    needs the evidence to make it on.
    """

    first_date = before_date - timedelta(days=days)
    items: list[PublishedItem] = []
    if not published_dir.exists():
        return items
    for path in sorted(published_dir.glob("*.json"), reverse=True):
        try:
            issue_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if not first_date <= issue_date < before_date:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for group in ("details", "briefs"):
            for entry in payload.get(group, []):
                headline = entry.get("headline")
                if isinstance(headline, str) and headline.strip():
                    items.append(
                        PublishedItem(
                            target_date=issue_date,
                            category=str(entry.get("category") or ""),
                            headline=headline,
                        )
                    )
    return items[:limit]


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


def local_historical_index(
    published_dir: Path,
    days: int,
    before_date: date,
    title_days: int = TITLE_WINDOW_DAYS,
) -> HistoricalIndex:
    """Build the dedupe index from locally published issues.

    This replaces the GitHub API lookup. Reading our own records removes a
    network dependency from the critical path: an API hiccup used to mean the
    index came back empty, which silently let yesterday's stories run again.

    ``days`` bounds the URL index and ``title_days`` the headline index; see
    :data:`TITLE_WINDOW_DAYS` for why the two differ.
    """

    first_date = before_date - timedelta(days=days)
    first_title_date = before_date - timedelta(days=min(title_days, days))
    urls: set[str] = set()
    titles: set[str] = set()
    if not published_dir.exists():
        return HistoricalIndex(urls=urls, titles=titles)

    for path in published_dir.glob("*.json"):
        try:
            issue_date = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if not first_date <= issue_date < before_date:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        index_titles = first_title_date <= issue_date
        for group in ("details", "briefs"):
            for entry in payload.get(group, []):
                headline = entry.get("headline")
                if index_titles and isinstance(headline, str):
                    titles.add(headline)
                for source in entry.get("sources", []):
                    url = source.get("url")
                    if isinstance(url, str):
                        urls.add(url)
    return HistoricalIndex(urls=urls, titles=titles)


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
