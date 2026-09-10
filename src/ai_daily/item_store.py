"""The item store: what the collector has seen, kept until the issue runs.

Every timer window used to fetch the feeds afresh and throw the result away.
IT之家's site feed holds four to seven hours, so anything published in the
afternoon was gone by the 04:20 run whichever window looked. The store keeps
each item from the first collection that saw it, merging later sightings
field by field, so the 06:30 issue reads a day and a half of what the feeds
carried rather than what they happen to hold at that minute.

Standard-library sqlite, WAL mode, one file next to ``published/``. Losing it
costs a few days of sightings, never an issue.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType

from ai_daily.models import RawItem, SourceConfig, SourceHealth

#: Sightings older than this, with nothing newer to their name, are dropped.
ITEM_RETENTION_DAYS = 7
ROUND_RETENTION_DAYS = 30
BUSY_TIMEOUT_SECONDS = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source         TEXT NOT NULL,
    source_item_id TEXT NOT NULL,
    canonical_url  TEXT NOT NULL,
    payload        TEXT NOT NULL,
    published_at   TEXT,
    discovered_at  TEXT NOT NULL,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    seen_count     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (source, source_item_id)
);
CREATE INDEX IF NOT EXISTS items_canonical_url ON items (canonical_url);
CREATE INDEX IF NOT EXISTS items_published_at ON items (published_at);
CREATE INDEX IF NOT EXISTS items_discovered_at ON items (discovered_at);
CREATE TABLE IF NOT EXISTS rounds (
    round_id       TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT NOT NULL,
    ok_sources     INTEGER NOT NULL,
    failed_sources INTEGER NOT NULL,
    health         TEXT NOT NULL
);
"""


class ItemStoreError(RuntimeError):
    """Any sqlite failure: the collector fails its round; the issue run goes on without it."""


@dataclass(frozen=True)
class MergeStats:
    inserted: int
    updated: int


@dataclass(frozen=True)
class RoundRecord:
    round_id: str
    kind: str
    started_at: datetime
    finished_at: datetime
    ok_sources: int
    failed_sources: int


def merge_payload(old: RawItem, new: RawItem) -> RawItem:
    """Combine two sightings of one item, keeping whichever field is more complete.

    No sighting outranks another. The publication time a source reported the
    first time is the publication time; a longer summary beats a shorter one;
    the title and the metrics follow the newest sighting because corrections
    and popularity move. ``discovered_at`` is the source's own time - Hacker
    News writes the submission time there, and the freshness gate reads it for
    undated community items - so it never changes.
    """

    keep_old_time = old.published_at is not None
    return new.model_copy(
        update={
            "published_at": old.published_at if keep_old_time else new.published_at,
            "source_time_kind": old.source_time_kind if keep_old_time else new.source_time_kind,
            "summary": old.summary if len(old.summary) >= len(new.summary) else new.summary,
            "discovered_at": old.discovered_at,
        }
    )


