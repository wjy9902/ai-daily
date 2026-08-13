from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pydantic_ai.exceptions import UsageLimitExceeded

from ai_daily.artifacts import write_artifact
from ai_daily.budget import BudgetExceeded, BudgetLedger, StageBudgetExceeded
from ai_daily.composer import (
    ComposeError,
    build_brief_only_publication,
    build_full_publication,
    build_judged_publication,
    build_ranked_publication,
)
from ai_daily.config import AppConfig, Secrets
from ai_daily.content import draft_selected, judge_events, plan_digest, validate_editorial_plan
from ai_daily.degradation import DegradationTracker, FailureClass
from ai_daily.history import local_historical_index
from ai_daily.model_gateway import (
    MissingProviderSecret,
    ModelGateway,
    ModelInvocationFailed,
)
from ai_daily.models import (
    DraftItem,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    Event,
    JudgeDecision,
    PipelineConfig,
    Publication,
    RawItem,
    RunArtifact,
    SourceChannel,
    SourceHealth,
    SourceTier,
)
from ai_daily.normalize import (
    canonicalize_url,
    cluster_items,
    is_ai_related,
    remove_historical,
    score_events,
    select_candidate_pool,
)
from ai_daily.publication import DailyPublication, PublicationLevel
from ai_daily.site_publisher import SiteLayout
from ai_daily.sources import Collector


class QualityGateFailed(RuntimeError):
    """Raised only for misconfiguration, never for a bad news day."""


