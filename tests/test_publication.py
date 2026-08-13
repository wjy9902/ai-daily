"""Marker semantics: the signature is what makes a page mean a record."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest

import factories
from ai_daily.publication import (
    LEVEL_ORDER,
    DailyPublication,
    PublicationLevel,
    is_upgrade,
    load_publication,
)


def test_marker_survives_a_serialise_deserialise_round_trip() -> None:
    record = factories.publication()

    reloaded = load_publication(record.model_dump_json())

    assert reloaded == record
    assert reloaded.marker == record.marker
    assert reloaded.compute_marker() == record.compute_marker()


def test_marker_ignores_key_ordering() -> None:
    record = factories.publication()
    payload: dict[str, Any] = json.loads(record.model_dump_json())

    reordered = dict(reversed(list(payload.items())))
    reordered["details"] = [dict(reversed(list(item.items()))) for item in payload["details"]]
    reordered["briefs"] = [dict(reversed(list(item.items()))) for item in payload["briefs"]]

    assert list(reordered) != list(payload)
    assert DailyPublication.model_validate(reordered).compute_marker() == record.marker


@pytest.mark.parametrize(
    "update",
    [
        {"highlight": "另一个亮点"},
        {"notice": "另一条提示"},
        {"level": PublicationLevel.L1},
        {"target_date": date(2026, 8, 14)},
        {"generated_at": datetime(2026, 8, 13, 2, 30, tzinfo=UTC)},
        {"degradation_reasons": ["部分详报起草失败"]},
        {"details": []},
        {"briefs": [factories.brief_card(9)]},
        {"viewpoints": []},
    ],
)
def test_any_content_change_changes_the_marker(update: dict[str, Any]) -> None:
    record = factories.publication()

    mutated = record.model_copy(update=update)

    assert mutated.compute_marker() != record.marker
    assert not mutated.marker_is_valid()


def test_load_rejects_a_record_whose_marker_does_not_match_its_content() -> None:
    record = factories.publication()
    payload = json.loads(record.model_dump_json())
    payload["highlight"] = "被改写过的亮点"

    with pytest.raises(ValueError, match="marker does not match"):
        load_publication(json.dumps(payload, ensure_ascii=False))


def test_load_rejects_an_unsigned_record() -> None:
    unsigned = factories.publication(sign=False)

    with pytest.raises(ValueError, match="marker does not match"):
        load_publication(unsigned.model_dump_json())


def test_a_truncated_file_raises_instead_of_publishing_partial_content() -> None:
    raw = factories.publication().model_dump_json()

    with pytest.raises(ValueError):  # noqa: PT011 - pydantic raises a ValidationError
        load_publication(raw[: len(raw) // 2])


def test_signing_is_idempotent() -> None:
    record = factories.publication()

    assert record.signed().marker == record.marker
    assert record.marker_is_valid()


@pytest.mark.parametrize(
    ("previous", "candidate", "expected"),
    [
        (PublicationLevel.L3, PublicationLevel.L2B, True),
        (PublicationLevel.L2B, PublicationLevel.L2A, True),
        (PublicationLevel.L2B, PublicationLevel.L0, True),
        (PublicationLevel.L1, PublicationLevel.L0, True),
        (PublicationLevel.L0, PublicationLevel.L0, False),
        (PublicationLevel.L0, PublicationLevel.L1, False),
        (PublicationLevel.L0, PublicationLevel.L2B, False),
        (PublicationLevel.L2A, PublicationLevel.L2B, False),
    ],
)
def test_is_upgrade_follows_the_ladder(
    previous: PublicationLevel, candidate: PublicationLevel, expected: bool
) -> None:
    assert is_upgrade(previous, candidate) is expected


def test_the_ladder_is_totally_ordered_worst_last() -> None:
    assert list(LEVEL_ORDER) == [
        PublicationLevel.L0,
        PublicationLevel.L1,
        PublicationLevel.L2A,
        PublicationLevel.L2B,
        PublicationLevel.L3,
    ]
    assert sorted(LEVEL_ORDER.values()) == [0, 1, 2, 3, 4]