def merge_sightings(live: Iterable[RawItem], stored: Iterable[RawItem]) -> list[RawItem]:
    """Join what this run fetched with what the store held, one item per (source, id)."""

    merged: dict[tuple[str, str], RawItem] = {}
    for item in stored:
        merged[(item.source, item.source_item_id)] = item
    for item in live:
        key = (item.source, item.source_item_id)
        merged[key] = merge_payload(merged[key], item) if key in merged else item
    return list(merged.values())


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class ItemStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> ItemStore:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            connection.commit()
        except (sqlite3.Error, OSError) as error:
            raise ItemStoreError(f"cannot open item store at {self.path}: {error}") from error
        self._connection = connection
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise ItemStoreError("item store is not open")
        return self._connection

    def merge_many(self, items: Iterable[RawItem], now: datetime) -> MergeStats:
        stamp = _iso(now)
        inserted = updated = 0
        try:
            with self.connection:
                for item in items:
                    row = self.connection.execute(
                        "SELECT payload FROM items WHERE source = ? AND source_item_id = ?",
                        (item.source, item.source_item_id),
                    ).fetchone()
                    if row is None:
                        self._insert(item, stamp)
                        inserted += 1
                    else:
                        self._update(
                            merge_payload(RawItem.model_validate_json(row[0]), item), stamp
                        )
                        updated += 1
        except sqlite3.Error as error:
            raise ItemStoreError(f"item store write failed: {error}") from error
        return MergeStats(inserted=inserted, updated=updated)

    def _insert(self, item: RawItem, stamp: str) -> None:
        from ai_daily.normalize import canonicalize_url

        self.connection.execute(
            "INSERT INTO items (source, source_item_id, canonical_url, payload, published_at,"
            " discovered_at, first_seen, last_seen, seen_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                item.source,
                item.source_item_id,
                canonicalize_url(str(item.url)),
                item.model_dump_json(),
                _iso(item.published_at) if item.published_at else None,
                _iso(item.discovered_at),
                stamp,
                stamp,
            ),
        )

    def _update(self, item: RawItem, stamp: str) -> None:
        self.connection.execute(
            "UPDATE items SET payload = ?, published_at = ?, last_seen = ?,"
            " seen_count = seen_count + 1 WHERE source = ? AND source_item_id = ?",
            (
                item.model_dump_json(),
                _iso(item.published_at) if item.published_at else None,
                stamp,
                item.source,
                item.source_item_id,
            ),
        )

    def read_window(self, cutoff: datetime, sources: Mapping[str, SourceConfig]) -> list[RawItem]:
        """Items that can still be news at ``cutoff``, as the current config sees them.

        A source dropped or disabled since the sighting does not come back
        through the cache, and a row keeps the tier, channel and region the
        config gives its source today, not the ones it had when stored.
        """

        try:
            rows = self.connection.execute(
                "SELECT payload, first_seen FROM items WHERE published_at >= ?"
                " OR (published_at IS NULL AND discovered_at >= ?)",
                (_iso(cutoff), _iso(cutoff)),
            ).fetchall()
        except sqlite3.Error as error:
            raise ItemStoreError(f"item store read failed: {error}") from error
        items: list[RawItem] = []
        for payload, first_seen in rows:
            item = RawItem.model_validate_json(payload)
            source = sources.get(item.source)
            if source is None or not source.enabled:
                continue
            items.append(
                item.model_copy(
                    update={
                        "source_tier": source.tier,
                        "source_label": source.display_name or source.name,
                        "source_channel": source.channel,
                        "source_region": source.region,
                        "source_ai_focused": source.ai_focused,
                        "metrics": {**item.metrics, "first_seen": first_seen},
                    }
                )
            )
        return items

    def prune(self, now: datetime) -> int:
        horizon = _iso(now - timedelta(days=ITEM_RETENTION_DAYS))
        rounds_horizon = _iso(now - timedelta(days=ROUND_RETENTION_DAYS))
        try:
            with self.connection:
                cursor = self.connection.execute(
                    "DELETE FROM items WHERE last_seen < ?"
                    " AND COALESCE(published_at, discovered_at) < ?",
                    (horizon, horizon),
                )
                self.connection.execute(
                    "DELETE FROM rounds WHERE finished_at < ?", (rounds_horizon,)
                )
        except sqlite3.Error as error:
            raise ItemStoreError(f"item store prune failed: {error}") from error
        return int(cursor.rowcount)

    def record_round(
        self,
        kind: str,
        started_at: datetime,
        finished_at: datetime,
        health: list[SourceHealth],
    ) -> RoundRecord:
        ok = sum(item.status in {"ok", "partial", "not_modified"} for item in health)
        record = RoundRecord(
            round_id=f"{finished_at.astimezone(UTC).date().isoformat()}-{uuid.uuid4().hex[:8]}",
            kind=kind,
            started_at=started_at,
            finished_at=finished_at,
            ok_sources=ok,
            failed_sources=len(health) - ok,
        )
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO rounds (round_id, kind, started_at, finished_at, ok_sources,"
                    " failed_sources, health) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.round_id,
                        kind,
                        _iso(started_at),
                        _iso(finished_at),
                        ok,
                        record.failed_sources,
                        json.dumps([item.model_dump(mode="json") for item in health]),
                    ),
                )
        except sqlite3.Error as error:
            raise ItemStoreError(f"item store round write failed: {error}") from error
        return record

    def latest_round(self) -> RoundRecord | None:
        try:
            row = self.connection.execute(
                "SELECT round_id, kind, started_at, finished_at, ok_sources, failed_sources"
                " FROM rounds ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error as error:
            raise ItemStoreError(f"item store read failed: {error}") from error
        if row is None:
            return None
        return RoundRecord(
            round_id=row[0],
            kind=row[1],
            started_at=datetime.fromisoformat(row[2]),
            finished_at=datetime.fromisoformat(row[3]),
            ok_sources=row[4],
            failed_sources=row[5],
        )

    def counts(self, cutoff: datetime) -> tuple[int, int]:
        """(rows in the store, rows inside the window)."""

        try:
            total = self.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            in_window = self.connection.execute(
                "SELECT COUNT(*) FROM items WHERE published_at >= ?"
                " OR (published_at IS NULL AND discovered_at >= ?)",
                (_iso(cutoff), _iso(cutoff)),
            ).fetchone()[0]
        except sqlite3.Error as error:
            raise ItemStoreError(f"item store read failed: {error}") from error
        return int(total), int(in_window)
