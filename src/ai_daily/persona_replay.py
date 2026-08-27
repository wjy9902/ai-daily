from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ai_daily.artifacts import write_artifact
from ai_daily.config import AppConfig, Secrets
from ai_daily.persona_models import sha256_payload
from ai_daily.persona_pipeline import PersonaPipeline
from ai_daily.persona_render import RENDERER_VERSION, TEMPLATE_VERSION
from ai_daily.persona_snapshot import activate_upstream_snapshot, load_upstream_snapshot
from ai_daily.site_publisher import SiteLayout


def freeze_replay_dataset(
    layout: SiteLayout,
    dates: list[date],
    output: Path,
    config: AppConfig,
    project_root: Path,
) -> dict[str, Any]:
    cases = []
    for target in dates:
        snapshot = load_upstream_snapshot(layout, target)
        cases.append(
            {
                "target_date": target.isoformat(),
                "publication_marker": snapshot.publication_marker,
                "snapshot_sha256": snapshot.snapshot_sha256,
            }
        )
    payload = {
        "schema_version": 1,
        "frozen_at": datetime.now(UTC).isoformat(),
        "runtime_manifest": _runtime_manifest(config, project_root),
        "cases": cases,
    }
    write_artifact(output, payload)
    return payload


async def run_replay(
    config: AppConfig,
    secrets: Secrets,
    layout: SiteLayout,
    project_root: Path,
    dataset: Path,
) -> dict[str, Any]:
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    if payload.get("runtime_manifest") != _runtime_manifest(config, project_root):
        raise ValueError("replay runtime inputs drifted from the frozen manifest")
    with tempfile.TemporaryDirectory(prefix="ai-daily-persona-replay-") as temporary:
        replay_layout = SiteLayout(Path(temporary))
        replay_layout.ensure()
        _copy_replay_inputs(layout, replay_layout, payload["cases"])
        results = []
        for case in payload["cases"]:
            target = date.fromisoformat(case["target_date"])
            pipeline = PersonaPipeline(config, secrets, replay_layout, project_root, target)
            result = await pipeline.run(target)
            results.append(result.model_copy(update={"edition_path": None}).model_dump(mode="json"))
    ready = sum(item["editorial_state"] == "ready" for item in results)
    return {
        "schema_version": 1,
        "case_count": len(results),
        "ready_count": ready,
        "held_count": len(results) - ready,
        "results": results,
    }


def _copy_replay_inputs(
    source: SiteLayout,
    destination: SiteLayout,
    cases: list[dict[str, Any]],
) -> None:
    for case in cases:
        target = date.fromisoformat(case["target_date"])
        snapshot = load_upstream_snapshot(source, target)
        if (
            snapshot.publication_marker != case["publication_marker"]
            or snapshot.snapshot_sha256 != case["snapshot_sha256"]
        ):
            raise ValueError(f"replay input drifted for {target}")
        write_artifact(destination.upstream_object_path(snapshot.publication_marker), snapshot)
        activate_upstream_snapshot(destination, target, snapshot.publication_marker)


def _runtime_manifest(config: AppConfig, project_root: Path) -> dict[str, str]:
    if config.persona is None:
        raise ValueError("persona configuration is required for replay")
    memories = project_root / config.persona.memories_path
    constitution = project_root / config.persona.constitution_path
    return {
        "persona_config_sha256": sha256_payload(config.persona.model_dump(mode="json")),
        "models_config_sha256": sha256_payload(config.models.model_dump(mode="json")),
        "memories_sha256": hashlib.sha256(memories.read_bytes()).hexdigest(),
        "constitution_sha256": hashlib.sha256(constitution.read_bytes()).hexdigest(),
        "renderer_version": RENDERER_VERSION,
        "template_version": TEMPLATE_VERSION,
        "persona_runtime_sha256": _persona_runtime_sha256(project_root),
    }


def _persona_runtime_sha256(project_root: Path) -> str:
    files = (
        "src/ai_daily/persona_models.py",
        "src/ai_daily/persona_content.py",
        "src/ai_daily/persona_memory.py",
        "src/ai_daily/persona_baseline.py",
        "src/ai_daily/persona_pipeline.py",
        "src/ai_daily/persona_verifier.py",
        "src/ai_daily/persona_render.py",
    )
    digest = hashlib.sha256()
    for relative in files:
        path = project_root / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
