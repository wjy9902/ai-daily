from datetime import date
from pathlib import Path

import httpx
import pytest

import factories
from ai_daily.history import fetch_historical_index, local_historical_index
from ai_daily.publication import DailyPublication
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


# ------------------------------------------------- the local dedupe index


def _publish(published: Path, target_date: date, record: DailyPublication | None = None) -> None:
    """Drop one published record where the local index will find it."""

    published.mkdir(parents=True, exist_ok=True)
    issue = record or factories.publication(target_date=target_date)
    (published / f"{target_date.isoformat()}.json").write_text(
        issue.model_dump_json(indent=2), encoding="utf-8"
    )


def test_local_index_reads_titles_and_source_urls_from_published_records(
    tmp_path: Path,
) -> None:
    published = tmp_path / "published"
    _publish(published, date(2026, 8, 12))

    index = local_historical_index(published, 45, date(2026, 8, 13))

    assert index.titles == {"详报标题 0", "快讯标题 1"}
    assert index.urls == {
        "https://example.test/story-0",
        "https://example.test/story-1",
    }


def test_local_index_covers_every_day_inside_the_window(tmp_path: Path) -> None:
    published = tmp_path / "published"
    for day in (date(2026, 7, 1), date(2026, 8, 1), date(2026, 8, 12)):
        _publish(
            published,
            day,
            factories.publication(
                target_date=day,
                details=[],
                briefs=[factories.brief_card(day.day, headline=f"{day.isoformat()} 的头条")],
            ),
        )

    index = local_historical_index(published, 45, date(2026, 8, 13))

    assert index.titles == {
        "2026-07-01 的头条",
        "2026-08-01 的头条",
        "2026-08-12 的头条",
    }


@pytest.mark.parametrize(
    "outside",
    [date(2026, 6, 28), date(2026, 8, 13), date(2026, 8, 14)],
)
def test_local_index_ignores_records_outside_the_window(tmp_path: Path, outside: date) -> None:
    published = tmp_path / "published"
    _publish(published, outside)

    index = local_historical_index(published, 45, date(2026, 8, 13))

    assert index.titles == set()
    assert index.urls == set()


def test_local_index_ignores_corrupt_and_misnamed_files(tmp_path: Path) -> None:
    published = tmp_path / "published"
    _publish(published, date(2026, 8, 12))
    (published / "2026-08-11.json").write_text('{"details": [', encoding="utf-8")
    (published / "notes.json").write_text("{}", encoding="utf-8")
    (published / "2026-13-99.json").write_text("{}", encoding="utf-8")

    index = local_historical_index(published, 45, date(2026, 8, 13))

    assert index.titles == {"详报标题 0", "快讯标题 1"}


def test_local_index_is_empty_before_the_first_issue(tmp_path: Path) -> None:
    index = local_historical_index(tmp_path / "published", 45, date(2026, 8, 13))

    assert index.titles == set()
    assert index.urls == set()
