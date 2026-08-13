import json
from datetime import date

import httpx

from ai_daily.publisher import GitHubPublisher
from ai_daily.site_trust import daily_marker


async def test_publish_creates_then_updates_same_issue() -> None:
    created = False
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created
        if request.url.path.endswith("/labels/Daily"):
            return httpx.Response(200, json={"name": "Daily"})
        if request.method == "GET":
            issues = []
            if created:
                issues = [
                    {
                        "number": 12,
                        "title": "2026-08-12",
                        "body": daily_marker(date(2026, 8, 12)),
                        "user": {"login": "github-actions[bot]"},
                        "labels": [{"name": "Daily"}],
                        "url": "https://api.github.com/repos/o/r/issues/12",
                        "html_url": "https://github.com/o/r/issues/12",
                    }
                ]
            return httpx.Response(200, json=issues)
        writes.append(request.method)
        created = True
        return httpx.Response(
            200 if request.method == "PATCH" else 201,
            json={
                "number": 12,
                "html_url": "https://github.com/o/r/issues/12",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = GitHubPublisher("o/r", "token", client)
    body = f"{daily_marker(date(2026, 8, 12))}\ncontent"
    first = await publisher.publish(date(2026, 8, 12), body)
    second = await publisher.publish(date(2026, 8, 12), body)
    assert first.issue_number == second.issue_number == 12
    assert writes == ["POST", "PATCH"]


async def test_publish_rejects_unmarked_body() -> None:
    publisher = GitHubPublisher("o/r", "token")
    try:
        await publisher.publish(date(2026, 8, 12), "content")
    except ValueError as error:
        assert "machine marker" in str(error)
    else:
        raise AssertionError("unmarked body was accepted")


async def test_publish_upgrades_an_existing_legacy_marker() -> None:
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels/Daily"):
            return httpx.Response(200, json={"name": "Daily"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 12,
                        "title": "2026-08-12",
                        "body": "<!-- ai-daily:2026-08-12:v1 -->",
                        "user": {"login": "github-actions[bot]"},
                        "labels": [{"name": "Daily"}],
                        "url": "https://api.github.com/repos/o/r/issues/12",
                        "html_url": "https://github.com/o/r/issues/12",
                    }
                ],
            )
        writes.append(request.method)
        return httpx.Response(
            200,
            json={"number": 12, "html_url": "https://github.com/o/r/issues/12"},
        )

    publisher = GitHubPublisher(
        "o/r", "token", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    body = f"{daily_marker(date(2026, 8, 12))}\nnew content"

    publication = await publisher.publish(date(2026, 8, 12), body)

    assert publication.issue_number == 12
    assert writes == ["PATCH"]


async def test_publish_ignores_existing_untrusted_issue() -> None:
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels/Daily"):
            return httpx.Response(200, json={"name": "Daily"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 10,
                        "title": "2026-08-12",
                        "body": "legacy body",
                        "user": {"login": "attacker"},
                        "labels": [],
                        "url": "https://api.github.com/repos/o/r/issues/10",
                        "html_url": "https://github.com/o/r/issues/10",
                    }
                ],
            )
        writes.append(request.method)
        return httpx.Response(
            201,
            json={"number": 11, "html_url": "https://github.com/o/r/issues/11"},
        )

    publisher = GitHubPublisher(
        "o/r", "token", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    body = f"{daily_marker(date(2026, 8, 12))}\ncontent"
    publication = await publisher.publish(date(2026, 8, 12), body)

    assert publication.issue_number == 11
    assert writes == ["POST"]


async def test_publish_does_not_overwrite_unmarked_owner_issue() -> None:
    writes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels/Daily"):
            return httpx.Response(200, json={"name": "Daily"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 10,
                        "title": "2026-08-12",
                        "body": "unrelated owner note",
                        "user": {"login": "o"},
                        "labels": [],
                        "url": "https://api.github.com/repos/o/r/issues/10",
                        "html_url": "https://github.com/o/r/issues/10",
                    }
                ],
            )
        writes.append(request.method)
        return httpx.Response(
            201,
            json={"number": 11, "html_url": "https://github.com/o/r/issues/11"},
        )

    publisher = GitHubPublisher(
        "o/r", "token", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    target = date(2026, 8, 12)

    publication = await publisher.publish(target, f"{daily_marker(target)}\ncontent")

    assert publication.issue_number == 11
    assert writes == ["POST"]


async def test_find_publication_paginates_for_old_daily_issues() -> None:
    target = date(2026, 1, 1)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        calls.append(page)
        if page == 1:
            return httpx.Response(
                200,
                json=[
                    {
                        "number": index,
                        "title": f"other-{index}",
                        "body": "",
                        "url": f"https://api.github.com/repos/o/r/issues/{index}",
                        "html_url": f"https://github.com/o/r/issues/{index}",
                    }
                    for index in range(100)
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "number": 101,
                    "title": target.isoformat(),
                    "body": "<!-- ai-daily:2026-01-01:v1 -->",
                    "user": {"login": "github-actions[bot]"},
                    "labels": [{"name": "Daily"}],
                    "url": "https://api.github.com/repos/o/r/issues/101",
                    "html_url": "https://github.com/o/r/issues/101",
                }
            ],
        )

    publisher = GitHubPublisher(
        "o/r", "token", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )

    publication = await publisher.find_publication(target)

    assert publication is not None
    assert publication.issue_number == 101
    assert calls == [1, 2]


async def test_closed_daily_is_not_verified_but_publish_reopens_it() -> None:
    writes: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels/Daily"):
            return httpx.Response(200, json={"name": "Daily"})
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 12,
                        "title": "2026-08-12",
                        "state": "closed",
                        "body": daily_marker(date(2026, 8, 12)),
                        "user": {"login": "github-actions[bot]"},
                        "labels": [{"name": "Daily"}],
                        "url": "https://api.github.com/repos/o/r/issues/12",
                        "html_url": "https://github.com/o/r/issues/12",
                    }
                ],
            )
        writes.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"number": 12, "html_url": "https://github.com/o/r/issues/12"},
        )

    publisher = GitHubPublisher(
        "o/r", "token", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    target = date(2026, 8, 12)

    assert await publisher.find_publication(target) is None
    publication = await publisher.publish(target, f"{daily_marker(target)}\ncontent")

    assert publication.issue_number == 12
    assert writes == [
        {
            "title": "2026-08-12",
            "body": f"{daily_marker(target)}\ncontent",
            "labels": ["Daily"],
            "state": "open",
        }
    ]
