import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import BaseModel

from ai_daily.budget import BudgetLedger
from ai_daily.config import Secrets, load_config
from ai_daily.content import JudgeBatch
from ai_daily.degradation import DegradationTracker, FailureClass
from ai_daily.model_gateway import ModelInvocationFailed
from ai_daily.models import (
    DraftItem,
    EditorialInsight,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    Event,
    FactClaim,
    JudgeDecision,
    RawItem,
    SourceChannel,
    SourceConfig,
    SourceHealth,
    SourceTier,
)
from ai_daily.pipeline import (
    DETAIL_EVIDENCE_MIN_CHARS,
    DailyPipeline,
    QualityGateFailed,
    RunOutcome,
    _detail_evidence_audit,
    _repair_detail_evidence,
    collection_window,
    filter_fresh_items,
)
from ai_daily.publication import PublicationLevel
from ai_daily.site_publisher import SiteLayout


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
                source_channel=SourceChannel.OFFICIAL,
                source_item_id=str(index),
                url=f"https://example.com/{index}",
                title=f"Unique{index} Product{index} Change{index} AI",
                summary=(f"evidence{index} topic{index} fact{index} detail{index} " * 60),
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

    async def enrich_event_content(
        self, events: list[Event], sources: list[SourceConfig], event_ids: set[str]
    ) -> list[dict[str, object]]:
        return []

    async def aclose(self) -> None:
        return None


class SpyEnrichmentCollector(FakeCollector):
    def __init__(self) -> None:
        super().__init__()
        self.event_ids: set[str] = set()

    async def enrich_event_content(
        self, events: list[Event], sources: list[SourceConfig], event_ids: set[str]
    ) -> list[dict[str, object]]:
        self.event_ids.update(event_ids)
        events[0].items[0].summary += " enriched-marker"
        events[0].summary = events[0].items[0].summary
        return [{"event_id": events[0].event_id, "status": "enriched"}]


class ShortEvidenceCollector(FakeCollector):
    async def collect(self, sources: object) -> tuple[list[RawItem], list[SourceHealth]]:
        items, health = await super().collect(sources)
        for index, item in enumerate(items):
            item.summary = f"Short evidence {index}."
        return items, health


class PromotedFalseNegativeCollector(FakeCollector):
    def __init__(self) -> None:
        super().__init__()
        self.enrichment_calls: list[set[str]] = []

    async def collect(self, sources: object) -> tuple[list[RawItem], list[SourceHealth]]:
        items, health = await super().collect(sources)
        items[0].summary = "Short but important evidence."
        return items, health

    async def enrich_event_content(
        self, events: list[Event], sources: list[SourceConfig], event_ids: set[str]
    ) -> list[dict[str, object]]:
        self.enrichment_calls.append(event_ids)
        for event in events:
            if event.event_id not in event_ids:
                continue
            event.items[0].summary = "Verified full article evidence. " * 40
            event.summary = event.items[0].summary
        return [{"event_id": event_id, "status": "enriched"} for event_id in event_ids]


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
        stage: Any = None,
    ) -> Any:
        values = json.loads(prompt)
        if output_type is JudgeBatch:
            return _judge_output(values)
        if output_type.__name__.startswith("EditorialPlanOutput_"):
            if self.fail_plan:
                raise RuntimeError("editor failed")
            return output_type.model_validate(_grouped_plan_output(_plan_output(values)))
        return _draft_output(values)


class FalseNegativeGateway(FakeGateway):
    def __init__(self, config: object) -> None:
        super().__init__(config)
        self.rejected_id: str | None = None

    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        value = await super().generate(role, output_type, instructions, prompt, validator)
        if isinstance(value, JudgeBatch) and self.rejected_id is None:
            value.decisions[0].selected = False
            value.decisions[0].relevance = 20
            self.rejected_id = value.decisions[0].event_id
        return value


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


def _grouped_plan_output(plan: EditorialPlan) -> dict[str, object]:
    return {
        "today_highlight": plan.today_highlight,
        "editor_viewpoint": plan.editor_viewpoint,
        **{
            tier.value: [
                selection.model_dump(exclude={"tier"})
                for selection in plan.selections
                if selection.tier == tier
            ]
            for tier in EditorialTier
        },
    }


