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
from ai_daily.persona_cli import (
    authorize_wechat as _authorize_wechat,
)
from ai_daily.persona_cli import (
    persona_draft as _persona_draft,
)
from ai_daily.persona_cli import (
    wechat_probe as _wechat_probe,
)
from ai_daily.persona_cli import (
    wechat_reconcile as _wechat_reconcile,
)
from ai_daily.persona_models import PersonaEdition
from ai_daily.persona_pipeline import PersonaPipeline
from ai_daily.persona_render import render_persona_placeholder
from ai_daily.persona_replay import freeze_replay_dataset, run_replay
from ai_daily.persona_snapshot import activate_upstream_snapshot, load_upstream_snapshot
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
    persona_run_lock,
    prune_releases,
    publication_lock,
    publish_site,
    published_dates,
    read_publication,
    recent_persona_editions,
    recent_publications,
    render_release,
    write_persona_status,
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
            release = publish_site(layout, outcome.publication, _site_base_url(config, secrets))
        except PublicationRefused as error:
            if _repair_snapshot_pointer(layout, target, outcome.publication.marker):
                status["action"] = "snapshot_pointer_repaired"
                write_status(layout, _status_payload(layout, outcome.publication, status))
                _emit(
                    {
                        "action": "snapshot_pointer_repaired",
                        "marker": outcome.publication.marker,
                    }
                )
                return 0
            status["action"] = "refused"
            status["reason"] = str(error)
            write_status(layout, _status_payload(layout, outcome.publication, status))
            _emit({"level": outcome.publication.level.value, "refused": str(error)})
            return 0

        activate_upstream_snapshot(layout, target, outcome.publication.marker)

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


def _repair_snapshot_pointer(layout: SiteLayout, target: date, marker: str) -> bool:
    """Repair the only crash gap left after the site commit point."""

    committed = read_publication(layout, target)
    if committed is None or committed.marker != marker:
        return False
    try:
        activate_upstream_snapshot(layout, target, marker)
    except (OSError, ValueError, KeyError):
        return False
    return True


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
                if _repair_snapshot_pointer(layout, target, existing.marker):
                    _emit({"action": "noop", "level": verified.level.value})
                    return 0
                _emit({"action": "republish", "reason": "upstream snapshot missing"})

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
    persona = layout.fallback / "jiayu"
    persona.mkdir(parents=True, exist_ok=True)
    (persona / "index.html").write_text(
        render_persona_placeholder(_site_base_url(config, Secrets()), held=False),
        encoding="utf-8",
    )
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


async def _persona_run(args: argparse.Namespace) -> int:
    with persona_run_lock(_layout(args)):
        return await _persona_run_unlocked(args)


