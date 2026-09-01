"""The publication transaction: render, commit, activate — and crash recovery.

Every test works inside ``tmp_path``; no test ever touches a real site root.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import factories
import pytest

from ai_daily import cli
from ai_daily.publication import DailyPublication, PublicationLevel
from ai_daily.site_publisher import (
    PublicationRefused,
    SiteLayout,
    activate_release,
    guard_same_day_overwrite,
    hold_previous_release,
    prune_releases,
    publication_lock,
    publish_site,
    read_publication,
)

SITE = factories.SITE
RELEASE_FILES = ("index.html", "archive.html", "rss.xml", "assets/site.css")
FIRST_ATTEMPT = datetime(2026, 8, 13, 1, 0, tzinfo=UTC)
SECOND_ATTEMPT = datetime(2026, 8, 13, 3, 0, tzinfo=UTC)


@pytest.fixture
def layout(tmp_path: Path) -> SiteLayout:
    site = SiteLayout(tmp_path / "site")
    site.ensure()
    return site


def _rebuild_args(layout: SiteLayout) -> SimpleNamespace:
    return SimpleNamespace(config_dir="config", site_root=str(layout.root), date=None)


def assert_serves(layout: SiteLayout, record: DailyPublication) -> Path:
    """The site is consistent: ``current`` is a complete release for ``record``."""

    assert layout.current.is_symlink()
    release = layout.current.resolve()
    assert release.is_dir()
    for name in RELEASE_FILES:
        assert (release / name).exists(), name
    page = release / "daily" / record.target_date.isoformat() / "index.html"
    assert record.marker in page.read_text(encoding="utf-8")
    committed = read_publication(layout, record.target_date)
    assert committed is not None
    assert committed.marker == record.marker
    return release


def _releases(layout: SiteLayout) -> list[Path]:
    return sorted(path for path in layout.releases.iterdir() if path.is_dir())


# --------------------------------------------------------------- happy path


def test_publish_site_renders_commits_and_activates(layout: SiteLayout) -> None:
    record = factories.publication()

    release = publish_site(layout, record, SITE)

    assert release == assert_serves(layout, record)
    assert layout.publication_path(record.target_date).exists()


@pytest.mark.parametrize("level", [PublicationLevel.L0, PublicationLevel.L1, PublicationLevel.L2B])
def test_every_publishable_level_completes_the_transaction(
    layout: SiteLayout, level: PublicationLevel
) -> None:
    record = factories.publication(level=level)

    publish_site(layout, record, SITE)

    assert_serves(layout, record)


def test_publish_site_refuses_an_unsigned_record_before_writing_anything(
    layout: SiteLayout,
) -> None:
    record = factories.publication(sign=False)

    with pytest.raises(PublicationRefused, match="unsigned"):
        publish_site(layout, record, SITE)

    assert list(layout.published.iterdir()) == []
    assert _releases(layout) == []


def test_publish_site_refuses_a_blocked_day(layout: SiteLayout) -> None:
    record = factories.publication(level=PublicationLevel.L3, details=[], briefs=[])

    with pytest.raises(PublicationRefused, match="L3"):
        publish_site(layout, record, SITE)

    assert list(layout.published.iterdir()) == []


# ------------------------------------------------------------ crash recovery


@pytest.fixture
def crash_commit(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], None]]:
    """Make the commit step fail, until the returned callable repairs it."""

    from ai_daily import site_publisher

    original = site_publisher._write_atomic
    failing = {"on": True}

    def patched(path: Path, payload: str) -> None:
        if failing["on"] and path.parent.name == "published":
            raise OSError("power cut between render and commit")
        original(path, payload)

    monkeypatch.setattr(site_publisher, "_write_atomic", patched)
    yield lambda: failing.__setitem__("on", False)


def test_a_crash_before_the_commit_leaves_no_record_and_the_rerun_recovers(
    layout: SiteLayout, crash_commit: Callable[[], None]
) -> None:
    record = factories.publication()

    with pytest.raises(OSError, match="power cut"):
        publish_site(layout, record, SITE, now=FIRST_ATTEMPT)

    assert list(layout.published.iterdir()) == []
    assert not layout.current.exists()
    orphan = _releases(layout)[0]

    crash_commit()
    release = publish_site(layout, record, SITE, now=SECOND_ATTEMPT)

    assert release != orphan
    assert_serves(layout, record)


def test_an_orphaned_release_is_harmless_and_prunes_away(
    layout: SiteLayout, crash_commit: Callable[[], None]
) -> None:
    record = factories.publication()
    with pytest.raises(OSError, match="power cut"):
        publish_site(layout, record, SITE, now=FIRST_ATTEMPT)
    orphan = _releases(layout)[0]

    crash_commit()
    live = publish_site(layout, record, SITE, now=SECOND_ATTEMPT)
    removed = prune_releases(layout, keep=1)

    assert removed == [orphan]
    assert not orphan.exists()
    assert live.exists()
    assert_serves(layout, record)


def test_a_committed_record_without_a_symlink_is_recovered_by_a_rebuild(
    layout: SiteLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_daily import site_publisher

    record = factories.publication()

    def refuse(target: SiteLayout, release: Path) -> None:
        raise OSError("power cut between commit and symlink swap")

    monkeypatch.setattr(site_publisher, "activate_release", refuse)
    with pytest.raises(OSError, match="symlink swap"):
        publish_site(layout, record, SITE)

    committed = read_publication(layout, record.target_date)
    assert committed is not None and committed.marker == record.marker
    assert not layout.current.exists()

    monkeypatch.undo()
    exit_code = _run_rebuild(layout)

    assert exit_code == 0
    assert_serves(layout, record)


def _run_rebuild(layout: SiteLayout) -> int:
    import asyncio

    return asyncio.run(cli._rebuild(_rebuild_args(layout)))


def test_a_committed_record_that_fails_its_round_trip_is_never_activated(
    layout: SiteLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_daily import site_publisher

    yesterday = factories.publication(target_date=date(2026, 8, 12))
    publish_site(layout, yesterday, SITE)
    previous_release = layout.current.resolve()
    record = factories.publication(target_date=date(2026, 8, 13))
    reads: list[int] = []

    def truncated_read(target: SiteLayout, target_date: date) -> DailyPublication | None:
        reads.append(1)
        if len(reads) == 1:  # the same-day guard, before anything is written
            return None
        return factories.publication(target_date=target_date, highlight="半个文件")

    monkeypatch.setattr(site_publisher, "read_publication", truncated_read)

    with pytest.raises(PublicationRefused, match="did not survive the round trip"):
        publish_site(layout, record, SITE)

    assert layout.current.resolve() == previous_release
    monkeypatch.undo()
    assert_serves(layout, yesterday)
    # What did reach the disk is intact, so a rebuild can serve it.
    assert _run_rebuild(layout) == 0
    assert_serves(layout, record)


def test_a_naive_rerun_of_the_same_level_is_refused_and_keeps_the_record(
    layout: SiteLayout,
) -> None:
    record = factories.publication(level=PublicationLevel.L2B)
    publish_site(layout, record, SITE)

    with pytest.raises(PublicationRefused, match="would not improve"):
        publish_site(layout, record.model_copy(), SITE)

    assert_serves(layout, record)


def test_a_same_level_rerun_that_carries_more_stories_replaces_the_issue(
    layout: SiteLayout,
) -> None:
    """2026-09-01: four windows all landed L1, so the first one won.

    The window that carried 29 stories was refused against the 25 already
    published, and the Nvidia/MediaTek and Anthropic alignment stories the
    benchmark digest led on went with it.
    """

    published = factories.publication(
        level=PublicationLevel.L1,
        details=[factories.story_card()],
        briefs=[factories.brief_card(i) for i in range(1, 4)],
    )
    publish_site(layout, published, SITE)

    richer = factories.publication(
        level=PublicationLevel.L1,
        details=[factories.story_card()],
        briefs=[factories.brief_card(i) for i in range(1, 7)],
    )
    assert guard_same_day_overwrite(layout, richer) is None
    publish_site(layout, richer, SITE)
    assert_serves(layout, richer)


def test_a_same_level_rerun_that_carries_less_is_still_refused(
    layout: SiteLayout,
) -> None:
    published = factories.publication(
        level=PublicationLevel.L1,
        details=[factories.story_card()],
        briefs=[factories.brief_card(i) for i in range(1, 7)],
    )
    publish_site(layout, published, SITE)

    thinner = factories.publication(
        level=PublicationLevel.L1,
        details=[factories.story_card()],
        briefs=[factories.brief_card(i) for i in range(1, 4)],
    )
    with pytest.raises(PublicationRefused, match="would not improve"):
        publish_site(layout, thinner, SITE)
    assert_serves(layout, published)


def test_briefs_cannot_displace_an_issue_that_reported_more_stories(
    layout: SiteLayout,
) -> None:
    """Details are compared first, so volume alone cannot win."""

    detailed = factories.publication(
        level=PublicationLevel.L1,
        details=[factories.story_card(0), factories.story_card(1)],
        briefs=[factories.brief_card(2)],
    )
    publish_site(layout, detailed, SITE)

    brief_heavy = factories.publication(
        level=PublicationLevel.L1,
        details=[factories.story_card(0)],
        briefs=[factories.brief_card(i) for i in range(1, 9)],
    )
    with pytest.raises(PublicationRefused, match="would not improve"):
        publish_site(layout, brief_heavy, SITE)
    assert_serves(layout, detailed)


def test_the_cli_publishes_through_the_guarded_transaction() -> None:
    """The CLI must not carry a second publish path of its own.

    It used to render, commit and activate inline, which skipped
    :func:`guard_same_day_overwrite` and let a retry window replace a full
    issue with a degraded one.
    """

    assert cli.publish_site is publish_site
    assert not hasattr(cli, "_publish_publication")


# ------------------------------------------------------------------ activation


def test_current_never_points_at_a_half_written_release(
    layout: SiteLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_daily import site_publisher

    first = factories.publication(target_date=date(2026, 8, 12))
    publish_site(layout, first, SITE)
    good_release = layout.current.resolve()

    def broken_index(*args: object, **kwargs: object) -> str:
        raise RuntimeError("renderer died halfway")

    monkeypatch.setattr(site_publisher, "render_index", broken_index)
    second = factories.publication(target_date=date(2026, 8, 13))

    with pytest.raises(RuntimeError, match="renderer died halfway"):
        publish_site(layout, second, SITE)

    assert layout.current.resolve() == good_release
    assert_serves(layout, first)
    assert read_publication(layout, second.target_date) is None


def test_activation_swaps_over_an_existing_symlink(layout: SiteLayout) -> None:
    first = factories.publication(target_date=date(2026, 8, 12))
    second = factories.publication(target_date=date(2026, 8, 13))

    publish_site(layout, first, SITE)
    first_release = layout.current.resolve()
    second_release = publish_site(layout, second, SITE)

    assert layout.current.is_symlink()
    assert layout.current.resolve() == second_release != first_release
    assert not (layout.root / ".current.staging").exists()
    assert_serves(layout, second)


def test_prune_never_deletes_the_release_current_points_at(layout: SiteLayout) -> None:
    kept = publish_site(layout, factories.publication(target_date=date(2026, 8, 11)), SITE)
    publish_site(layout, factories.publication(target_date=date(2026, 8, 12)), SITE)
    newest = publish_site(layout, factories.publication(target_date=date(2026, 8, 13)), SITE)
    activate_release(layout, kept)

    removed = prune_releases(layout, keep=1)

    assert kept.exists()
    assert newest.exists()
    assert kept not in removed
    assert layout.current.resolve() == kept
    assert len(_releases(layout)) == 2


def test_hold_previous_release_keeps_the_last_good_issue(layout: SiteLayout) -> None:
    record = factories.publication()
    release = publish_site(layout, record, SITE)

    held = hold_previous_release(layout, "今日采集受阻")

    assert held == release
    assert_serves(layout, record)


def test_hold_previous_release_needs_a_fallback_when_nothing_was_published(
    layout: SiteLayout,
) -> None:
    with pytest.raises(PublicationRefused, match="fallback"):
        hold_previous_release(layout, "今日采集受阻")


# ----------------------------------------------------------------- the ladder


@pytest.mark.parametrize(
    ("existing", "candidate", "allowed"),
    [
        (PublicationLevel.L2B, PublicationLevel.L0, True),
        (PublicationLevel.L2B, PublicationLevel.L2A, True),
        (PublicationLevel.L1, PublicationLevel.L0, True),
        (PublicationLevel.L0, PublicationLevel.L2B, False),
        (PublicationLevel.L0, PublicationLevel.L0, False),
        (PublicationLevel.L2A, PublicationLevel.L2B, False),
    ],
)
def test_same_day_republication_may_only_improve_the_issue(
    layout: SiteLayout,
    existing: PublicationLevel,
    candidate: PublicationLevel,
    allowed: bool,
) -> None:
    publish_site(layout, factories.publication(level=existing), SITE)
    replacement = factories.publication(level=candidate, highlight="重新出刊的亮点。")

    if allowed:
        assert guard_same_day_overwrite(layout, replacement) is None
        publish_site(layout, replacement, SITE)
        assert_serves(layout, replacement)
    else:
        with pytest.raises(PublicationRefused, match="would not improve"):
            guard_same_day_overwrite(layout, replacement)
        served = read_publication(layout, replacement.target_date)
        assert served is not None and served.level is existing


def test_an_upgrade_replaces_what_the_site_serves(layout: SiteLayout) -> None:
    degraded = factories.publication(level=PublicationLevel.L2B)
    publish_site(layout, degraded, SITE)

    full = factories.publication(level=PublicationLevel.L0)
    publish_site(layout, full, SITE)

    release = assert_serves(layout, full)
    assert degraded.marker not in (
        release / "daily" / full.target_date.isoformat() / "index.html"
    ).read_text(encoding="utf-8")


def test_a_corrupt_record_for_today_is_replaceable(layout: SiteLayout) -> None:
    record = factories.publication()
    layout.publication_path(record.target_date).write_text('{"target_date": ', encoding="utf-8")

    assert guard_same_day_overwrite(layout, record) is None
    publish_site(layout, record, SITE)

    assert_serves(layout, record)


# ------------------------------------------------------------------- locking


def test_a_second_concurrent_publication_is_refused(layout: SiteLayout) -> None:
    with publication_lock(layout):
        with pytest.raises(PublicationRefused, match="another publication run"):
            with publication_lock(layout):
                raise AssertionError("the second acquisition must not proceed")


def test_the_lock_is_released_for_the_next_run(layout: SiteLayout) -> None:
    with publication_lock(layout):
        pass

    with publication_lock(layout):
        assert layout.lock_file.exists()
