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

from ai_daily.benchmark import benchmark_models
from ai_daily.config import AppConfig, Secrets, load_config
from ai_daily.pipeline import DailyPipeline
from ai_daily.probe import probe_sources
from ai_daily.publication import LEVEL_NOTICE, DailyPublication, PublicationLevel
from ai_daily.render import render_fallback
from ai_daily.site_publisher import (
    RSS_LIMIT,
    PublicationRefused,
    SiteLayout,
    activate_release,
    build_archive,
    hold_previous_release,
    publication_lock,
    publish_site,
    published_dates,
    read_publication,
    recent_publications,
    render_release,
    write_status,
)
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

    with publication_lock(layout):
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

        if outcome.publication.level is PublicationLevel.L3:
            release = hold_previous_release(layout, LEVEL_NOTICE[PublicationLevel.L3] or "")
            status["action"] = "held_previous_release"
            status["release"] = str(release)
            write_status(layout, _status_payload(layout, outcome.publication, status))
            _emit({"level": "L3", "action": "held_previous_release"})
            return 1

        try:
            # publish_site owns the upgrade guard: a retry window may replace
            # today's issue only with a better one, never an equal or poorer.
            release = publish_site(
                layout, outcome.publication, _site_base_url(config, secrets)
            )
        except PublicationRefused as error:
            status["action"] = "refused"
            status["reason"] = str(error)
            write_status(layout, _status_payload(layout, outcome.publication, status))
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


async def _daily(args: argparse.Namespace) -> int:
    """The timer entry point.

    Verifies what is already live before spending anything. A full issue means
    there is nothing to do and the window costs nothing; a degraded issue is
    re-run so a later window can upgrade it.
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

    if existing is not None and existing.level is PublicationLevel.L0:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                verified = await verify_publication(existing, base_url, client)
            except PublicationNotVisible as error:
                _emit({"action": "republish", "reason": str(error)})
            else:
                _emit({"action": "noop", "level": verified.level.value})
                return 0

    args.mode = "publish"
    return await _run(args)


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

    benchmark = subparsers.add_parser("benchmark-models")
    benchmark.add_argument("--dataset", type=Path, required=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    handlers = {
        "run": _run,
        "daily": _daily,
        "verify": _verify,
        "rebuild-site": _rebuild,
        "write-fallback": _write_fallback,
        "probe-sources": _probe,
        "archive": _archive,
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
