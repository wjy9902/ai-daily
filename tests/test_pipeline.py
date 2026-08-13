import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import BaseModel

from ai_daily.budget import BudgetLedger
from ai_daily.config import Secrets, load_config
from ai_daily.content import JudgeBatch
from ai_daily.models import (
    DraftItem,
    EditorialInsight,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    JudgeDecision,
    RawItem,
    SourceHealth,
    SourceTier,
)
from ai_daily.pipeline import (
    DailyPipeline,
    QualityGateFailed,
    collection_window,
    filter_fresh_items,
)


class FakeCollector:
    def __init__(self, count: int = 17) -> None:
        self.count = count

    async def collect(self, sources: object) -> tuple[list[RawItem], list[SourceHealth]]:
        now = datetime.now(UTC)
        items = [
            RawItem(
                source=f"source-{index}",
                source_label=f"Source {index}",
                source_tier=SourceTier.A,
                source_item_id=str(index),
                url=f"https://example.com/{index}",
                title=f"Unique{index} Product{index} Change{index} AI",
                summary="",
                published_at=now,
                discovered_at=now,
            )
            for index in range(self.count)
        ]
        health = [
            SourceHealth(
                source="source-health",
                tier=SourceTier.A,
                status="ok",
                item_count=self.count,
                latency_ms=1,
            )
        ]
        return items, health


class FakeGateway:
    def __init__(self, config: object, fail_plan: bool = False) -> None:
        self.runs: list[Any] = []
        self.ledger = BudgetLedger(config.models.budget)  # type: ignore[attr-defined]
        self.fail_plan = fail_plan

    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
    ) -> Any:
        values = json.loads(prompt)
        if output_type is JudgeBatch:
            return _judge_output(values)
        if output_type is EditorialPlan:
            if self.fail_plan:
                raise RuntimeError("editor failed")
            return _plan_output(values)
        return _draft_output(values)


def _judge_output(values: list[dict[str, object]]) -> JudgeBatch:
    return JudgeBatch(
        decisions=[
            JudgeDecision(
                event_id=str(value["event_id"]),
                selected=True,
                category="模型与平台",
                relevance=80,
                confidence=0.9,
                reason="相关候选",
                evidence_ids=[str(value["evidence"][0]["evidence_id"])],  # type: ignore[index]
            )
            for value in values
        ]
    )


def _plan_output(values: list[dict[str, object]]) -> EditorialPlan:
    tiers = [EditorialTier.LEAD] * 4 + [EditorialTier.FOLLOW] * 5
    tiers += [EditorialTier.BRIEF] * 8
    categories = ["模型与平台", "行业动态", "国内 AI", "值得试的项目"]
    selections = [
        EditorialSelection(
            event_id=str(value["event_id"]),
            tier=tiers[index],
            category=categories[index % len(categories)],  # type: ignore[arg-type]
            headline=f"重要新闻 {index}",
            brief="一条经过全局比较的重要更新。",
            importance=100 - index,
            confidence=0.9,
            reason="重要性较高",
            evidence_ids=[str(value["evidence"][0]["evidence_id"])],  # type: ignore[index]
        )
        for index, value in enumerate(values[:17])
    ]
    return EditorialPlan(
        today_highlight="模型、产品和产业均有重要变化。",
        selections=selections,
        editor_viewpoint=[
            EditorialInsight(text="产品更新更关注落地。", evidence_ids=selections[0].evidence_ids),
            EditorialInsight(
                text="来源覆盖决定选题质量。", evidence_ids=selections[1].evidence_ids
            ),
        ],
    )


def _draft_output(value: dict[str, object]) -> DraftItem:
    selection = value["selection"]
    bundle = value["bundle"]
    return DraftItem(
        event_id=str(selection["event_id"]),  # type: ignore[index]
        tldr="这是一项已经确认的重要变化。",
        facts=["证据确认该变化已经发生。"],
        why_it_matters="它会影响实际模型与产品判断。",
        evidence_ids=[str(bundle["evidence"][0]["evidence_id"])],  # type: ignore[index]
    )


def _client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=[])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_collection_window_uses_beijing_run_time() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 12, 4, 20, tzinfo=timezone)
    cutoff, run_time = collection_window(now.date(), "Asia/Shanghai", 36, now)
    assert cutoff == datetime(2026, 8, 10, 16, 20, tzinfo=timezone)
    assert run_time == now


