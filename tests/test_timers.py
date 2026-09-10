"""The schedule is part of the contract: collection all day, one issue at 06:30."""

import re
from itertools import pairwise
from pathlib import Path

UNITS = Path("ops/systemd")


def _calendar(name: str) -> list[str]:
    text = (UNITS / name).read_text(encoding="utf-8")
    return re.findall(r"^OnCalendar=\*-\*-\* (\d\d:\d\d):00$", text, re.MULTILINE)


def test_the_issue_runs_once_at_0630_with_one_retry() -> None:
    assert _calendar("ai-daily.timer") == ["06:30", "07:30"]


def test_collection_runs_every_three_hours_and_keeps_clear_of_the_issue() -> None:
    times = _calendar("ai-daily-collect.timer")
    assert len(times) == 8
    hours = [int(value[:2]) for value in times]
    assert hours == sorted(hours)
    assert all((later - earlier) == 3 for earlier, later in pairwise(hours))
    assert not any("06:10" <= value <= "06:59" for value in times)


def test_papers_run_after_the_issue_and_its_retry() -> None:
    (papers,) = _calendar("ai-daily-papers.timer")
    assert papers > "07:30"


def test_the_collect_service_carries_the_daily_services_hardening() -> None:
    daily = (UNITS / "ai-daily.service").read_text(encoding="utf-8")
    collect = (UNITS / "ai-daily-collect.service").read_text(encoding="utf-8")
    for line in (
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "NoNewPrivileges=yes",
        "ReadWritePaths=/www/wwwroot/ai-daily",
        "ReadOnlyPaths=/www/wwwroot/ai-daily/app",
        "User=ai-daily",
    ):
        assert line in daily and line in collect, line
    assert "ai-daily collect" in collect
    assert "EnvironmentFile" not in collect, "collection needs no model keys"
