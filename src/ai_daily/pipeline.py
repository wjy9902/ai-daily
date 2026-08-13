from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from ai_daily.artifacts import write_artifact
from ai_daily.assembler import assemble_markdown
from ai_daily.config import AppConfig, Secrets
from ai_daily.content import draft_selected, judge_events, plan_digest, validate_editorial_plan
from ai_daily.history import fetch_historical_index
from ai_daily.model_gateway import ModelGateway
from ai_daily.models import (
    DraftItem,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    Event,
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
from ai_daily.publisher import GitHubPublisher
from ai_daily.site_trust import verified_daily_marker
from ai_daily.sources import Collector


class QualityGateFailed(RuntimeError):
    pass


DETAIL_EVIDENCE_MIN_CHARS = 800


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
    ) -> None:
        self.config = config
        self.secrets = secrets or Secrets()
        self.client = client or httpx.AsyncClient(follow_redirects=True)
        self.collector = collector or Collector(self.client)
        self.gateway = gateway or ModelGateway(config.models, self.secrets)

    async def run(self, target_date: date, publish: bool) -> tuple[RunArtifact, str]:
        run_id = f"{target_date.isoformat()}-{uuid.uuid4().hex[:8]}"
        run_dir = Path(self.config.pipeline.artifacts_dir) / target_date.isoformat() / run_id
        repository = self.secrets.github_repository or self.config.pipeline.repository
        items, health = await self._collect(run_dir)
        filtered, candidates = await self._candidates(target_date, items, run_dir, repository)
        editorial_plan, drafts = await self._generate_content(candidates, run_dir)
        audited_requests = sum(run.request_count for run in self.gateway.runs)
        if audited_requests != self.gateway.ledger.requests:
            raise QualityGateFailed("model request audit is incomplete")
        body = assemble_markdown(target_date, editorial_plan, drafts, candidates)
        publication = await self._publish(target_date, body, repository, publish)
        artifact = RunArtifact(
            run_id=run_id,
            target_date=target_date,
            items=filtered,
            health=health,
            events=candidates,
            model_runs=self.gateway.runs,
            publication=publication,
            metadata={
                "candidate_count": len(candidates),
                "selected_count": len(editorial_plan.selections),
                "detailed_count": len(drafts),
                "model_requests": self.gateway.ledger.requests,
                "audited_model_requests": audited_requests,
                "input_tokens": self.gateway.ledger.input_tokens,
                "output_tokens": self.gateway.ledger.output_tokens,
                "cost_cny": round(self.gateway.ledger.cost_cny, 6),
            },
        )
        write_artifact(run_dir / "run.json", artifact)
        (run_dir / "digest.md").write_text(body, encoding="utf-8")
        return artifact, body

    async def _collect(self, run_dir: Path) -> tuple[list[RawItem], list[SourceHealth]]:
        items, health = await self.collector.collect(self.config.sources)
        write_artifact(
            run_dir / "sources.json",
            {
                "items": [item.model_dump(mode="json") for item in items],
                "health": [item.model_dump(mode="json") for item in health],
            },
        )
        self._check_source_health(health)
        return items, health

    async def _candidates(
        self,
        target_date: date,
        items: list[RawItem],
        run_dir: Path,
        repository: str,
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
        historical_index = await fetch_historical_index(
            self.client,
            repository,
            self.secrets.github_token,
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
        if len(candidates) < minimum_items:
            raise QualityGateFailed(f"only {len(candidates)} candidates remain after deduplication")
        return filtered, candidates

    async def _generate_content(
        self, candidates: list[Event], run_dir: Path
    ) -> tuple[EditorialPlan, list[DraftItem]]:
        try:
            decisions = await judge_events(self.gateway, candidates)
            write_artifact(run_dir / "decisions.json", decisions)
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
            failed = [item for item in evidence_audit if not item["passed"]]
            if failed:
                event_ids = ", ".join(str(item["event_id"]) for item in failed)
                raise QualityGateFailed(f"detailed stories lack article evidence: {event_ids}")
            drafts = await draft_selected(self.gateway, candidates, editorial_plan)
        finally:
            write_artifact(run_dir / "model-runs.json", self.gateway.runs)
        return editorial_plan, drafts

    async def _publish(
        self, target_date: date, body: str, repository: str, publish: bool
    ) -> Publication:
        publication = Publication(
            target_date=target_date,
            status="dry_run",
            marker=verified_daily_marker(body, target_date) or "invalid",
        )
        if publish:
            if not self.secrets.github_token:
                raise QualityGateFailed("GITHUB_TOKEN is required in publish mode")
            publisher = GitHubPublisher(repository, self.secrets.github_token, self.client)
            publication = await publisher.publish(target_date, body)
        return publication

    def _check_source_health(self, health: Sequence[SourceHealth]) -> None:
        tier_a = [item for item in health if item.tier == SourceTier.A]
        if not tier_a:
            raise QualityGateFailed("no Tier A sources are configured")
        successful = sum(item.status in {"ok", "partial", "not_modified"} for item in tier_a)
        coverage = successful / len(tier_a)
        if coverage < self.config.pipeline.tier_a_min_coverage:
            raise QualityGateFailed(f"Tier A source coverage is {coverage:.0%}")


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
