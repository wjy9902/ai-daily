from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol

DAILY_LABEL = "Daily"
DAILY_MARKER_RE = re.compile(r"<!-- ai-daily:\d{4}-\d{2}-\d{2}:v\d+ -->")
TRUSTED_BOT = "github-actions[bot]"


class UserLike(Protocol):
    login: str


class LabelLike(Protocol):
    name: str


class IssueLike(Protocol):
    user: UserLike
    labels: Iterable[LabelLike]
    body: str | None


def is_trusted_issue(issue: IssueLike, owner: str) -> bool:
    if issue.user.login == owner:
        return True
    labels = {label.name for label in issue.labels}
    body = issue.body or ""
    return (
        issue.user.login == TRUSTED_BOT
        and DAILY_LABEL in labels
        and DAILY_MARKER_RE.search(body) is not None
    )
