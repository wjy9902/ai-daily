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
from ai_daily.content import draft_selected, judge_events, plan_digest
from ai_daily.history import fetch_historical_index
from ai_daily.model_gateway import ModelGateway
from ai_daily.models import (
    DraftItem,
    EditorialPlan,
    Event,
    Publication,
    RawItem,
    RunArtifact,
    SourceHealth,
    SourceTier,
)
from ai_daily.normalize import (
    cluster_items,
    is_ai_related,
    remove_historical,
    score_events,
    select_candidate_pool,
)
from ai_daily.publisher import GitHubPublisher
from ai_daily.site_trust import daily_marker
from ai_daily.sources import Collector


class QualityGateFailed(RuntimeError):
    pass


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
        filtered = [
            item
            for item in items
            if is_ai_related(item)
            and cutoff
            <= (item.published_at or item.discovered_at).astimezone(timezone)
            <= run_time + timedelta(minutes=5)
        ]
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
            editorial_plan = await plan_digest(
                self.gateway,
                candidates,
                decisions,
                self.config.pipeline,
            )
            write_artifact(run_dir / "editorial-plan.json", editorial_plan)
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
            marker=daily_marker(target_date),
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
