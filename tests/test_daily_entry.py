"""The timer entry point publishes once a day; retries exist only for failure.

Also the two operator paths that replaced the four-window era's implicit
choices: ``collect`` (a round into the store, no model calls) and
``publish-artifact`` (release an issue an earlier run built).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import factories
import pytest

from ai_daily import cli
from ai_daily.item_store import ItemStore
from ai_daily.models import RawItem, SourceHealth, SourceTier
from ai_daily.publication import PublicationLevel
from ai_daily.site_publisher import SiteLayout, publish_site, read_publication


@pytest.fixture
def layout(tmp_path: Path) -> SiteLayout:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    return layout


def _args(layout: SiteLayout, **extra: object) -> SimpleNamespace:
    return SimpleNamespace(
        config_dir="config",
        site_root=str(layout.root),
        date=factories.TARGET_DATE.isoformat(),
        **extra,
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the expensive paths instead of running them."""

    counts = {"run": 0, "rebuild": 0}

    async def fake_run(args: SimpleNamespace) -> int:
        counts["run"] += 1
        return 0

    async def fake_rebuild(args: SimpleNamespace) -> int:
        counts["rebuild"] += 1
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli, "_rebuild", fake_rebuild)
    return counts


def _serving(monkeypatch: pytest.MonkeyPatch, answers: list[bool]) -> None:
    async def fake_served(publication: object, base_url: str) -> bool:
        return answers.pop(0)

    monkeypatch.setattr(cli, "_served", fake_served)


# ------------------------------------------------------------- the entry point


async def test_a_day_without_an_issue_runs_the_pipeline(
    layout: SiteLayout, calls: dict[str, int]
) -> None:
    assert await cli._daily(_args(layout)) == 0
    assert calls == {"run": 1, "rebuild": 0}


@pytest.mark.parametrize("level", [PublicationLevel.L0, PublicationLevel.L1])
async def test_a_live_l1_or_l0_is_left_alone(
    layout: SiteLayout,
    calls: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    level: PublicationLevel,
) -> None:
    """L1 is a finished issue. Rerunning it would cost a round and be refused."""

    publish_site(layout, factories.publication(level=level), factories.SITE)
    _serving(monkeypatch, [True])

    assert await cli._daily(_args(layout)) == 0
    assert calls == {"run": 0, "rebuild": 0}


@pytest.mark.parametrize("level", [PublicationLevel.L2A, PublicationLevel.L2B])
async def test_a_brief_only_issue_is_retried(
    layout: SiteLayout,
    calls: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    level: PublicationLevel,
) -> None:
    publish_site(layout, factories.publication(level=level), factories.SITE)
    _serving(monkeypatch, [True])

    assert await cli._daily(_args(layout)) == 0
    assert calls == {"run": 1, "rebuild": 0}


