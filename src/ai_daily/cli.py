from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx

from ai_daily.artifacts import write_artifact
from ai_daily.benchmark import benchmark_models
from ai_daily.config import AppConfig, Secrets, load_config
from ai_daily.item_store import ItemStore
from ai_daily.papers import PapersPipeline, build_papers_publication, publication_gate
from ai_daily.papers_config import load_papers_config
from ai_daily.papers_models import (
    PaperCandidate,
    PapersPublication,
    PapersRunArtifact,
    load_papers_publication,
)
from ai_daily.pipeline import DailyPipeline, RunOutcome, collection_window
from ai_daily.probe import probe_sources
from ai_daily.publication import (
    LEVEL_NOTICE,
    DailyPublication,
    PublicationLevel,
    load_publication,
)
from ai_daily.render import render_fallback
from ai_daily.site_publisher import (
    RSS_LIMIT,
    PublicationRefused,
    SiteLayout,
    activate_release,
    build_archive,
    collect_lock,
    daily_run_lock,
    hold_previous_release,
    publication_lock,
    publish_papers_site,
    publish_site,
    published_dates,
    read_publication,
    recent_publications,
    render_release,
    write_status,
)
from ai_daily.sources import Collector
from ai_daily.verifier import PublicationNotVisible, verify_publication

BEIJING = ZoneInfo("Asia/Shanghai")


def _target_date(value: str | None) -> date:
    """The issue's date.

    Always today in Beijing when unset. A run that starts after a reboot does
    not try to backfill the day it missed: a news digest for a day that has
    already passed is not worth publishing, and the gap is shown in the
    archive instead of being papered over.
    """

    return date.fromisoformat(value) if value else datetime.now(BEIJING).date()


def _layout(args: argparse.Namespace) -> SiteLayout:
    root = getattr(args, "site_root", None) or os.environ.get("AI_DAILY_SITE_ROOT")
    if root:
        return SiteLayout(Path(root))
    return SiteLayout(Path.cwd() / "site")


def _site_base_url(config: AppConfig, secrets: Secrets) -> str:
    return str(
        os.environ.get("AI_DAILY_SITE_BASE_URL")
        or secrets.site_base_url
        or config.pipeline.site_base_url
    )


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


# ------------------------------------------------------------------ publishing


def _served_publication(layout: SiteLayout) -> DailyPublication | None:
    """The issue the site is actually serving right now.

    Two paths finish a run without publishing what the run built: the upgrade
    guard refuses a poorer retry, and an L3 holds the previous release. Both
    used to describe the rejected publication in status.json while
    ``latest_published`` named the record on disk, so 2026-09-04 reported
    9 details and 16 briefs for an issue that was never published - the record
    had 9 and 19 - and anyone reading the status to see what was live got the
    counts, level, notice and degradation reasons of a discarded run.
    """

    dates = published_dates(layout)
    if not dates:
        return None
    try:
        return read_publication(layout, dates[0])
    except ValueError:
        return None


def _status_payload(
    layout: SiteLayout,
    publication: DailyPublication | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    dates = published_dates(layout)
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "checked_at_beijing": datetime.now(BEIJING).isoformat(),
        "latest_published": dates[0].isoformat() if dates else None,
        "issue_count": len(dates),
        "level": publication.level.value if publication else None,
        "notice": publication.notice if publication else None,
        "detail_count": len(publication.details) if publication else 0,
        "brief_count": len(publication.briefs) if publication else 0,
        "degradation_reasons": publication.degradation_reasons if publication else [],
        **extra,
    }


