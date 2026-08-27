"""CLI level decisions: which day an unattended run is for, and where it writes."""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import factories
import pytest

from ai_daily import cli
from ai_daily.persona_snapshot import persist_upstream_snapshot
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
    publish_site(layout, factories.publication(target_date=date(2026, 8, 11)), factories.SITE)
    missed = date(2026, 8, 12)

    target = cli._target_date(None)
    publish_site(layout, factories.publication(target_date=target), factories.SITE)

    assert target == date(2026, 8, 13)
    assert not layout.publication_path(missed).exists()
    gap = next(
        entry
        for entry in build_archive(layout, recent_publications(layout, 30))
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


@pytest.mark.asyncio
async def test_daily_repairs_a_missing_snapshot_pointer_before_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    publication = factories.publication(target_date=date(2026, 8, 27))
    publish_site(layout, publication, factories.SITE)
    event = factories.event(0)
    persist_upstream_snapshot(
        layout,
        publication,
        [event],
        [factories.judge_decision(0)],
        None,
    )
    assert not layout.upstream_pointer_path(publication.target_date).exists()

    async def verified(*args: Any, **kwargs: Any) -> Any:
        return publication

    async def must_not_run(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("an intact L0 publication must not spend model budget")

    monkeypatch.setattr(cli, "verify_publication", verified)
    monkeypatch.setattr(cli, "_run", must_not_run)
    result = await cli._daily(
        SimpleNamespace(
            config_dir="config",
            site_root=str(layout.root),
            date=publication.target_date.isoformat(),
        )
    )

    assert result == 0
    assert layout.upstream_pointer_path(publication.target_date).exists()


@pytest.mark.asyncio
async def test_fallback_contains_the_persona_route(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path / "site")
    result = await cli._write_fallback(
        SimpleNamespace(config_dir="config", site_root=str(layout.root), date=None)
    )

    assert result == 0
    assert (layout.fallback / "index.html").exists()
    persona = layout.fallback / "jiayu" / "index.html"
    assert persona.exists()
    assert "甲鱼" in persona.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_persona_daily_holds_when_active_release_changes_during_stability_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    layout.upstream_pointer_path(date(2026, 8, 27)).write_text("{}", encoding="utf-8")
    states = iter([(b"pointer", "release-a"), (b"pointer", "release-b")])

    monkeypatch.setattr(cli, "_persona_stability_state", lambda *args: next(states))

    async def no_wait(_: float) -> None:
        return None

    async def must_not_run(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("changed release must not enter the persona pipeline")

    monkeypatch.setattr(cli.asyncio, "sleep", no_wait)
    monkeypatch.setattr(cli, "_persona_run", must_not_run)
    result = await cli._persona_daily(
        SimpleNamespace(
            site_root=str(layout.root),
            date="2026-08-27",
            stability_seconds=30,
        )
    )

    assert result == 1


@pytest.mark.asyncio
async def test_persona_daily_hands_off_after_stable_marker_and_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    layout.upstream_pointer_path(date(2026, 8, 27)).write_text("{}", encoding="utf-8")
    state = (b"pointer", "release-a")
    calls = 0

    monkeypatch.setattr(cli, "_persona_stability_state", lambda *args: state)

    async def no_wait(_: float) -> None:
        return None

    async def completed(*args: Any, **kwargs: Any) -> int:
        nonlocal calls
        calls += 1
        return 0

    monkeypatch.setattr(cli.asyncio, "sleep", no_wait)
    monkeypatch.setattr(cli, "_persona_run", completed)
    result = await cli._persona_daily(
        SimpleNamespace(
            site_root=str(layout.root),
            date="2026-08-27",
            stability_seconds=30,
        )
    )

    assert result == 0
    assert calls == 1