async def test_a_committed_but_unserved_record_is_rebuilt_before_anything_else(
    layout: SiteLayout, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publish killed between writing the record and flipping ``current``.

    This used to be repaired only for L0. An L1 in that state was rerun at
    full cost and then refused as a same-level duplicate, and the site never
    recovered.
    """

    publish_site(layout, factories.publication(level=PublicationLevel.L1), factories.SITE)
    _serving(monkeypatch, [False, True])

    assert await cli._daily(_args(layout)) == 0
    assert calls == {"run": 0, "rebuild": 1}


async def test_a_record_that_cannot_be_served_even_after_a_rebuild_fails_loudly(
    layout: SiteLayout, calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_site(layout, factories.publication(level=PublicationLevel.L1), factories.SITE)
    _serving(monkeypatch, [False, False])

    assert await cli._daily(_args(layout)) == 1
    assert calls == {"run": 0, "rebuild": 1}
    status = json.loads(layout.status_file.read_text(encoding="utf-8"))
    assert status["action"] == "unrecoverable"


# ------------------------------------------------------------ publish-artifact


def _artifact(tmp_path: Path, publication: object) -> Path:
    path = tmp_path / "artifacts" / "run-1" / "publication.json"
    path.parent.mkdir(parents=True)
    path.write_text(publication.model_dump_json(indent=2), encoding="utf-8")  # type: ignore[attr-defined]
    return path


async def test_publish_artifact_releases_a_built_issue_without_running_anything(
    layout: SiteLayout, tmp_path: Path, calls: dict[str, int]
) -> None:
    built = factories.publication(level=PublicationLevel.L1)

    exit_code = await cli._publish_artifact(
        _args(layout, artifact=_artifact(tmp_path, built), replace=False)
    )

    assert exit_code == 0
    assert calls["run"] == 0
    served = read_publication(layout, built.target_date)
    assert served is not None and served.marker == built.marker


async def test_publish_artifact_respects_the_guard_unless_told_to_replace(
    layout: SiteLayout, tmp_path: Path
) -> None:
    live = factories.publication(level=PublicationLevel.L1)
    publish_site(layout, live, factories.SITE)
    other = factories.publication(level=PublicationLevel.L1, highlight="另一轮建好的刊。")
    artifact = _artifact(tmp_path, other)

    assert await cli._publish_artifact(_args(layout, artifact=artifact, replace=False)) == 1
    assert await cli._publish_artifact(_args(layout, artifact=artifact, replace=True)) == 0

    served = read_publication(layout, other.target_date)
    assert served is not None and served.marker == other.marker
    status = json.loads(layout.status_file.read_text(encoding="utf-8"))
    assert status["action"] == "replace"
    assert status["previous_marker"] == live.marker
    assert list(layout.published.glob("*.replaced-*.json"))


# ----------------------------------------------------------------- collect


class _Collector:
    def __init__(self, items: list[RawItem], health: list[SourceHealth]) -> None:
        self._items = items
        self._health = health

    async def __aenter__(self) -> _Collector:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def collect(self, sources: object) -> tuple[list[RawItem], list[SourceHealth]]:
        return self._items, self._health


def _raw(item_id: str) -> RawItem:
    now = datetime.now(UTC)
    return RawItem(
        source="openai-news",
        source_tier=SourceTier.A,
        source_item_id=item_id,
        url=f"https://openai.com/index/{item_id}",
        title=f"Story {item_id}",
        published_at=now - timedelta(hours=1),
        discovered_at=now,
    )


async def test_collect_writes_the_store_and_its_status_without_touching_the_site(
    layout: SiteLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    health = [
        SourceHealth(
            source="openai-news", tier=SourceTier.A, status="ok", item_count=2, latency_ms=5
        )
    ]
    monkeypatch.setattr(cli, "Collector", lambda: _Collector([_raw("a"), _raw("b")], health))

    assert await cli._collect_round(_args(layout)) == 0

    status = json.loads(layout.collect_status_file.read_text(encoding="utf-8"))
    assert (status["fetched"], status["inserted"], status["stored"], status["in_window"]) == (
        2,
        2,
        2,
        2,
    )
    assert status["ok_sources"] == 1
    with ItemStore(layout.item_store) as store:
        latest = store.latest_round()
    assert latest is not None and latest.kind == "collect"
    assert not layout.status_file.exists(), "a collection round is not a publication"
    assert not list(layout.published.glob("*.json"))


async def test_collect_fails_the_round_when_every_source_failed(
    layout: SiteLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    health = [
        SourceHealth(
            source="openai-news",
            tier=SourceTier.A,
            status="failed",
            item_count=0,
            latency_ms=5,
            error="x",
        )
    ]
    monkeypatch.setattr(cli, "Collector", lambda: _Collector([], health))

    assert await cli._collect_round(_args(layout)) == 1
    status = json.loads(layout.collect_status_file.read_text(encoding="utf-8"))
    assert status["failed"] == ["openai-news"]


def test_status_reports_a_stale_collection(layout: SiteLayout) -> None:
    assert cli._collect_staleness(layout)["collect_stale"] is True
    layout.status_dir.mkdir(parents=True, exist_ok=True)
    layout.collect_status_file.write_text(
        json.dumps({"finished_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}),
        encoding="utf-8",
    )
    fresh = cli._collect_staleness(layout)
    assert fresh["collect_stale"] is False
    assert 0.9 < fresh["collect_age_hours"] < 1.2
