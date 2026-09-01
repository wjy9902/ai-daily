from datetime import UTC, datetime, timedelta

from ai_daily.probe import STALE_AFTER_DAYS, _freshness
from tests.factories import raw_item

RUN_TIME = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)


def test_a_feed_frozen_on_last_years_posts_is_reported_stale() -> None:
    """The failure that hid fourteen dead X feeds: answers fine, publishes nothing."""

    items = [raw_item(i, published_at=datetime(2025, 11, 14, tzinfo=UTC)) for i in range(100)]
    report = _freshness(items, RUN_TIME)
    assert report["stale"] is True
    assert report["newest_item_age_days"] is not None
    assert float(report["newest_item_age_days"]) > 290


def test_a_source_that_is_merely_quiet_is_not_stale() -> None:
    """A blog posting every few weeks is quiet, not broken."""

    items = [raw_item(0, published_at=RUN_TIME - timedelta(days=STALE_AFTER_DAYS - 1))]
    assert _freshness(items, RUN_TIME)["stale"] is False


def test_freshness_reports_the_newest_item_not_the_first() -> None:
    items = [
        raw_item(0, published_at=datetime(2025, 1, 1, tzinfo=UTC)),
        raw_item(1, published_at=RUN_TIME - timedelta(hours=2)),
        raw_item(2, published_at=datetime(2024, 6, 1, tzinfo=UTC)),
    ]
    report = _freshness(items, RUN_TIME)
    assert report["stale"] is False
    assert float(str(report["newest_item_age_days"])) < 1


def test_a_source_with_nothing_datable_reports_unknown_rather_than_stale() -> None:
    """Undated is a different failure and already has its own rejection bucket."""

    report = _freshness([raw_item(0, published_at=None)], RUN_TIME)
    assert report == {"newest_item_at": None, "newest_item_age_days": None, "stale": None}
