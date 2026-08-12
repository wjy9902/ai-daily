from datetime import date

import httpx

from ai_daily.assembler import marker
from ai_daily.publisher import GitHubPublisher


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
                        "body": marker(date(2026, 8, 12)),
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
    body = f"{marker(date(2026, 8, 12))}\ncontent"
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


async def test_publish_rejects_existing_unmarked_issue() -> None:
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
                        "url": "https://api.github.com/repos/o/r/issues/10",
                        "html_url": "https://github.com/o/r/issues/10",
                    }
                ],
            )
        raise AssertionError("publisher attempted to overwrite the legacy issue")

    publisher = GitHubPublisher(
        "o/r", "token", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    body = f"{marker(date(2026, 8, 12))}\ncontent"
    try:
        await publisher.publish(date(2026, 8, 12), body)
    except RuntimeError as error:
        assert "unmarked" in str(error)
    else:
        raise AssertionError("a duplicate issue would have been created")
