from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from ai_daily.benchmark import benchmark_models
from ai_daily.config import Secrets, load_config
from ai_daily.pipeline import DailyPipeline
from ai_daily.publisher import GitHubPublisher
from ai_daily.verifier import verify_publication


def _target_date(value: str | None) -> date:
    return date.fromisoformat(value) if value else datetime.now(ZoneInfo("Asia/Shanghai")).date()


async def _run(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    async with httpx.AsyncClient(follow_redirects=True) as client:
        artifact, _ = await DailyPipeline(config, Secrets(), client=client).run(
            _target_date(args.date), publish=args.mode == "publish"
        )
    if artifact.publication is None:
        raise RuntimeError("pipeline did not produce a publication record")
    print(json.dumps(artifact.publication.model_dump(mode="json"), ensure_ascii=False))
    return 0


async def _verify(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    secrets = Secrets()
    if not secrets.github_token:
        raise RuntimeError("GITHUB_TOKEN is required")
    target = _target_date(args.date)
    repository = secrets.github_repository or config.pipeline.repository
    async with httpx.AsyncClient(follow_redirects=True) as client:
        publisher = GitHubPublisher(repository, secrets.github_token, client)
        publication = await publisher.find_publication(target)
        if not publication or publication.issue_number is None:
            raise RuntimeError("daily issue does not exist")
        verified = await verify_publication(
            target,
            str(secrets.site_base_url or config.pipeline.site_base_url),
            publication.issue_number,
            client,
        )
    print(verified.model_dump_json())
    return 0


async def _rebuild(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    secrets = Secrets()
    if not secrets.github_token:
        raise RuntimeError("GITHUB_TOKEN is required")
    async with httpx.AsyncClient() as client:
        repository = secrets.github_repository or config.pipeline.repository
        response = await client.post(
            f"https://api.github.com/repos/{repository}/actions/workflows/generate_site.yml/dispatches",
            headers={
                "Authorization": f"Bearer {secrets.github_token}",
                "Accept": "application/vnd.github+json",
            },
            json={"ref": "main"},
            timeout=20,
        )
        response.raise_for_status()
    print(json.dumps({"status": "dispatched", "date": _target_date(args.date).isoformat()}))
    return 0


async def _issue_exists(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    secrets = Secrets()
    if not secrets.github_token:
        raise RuntimeError("GITHUB_TOKEN is required")
    repository = secrets.github_repository or config.pipeline.repository
    async with httpx.AsyncClient() as client:
        publication = await GitHubPublisher(
            repository, secrets.github_token, client
        ).find_publication(_target_date(args.date))
    if publication is None:
        return 1
    print(publication.model_dump_json())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-daily")
    parser.add_argument("--config-dir", default="config")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--date")
    run.add_argument("--mode", choices=("dry-run", "publish"), required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--date")
    rebuild = subparsers.add_parser("rebuild-site")
    rebuild.add_argument("--date")
    issue_exists = subparsers.add_parser("issue-exists")
    issue_exists.add_argument("--date")
    benchmark = subparsers.add_parser("benchmark-models")
    benchmark.add_argument("--dataset", type=Path, required=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        code = asyncio.run(_run(args))
    elif args.command == "verify":
        code = asyncio.run(_verify(args))
    elif args.command == "rebuild-site":
        code = asyncio.run(_rebuild(args))
    elif args.command == "issue-exists":
        code = asyncio.run(_issue_exists(args))
    else:
        config = load_config(Path(args.config_dir))
        result = asyncio.run(benchmark_models(args.dataset, config, Secrets()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
