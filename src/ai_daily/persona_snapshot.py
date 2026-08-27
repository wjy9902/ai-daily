from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import cast

from ai_daily.artifacts import write_artifact
from ai_daily.content import evidence_bundle
from ai_daily.models import EditorialPlan, Event, JudgeDecision
from ai_daily.persona_models import UpstreamSnapshot, sha256_payload
from ai_daily.publication import DailyPublication
from ai_daily.site_publisher import SiteLayout


def persist_upstream_snapshot(
    layout: SiteLayout,
    publication: DailyPublication,
    events: list[Event],
    decisions: list[JudgeDecision],
    plan: EditorialPlan | None,
) -> UpstreamSnapshot:
    if not publication.marker_is_valid():
        raise ValueError("cannot snapshot an invalid publication marker")
    payload = {
        "schema_version": 1,
        "target_date": publication.target_date,
        "created_at": datetime.now(UTC),
        "publication_level": publication.level,
        "publication_marker": publication.marker,
        "events": events,
        "evidence_bundles": [evidence_bundle(event) for event in events],
        "decisions": decisions,
        "editorial_plan": plan,
    }
    serializable = _json_payload(payload)
    provisional = UpstreamSnapshot.model_validate({**serializable, "snapshot_sha256": "0" * 64})
    snapshot = provisional.model_copy(
        update={"snapshot_sha256": sha256_payload(provisional.canonical_payload())}
    )
    object_path = layout.upstream_object_path(publication.marker)
    if object_path.exists():
        existing = UpstreamSnapshot.model_validate_json(object_path.read_text(encoding="utf-8"))
        if not existing.hash_is_valid():
            raise ValueError("existing immutable upstream snapshot is corrupt")
        if _logical_payload(existing) != _logical_payload(snapshot):
            raise ValueError("publication marker maps to conflicting upstream snapshots")
        return existing
    write_artifact(object_path, snapshot)
    return snapshot


def activate_upstream_snapshot(
    layout: SiteLayout, target_date: date, publication_marker: str
) -> UpstreamSnapshot:
    """Activate a snapshot only after the matching site release is committed."""

    object_path = layout.upstream_object_path(publication_marker)
    snapshot = UpstreamSnapshot.model_validate_json(object_path.read_text(encoding="utf-8"))
    if not snapshot.hash_is_valid():
        raise ValueError("upstream snapshot hash mismatch")
    if snapshot.target_date != target_date or snapshot.publication_marker != publication_marker:
        raise ValueError("upstream snapshot does not match activation target")
    write_artifact(
        layout.upstream_pointer_path(target_date),
        {
            "schema_version": 1,
            "target_date": target_date.isoformat(),
            "publication_marker": publication_marker,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "object_path": str(object_path.relative_to(layout.root)),
        },
    )
    return snapshot


def load_upstream_snapshot(layout: SiteLayout, target_date: date) -> UpstreamSnapshot:
    pointer_path = layout.upstream_pointer_path(target_date)
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    marker = str(pointer["publication_marker"])
    snapshot = UpstreamSnapshot.model_validate_json(
        layout.upstream_object_path(marker).read_text(encoding="utf-8")
    )
    if not snapshot.hash_is_valid():
        raise ValueError("upstream snapshot hash mismatch")
    if snapshot.target_date != target_date or snapshot.publication_marker != marker:
        raise ValueError("upstream pointer does not match snapshot")
    if snapshot.snapshot_sha256 != pointer.get("snapshot_sha256"):
        raise ValueError("upstream pointer hash mismatch")
    return snapshot


def _json_payload(payload: dict[str, object]) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(
            json.dumps(
                payload,
                default=lambda item: (
                    item.model_dump(mode="json")
                    if hasattr(item, "model_dump")
                    else item.isoformat()
                    if hasattr(item, "isoformat")
                    else item.value
                ),
                ensure_ascii=False,
            )
        ),
    )


def _logical_payload(snapshot: UpstreamSnapshot) -> dict[str, object]:
    payload = snapshot.canonical_payload()
    payload.pop("created_at", None)
    return cast(dict[str, object], payload)