class ModelStageFailed(RuntimeError):
    """A model stage failed in a way that maps onto a degraded level.

    Carries whatever the earlier stages did produce. Without this the judge
    decisions are lost when planning fails, and a run that could have published
    a judged issue (L2A) falls all the way to bare ranking (L2B).
    """

    def __init__(
        self,
        failure: FailureClass,
        decisions: list[JudgeDecision] | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(detail or failure.value)
        self.failure = failure
        self.decisions = decisions or []
        self.detail = detail


@dataclass(frozen=True)
class RunOutcome:
    """Everything a caller needs to publish and report on a run."""

    artifact: RunArtifact
    publication: DailyPublication
    tracker: DegradationTracker
    run_dir: Path


DETAIL_EVIDENCE_MIN_CHARS = 800

#: Below this many fresh candidates there is no issue worth publishing, and the
#: site holds yesterday's rather than showing a near-empty page.
MINIMUM_PUBLISHABLE_CANDIDATES = 5


def _classify_model_failure(error: Exception, stage: str) -> FailureClass:
    """Name the failure after the stage that actually failed.

    ``stage`` matters: reporting a judge outage as 编辑规划失败 sends the
    operator to the wrong place, and it was doing exactly that.
    """

    if isinstance(error, StageBudgetExceeded | BudgetExceeded | UsageLimitExceeded):
        # UsageLimitExceeded is pydantic-ai's own ceiling, raised inside a run
        # rather than by our ledger. It means the same thing: stop spending.
        return FailureClass.BUDGET_EXHAUSTED
    if isinstance(error, MissingProviderSecret):
        # A revoked or missing key at 04:20 must not cost the day. Nothing
        # model-driven can run, so the issue falls all the way back to ranking,
        # and status.json carries the reason.
        return FailureClass.JUDGE_FAILED
    if stage == "judge":
        return FailureClass.JUDGE_FAILED
    if stage == "draft":
        return FailureClass.DRAFT_FAILED
    return FailureClass.PLAN_FAILED


def _demote_selections(plan: EditorialPlan, event_ids: set[str]) -> EditorialPlan:
    """Move the named stories down to brief tier, keeping everything else."""

    selections = [
        selection.model_copy(update={"tier": EditorialTier.BRIEF})
        if selection.event_id in event_ids
        else selection
        for selection in plan.selections
    ]
    return plan.model_copy(update={"selections": selections})


def filter_fresh_items(
    items: list[RawItem], cutoff: datetime, run_time: datetime, timezone: ZoneInfo
) -> tuple[list[RawItem], dict[str, object]]:
    relevant = [item for item in items if is_ai_related(item)]
    accepted: list[RawItem] = []
    accepted_corroboration: list[dict[str, str]] = []
    rejected_undated: list[dict[str, str]] = []
    rejected_outside_window: list[dict[str, str]] = []
    latest_allowed = run_time + timedelta(minutes=5)
    verified_urls: set[str] = set()
    for item in relevant:
        item_summary = {
            "source": item.source,
            "title": item.title,
            "url": str(item.url),
        }
        if item.published_at is None:
            if item.source_channel == SourceChannel.COMMUNITY:
                continue
            rejected_undated.append(item_summary)
            continue
        published_at = item.published_at.astimezone(timezone)
        if not cutoff <= published_at <= latest_allowed:
            rejected_outside_window.append(
                {**item_summary, "published_at": item.published_at.isoformat()}
            )
            continue
        accepted.append(item)
        verified_urls.add(canonicalize_url(str(item.url)))
    for item in relevant:
        if item.published_at is not None or item.source_channel != SourceChannel.COMMUNITY:
            continue
        item_summary = {
            "source": item.source,
            "title": item.title,
            "url": str(item.url),
        }
        observed_at = item.discovered_at.astimezone(timezone)
        if (
            cutoff <= observed_at <= latest_allowed
            and canonicalize_url(str(item.url)) in verified_urls
        ):
            accepted.append(item)
            accepted_corroboration.append(item_summary)
        else:
            rejected_undated.append(item_summary)
    return accepted, {
        "policy": "verified-publication-time-with-exact-url-community-corroboration",
        "cutoff": cutoff.isoformat(),
        "run_time": run_time.isoformat(),
        "accepted_count": len(accepted),
        "accepted_corroboration": accepted_corroboration,
        "rejected_undated": rejected_undated,
        "rejected_outside_window": rejected_outside_window,
    }


def collection_window(
    target_date: date,
    timezone_name: str,
    window_hours: int,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    local_timezone = ZoneInfo(timezone_name)
    run_time = now.astimezone(local_timezone) if now else datetime.now(local_timezone)
    if run_time.date() != target_date:
        run_time = datetime.combine(target_date, run_time.timetz())
    return run_time - timedelta(hours=window_hours), run_time


class DailyPipeline:
    def __init__(
        self,
        config: AppConfig,
        secrets: Secrets | None = None,
        client: httpx.AsyncClient | None = None,
        collector: Collector | None = None,
        gateway: ModelGateway | None = None,
        layout: SiteLayout | None = None,
    ) -> None:
        self.config = config
        self.secrets = secrets or Secrets()
        self.client = client or httpx.AsyncClient(follow_redirects=True)
        self.collector = collector or Collector()
        self.layout = layout or SiteLayout(Path(self.config.pipeline.artifacts_dir).parent)
        self.gateway = gateway or ModelGateway(config.models, self.secrets)
        # An injected gateway keeps its own ledger; tests want that. Only a
        # gateway we own gets rebound to the day's on-disk budget in run().
        self._owns_gateway = gateway is None

    def _bind_daily_budget(self, target_date: date) -> None:
        """Point the ledger at this date's on-disk budget.

        Without this the ceiling is per process, so three timer windows in one
        morning could each spend the full daily allowance. The path depends on
        the target date, which is not known until the run starts.
        """

        if not self._owns_gateway:
            return
        ledger = BudgetLedger(
            self.config.models.budget,
            store_path=self.layout.budget_path(target_date),
        )
        ledger.start_run()
        self.gateway.ledger = ledger

    async def run(self, target_date: date, publish: bool) -> RunOutcome:
        """Produce the best issue today's inputs allow.

        No quality problem raises here any more. Each stage records what went
        wrong and the run falls through to whichever builder that failure still
        permits, so a bad morning shrinks the issue instead of erasing it.
        """

        run_id = f"{target_date.isoformat()}-{uuid.uuid4().hex[:8]}"
        run_dir = Path(self.config.pipeline.artifacts_dir) / target_date.isoformat() / run_id
        tracker = DegradationTracker()
        self.layout.ensure()
        self._bind_daily_budget(target_date)

        items, health = await self._collect(run_dir, tracker)
        filtered, candidates = await self._candidates(target_date, items, run_dir, tracker)

        if tracker.blocked or not candidates:
            publication = build_brief_only_publication(
                target_date=target_date,
                briefs=[],
                level=PublicationLevel.L3,
                tracker=tracker,
            )
        else:
            publication = await self._compose(target_date, candidates, run_dir, tracker)

        artifact = RunArtifact(
            run_id=run_id,
            target_date=target_date,
            items=filtered,
            health=health,
            events=candidates,
            model_runs=self.gateway.runs,
            publication=Publication(
                target_date=target_date,
                status="issue_published" if publish else "dry_run",
                marker=publication.marker or "invalid",
            ),
            metadata={
                "candidate_count": len(candidates),
                "level": publication.level.value,
                "detail_count": len(publication.details),
                "brief_count": len(publication.briefs),
                "degradation": [failure.value for failure in tracker.failures],
                "degradation_detail": dict(tracker.details),
                **self.gateway.ledger.snapshot(),
            },
        )
        write_artifact(run_dir / "run.json", artifact)
        write_artifact(run_dir / "publication.json", publication)
        return RunOutcome(
            artifact=artifact,
            publication=publication,
            tracker=tracker,
            run_dir=run_dir,
        )

    async def _compose(
        self,
        target_date: date,
        candidates: list[Event],
        run_dir: Path,
        tracker: DegradationTracker,
    ) -> DailyPublication:
        """Run the model stages, degrading to the best surviving builder."""

        decisions: list[JudgeDecision] = []
        plan: EditorialPlan | None = None
        drafts: list[DraftItem] = []
        try:
            decisions, plan, drafts = await self._generate_content(candidates, run_dir, tracker)
        except ModelStageFailed as error:
            # Tuple unpacking above never runs when the stage raises, so the
            # partial judge output has to come off the exception.
            decisions = error.decisions
            tracker.record(error.failure, error.detail)

        if plan is not None:
            try:
                return build_full_publication(target_date, plan, drafts, candidates, tracker)
            except ComposeError:
                tracker.record(FailureClass.PLAN_FAILED)

        if decisions:
            return build_judged_publication(target_date, decisions, candidates, tracker)
        return build_ranked_publication(target_date, candidates, tracker)

    async def _collect(
        self, run_dir: Path, tracker: DegradationTracker
    ) -> tuple[list[RawItem], list[SourceHealth]]:
        items, health = await self.collector.collect(self.config.sources)
        write_artifact(
            run_dir / "sources.json",
            {
                "items": [item.model_dump(mode="json") for item in items],
                "health": [item.model_dump(mode="json") for item in health],
            },
        )
        self._check_source_health(health, tracker)
        return items, health

    async def _candidates(
        self,
        target_date: date,
        items: list[RawItem],
        run_dir: Path,
        tracker: DegradationTracker,
    ) -> tuple[list[RawItem], list[Event]]:
        timezone = ZoneInfo(self.config.pipeline.timezone)
        cutoff, run_time = collection_window(
            target_date,
            self.config.pipeline.timezone,
            self.config.pipeline.collection_window_hours,
        )
        filtered, freshness_audit = filter_fresh_items(items, cutoff, run_time, timezone)
        write_artifact(run_dir / "freshness.json", freshness_audit)
        events = score_events(
            cluster_items(filtered, self.config.pipeline.cluster_window_hours),
            run_time.astimezone(UTC),
        )
        historical_index = local_historical_index(
            self.layout.published,
            self.config.pipeline.history_window_days,
            target_date,
        )
        deduplicated = remove_historical(events, historical_index)
        candidates = select_candidate_pool(
            deduplicated,
            self.config.pipeline.candidate_limit,
            self.config.pipeline.max_research_candidates,
            self.config.pipeline.max_release_candidates,
        )
        write_artifact(run_dir / "candidates.json", candidates)
        minimum_items = (
            self.config.pipeline.lead_min
            + self.config.pipeline.follow_min
            + self.config.pipeline.brief_min
        )
        if len(candidates) < MINIMUM_PUBLISHABLE_CANDIDATES:
            tracker.record(FailureClass.CANDIDATES_EXHAUSTED)
        elif len(candidates) < minimum_items:
            tracker.record(FailureClass.CANDIDATES_THIN)
        return filtered, candidates

    async def _generate_content(
        self, candidates: list[Event], run_dir: Path, tracker: DegradationTracker
    ) -> tuple[list[JudgeDecision], EditorialPlan | None, list[DraftItem]]:
        decisions: list[JudgeDecision] = []
        stage = "judge"
        try:
            decisions, judge_failures = await judge_events(self.gateway, candidates)
            write_artifact(run_dir / "decisions.json", decisions)
            if judge_failures:
                # Some candidates went unjudged. That costs coverage, not the
                # issue, so it caps the level rather than ending the run.
                tracker.record(
                    FailureClass.JUDGE_PARTIAL, "; ".join(judge_failures)
                )
            if not decisions:
                raise ModelStageFailed(FailureClass.JUDGE_FAILED)
            enrichment_ids = {
                decision.event_id
                for decision in decisions
                if decision.selected or decision.relevance >= 70
            }
            enrichment = await self.collector.enrich_event_content(
                candidates,
                self.config.sources,
                enrichment_ids,
            )
            stage = "plan"
            editorial_plan = await plan_digest(
                self.gateway,
                candidates,
                decisions,
                self.config.pipeline,
            )
            write_artifact(run_dir / "editorial-plan.json", editorial_plan)
            detail_ids = {
                selection.event_id
                for selection in editorial_plan.selections
                if selection.tier != EditorialTier.BRIEF
            }
            missed_detail_ids = detail_ids - enrichment_ids
            if missed_detail_ids:
                enrichment.extend(
                    await self.collector.enrich_event_content(
                        candidates,
                        self.config.sources,
                        missed_detail_ids,
                    )
                )
            write_artifact(run_dir / "article-enrichment.json", {"items": enrichment})
            write_artifact(run_dir / "candidates-enriched.json", candidates)
            original_plan = editorial_plan
            editorial_plan = _repair_detail_evidence(
                editorial_plan,
                candidates,
                self.config.pipeline,
            )
            if editorial_plan != original_plan:
                write_artifact(run_dir / "editorial-plan-adjusted.json", editorial_plan)
            evidence_audit = _detail_evidence_audit(editorial_plan, candidates)
            write_artifact(run_dir / "evidence-quality.json", {"items": evidence_audit})
            failed = {str(item["event_id"]) for item in evidence_audit if not item["passed"]}
            if failed:
                # Repair already tried swapping these out. Rather than losing the
                # whole issue, the stories that still lack evidence drop out of
                # the detailed set and the issue publishes as L1.
                tracker.record(FailureClass.DETAIL_EVIDENCE_THIN)
                editorial_plan = _demote_selections(editorial_plan, failed)
                write_artifact(run_dir / "editorial-plan-demoted.json", editorial_plan)
            stage = "draft"
            drafts = await draft_selected(self.gateway, candidates, editorial_plan)
        except (
            BudgetExceeded,
            ModelInvocationFailed,
            MissingProviderSecret,
            UsageLimitExceeded,
            ValueError,
        ) as error:
            raise ModelStageFailed(
                _classify_model_failure(error, stage),
                decisions,
                f"{stage} stage: {type(error).__name__}: {error}",
            ) from error
        finally:
            write_artifact(run_dir / "model-runs.json", self.gateway.runs)
            await self.collector.aclose()
        return decisions, editorial_plan, drafts

    def _check_source_health(
        self, health: Sequence[SourceHealth], tracker: DegradationTracker
    ) -> None:
        """Record coverage, but never stop the run on it.

        Coverage counts sources that answered, not sources that yielded fresh
        news: it can read 100% on a day with nothing worth publishing and 40%
        on a day with plenty. Whether to publish is decided later, on the
        actual candidate count.
        """

        tier_a = [item for item in health if item.tier == SourceTier.A]
        if not tier_a:
            raise QualityGateFailed("no Tier A sources are configured")
        successful = sum(item.status in {"ok", "partial", "not_modified"} for item in tier_a)
        coverage = successful / len(tier_a)
        if coverage < self.config.pipeline.tier_a_min_coverage:
            tracker.record(FailureClass.SOURCE_COVERAGE_LOW)


def _detail_evidence_audit(
    plan: EditorialPlan,
    events: list[Event],
) -> list[dict[str, object]]:
    events_by_id = {event.event_id: event for event in events}
    audit: list[dict[str, object]] = []
    for selection in plan.selections:
        if selection.tier == EditorialTier.BRIEF:
            continue
        event = events_by_id[selection.event_id]
        evidence_chars = [len(item.summary or item.title) for item in event.items[:3]]
        total_chars = sum(evidence_chars)
        max_chars = max(evidence_chars, default=0)
        audit.append(
            {
                "event_id": selection.event_id,
                "tier": selection.tier.value,
                "evidence_chars": evidence_chars,
                "total_chars": total_chars,
                "max_chars": max_chars,
                "passed": max_chars >= DETAIL_EVIDENCE_MIN_CHARS,
            }
        )
    return audit


def _repair_detail_evidence(
    plan: EditorialPlan,
    events: list[Event],
    config: PipelineConfig,
) -> EditorialPlan:
    """Swap unsupported details with grounded briefs while preserving editorial quotas."""
    repaired = plan
    while True:
        failed = [item for item in _detail_evidence_audit(repaired, events) if not item["passed"]]
        if not failed:
            return repaired
        failed_id = str(failed[0]["event_id"])
        failed_selection = next(item for item in repaired.selections if item.event_id == failed_id)
        briefs = sorted(
            (item for item in repaired.selections if item.tier == EditorialTier.BRIEF),
            key=lambda item: item.importance,
            reverse=True,
        )
        replacement = _valid_detail_replacement(
            repaired,
            failed_selection,
            briefs,
            events,
            config,
        )
        if replacement is None:
            return repaired
        repaired = replacement


def _valid_detail_replacement(
    plan: EditorialPlan,
    failed: EditorialSelection,
    briefs: list[EditorialSelection],
    events: list[Event],
    config: PipelineConfig,
) -> EditorialPlan | None:
    tier_rank = {EditorialTier.LEAD: 0, EditorialTier.FOLLOW: 1, EditorialTier.BRIEF: 2}
    for candidate in briefs:
        swapped: list[EditorialSelection] = []
        for item in plan.selections:
            if item.event_id == failed.event_id:
                swapped.append(item.model_copy(update={"tier": EditorialTier.BRIEF}))
            elif item.event_id == candidate.event_id:
                swapped.append(item.model_copy(update={"tier": failed.tier}))
            else:
                swapped.append(item)
        swapped.sort(key=lambda item: (tier_rank[item.tier], -item.importance))
        proposal = plan.model_copy(update={"selections": swapped})
        if any(not item["passed"] for item in _detail_evidence_audit(proposal, events)):
            continue
        try:
            validate_editorial_plan(proposal, events, config)
        except ValueError:
            continue
        return proposal
    return None