async def _run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    secrets = Secrets()
    layout = _layout(args)
    layout.ensure()
    target = _target_date(args.date)
    publish = args.mode == "publish"

    # Two locks, two jobs. ``daily_run_lock`` spans the whole run so a second
    # daily fails before it spends anything. ``publication_lock`` is shared
    # with the papers publisher and is held only for the release transaction
    # below: gathering and drafting under it starved the papers issue for a
    # full 25 minutes, longer than the ten it is willing to wait.
    with daily_run_lock(layout):
        # Mark the run as started before anything can kill it, so a status file
        # that still says "running" hours later reads as the failure it is,
        # rather than as the previous run's success.
        write_status(
            layout,
            _status_payload(
                layout,
                _served_publication(layout),
                {
                    "action": "running",
                    "target_date": target.isoformat(),
                    "mode": args.mode,
                    "started_at": datetime.now(UTC).isoformat(),
                    **_collect_staleness(layout),
                },
            ),
        )
        async with httpx.AsyncClient(follow_redirects=True) as client:
            pipeline = DailyPipeline(config, secrets, client=client, layout=layout)
            outcome = await pipeline.run(target, publish=publish)

        status: dict[str, Any] = {
            "run_id": outcome.artifact.run_id,
            "target_date": target.isoformat(),
            "mode": args.mode,
            "budget": pipeline.gateway.ledger.snapshot(),
            "degradation_detail": dict(outcome.tracker.details),
            "sources": [item.model_dump(mode="json") for item in outcome.artifact.health],
            **_collect_staleness(layout),
        }

        if not publish:
            write_status(layout, _status_payload(layout, outcome.publication, status))
            _emit(
                {
                    "level": outcome.publication.level.value,
                    "marker": outcome.publication.marker,
                    "details": len(outcome.publication.details),
                    "briefs": len(outcome.publication.briefs),
                    "mode": "dry-run",
                }
            )
            return 0

        with publication_lock(layout):
            return _publish_daily(layout, outcome, status, _site_base_url(config, secrets))


def _publish_daily(
    layout: SiteLayout,
    outcome: RunOutcome,
    status: dict[str, Any],
    site_base_url: str,
) -> int:
    """The release transaction. The caller holds ``publication_lock``."""

    if outcome.publication.level is PublicationLevel.L3:
        release = hold_previous_release(layout, LEVEL_NOTICE[PublicationLevel.L3] or "")
        status["action"] = "held_previous_release"
        status["release"] = str(release)
        write_status(layout, _status_payload(layout, _served_publication(layout), status))
        _emit({"level": "L3", "action": "held_previous_release"})
        return 1

    try:
        # publish_site owns the upgrade guard: a retry window may replace
        # today's issue only with a better one, never an equal or poorer.
        release = publish_site(layout, outcome.publication, site_base_url)
    except PublicationRefused as error:
        status["action"] = "refused"
        status["reason"] = str(error)
        write_status(layout, _status_payload(layout, _served_publication(layout), status))
        _emit({"level": outcome.publication.level.value, "refused": str(error)})
        return 0

    status["action"] = "published"
    status["release"] = str(release)
    write_status(layout, _status_payload(layout, outcome.publication, status))
    _emit(
        {
            "level": outcome.publication.level.value,
            "marker": outcome.publication.marker,
            "release": str(release),
        }
    )
    return 0


#: Levels that end the day: the model stages ran and an issue with editorial
#: content is live. A rerun would spend a full round to build another issue of
#: the same level and then be refused by the same-day guard.
SETTLED_LEVELS = frozenset({PublicationLevel.L0, PublicationLevel.L1})


async def _daily(args: argparse.Namespace) -> int:
    """The timer entry point: one issue a day, and a retry only for failure.

    1. No record for today: run and publish.
    2. A record exists: check the site actually serves it. If not, rebuild the
       site from the record and check again - this is the recovery for a
       publish transaction killed between committing the record and flipping
       ``current``, at any level, not only L0.
    3. The record is L1 or L0: nothing to do. L1 is an issue whose editorial
       stages succeeded; rerunning it costs a full round and is refused.
    4. The record is a brief-only L2: the model stages failed, so run again;
       the guard admits the result only if it reaches L1 or L0.
    """

    config = load_config(Path(args.config_dir))
    secrets = Secrets()
    layout = _layout(args)
    layout.ensure()
    target = _target_date(args.date)
    base_url = _site_base_url(config, secrets)

    existing = None
    try:
        existing = read_publication(layout, target)
    except ValueError:
        existing = None

    if existing is None:
        args.mode = "publish"
        return await _run(args)

    if not await _served(existing, base_url):
        _emit({"action": "republish", "level": existing.level.value})
        rebuilt = await _rebuild(args)
        if rebuilt != 0 or not await _served(existing, base_url):
            write_status(
                layout,
                _status_payload(
                    layout, existing, {"action": "unrecoverable", "target_date": target.isoformat()}
                ),
            )
            _emit({"action": "unrecoverable", "level": existing.level.value})
            return 1

    if existing.level in SETTLED_LEVELS:
        _emit({"action": "noop", "level": existing.level.value})
        return 0

    args.mode = "publish"
    return await _run(args)