def _draft_output(value: dict[str, object]) -> DraftItem:
    selection = value["selection"]
    bundle = value["bundle"]
    evidence = bundle["evidence"][0]  # type: ignore[index]
    evidence_id = str(evidence["evidence_id"])
    quote = str(evidence["excerpt"])[:60]
    return DraftItem(
        event_id=str(selection["event_id"]),  # type: ignore[index]
        tldr="这是一项已经确认的重要变化。",
        tldr_evidence_id=evidence_id,
        tldr_quote=quote,
        facts=[
            FactClaim(text="证据确认该变化已经发生。", evidence_id=evidence_id, quote=quote)
        ],
        why_it_matters="它会影响实际模型与产品判断。",
        evidence_ids=[evidence_id],
    )


class UnavailableEditorGateway(FakeGateway):
    """The judge answers, the editor is unreachable. This is the common outage."""

    async def generate(
        self,
        role: str,
        output_type: type[BaseModel],
        instructions: str,
        prompt: str,
        validator: Any = None,
        stage: Any = None,
    ) -> Any:
        if output_type.__name__.startswith("EditorialPlanOutput_"):
            raise ModelInvocationFailed("editor endpoint is unreachable")
        return await super().generate(role, output_type, instructions, prompt, validator)


def _client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=[])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def _site(tmp_path: Path) -> SiteLayout:
    """An isolated site root: no test may write to a real one."""

    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    return layout


def _today() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


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
    pipeline = DailyPipeline(config, Secrets(), client=_client(), layout=_site(tmp_path))
    tracker = DegradationTracker()

    filtered, candidates = await pipeline._candidates(
        run_time.date(), undated, tmp_path, tracker
    )

    assert filtered == []
    assert candidates == []
    assert tracker.failures == [FailureClass.CANDIDATES_EXHAUSTED]
    assert tracker.blocked is True
    audit = json.loads((tmp_path / "freshness.json").read_text())
    assert audit["policy"] == ("verified-publication-time-with-exact-url-community-corroboration")
    assert audit["accepted_count"] == 0
    assert len(audit["rejected_undated"]) == len(undated)


def test_community_submission_time_only_corrobates_exact_verified_url() -> None:
    timezone = ZoneInfo("Asia/Shanghai")
    run_time = datetime(2026, 8, 13, 4, 20, tzinfo=timezone)
    official = RawItem(
        source="qwen-models",
        source_label="Qwen Official Models",
        source_tier=SourceTier.A,
        source_channel=SourceChannel.OFFICIAL,
        source_item_id="official",
        url="https://huggingface.co/Qwen/Qwen3.8-2.4T-A95B",
        title="Qwen model repository update",
        published_at=run_time - timedelta(hours=2),
        discovered_at=run_time,
    )
    matching = RawItem(
        source="hacker-news",
        source_label="Hacker News",
        source_tier=SourceTier.B,
        source_channel=SourceChannel.COMMUNITY,
        source_item_id="matching",
        url=official.url,
        title="Qwen3.8-2.4T-A95B",
        published_at=None,
        discovered_at=run_time - timedelta(hours=1),
    )
    unrelated = matching.model_copy(
        update={
            "source_item_id": "unrelated",
            "url": "https://example.com/unverified-ai-news",
        }
    )

    accepted, audit = filter_fresh_items(
        [matching, unrelated, official],
        run_time - timedelta(hours=36),
        run_time,
        timezone,
    )

    assert accepted == [official, matching]
    assert [item["url"] for item in audit["accepted_corroboration"]] == [str(matching.url)]  # type: ignore[index]
    assert [item["url"] for item in audit["rejected_undated"]] == [str(unrelated.url)]  # type: ignore[index]


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
    pipeline = DailyPipeline(config, Secrets(), client=_client(), layout=_site(tmp_path))
    tracker = DegradationTracker()

    _, candidates = await pipeline._candidates(target_date, items, tmp_path, tracker)

    assert len(candidates) == 17
    assert captured["now"].astimezone(timezone).date() == target_date
    assert tracker.failures == []


