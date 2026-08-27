from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from pydantic import HttpUrl

from ai_daily.models import Publication
from ai_daily.site_trust import (
    has_daily_marker,
    is_trusted_issue_payload,
    verified_daily_marker,
)


class GitHubPublisher:
    def __init__(
        self, repository: str, token: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self.repository = repository
        self.client = client or httpx.AsyncClient()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def publish(self, target_date: date, body: str) -> Publication:
        expected_marker = verified_daily_marker(body, target_date)
        if expected_marker is None:
            raise ValueError("publication body is missing a valid content marker")
        await self._ensure_daily_label()
        issue = await self._find(target_date, include_closed=True)
        payload = {"title": target_date.isoformat(), "body": body, "labels": ["Daily"]}
        try:
            response = await self._write_issue(issue, payload)
            response.raise_for_status()
            value = response.json()
        except httpx.TransportError:
            value = await self._reconcile_write(target_date, body)
        self._validate_published_issue(value, target_date, body)
        return Publication(
            target_date=target_date,
            issue_number=value["number"],
            issue_url=HttpUrl(str(value["html_url"])),
            status="issue_published",
            marker=expected_marker,
        )

    async def _write_issue(
        self, issue: dict[str, Any] | None, payload: dict[str, Any]
    ) -> httpx.Response:
        if issue:
            payload["state"] = "open"
            return await self.client.patch(
                str(issue["url"]), headers=self.headers, json=payload, timeout=20
            )
        return await self.client.post(
            f"https://api.github.com/repos/{self.repository}/issues",
            headers=self.headers,
            json=payload,
            timeout=20,
        )

    async def _reconcile_write(self, target_date: date, body: str) -> dict[str, Any]:
        issue = await self._find(target_date, include_closed=False)
        if issue is None or issue.get("body") != body:
            raise RuntimeError("publication write could not be confirmed")
        return issue

    @staticmethod
    def _validate_published_issue(value: dict[str, Any], target_date: date, body: str) -> None:
        if value.get("title") != target_date.isoformat() or value.get("body") != body:
            raise RuntimeError("GitHub returned an unconfirmed publication body")

    async def _ensure_daily_label(self) -> None:
        label_url = f"https://api.github.com/repos/{self.repository}/labels/Daily"
        response = await self.client.get(label_url, headers=self.headers, timeout=20)
        if response.status_code == 200:
            return
        if response.status_code != 404:
            response.raise_for_status()
        created = await self.client.post(
            f"https://api.github.com/repos/{self.repository}/labels",
            headers=self.headers,
            json={"name": "Daily", "color": "1d76db", "description": "Automated AI daily"},
            timeout=20,
        )
        created.raise_for_status()

    async def find_publication(self, target_date: date) -> Publication | None:
        issue = await self._find(target_date, include_closed=False)
        if not issue:
            return None
        body = issue.get("body")
        expected_marker = verified_daily_marker(
            body if isinstance(body, str) else None, target_date
        )
        if expected_marker is None:
            return None
        return Publication(
            target_date=target_date,
            issue_number=int(str(issue["number"])),
            issue_url=HttpUrl(str(issue["html_url"])),
            status="issue_published",
            marker=expected_marker,
        )

    async def _find(self, target_date: date, *, include_closed: bool) -> dict[str, Any] | None:
        title_matches = [
            issue
            for issue in await self._issues()
            if "pull_request" not in issue and issue.get("title") == target_date.isoformat()
        ]
        if not include_closed:
            title_matches = [
                issue for issue in title_matches if issue.get("state", "open") == "open"
            ]
        matches = [issue for issue in title_matches if self._is_trusted_daily(issue, target_date)]
        if len(matches) > 1:
            raise RuntimeError("multiple daily issues exist for the same date")
        owner = self.repository.split("/", 1)[0]
        owner_conflicts = [
            issue
            for issue in title_matches
            if issue.get("user", {}).get("login") == owner and issue not in matches
        ]
        if owner_conflicts:
            raise RuntimeError("unmarked owner issue conflicts with the daily publication")
        return matches[0] if matches else None

    def _is_trusted_daily(self, issue: dict[str, Any], target_date: date) -> bool:
        body = issue.get("body")
        target = target_date.isoformat()
        return has_daily_marker(
            body if isinstance(body, str) else None, target
        ) and is_trusted_issue_payload(
            issue,
            self.repository.split("/", 1)[0],
            target,
        )

    async def _issues(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        page = 1
        while True:
            response = await self.client.get(
                f"https://api.github.com/repos/{self.repository}/issues",
                headers=self.headers,
                params={"state": "all", "per_page": 100, "page": page},
                timeout=20,
            )
            response.raise_for_status()
            values = response.json()
            issues.extend(values)
            if len(values) < 100:
                return issues
            page += 1
