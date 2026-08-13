from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Iterable
from datetime import date
from typing import Protocol

DAILY_LABEL = "Daily"
DAILY_MARKER_RE = re.compile(
    r"<!-- ai-daily:(\d{4}-\d{2}-\d{2}):v\d+(?::sha256=([a-f0-9]{64}))? -->"
)
SIGNED_MARKER_VERSION = "v3"
TRUSTED_BOT = "github-actions[bot]"
STORY_MARKER_RE = re.compile(r"<!-- ai-daily-story:([A-Za-z0-9_-]+) -->")
MARKER_VERSION = "v2"


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
    return match.group(1) == target_date


def daily_marker(target_date: date) -> str:
    return f"<!-- ai-daily:{target_date.isoformat()}:{MARKER_VERSION} -->"


def signed_daily_body(target_date: date, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker = (
        f"<!-- ai-daily:{target_date.isoformat()}:{SIGNED_MARKER_VERSION}:"
        f"sha256={digest} -->"
    )
    return f"{marker}\n{content}"


def verified_daily_marker(body: str | None, target_date: date) -> str | None:
    value = body or ""
    match = DAILY_MARKER_RE.match(value)
    if match is None or match.group(1) != target_date.isoformat() or match.group(2) is None:
        return None
    marker = match.group(0)
    if not value.startswith(f"{marker}\n"):
        return None
    content = value[len(marker) + 1 :]
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return marker if digest == match.group(2) else None


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


def is_trusted_issue_payload(
    issue: dict[str, object], owner: str, target_date: str | None = None
) -> bool:
    user = issue.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    if author == owner:
        return True
    raw_labels = issue.get("labels")
    labels = raw_labels if isinstance(raw_labels, list) else []
    label_names = {label.get("name") for label in labels if isinstance(label, dict)}
    body = issue.get("body")
    return (
        author == TRUSTED_BOT
        and DAILY_LABEL in label_names
        and has_daily_marker(body if isinstance(body, str) else None, target_date)
    )