async def test_full_dry_run_composes_an_issue_without_publishing_it(tmp_path: Path) -> None:
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    layout = _site(tmp_path)
    gateway = FakeGateway(config)
    collector = SpyEnrichmentCollector()
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=collector,
        gateway=gateway,  # type: ignore[arg-type]
        layout=layout,
    )

    outcome = await pipeline.run(_today(), publish=False)

    assert isinstance(outcome, RunOutcome)
    assert outcome.publication.level is PublicationLevel.L0
    assert outcome.publication.marker_is_valid()
    assert outcome.publication.details and outcome.publication.briefs
    assert outcome.publication.viewpoints
    assert outcome.tracker.failures == []
    assert outcome.artifact.publication is not None
    assert outcome.artifact.publication.status == "dry_run"
    assert outcome.artifact.publication.marker == outcome.publication.marker
    assert outcome.artifact.metadata["level"] == "L0"
    assert outcome.artifact.metadata["degradation"] == []
    assert outcome.artifact.metadata["candidate_count"] == len(outcome.artifact.events)
    # A dry run touches nothing a reader could see.
    assert list(layout.published.iterdir()) == []
    assert list(layout.releases.iterdir()) == []
    assert not layout.current.exists()
    assert json.loads((outcome.run_dir / "publication.json").read_text()) == json.loads(
        outcome.publication.model_dump_json()
    )
    assert collector.event_ids == {event.event_id for event in outcome.artifact.events}
    enrichment = json.loads(next(tmp_path.rglob("article-enrichment.json")).read_text())
    assert enrichment["items"][0]["status"] == "enriched"
    candidates = json.loads(next(tmp_path.rglob("candidates-enriched.json")).read_text())
    assert any("enriched-marker" in item["summary"] for item in candidates)
    evidence = json.loads(next(tmp_path.rglob("evidence-quality.json")).read_text())
    assert all(item["passed"] for item in evidence["items"])


async def test_a_thin_candidate_pool_degrades_instead_of_aborting(tmp_path: Path) -> None:
    """A short news day shrinks the issue; it no longer erases it."""

    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    gateway = FakeGateway(config)
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=FakeCollector(count=5),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        layout=_site(tmp_path),
    )

    outcome = await pipeline.run(_today(), publish=False)

    assert FailureClass.CANDIDATES_THIN in outcome.tracker.failures
    assert outcome.publication.level is PublicationLevel.L2B
    assert outcome.publication.details == []
    assert outcome.publication.briefs
    assert outcome.publication.marker_is_valid()
    # Nothing is invented: every brief is one of the collected candidates.
    collected = {event.event_id: event for event in outcome.artifact.events}
    for brief in outcome.publication.briefs:
        assert brief.event_id in collected
        assert brief.headline == collected[brief.event_id].title[:100]
        assert {str(ref.url) for ref in brief.sources} <= {
            str(item.url) for item in collected[brief.event_id].items
        }


async def test_an_exhausted_candidate_pool_blocks_the_day_before_any_model_call(
    tmp_path: Path,
) -> None:
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    gateway = FakeGateway(config)
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=FakeCollector(count=2),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        layout=_site(tmp_path),
    )

    outcome = await pipeline.run(_today(), publish=False)

    assert outcome.tracker.failures == [FailureClass.CANDIDATES_EXHAUSTED]
    assert outcome.tracker.blocked is True
    assert outcome.publication.level is PublicationLevel.L3
    assert outcome.publication.has_content is False
    assert gateway.ledger.requests == 0
    assert gateway.runs == []


async def test_short_feed_excerpts_demote_detailed_stories_to_briefs(tmp_path: Path) -> None:
    """Evidence too thin for a detailed story still yields a brief, never prose."""

    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=ShortEvidenceCollector(),  # type: ignore[arg-type]
        gateway=FakeGateway(config),  # type: ignore[arg-type]
        layout=_site(tmp_path),
    )

    outcome = await pipeline.run(_today(), publish=False)

    assert FailureClass.DETAIL_EVIDENCE_THIN in outcome.tracker.failures
    assert outcome.publication.level is PublicationLevel.L1
    assert outcome.publication.details == []
    assert outcome.publication.briefs
    assert "详报证据不足" in outcome.publication.degradation_reasons
    audit = json.loads(next(tmp_path.rglob("evidence-quality.json")).read_text())
    assert any(not item["passed"] for item in audit["items"])
    demoted = json.loads(next(tmp_path.rglob("editorial-plan-demoted.json")).read_text())
    assert {item["tier"] for item in demoted["selections"]} == {"brief"}


async def test_an_unreachable_editor_still_produces_a_brief_only_issue(tmp_path: Path) -> None:
    """The editor outage costs the prose, not the issue or the judgement.

    Judging succeeded before the editor failed, so the issue is rebuilt from
    those decisions (L2A) rather than from ranking alone (L2B). The decisions
    ride out on the exception; tuple unpacking never runs when a stage raises.
    """

    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    gateway = UnavailableEditorGateway(config)
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=FakeCollector(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        layout=_site(tmp_path),
    )

    outcome = await pipeline.run(_today(), publish=False)

    assert FailureClass.PLAN_FAILED in outcome.tracker.failures
    assert outcome.publication.level is PublicationLevel.L2A
    assert outcome.publication.details == []
    assert outcome.publication.viewpoints == []
    assert outcome.publication.briefs
    assert outcome.publication.degradation_reasons == ["编辑规划失败"]
    assert outcome.publication.marker_is_valid()
    assert len(list(tmp_path.rglob("decisions.json"))) == 1
    # Every brief traces back to something the judge actually selected, which
    # is what separates L2A from a bare ranking.
    judged = {
        decision["event_id"]
        for decision in json.loads(next(tmp_path.rglob("decisions.json")).read_text())
        if decision["selected"]
    }
    assert {brief.event_id for brief in outcome.publication.briefs} <= judged


