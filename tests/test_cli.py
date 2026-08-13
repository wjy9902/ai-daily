"""CLI level decisions: which day an unattended run is for, and where it writes."""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace

import factories
import pytest

from ai_daily import cli
from ai_daily.site_publisher import SiteLayout, build_archive, publish_site, recent_publications

#: 2026-08-13 00:30 in Beijing is still 2026-08-12 in UTC.
JUST_AFTER_BEIJING_MIDNIGHT = datetime(2026, 8, 12, 16, 30, tzinfo=UTC)

#: A run that starts at 03:00 Beijing, hours after a cross-midnight outage.
AFTER_AN_OUTAGE = datetime(2026, 8, 12, 19, 0, tzinfo=UTC)


class FrozenClock:
    """A stand-in for ``datetime`` that is stuck at one instant."""

    instant = JUST_AFTER_BEIJING_MIDNIGHT

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return cls.instant.astimezone(tz) if tz else cls.instant


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> type[FrozenClock]:
    monkeypatch.setattr(cli, "datetime", FrozenClock)
    return FrozenClock


def test_target_date_defaults_to_today_in_beijing(
    monkeypatch: pytest.MonkeyPatch, frozen_clock: type[FrozenClock]
) -> None:
    monkeypatch.setattr(frozen_clock, "instant", JUST_AFTER_BEIJING_MIDNIGHT)

    assert cli._target_date(None) == date(2026, 8, 13)
    assert JUST_AFTER_BEIJING_MIDNIGHT.date() == date(2026, 8, 12)


def test_target_date_accepts_an_explicit_day(frozen_clock: type[FrozenClock]) -> None:
    assert cli._target_date("2026-07-04") == date(2026, 7, 4)


def test_a_run_after_a_cross_midnight_outage_does_not_backfill_the_missed_day(
    monkeypatch: pytest.MonkeyPatch, frozen_clock: type[FrozenClock], tmp_path: Path
) -> None:
    """The gap stays visible in the archive instead of being papered over."""

    monkeypatch.setattr(frozen_clock, "instant", AFTER_AN_OUTAGE)
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    publish_site(
        layout, factories.publication(target_date=date(2026, 8, 11)), factories.SITE
    )
    missed = date(2026, 8, 12)

    target = cli._target_date(None)
    publish_site(layout, factories.publication(target_date=target), factories.SITE)

    assert target == date(2026, 8, 13)
    assert not layout.publication_path(missed).exists()
    gap = next(
        entry for entry in build_archive(layout, recent_publications(layout, 30))
        if entry.target_date == missed
    )
    assert gap.published is False
    # Only an operator asking for that date explicitly can still fill it in.
    assert cli._target_date(missed.isoformat()) == missed


def test_the_site_root_comes_from_the_flag_then_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AI_DAILY_SITE_ROOT", str(tmp_path / "from-env"))

    assert cli._layout(SimpleNamespace(site_root=str(tmp_path / "from-flag"))) == SiteLayout(
        tmp_path / "from-flag"
    )
    assert cli._layout(SimpleNamespace(site_root=None)) == SiteLayout(tmp_path / "from-env")

    monkeypatch.delenv("AI_DAILY_SITE_ROOT")
    assert cli._layout(SimpleNamespace(site_root=None)) == SiteLayout(Path.cwd() / "site")
