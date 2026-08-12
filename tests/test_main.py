import runpy
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import feedparser

generate_rss_feed = cast(Callable[..., None], runpy.run_path("main.py")["generate_rss_feed"])


class FakeRepo:
    def __init__(self, issues: list[SimpleNamespace]) -> None:
        self.issues = issues

    def get_issues(self, **kwargs: Any) -> list[SimpleNamespace]:
        return self.issues


def _issue(number: int, created_at: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        title=f"2026-08-{number:02d}",
        body="digest",
        html_url=f"https://github.com/wjy9902/ai-daily/issues/{number}",
        created_at=created_at,
        updated_at=created_at,
        labels=[],
        user=SimpleNamespace(login="wjy9902"),
    )


def test_rss_emits_newest_issue_first(tmp_path) -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    issues = [_issue(12, now), _issue(11, now - timedelta(days=1))]
    rss_path = tmp_path / "rss.xml"
    generate_rss_feed(FakeRepo(issues), str(rss_path), "wjy9902")
    feed = feedparser.parse(rss_path.read_bytes())
    assert feed.entries[0].title == "2026-08-12"
    assert feed.entries[0].link.endswith("/issue-12/")