async def test_editor_promoted_false_negative_is_enriched_after_planning(
    tmp_path: Path,
) -> None:
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    collector = PromotedFalseNegativeCollector()
    gateway = FalseNegativeGateway(config)
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=collector,  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        layout=_site(tmp_path),
    )

    outcome = await pipeline.run(_today(), publish=False)

    events = outcome.artifact.events
    promoted = next(event for event in events if event.event_id == gateway.rejected_id)
    assert gateway.rejected_id not in collector.enrichment_calls[0]
    assert gateway.rejected_id in collector.enrichment_calls[1]
    assert len(promoted.items[0].summary) >= DETAIL_EVIDENCE_MIN_CHARS


def _audit_event(index: int, sizes: list[int]) -> Event:
    items = [
        RawItem(
            source=f"source-{item_index}",
            source_tier=SourceTier.A,
            source_channel=SourceChannel.OFFICIAL,
            source_item_id=str(item_index),
            url=f"https://example.com/{index}/{item_index}",
            title=f"Evidence {item_index}",
            summary="x" * size,
            published_at=datetime(2026, 8, 12, tzinfo=UTC),
            discovered_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
        for item_index, size in enumerate(sizes)
    ]
    return Event(
        event_id=f"event-{index}",
        canonical_url=items[0].url,
        title=items[0].title,
        summary=items[0].summary,
        published_at=items[0].published_at,
        items=items,
    )


@pytest.mark.parametrize(("size", "passed"), [(799, False), (800, True)])
def test_detail_evidence_gate_enforces_boundary(size: int, passed: bool) -> None:
    event = _audit_event(0, [size])
    selection = EditorialSelection(
        event_id=event.event_id,
        tier=EditorialTier.LEAD,
        category="模型与平台",
        headline="Headline",
        brief="Brief",
        importance=90,
        confidence=0.9,
        reason="Reason",
        evidence_ids=["event-0-1"],
    )
    plan = EditorialPlan(
        today_highlight="Highlight",
        selections=[selection],
        editor_viewpoint=[
            EditorialInsight(text="One", evidence_ids=["event-0-1"]),
            EditorialInsight(text="Two", evidence_ids=["event-0-1"]),
        ],
    )

    audit = _detail_evidence_audit(plan, [event])

    assert audit[0]["passed"] is passed


def test_detail_evidence_uses_first_three_items_and_exempts_briefs() -> None:
    qualified_secondary = _audit_event(0, [10, 800])
    ignored_fourth = _audit_event(1, [10, 10, 10, 800])
    brief = _audit_event(2, [10])
    selections = [
        EditorialSelection(
            event_id=event.event_id,
            tier=tier,
            category="模型与平台",
            headline=f"Headline {event.event_id}",
            brief="Brief",
            importance=90 - index,
            confidence=0.9,
            reason="Reason",
            evidence_ids=[f"{event.event_id}-1"],
        )
        for index, (event, tier) in enumerate(
            [
                (qualified_secondary, EditorialTier.LEAD),
                (ignored_fourth, EditorialTier.FOLLOW),
                (brief, EditorialTier.BRIEF),
            ]
        )
    ]
    plan = EditorialPlan(
        today_highlight="Highlight",
        selections=selections,
        editor_viewpoint=[
            EditorialInsight(text="One", evidence_ids=["event-0-1"]),
            EditorialInsight(text="Two", evidence_ids=["event-0-1"]),
        ],
    )

    audit = _detail_evidence_audit(plan, [qualified_secondary, ignored_fourth, brief])

    assert [item["passed"] for item in audit] == [True, False]
    assert [item["event_id"] for item in audit] == ["event-0", "event-1"]


def test_detail_evidence_repair_swaps_unsupported_detail_with_grounded_brief() -> None:
    config = load_config(Path("config"))
    events = [_audit_event(index, [607 if index == 4 else 900]) for index in range(17)]
    tiers = [EditorialTier.LEAD] * 4 + [EditorialTier.FOLLOW] * 5
    tiers += [EditorialTier.BRIEF] * 8
    categories = ["模型与平台", "行业动态", "国内 AI", "值得试的项目"]
    selections = [
        EditorialSelection(
            event_id=event.event_id,
            tier=tiers[index],
            category=categories[index % len(categories)],  # type: ignore[arg-type]
            headline=f"Headline {index}",
            brief=f"Brief {index}",
            importance=100 - index,
            confidence=0.9,
            reason="Reason",
            evidence_ids=[f"{event.event_id}-1"],
        )
        for index, event in enumerate(events)
    ]
    plan = EditorialPlan(
        today_highlight="Highlight",
        selections=selections,
        editor_viewpoint=[
            EditorialInsight(text="One", evidence_ids=["event-0-1"]),
            EditorialInsight(text="Two", evidence_ids=["event-1-1"]),
        ],
    )

    repaired = _repair_detail_evidence(plan, events, config.pipeline)

    tiers_by_id = {item.event_id: item.tier for item in repaired.selections}
    assert tiers_by_id["event-4"] == EditorialTier.BRIEF
    assert tiers_by_id["event-9"] == EditorialTier.FOLLOW
    assert all(item["passed"] for item in _detail_evidence_audit(repaired, events))


async def test_spend_from_an_earlier_window_carries_into_this_runs_audit(
    tmp_path: Path,
) -> None:
    """The day's ledger is shared, so an earlier window's spend is still counted."""

    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    gateway = FakeGateway(config)
    gateway.ledger.requests = 1
    gateway.ledger.cost_cny = 0.25
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=FakeCollector(),  # type: ignore[arg-type]
        gateway=gateway,  # type: ignore[arg-type]
        layout=_site(tmp_path),
    )

    outcome = await pipeline.run(_today(), publish=False)

    assert outcome.publication.level is PublicationLevel.L0
    assert outcome.artifact.metadata["requests"] == gateway.ledger.requests >= 1
    assert outcome.artifact.metadata["cost_cny"] == 0.25
    assert outcome.artifact.metadata["request_limit"] == config.models.budget.request_limit


