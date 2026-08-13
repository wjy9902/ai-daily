from __future__ import annotations

import base64
import re
from collections.abc import Iterable
from typing import Protocol

DAILY_LABEL = "Daily"
DAILY_MARKER_RE = re.compile(r"<!-- ai-daily:\d{4}-\d{2}-\d{2}:v\d+ -->")
TRUSTED_BOT = "github-actions[bot]"
STORY_MARKER_RE = re.compile(r"<!-- ai-daily-story:([A-Za-z0-9_-]+) -->")


class UserLike(Protocol):
    login: str


class LabelLike(Protocol):
    name: str


class IssueLike(Protocol):
    user: UserLike
    labels: Iterable[LabelLike]
    body: str | None


def has_daily_marker(body: str | None, target_date: str | None = None) -> bool:
    match = DAILY_MARKER_RE.search(body or "")
    if match is None or target_date is None:
        return match is not None
    return match.group(0).startswith(f"<!-- ai-daily:{target_date}:")


def story_title_marker(title: str) -> str:
    encoded = base64.urlsafe_b64encode(title.encode()).decode().rstrip("=")
    return f"<!-- ai-daily-story:{encoded} -->"


def marked_story_titles(body: str) -> set[str]:
    titles: set[str] = set()
    for encoded in STORY_MARKER_RE.findall(body):
        padding = "=" * (-len(encoded) % 4)
        try:
            titles.add(base64.urlsafe_b64decode(encoded + padding).decode())
        except (ValueError, UnicodeDecodeError):
            continue
    return titles


def is_trusted_issue(issue: IssueLike, owner: str) -> bool:
    if issue.user.login == owner:
        return True
    labels = {label.name for label in issue.labels}
    return (
        issue.user.login == TRUSTED_BOT and DAILY_LABEL in labels and has_daily_marker(issue.body)
    )
