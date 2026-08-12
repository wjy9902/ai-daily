from __future__ import annotations

from datetime import date
from typing import Any

import httpx
from pydantic import HttpUrl

from ai_daily.assembler import marker
from ai_daily.models import Publication


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
        issue = await self._find(target_date, expected_marker)
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
        issue = await self._find(target_date, expected_marker)
        if not issue:
            return None
        return Publication(
            target_date=target_date,
            issue_number=int(str(issue["number"])),
            issue_url=HttpUrl(str(issue["html_url"])),
            status="issue_published",
            marker=expected_marker,
        )

    async def _find(self, target_date: date, expected_marker: str) -> dict[str, Any] | None:
        response = await self.client.get(
            f"https://api.github.com/repos/{self.repository}/issues",
            headers=self.headers,
            params={"state": "all", "per_page": 100},
            timeout=20,
        )
        response.raise_for_status()
        title_matches = [
            issue
            for issue in response.json()
            if "pull_request" not in issue and issue.get("title") == target_date.isoformat()
        ]
        matches = [issue for issue in title_matches if expected_marker in (issue.get("body") or "")]
        if title_matches and not matches:
            raise RuntimeError("an unmarked issue already exists for the target date")
        if len(matches) > 1:
            raise RuntimeError("multiple daily issues exist for the same date")
        return matches[0] if matches else None