async def test_model_failure_still_writes_audit_artifact(tmp_path: Path) -> None:
    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    pipeline = DailyPipeline(
        config,
        Secrets(),
        client=_client(),
        collector=FakeCollector(),  # type: ignore[arg-type]
        gateway=FakeGateway(config, fail_plan=True),  # type: ignore[arg-type]
        layout=_site(tmp_path),
    )

    with pytest.raises(RuntimeError, match="editor failed"):
        await pipeline.run(_today(), publish=False)

    assert len(list(tmp_path.rglob("model-runs.json"))) == 1


async def test_publish_mode_needs_no_credentials_and_publishes_nothing_by_itself(
    tmp_path: Path,
) -> None:
    """Publishing is a separate, local transaction; the run only composes."""

    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    layout = _site(tmp_path)
    pipeline = DailyPipeline(
        config,
        Secrets(github_token=None),
        client=_client(),
        collector=FakeCollector(),  # type: ignore[arg-type]
        gateway=FakeGateway(config),  # type: ignore[arg-type]
        layout=layout,
    )

    outcome = await pipeline.run(_today(), publish=True)

    assert outcome.publication.level is PublicationLevel.L0
    assert outcome.artifact.publication is not None
    assert outcome.artifact.publication.status == "issue_published"
    assert list(layout.published.iterdir()) == []
    assert list(layout.releases.iterdir()) == []
    assert not layout.current.exists()


async def test_a_misconfigured_source_set_is_still_fatal(tmp_path: Path) -> None:
    """Degradation covers bad news days, not a configuration with no Tier A source."""

    config = load_config(Path("config"))
    config.pipeline.artifacts_dir = str(tmp_path)
    pipeline = DailyPipeline(config, Secrets(), client=_client(), layout=_site(tmp_path))
    health = [
        SourceHealth(source="tier-b", tier=SourceTier.B, status="ok", item_count=1, latency_ms=1)
    ]

    with pytest.raises(QualityGateFailed, match="no Tier A sources"):
        pipeline._check_source_health(health, DegradationTracker())


def test_low_tier_a_coverage_is_recorded_but_never_fatal() -> None:
    config = load_config(Path("config"))
    pipeline = DailyPipeline(config, Secrets(), client=_client())
    health = [
        SourceHealth(
            source=f"source-{index}",
            tier=SourceTier.A,
            status="ok" if index == 0 else "failed",
            item_count=1,
            latency_ms=1,
        )
        for index in range(4)
    ]
    tracker = DegradationTracker()

    pipeline._check_source_health(health, tracker)

    assert tracker.failures == [FailureClass.SOURCE_COVERAGE_LOW]
    assert tracker.ceiling() is PublicationLevel.L0
    assert tracker.blocked is False
