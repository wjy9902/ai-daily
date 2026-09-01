"""Atomic publication of the static site.

The old chain wrote a GitHub Issue and hoped a separate workflow would turn it
into a page. Publishing is now a single ordered transaction on local disk:

1. render the publication into a fresh release directory
2. write ``published/<date>.json``  <- the commit point
3. re-read that file and check its marker survived the round trip
4. swap the ``current`` symlink onto the new release
5. write ``status/status.json`` (outside any release, so it stays readable
   even when a render fails)

A crash between any two steps is recoverable: the next run re-reads
``published/`` and rebuilds. Steps 1 and 2 are ordered render-then-commit so a
committed record always has a release that renders; a release without a commit
is simply garbage-collected.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .publication import (
    DailyPublication,
    PublicationLevel,
    is_upgrade,
    load_publication,
)
from .render import ArchiveEntry, render_archive, render_daily, render_index, render_rss

#: How many release directories to keep. The site is small, but the disk is
#: shared with other services, so retention is explicit rather than unbounded.
RELEASE_RETENTION = 30

#: How many issues the feed carries.
RSS_LIMIT = 30


class PublicationRefused(RuntimeError):
    """The publication was rejected before anything was written."""


@dataclass(frozen=True)
class SiteLayout:
    """Where everything lives on disk."""

    root: Path

    @property
    def published(self) -> Path:
        return self.root / "published"

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def current(self) -> Path:
        return self.root / "current"

    @property
    def status_dir(self) -> Path:
        return self.root / "status"

    @property
    def status_file(self) -> Path:
        return self.status_dir / "status.json"

    @property
    def fallback(self) -> Path:
        return self.root / "fallback"

    @property
    def budget(self) -> Path:
        return self.root / "budget"

    @property
    def lock_file(self) -> Path:
        return self.root / ".publish.lock"

    def ensure(self) -> None:
        for path in (
            self.published,
            self.releases,
            self.status_dir,
            self.fallback,
            self.budget,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def publication_path(self, target_date: date) -> Path:
        return self.published / f"{target_date.isoformat()}.json"

    def budget_path(self, target_date: date) -> Path:
        return self.budget / f"{target_date.isoformat()}.json"


@contextmanager
def _exclusive_lock(path: Path, refusal_message: str) -> Iterator[None]:
    """Acquire one non-blocking advisory lock and release it reliably."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PublicationRefused(refusal_message) from error
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def publication_lock(layout: SiteLayout) -> Iterator[None]:
    """Serialise publishing against a concurrent timer run or a manual run."""

    with _exclusive_lock(
        layout.lock_file,
        "another publication run holds the lock",
    ):
        yield


def read_publication(layout: SiteLayout, target_date: date) -> DailyPublication | None:
    """Load one day's record, or None when that day was never published."""

    path = layout.publication_path(target_date)
    if not path.exists():
        return None
    return load_publication(path.read_text(encoding="utf-8"))


def active_release_contains_publication(layout: SiteLayout, publication: DailyPublication) -> bool:
    """Return whether the active site serves this exact immutable publication."""

    if not layout.current.exists():
        return False
    daily = layout.current.resolve() / "daily" / publication.target_date.isoformat() / "index.html"
    return daily.exists() and publication.marker in daily.read_text(encoding="utf-8")


def published_dates(layout: SiteLayout) -> list[date]:
    dates: list[date] = []
    if not layout.published.exists():
        return dates
    for path in layout.published.glob("*.json"):
        try:
            dates.append(date.fromisoformat(path.stem))
        except ValueError:
            continue
    return sorted(dates, reverse=True)


def recent_publications(layout: SiteLayout, limit: int) -> list[DailyPublication]:
    """The newest ``limit`` issues, skipping any file that fails validation.

    A corrupt file must not take the whole site down, but it must not be
    silently rendered either, so it is left out and reported by the caller.
    """

    publications: list[DailyPublication] = []
    for target_date in published_dates(layout)[:limit]:
        try:
            publication = read_publication(layout, target_date)
        except ValueError:
            continue
        if publication is not None:
            publications.append(publication)
    return publications


def build_archive(layout: SiteLayout, publications: list[DailyPublication]) -> list[ArchiveEntry]:
    """Archive entries covering every day since the first issue.

    Days with no issue appear as gaps rather than being skipped, so a missed
    day is visible instead of invisible.
    """

    if not publications:
        return []
    by_date = {item.target_date: item for item in publications}
    newest = max(by_date)
    oldest = min(by_date)
    entries: list[ArchiveEntry] = []
    cursor = newest
    while cursor >= oldest:
        publication = by_date.get(cursor)
        if publication is None:
            entries.append(
                ArchiveEntry(
                    target_date=cursor,
                    level=PublicationLevel.L3,
                    headline="未出刊",
                    story_count=0,
                    published=False,
                )
            )
        else:
            entries.append(
                ArchiveEntry(
                    target_date=cursor,
                    level=publication.level,
                    headline=publication.highlight or "本期速览",
                    story_count=publication.story_count,
                    published=True,
                )
            )
        cursor = date.fromordinal(cursor.toordinal() - 1)
    return entries


# ---------------------------------------------------------------- transaction


