"""CLI level decisions: which day an unattended run is for, and where it writes."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from types import SimpleNamespace

import factories
import pytest

from ai_daily import cli
from ai_daily.degradation import DegradationTracker
from ai_daily.models import RunArtifact
from ai_daily.pipeline import RunOutcome
from ai_daily.site_publisher import (
    PublicationRefused,
    SiteLayout,
    build_archive,
    daily_run_lock,
    publication_lock,
    publish_site,
    recent_publications,
)

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


# ------------------------------------------------- lock scope during a run


class ProbingPipeline:
    """A stand-in daily pipeline that records who holds the publication lock.

    The bug this guards against: the daily used to hold ``publication_lock``
    across gathering and drafting, 23 to 28 minutes in production. The papers
    publisher waits ten for that lock, so it could never win, and 2026-09-02
    and 2026-09-03 both lost a finished issue to it.
    """

    publish_lock_was_free: bool | None = None

    def __init__(self, *args: object, layout: SiteLayout, **kwargs: object) -> None:
        del args, kwargs
        self._layout = layout
        self.gateway = SimpleNamespace(ledger=SimpleNamespace(snapshot=dict))

    async def run(self, target_date: date, publish: bool) -> object:
        del publish
        try:
            with publication_lock(self._layout):
                type(self).publish_lock_was_free = True
        except PublicationRefused:
            type(self).publish_lock_was_free = False
        publication = factories.publication(target_date=target_date)
        return RunOutcome(
            artifact=RunArtifact(
                run_id="probe",
                target_date=target_date,
                items=[],
                health=[],
                events=[],
                model_runs=[],
            ),
            publication=publication,
            tracker=DegradationTracker(),
            run_dir=self._layout.root / "artifacts",
        )


async def test_a_daily_run_leaves_the_publication_lock_free_while_it_works(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "DailyPipeline", ProbingPipeline)
    monkeypatch.setattr(ProbingPipeline, "publish_lock_was_free", None)
    args = SimpleNamespace(
        config_dir="config",
        site_root=str(tmp_path / "site"),
        date="2026-09-03",
        mode="publish",
    )

    assert await cli._run(args) == 0

    # Free during the run, which is what lets a papers release slip in.
    assert ProbingPipeline.publish_lock_was_free is True
    assert SiteLayout(tmp_path / "site").publication_path(date(2026, 9, 3)).exists()


async def test_a_daily_run_still_locks_out_a_second_daily_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Narrowing the publish lock must not let two runs both spend money."""

    monkeypatch.setattr(cli, "DailyPipeline", ProbingPipeline)
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    args = SimpleNamespace(
        config_dir="config",
        site_root=str(tmp_path / "site"),
        date="2026-09-03",
        mode="publish",
    )

    with daily_run_lock(layout):
        with pytest.raises(PublicationRefused, match="another daily run"):
            await cli._run(args)

    assert not layout.publication_path(date(2026, 9, 3)).exists()


# ------------------------------------- what status.json describes after a run


def _outcome(publication: object, layout: SiteLayout) -> RunOutcome:
    return RunOutcome(
        artifact=RunArtifact(
            run_id="rerun",
            target_date=factories.TARGET_DATE,
            items=[],
            health=[],
            events=[],
            model_runs=[],
        ),
        publication=publication,  # type: ignore[arg-type]
        tracker=DegradationTracker(),
        run_dir=layout.root / "artifacts",
    )


def test_a_refused_rerun_leaves_status_describing_the_live_issue(tmp_path: Path) -> None:
    """2026-09-04: status.json reported an issue that was never published.

    The 14:44 rerun carried 9 details and 16 briefs and the guard refused it;
    the record on disk had 9 and 19. Status then named the published date beside
    the rejected run's counts, so reading it to see what was live was wrong.
    """

    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    live = factories.publication(briefs=[factories.brief_card(index) for index in range(3)])
    publish_site(layout, live, "https://example.com")
    poorer = factories.publication(
        briefs=[factories.brief_card(1)], highlight="更少的一期。", generated_at=live.generated_at
    )

    exit_code = cli._publish_daily(
        layout, _outcome(poorer, layout), {"run_id": "rerun"}, "https://example.com"
    )

    status = json.loads(layout.status_file.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert status["action"] == "refused"
    assert status["brief_count"] == len(live.briefs) == 3
    assert status["level"] == live.level.value