async def _served(publication: DailyPublication, base_url: str) -> bool:
    """Whether the live site serves exactly this record. Any failure to confirm counts as no."""

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            await verify_publication(publication, base_url, client)
        except (PublicationNotVisible, httpx.HTTPError) as error:
            _emit({"action": "not_visible", "reason": str(error)})
            return False
    return True


async def _collect_round(args: argparse.Namespace) -> int:
    """One collection round: fetch every source, merge into the store, no model calls."""

    config = load_config(Path(args.config_dir))
    layout = _layout(args)
    layout.ensure()
    started = datetime.now(UTC)
    cutoff, _ = collection_window(
        datetime.now(BEIJING).date(),
        config.pipeline.timezone,
        config.pipeline.collection_window_hours,
    )
    with collect_lock(layout):
        async with Collector() as collector:
            items, health = await collector.collect(config.sources)
        finished = datetime.now(UTC)
        with ItemStore(layout.item_store) as store:
            stats = store.merge_many(items, finished)
            pruned = store.prune(finished)
            record = store.record_round("collect", started, finished, health)
            total, in_window = store.counts(cutoff)
    status = {
        "round_id": record.round_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "ok_sources": record.ok_sources,
        "failed_sources": record.failed_sources,
        "failed": [item.source for item in health if item.status == "failed"],
        "fetched": len(items),
        "inserted": stats.inserted,
        "updated": stats.updated,
        "pruned": pruned,
        "stored": total,
        "in_window": in_window,
    }
    _write_atomic_status(layout.collect_status_file, status)
    _emit(
        {
            "action": "collected",
            **{
                k: status[k]
                for k in (
                    "round_id",
                    "ok_sources",
                    "failed_sources",
                    "fetched",
                    "inserted",
                    "stored",
                    "in_window",
                )
            },
        }
    )
    return 0 if record.ok_sources else 1