async def _persona_run_unlocked(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    layout = _layout(args)
    layout.ensure()
    target = _target_date(args.date)
    edition_path = layout.persona_edition_path(target)
    edition = _read_persona_edition(layout, target)
    active_marker = load_upstream_snapshot(layout, target).publication_marker
    if edition_path.exists() and (edition is None or not edition.hash_is_valid()):
        return _hold_existing_persona(
            layout,
            target,
            "existing persona edition is invalid; refusing to regenerate an immutable date",
        )
    if edition is not None and edition.input_marker != active_marker:
        return _hold_existing_persona(
            layout,
            target,
            "upstream marker changed after the persona edition was frozen",
        )
    if layout.wechat_target_path(target).exists() and edition is None:
        return _hold_existing_persona(
            layout,
            target,
            "immutable WeChat target exists without its persona edition",
        )
    if edition is None:
        pipeline = PersonaPipeline(
            config,
            Secrets(),
            layout,
            Path(args.config_dir).resolve().parent,
            target,
        )
        result = await pipeline.run(target, getattr(args, "resume_run", None))
        if result.editorial_state == "held":
            write_persona_status(layout, result.model_dump(mode="json"))
            _emit(result.model_dump(mode="json"))
            return 1
        edition = _read_persona_edition(layout, target)
    if edition is None:
        _emit({"error": "persona edition was not persisted"})
        return 1
    if args.mode in {"site", "draft"}:
        _publish_persona_site(config, layout, edition)
    if args.mode == "draft":
        return await _persona_draft(args, config, layout, edition)
    action = "persona_ready" if args.mode == "dry-run" else "persona_site_published"
    write_persona_status(
        layout,
        {
            "target_date": target.isoformat(),
            "editorial_state": "ready",
            "site_state": "published" if args.mode == "site" else "not_attempted",
            "wechat_state": "not_attempted",
            "aggregate_state": "ready",
            "edition_sha256": edition.payload_sha256,
            "action": action,
        },
    )
    _emit(
        {
            "action": action,
            "date": target.isoformat(),
            "edition_sha256": edition.payload_sha256,
        }
    )
    return 0


def _hold_existing_persona(layout: SiteLayout, target: date, reason: str) -> int:
    payload = {
        "target_date": target.isoformat(),
        "editorial_state": "held",
        "site_state": "not_attempted",
        "wechat_state": "not_attempted",
        "aggregate_state": "held",
        "reason": reason,
        "action": "persona_immutable_date_held",
    }
    write_persona_status(layout, payload)
    _emit(payload)
    return 1


async def _persona_daily(args: argparse.Namespace) -> int:
    layout = _layout(args)
    target = _target_date(args.date)
    pointer = layout.upstream_pointer_path(target)
    if not pointer.exists():
        _emit({"action": "held", "reason": "upstream snapshot is not active"})
        return 1
    first = _persona_stability_state(layout, pointer)
    await asyncio.sleep(args.stability_seconds)
    if _persona_stability_state(layout, pointer) != first:
        _emit(
            {
                "action": "held",
                "reason": "upstream marker or active release changed during stability window",
            }
        )
        return 1
    return await _persona_run(args)


def _persona_stability_state(layout: SiteLayout, pointer: Path) -> tuple[bytes, str]:
    if not layout.current.exists():
        raise ValueError("active site release is missing")
    return pointer.read_bytes(), str(layout.current.resolve())


def _read_persona_edition(layout: SiteLayout, target: date) -> PersonaEdition | None:
    path = layout.persona_edition_path(target)
    if not path.exists():
        return None
    try:
        return PersonaEdition.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _publish_persona_site(config: AppConfig, layout: SiteLayout, edition: PersonaEdition) -> Path:
    with publication_lock(layout):
        publication = read_publication(layout, edition.target_date)
        if publication is None or publication.marker != edition.input_marker:
            raise ValueError("persona edition does not match committed daily publication")
        latest = recent_publications(layout, 1)
        if not latest:
            raise ValueError("no committed daily publication exists")
        if _active_release_contains(layout, latest[0], edition):
            return layout.current.resolve()
        stamp = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-persona"
        release = render_release(layout, latest[0], _site_base_url(config, Secrets()), stamp)
        activate_release(layout, release)
        prune_releases(layout)
    return release


def _active_release_contains(
    layout: SiteLayout,
    latest: DailyPublication,
    edition: PersonaEdition,
) -> bool:
    if not layout.current.exists():
        return False
    active = layout.current.resolve()
    daily = active / "daily" / latest.target_date.isoformat() / "index.html"
    persona = active / "jiayu" / f"{edition.target_date.isoformat()}.html"
    expected_editions = recent_persona_editions(layout)
    archive_complete = all(
        (active / "jiayu" / f"{item.target_date.isoformat()}.html").exists()
        and item.payload_sha256
        in (active / "jiayu" / f"{item.target_date.isoformat()}.html").read_text(encoding="utf-8")
        for item in expected_editions
    )
    return (
        daily.exists()
        and persona.exists()
        and latest.marker in daily.read_text(encoding="utf-8")
        and edition.payload_sha256 in persona.read_text(encoding="utf-8")
        and archive_complete
    )


async def _freeze_persona_replay(args: argparse.Namespace) -> int:
    dates = [date.fromisoformat(value) for value in args.dates.split(",")]
    config = load_config(Path(args.config_dir))
    payload = freeze_replay_dataset(
        _layout(args),
        dates,
        args.output,
        config,
        Path(args.config_dir).resolve().parent,
    )
    _emit({"action": "replay_frozen", "cases": len(payload["cases"])})
    return 0


async def _persona_replay(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    report = await run_replay(
        config,
        Secrets(),
        _layout(args),
        Path(args.config_dir).resolve().parent,
        args.dataset,
    )
    write_artifact(args.output, report)
    _emit({"action": "replay_complete", **report})
    return 0 if report["held_count"] == 0 else 1


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

    persona = subparsers.add_parser("persona-run", help="build the 甲鱼主编版")
    persona.add_argument("--date")
    persona.add_argument("--mode", choices=("dry-run", "site", "draft"), required=True)
    persona.add_argument("--authorization", type=Path)
    persona.add_argument("--execute", action="store_true")
    persona.add_argument(
        "--resume-run",
        type=Path,
        help="reuse a verified run that has no historical baseline match",
    )

    persona_daily = subparsers.add_parser("persona-daily", help="stable-marker timer entry")
    persona_daily.add_argument("--date")
    persona_daily.add_argument("--mode", choices=("site", "draft"), default="site")
    persona_daily.add_argument("--authorization", type=Path)
    persona_daily.add_argument("--execute", action="store_true")
    persona_daily.add_argument("--stability-seconds", type=int, default=30)
    persona_daily.add_argument(
        "--resume-run",
        type=Path,
        help="reuse a verified run that has no historical baseline match",
    )

    wechat_probe = subparsers.add_parser("wechat-probe", help="probe read-only capabilities")
    wechat_probe.add_argument("--date")

    reconcile = subparsers.add_parser(
        "wechat-reconcile", help="reconcile an unknown draft without retrying"
    )
    reconcile.add_argument("--date")
    reconcile.add_argument("--authorization", type=Path, required=True)

    authorize = subparsers.add_parser("authorize-wechat", help="create signed draft authorization")
    authorize.add_argument("--issuer", required=True)
    authorize.add_argument("--column-id", default="jiayu-editorial")
    authorize.add_argument("--valid-days", type=int, default=90)
    authorize.add_argument("--output", type=Path, required=True)

    freeze_replay = subparsers.add_parser(
        "freeze-persona-replay", help="freeze marker-keyed replay inputs"
    )
    freeze_replay.add_argument("--dates", required=True, help="comma-separated ISO dates")
    freeze_replay.add_argument("--output", type=Path, required=True)

    replay = subparsers.add_parser("persona-replay", help="run a frozen replay dataset")
    replay.add_argument("--dataset", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)

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
        "persona-run": _persona_run,
        "persona-daily": _persona_daily,
        "wechat-probe": _wechat_probe,
        "wechat-reconcile": _wechat_reconcile,
        "authorize-wechat": _authorize_wechat,
        "freeze-persona-replay": _freeze_persona_replay,
        "persona-replay": _persona_replay,
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
