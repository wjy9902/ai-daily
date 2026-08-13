from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from pydantic import HttpUrl

from ai_daily.assembler import marker
from ai_daily.models import Publication
from ai_daily.site_trust import DAILY_LABEL, TRUSTED_BOT, has_daily_marker


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
        expected_marker = marker(target_date)
        if expected_marker not in body:
            raise ValueError("publication body is missing its machine marker")
        await self._ensure_daily_label()
        issue = await self._find(target_date)
        payload = {"title": target_date.isoformat(), "body": body, "labels": ["Daily"]}
        if issue:
            response = await self.client.patch(
                str(issue["url"]), headers=self.headers, json=payload, timeout=20
            )
        else:
            response = await self.client.post(
                f"https://api.github.com/repos/{self.repository}/issues",
                headers=self.headers,
                json=payload,
                timeout=20,
            )
        response.raise_for_status()
        value = response.json()
        return Publication(
            target_date=target_date,
            issue_number=value["number"],
            issue_url=HttpUrl(str(value["html_url"])),
            status="issue_published",
            marker=expected_marker,
        )

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
        expected_marker = marker(target_date)
        issue = await self._find(target_date)
        if not issue:
            return None
        return Publication(
            target_date=target_date,
            issue_number=int(str(issue["number"])),
            issue_url=HttpUrl(str(issue["html_url"])),
            status="issue_published",
            marker=expected_marker,
        )

    async def _find(self, target_date: date) -> dict[str, Any] | None:
        title_matches = [
            issue
            for issue in await self._issues()
            if "pull_request" not in issue and issue.get("title") == target_date.isoformat()
        ]
        matches = [issue for issue in title_matches if self._is_trusted_daily(issue, target_date)]
        if len(matches) > 1:
            raise RuntimeError("multiple daily issues exist for the same date")
        return matches[0] if matches else None

    def _is_trusted_daily(self, issue: dict[str, Any], target_date: date) -> bool:
        author = (issue.get("user") or {}).get("login")
        if author == self.repository.split("/", 1)[0]:
            return True
        labels = {label.get("name") for label in issue.get("labels") or []}
        return (
            author == TRUSTED_BOT
            and DAILY_LABEL in labels
            and has_daily_marker(issue.get("body"), target_date.isoformat())
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