def _write_atomic_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _collect_staleness(layout: SiteLayout) -> dict[str, Any]:
    """How old the last collection round is, for status.json."""

    if not layout.collect_status_file.exists():
        return {"collect_age_hours": None, "collect_stale": True}
    try:
        payload = json.loads(layout.collect_status_file.read_text(encoding="utf-8"))
        finished = datetime.fromisoformat(payload["finished_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"collect_age_hours": None, "collect_stale": True}
    age = (datetime.now(UTC) - finished).total_seconds() / 3600
    return {"collect_age_hours": round(age, 2), "collect_stale": age > COLLECT_STALE_HOURS}


#: A collection round every three hours; four hours without one means the timer stopped.
COLLECT_STALE_HOURS = 4


async def _publish_artifact(args: argparse.Namespace) -> int:
    """Release an issue an earlier run built, without running anything again.

    ``--replace`` overrides the same-day guard; the record it displaces is
    backed up first (see publish_site). The timer entry point never takes
    this path.
    """

    config = load_config(Path(args.config_dir))
    secrets = Secrets()
    layout = _layout(args)
    layout.ensure()
    publication = load_publication(Path(args.artifact).read_text(encoding="utf-8"))
    previous = None
    try:
        previous = read_publication(layout, publication.target_date)
    except ValueError:
        previous = None
    with publication_lock(layout):
        try:
            release = publish_site(
                layout, publication, _site_base_url(config, secrets), replace=args.replace
            )
        except PublicationRefused as error:
            _emit({"level": publication.level.value, "refused": str(error)})
            return 1
    status = {
        "action": "replace" if args.replace else "published",
        "run_id": Path(args.artifact).parent.name,
        "target_date": publication.target_date.isoformat(),
        "previous_marker": previous.marker if previous else None,
        "release": str(release),
    }
    write_status(layout, _status_payload(layout, publication, status))
    _emit({"level": publication.level.value, **status})
    return 0


async def _verify(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    secrets = Secrets()
    layout = _layout(args)
    target = _target_date(args.date)
    publication = read_publication(layout, target)
    if publication is None:
        _emit({"error": "no publication record for that date", "date": target.isoformat()})
        return 1
    async with httpx.AsyncClient(follow_redirects=True) as client:
        verified = await verify_publication(publication, _site_base_url(config, secrets), client)
    _emit(
        {
            "target_date": verified.target_date.isoformat(),
            "level": verified.level.value,
            "marker": verified.marker,
            "page_url": verified.page_url,
        }
    )
    return 0 if verified.level is PublicationLevel.L0 else 2


async def _rebuild(args: argparse.Namespace) -> int:
    """Re-render the whole site from published records. Spends nothing."""

    config = load_config(Path(args.config_dir))
    secrets = Secrets()
    layout = _layout(args)
    layout.ensure()
    with publication_lock(layout):
        publications = recent_publications(layout, RSS_LIMIT)
        if not publications:
            _emit({"error": "no published records to rebuild from"})
            return 1
        latest = publications[0]
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        release = render_release(layout, latest, _site_base_url(config, secrets), stamp)
        activate_release(layout, release)
        write_status(layout, _status_payload(layout, latest, {"action": "rebuilt"}))
    _emit({"action": "rebuilt", "release": str(release), "issues": len(publications)})
    return 0


async def _write_fallback(args: argparse.Namespace) -> int:
    """Prebuild the page shown when there is nothing else to serve.

    It never goes through the normal render path, so it still works on the day
    the renderer is what broke.
    """

    config = load_config(Path(args.config_dir))
    layout = _layout(args)
    layout.ensure()
    page = render_fallback(_site_base_url(config, Secrets()))
    (layout.fallback / "index.html").write_text(page, encoding="utf-8")
    assets = layout.fallback / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[2] / "static" / "site.css"
    if source.exists():
        (assets / "site.css").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    _emit({"action": "fallback_written", "path": str(layout.fallback / "index.html")})
    return 0


async def _probe(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    rows = await probe_sources(
        config.sources,
        _target_date(args.date),
        config.pipeline.timezone,
        config.pipeline.collection_window_hours,
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    # ``probe_sources`` types its rows as ``dict[str, object]``; this column is
    # always the count of in-window items.
    usable = sum(1 for row in rows if int(cast(int, row.get("in_window", 0) or 0)) > 0)
    print(f"\n{usable}/{len(rows)} sources yielded at least one fresh item", flush=True)
    return 0


async def _papers(args: argparse.Namespace) -> int:
    """Select today's papers and optionally deep-read and publish them."""

    config_dir = Path(args.config_dir)
    config = load_papers_config(config_dir)
    layout = _layout(args)
    layout.ensure()
    if args.publish_artifact is not None:
        if args.mode != "publish":
            raise SystemExit("--publish-artifact only makes sense with --mode publish")
        return await _publish_papers_artifact(
            args.publish_artifact, layout, str(config.site_base_url)
        )
    target = datetime.now(BEIJING).date()
    pipeline = PapersPipeline(config, config_dir, layout, Secrets())
    try:
        artifact, run_dir = await pipeline.select_today(target)
        if not artifact.selected:
            _emit(
                {
                    "action": "held_previous_release",
                    "reasons": artifact.reasons,
                    "artifact": str(run_dir / "selection.json"),
                }
            )
            return 1
        if args.mode == "dry-run":
            _emit(_papers_dry_payload(artifact, run_dir))
            return 0
        return await _publish_selected_papers(
            pipeline, artifact.selected, target, run_dir, layout, str(config.site_base_url)
        )
    finally:
        await pipeline.aclose()


def _papers_dry_payload(artifact: PapersRunArtifact, run_dir: Path) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "artifact": str(run_dir / "selection.json"),
        "selected": [
            {
                "arxiv_id": item.arxiv_id,
                "title": item.title,
                "topic": item.topic,
                "supplement": item.supplement,
                "signals": item.signals.model_dump(mode="json"),
            }
            for item in artifact.selected
        ],
    }


async def _publish_selected_papers(
    pipeline: PapersPipeline,
    selected: list[PaperCandidate],
    target: date,
    run_dir: Path,
    layout: SiteLayout,
    site_base_url: str,
) -> int:
    publication = await build_papers_publication(
        target, selected, pipeline.collector, pipeline.gateway
    )
    artifact_path = run_dir / "publication.json"
    write_artifact(artifact_path, publication)
    return await _release_papers(publication, layout, site_base_url, artifact_path)


async def _publish_papers_artifact(
    artifact_path: Path, layout: SiteLayout, site_base_url: str
) -> int:
    """Release an issue an earlier run built but never got to publish.

    Deep-reading a day costs real money and the better part of an hour. When
    the release step loses (a busy lock, a systemd timeout), the finished
    publication is still on disk; this republishes it without paying twice.
    The marker check inside ``load_papers_publication`` refuses an artifact
    whose content no longer matches what was signed.
    """

    publication = load_papers_publication(artifact_path.read_text(encoding="utf-8"))
    return await _release_papers(publication, layout, site_base_url, artifact_path)


async def _release_papers(
    publication: PapersPublication,
    layout: SiteLayout,
    site_base_url: str,
    artifact_path: Path,
) -> int:
    accepted, reason = publication_gate(publication)
    if not accepted:
        _emit(
            {
                "action": "held_previous_release",
                "reason": reason,
                "artifact": str(artifact_path),
            }
        )
        return 1
    release = await publish_papers_site(layout, publication, site_base_url)
    _emit(
        {
            "action": "published",
            "release": str(release),
            "papers": len(publication.papers),
            "deep_reads": publication.deep_read_count,
        }
    )
    return 0


async def _archive(args: argparse.Namespace) -> int:
    """List every day since the first issue, gaps included."""

    layout = _layout(args)
    entries = build_archive(layout, recent_publications(layout, 400))
    print(
        json.dumps(
            [
                {
                    "date": entry.target_date.isoformat(),
                    "level": entry.level.value,
                    "published": entry.published,
                    "stories": entry.story_count,
                }
                for entry in entries
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-daily")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--site-root", default=None, help="site data root (or AI_DAILY_SITE_ROOT)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="collect, compose and optionally publish")
    run.add_argument("--date")
    run.add_argument("--mode", choices=("dry-run", "publish"), required=True)

    daily = subparsers.add_parser("daily", help="timer entry point: verify, then run if needed")
    daily.add_argument("--date")

    subparsers.add_parser("collect", help="fetch every source into the item store; no model calls")

    publish_artifact = subparsers.add_parser(
        "publish-artifact", help="release a publication.json an earlier run built"
    )
    publish_artifact.add_argument("artifact", type=Path)
    publish_artifact.add_argument(
        "--replace",
        action="store_true",
        help="override the same-day guard; the displaced record is backed up first",
    )

    verify = subparsers.add_parser("verify", help="check the live site serves today's issue")
    verify.add_argument("--date")

    rebuild = subparsers.add_parser("rebuild-site", help="re-render from published records")
    rebuild.add_argument("--date")

    fallback = subparsers.add_parser("write-fallback", help="prebuild the fallback page")
    fallback.add_argument("--date")

    probe = subparsers.add_parser("probe-sources", help="diagnose every configured source")
    probe.add_argument("--date")

    archive = subparsers.add_parser("archive", help="list published days and gaps")
    archive.add_argument("--date")

    papers = subparsers.add_parser("papers", help="select and deep-read today's papers")
    papers.add_argument("--mode", choices=("dry-run", "publish"), required=True)
    papers.add_argument(
        "--publish-artifact",
        type=Path,
        help="publish a publication.json an earlier run built but never released",
    )

    benchmark = subparsers.add_parser("benchmark-models")
    benchmark.add_argument("--dataset", type=Path, required=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    handlers = {
        "run": _run,
        "daily": _daily,
        "collect": _collect_round,
        "publish-artifact": _publish_artifact,
        "verify": _verify,
        "rebuild-site": _rebuild,
        "write-fallback": _write_fallback,
        "probe-sources": _probe,
        "archive": _archive,
        "papers": _papers,
    }
    handler = handlers.get(args.command)
    if handler is not None:
        code = asyncio.run(handler(args))
    else:
        config = load_config(Path(args.config_dir))
        result = asyncio.run(benchmark_models(args.dataset, config, Secrets()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