def guard_same_day_overwrite(
    layout: SiteLayout, publication: DailyPublication
) -> DailyPublication | None:
    """Refuse to replace an issue with a poorer one.

    A retry window may republish the same date, but only to *improve* it. This
    is what makes L2 -> L0 upgrades safe and downgrades impossible.
    """

    try:
        existing = read_publication(layout, publication.target_date)
    except ValueError:
        # A corrupt record for today is replaceable: anything valid beats it.
        return None
    if existing is None:
        return None
    if is_upgrade(existing.level, publication.level):
        return None
    raise PublicationRefused(
        f"{publication.target_date} is already published at {existing.level.value}; "
        f"{publication.level.value} would not improve it"
    )


def _write_atomic(path: Path, payload: str) -> None:
    """Write via a temp file, fsync, then rename, so readers never see a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def render_release(
    layout: SiteLayout,
    publication: DailyPublication,
    site_base_url: str,
    stamp: str,
) -> Path:
    """Render a complete site into a new release directory."""

    release = layout.releases / f"{publication.target_date.isoformat()}-{stamp}"
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True)

    history = [publication] + [
        item
        for item in recent_publications(layout, RSS_LIMIT)
        if item.target_date != publication.target_date
    ]
    history.sort(key=lambda item: item.target_date, reverse=True)
    archive = build_archive(layout, history)

    (release / "index.html").write_text(
        render_index(publication, archive, site_base_url), encoding="utf-8"
    )
    (release / "archive.html").write_text(render_archive(archive, site_base_url), encoding="utf-8")
    (release / "rss.xml").write_text(
        render_rss(history[:RSS_LIMIT], site_base_url), encoding="utf-8"
    )
    daily_dir = release / "daily"
    daily_dir.mkdir()
    for item in history[:RSS_LIMIT]:
        issue_dir = daily_dir / item.target_date.isoformat()
        issue_dir.mkdir(parents=True, exist_ok=True)
        (issue_dir / "index.html").write_text(render_daily(item, site_base_url), encoding="utf-8")
    _copy_assets(release)
    _assert_marker_rendered(release, publication)
    return release


def _copy_assets(release: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "static" / "site.css"
    if not source.exists():
        raise PublicationRefused(f"stylesheet is missing at {source}")
    assets = release / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, assets / "site.css")


def _assert_marker_rendered(release: Path, publication: DailyPublication) -> None:
    """The rendered page must carry the marker recomputed from the record.

    Checking the digest rather than the mere presence of a string is what makes
    the marker mean "this page is this record" instead of "this page has a
    comment in it".
    """

    expected = publication.compute_marker()
    if expected != publication.marker:
        raise PublicationRefused("publication marker does not match its content")
    page = release / "daily" / publication.target_date.isoformat() / "index.html"
    if expected not in page.read_text(encoding="utf-8"):
        raise PublicationRefused("rendered page is missing the publication marker")


def activate_release(layout: SiteLayout, release: Path) -> None:
    """Point ``current`` at ``release`` in one atomic step."""

    staging = layout.root / ".current.staging"
    if staging.exists() or staging.is_symlink():
        staging.unlink()
    staging.symlink_to(release, target_is_directory=True)
    os.replace(staging, layout.current)


def prune_releases(layout: SiteLayout, keep: int = RELEASE_RETENTION) -> list[Path]:
    """Delete old releases, never the one ``current`` points at."""

    if not layout.releases.exists():
        return []
    active = layout.current.resolve() if layout.current.exists() else None
    releases = sorted(
        (path for path in layout.releases.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    removed: list[Path] = []
    for path in releases[keep:]:
        if active is not None and path.resolve() == active:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    return removed


def write_status(layout: SiteLayout, status: dict[str, Any]) -> None:
    """Write the operator-facing status file.

    It lives outside every release on purpose: when a render fails, this is the
    one thing that still tells you what happened.
    """

    layout.status_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(layout.status_file, json.dumps(status, ensure_ascii=False, indent=2))


def _write_checked_status(destination: Path, status: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "checked_at": datetime.now(UTC).isoformat(),
        **status,
    }
    _write_atomic(
        destination,
        json.dumps(payload, ensure_ascii=False, indent=2),
    )


def publish_site(
    layout: SiteLayout,
    publication: DailyPublication,
    site_base_url: str,
    now: datetime | None = None,
) -> Path:
    """Run the whole transaction and return the activated release directory."""

    if not publication.marker_is_valid():
        raise PublicationRefused("publication is unsigned or its marker is stale")
    if publication.level is PublicationLevel.L3:
        raise PublicationRefused("an L3 run has nothing to publish")
    layout.ensure()
    guard_same_day_overwrite(layout, publication)

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    release = render_release(layout, publication, site_base_url, stamp)

    _write_atomic(
        layout.publication_path(publication.target_date),
        publication.model_dump_json(indent=2),
    )
    committed = read_publication(layout, publication.target_date)
    if committed is None or committed.marker != publication.marker:
        raise PublicationRefused("committed publication did not survive the round trip")

    activate_release(layout, release)
    prune_releases(layout)
    return release


def hold_previous_release(layout: SiteLayout, notice: str) -> Path:
    """L3: keep serving the last good issue, or the fallback if there is none.

    The fallback page is prebuilt and does not go through the renderer, so this
    path still works when the renderer itself is what failed.
    """

    layout.ensure()
    if layout.current.exists() and layout.current.resolve().is_dir():
        return layout.current.resolve()
    fallback_page = layout.fallback / "index.html"
    if not fallback_page.exists():
        raise PublicationRefused(f"no previous release and no fallback page at {fallback_page}")
    activate_release(layout, layout.fallback)
    return layout.fallback
