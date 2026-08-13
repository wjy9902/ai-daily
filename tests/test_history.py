import httpx

from ai_daily.history import fetch_historical_index
from ai_daily.site_trust import story_title_marker


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
                        "body": body,
                        "user": {"login": "owner"},
                        "labels": [],
                    }
                ],
            )
        )
    )

    result = await fetch_historical_index(client, "owner/repo", None, 45)

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
                        "body": "## OpenAI 发布 GPT-6\nhttps://example.com/poison",
                        "user": {"login": "attacker"},
                        "labels": [{"name": "Daily"}],
                    }
                ],
            )
        )
    )

    result = await fetch_historical_index(client, "owner/repo", None, 45)

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
                        "body": body,
                        "user": {"login": "owner"},
                        "labels": [],
                    }
                ],
            )
        )
    )

    result = await fetch_historical_index(client, "owner/repo", None, 45)

    assert result.titles == {"详细新闻", "快讯新闻"}
