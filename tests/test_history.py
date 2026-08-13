from datetime import date

import httpx
import pytest

from ai_daily.history import fetch_historical_index
from ai_daily.site_trust import daily_marker, story_title_marker


async def test_history_indexes_story_titles_and_urls() -> None:
    body = """# AI 日报

## 速览目录

## <span class="story-tier">今日重点</span> Claude 发布新模型 `2026-08-12` 🔥

**来源：** [Anthropic](https://www.anthropic.com/news/model)

## 编辑观点
"""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "title": "2026-08-12",
                        "body": body,
                        "user": {"login": "owner"},
                        "labels": [],
                    }
                ],
            )
        )
    )

    result = await fetch_historical_index(client, "owner/repo", None, 45, date(2026, 8, 13))

    assert result.urls == {"https://www.anthropic.com/news/model"}
    assert result.titles == {"Claude 发布新模型"}


async def test_history_ignores_untrusted_issue_content() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {
                        "number": 2,
                        "title": "2026-08-12",
                        "body": "## OpenAI 发布 GPT-6\nhttps://example.com/poison",
                        "user": {"login": "attacker"},
                        "labels": [{"name": "Daily"}],
                    }
                ],
            )
        )
    )

    result = await fetch_historical_index(client, "owner/repo", None, 45, date(2026, 8, 13))

    assert result.urls == set()
    assert result.titles == set()


async def test_history_reads_machine_markers_for_detailed_and_brief_stories() -> None:
    body = "\n".join(
        [
            story_title_marker("详细新闻"),
            "## 页面栏目文案可以改变",
            story_title_marker("快讯新闻"),
            '<li id="story-brief"><strong>快讯新闻</strong></li>',
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {
                        "number": 3,
                        "title": "2026-08-12",
                        "body": body,
                        "user": {"login": "owner"},
                        "labels": [],
                    }
                ],
            )
        )
    )

    result = await fetch_historical_index(client, "owner/repo", None, 45, date(2026, 8, 13))

    assert result.titles == {"详细新闻", "快讯新闻"}


async def test_history_paginates_and_trusts_marked_daily_bot_issue() -> None:
    pages: list[int] = []
    body = "\n".join(
        [
            daily_marker(date(2026, 8, 12)),
            story_title_marker("分页后的可信新闻"),
            "https://example.com/trusted-story",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        if page == 1:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": index,
                        "title": "2026-08-01",
                        "body": "",
                        "user": {"login": "attacker"},
                        "labels": [],
                    }
                    for index in range(100)
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "number": 101,
                    "title": "2026-08-12",
                    "body": body,
                    "user": {"login": "github-actions[bot]"},
                    "labels": [{"name": "Daily"}],
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await fetch_historical_index(client, "owner/repo", None, 45, date(2026, 8, 13))

    assert pages == [1, 2]
    assert result.urls == {"https://example.com/trusted-story"}
    assert result.titles == {"分页后的可信新闻"}


@pytest.mark.parametrize(
    ("labels", "body"),
    [
        ([], daily_marker(date(2026, 8, 12))),
        ([{"name": "Daily"}], "missing marker"),
    ],
)
async def test_history_rejects_bot_issue_without_complete_trust_markers(
    labels: list[dict[str, str]], body: str
) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "title": "2026-08-12",
                        "body": f"{body}\nhttps://example.com/poison",
                        "user": {"login": "github-actions[bot]"},
                        "labels": labels,
                    }
                ],
            )
        )
    )

    result = await fetch_historical_index(client, "owner/repo", None, 45, date(2026, 8, 13))

    assert result.urls == set()


async def test_history_excludes_same_day_and_future_daily_issues() -> None:
    issues = [
        {
            "number": index,
            "title": issue_date,
            "body": f"https://example.com/{issue_date}",
            "user": {"login": "owner"},
            "labels": [],
        }
        for index, issue_date in enumerate(
            ("2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"), start=1
        )
    ]
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=issues))
    )

    result = await fetch_historical_index(client, "owner/repo", None, 45, date(2026, 8, 13))

    assert result.urls == {
        "https://example.com/2026-08-11",
        "https://example.com/2026-08-12",
    }


async def test_history_uses_beijing_midnight_for_github_since() -> None:
    seen_since = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_since
        seen_since = request.url.params["since"]
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await fetch_historical_index(client, "owner/repo", None, 45, date(2026, 8, 13))

    assert seen_since == "2026-06-28T16:00:00+00:00"


async def test_history_rejects_bot_marker_for_a_different_date() -> None:
    issue = {
        "number": 1,
        "title": "2026-08-12",
        "body": f"{daily_marker(date(2026, 8, 11))}\nhttps://example.com/wrong-date",
        "user": {"login": "github-actions[bot]"},
        "labels": [{"name": "Daily"}],
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[issue]))
    )

    result = await fetch_historical_index(client, "owner/repo", None, 45, date(2026, 8, 13))

    assert result.urls == set()