@pytest.mark.parametrize(
    ("offset", "expected_accepted"),
    [
        (timedelta(hours=-36, seconds=-1), False),
        (timedelta(hours=-36), True),
        (timedelta(minutes=5), True),
        (timedelta(minutes=5, seconds=1), False),
    ],
)
def test_freshness_filter_enforces_window_boundaries(
    offset: timedelta, expected_accepted: bool
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    run_time = datetime(2026, 8, 13, 4, 20, tzinfo=timezone)
    item = RawItem(
        source="source",
        source_tier=SourceTier.A,
        source_item_id="1",
        url="https://example.com/ai-model",
        title="AI model launch",
        published_at=run_time + offset,
        discovered_at=run_time,
    )

    accepted, audit = filter_fresh_items([item], run_time - timedelta(hours=36), run_time, timezone)

    assert bool(accepted) is expected_accepted
    assert len(audit["rejected_outside_window"]) == (0 if expected_accepted else 1)  # type: ignore[arg-type]


async def test_candidate_filter_rejects_unverified_publication_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    run_time = datetime(2026, 8, 13, 4, 20, tzinfo=timezone)
    cutoff = run_time - timedelta(hours=36)
    monkeypatch.setattr(
        "ai_daily.pipeline.collection_window",
        lambda target_date, timezone_name, window_hours: (cutoff, run_time),
    )
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    items, _ = await FakeCollector().collect(None)
    undated = [
        item.model_copy(update={"published_at": None, "discovered_at": run_time}) for item in items
    ]
    pipeline = DailyPipeline(config, Secrets(), client=_client())

    with pytest.raises(QualityGateFailed, match="only 0 candidates"):
        await pipeline._candidates(
            run_time.date(),
            undated,
            tmp_path,
            config.pipeline.repository,
        )

    audit = json.loads((tmp_path / "freshness.json").read_text())
    assert audit["policy"] == "verified-publication-time-only"
    assert audit["accepted_count"] == 0
    assert len(audit["rejected_undated"]) == len(undated)


async def test_backfill_scores_against_target_date_not_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_date = datetime(2026, 8, 1).date()
    timezone = ZoneInfo("Asia/Shanghai")
    published_at = datetime(2026, 7, 31, 23, tzinfo=timezone)
    items = [
        RawItem(
            source=f"source-{index}",
            source_tier=SourceTier.A,
            source_item_id=str(index),
            url=f"https://example.com/{index}",
            title=f"AI product-{index} topic-{index} change-{index}",
            published_at=published_at,
            discovered_at=published_at,
        )
        for index in range(17)
    ]
    captured: dict[str, datetime] = {}

    def capture_score(events: list[Any], now: datetime) -> list[Any]:
        captured["now"] = now
        return events

    monkeypatch.setattr("ai_daily.pipeline.score_events", capture_score)
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    pipeline = DailyPipeline(config, Secrets(), client=_client())

    _, candidates = await pipeline._candidates(
        target_date, items, tmp_path, config.pipeline.repository
    )

    assert len(candidates) == 17
    assert captured["now"].astimezone(timezone).date() == target_date


async def test_full_dry_run_writes_digest_without_publishing(tmp_path: Path) -> None:
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    gateway = FakeGateway(config)
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=FakeCollector(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )

    artifact, body = await pipeline.run(
        datetime.now(ZoneInfo("Asia/Shanghai")).date(), publish=False
    )

    assert artifact.publication is not None
    assert artifact.publication.status == "dry_run"
    assert "## 编辑观点" in body
    assert len(list(tmp_path.rglob("digest.md"))) == 1
    assert artifact.metadata["model_requests"] == artifact.metadata["audited_model_requests"]


async def test_candidate_shortage_stops_before_model_calls(tmp_path: Path) -> None:
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    gateway = FakeGateway(config)
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=FakeCollector(count=5),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )

    with pytest.raises(QualityGateFailed, match="candidates"):
        await pipeline.run(datetime.now(ZoneInfo("Asia/Shanghai")).date(), publish=False)

    assert gateway.ledger.requests == 0


async def test_model_request_audit_mismatch_blocks_run(tmp_path: Path) -> None:
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    gateway = FakeGateway(config)
    gateway.ledger.requests = 1
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=FakeCollector(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
    )

    with pytest.raises(QualityGateFailed, match="audit is incomplete"):
        await pipeline.run(datetime.now(ZoneInfo("Asia/Shanghai")).date(), publish=False)


async def test_model_failure_still_writes_audit_artifact(tmp_path: Path) -> None:
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=FakeCollector(),  # type: ignore[arg-type]
        gateway=FakeGateway(config, fail_plan=True),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="editor failed"):
        await pipeline.run(datetime.now(ZoneInfo("Asia/Shanghai")).date(), publish=False)

    assert len(list(tmp_path.rglob("model-runs.json"))) == 1


async def test_publish_mode_requires_github_token(tmp_path: Path) -> None:
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    pipeline = DailyPipeline(
        config,
        Secrets(github_token=None),
        client=_client(),
        collector=FakeCollector(),  # type: ignore[arg-type]
        gateway=FakeGateway(config),  # type: ignore[arg-type]
    )

    with pytest.raises(QualityGateFailed, match="GITHUB_TOKEN"):
        await pipeline.run(datetime.now(ZoneInfo("Asia/Shanghai")).date(), publish=True)
