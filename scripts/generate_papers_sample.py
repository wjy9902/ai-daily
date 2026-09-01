"""Generate 1-2 editor deep reads from a papers dry-run selection artifact."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ai_daily.artifacts import write_artifact
from ai_daily.config import Secrets
from ai_daily.papers import PapersPipeline, build_papers_publication
from ai_daily.papers_config import load_papers_config
from ai_daily.papers_models import PapersRunArtifact
from ai_daily.site_publisher import SiteLayout


async def run(args: argparse.Namespace) -> None:
    config_dir = Path(args.config_dir)
    config = load_papers_config(config_dir)
    artifact = PapersRunArtifact.model_validate_json(args.selection.read_text(encoding="utf-8"))
    layout = SiteLayout(args.site_root)
    layout.ensure()
    pipeline = PapersPipeline(config, config_dir, layout, Secrets())
    try:
        selected = artifact.selected[: args.count]
        if args.arxiv_id:
            by_id = {paper.arxiv_id: paper for paper in artifact.selected}
            missing = [paper_id for paper_id in args.arxiv_id if paper_id not in by_id]
            if missing:
                raise ValueError(f"arXiv IDs are not in the selection artifact: {missing}")
            selected = [by_id[paper_id] for paper_id in args.arxiv_id]
        publication = await build_papers_publication(
            artifact.target_date,
            selected,
            pipeline.collector,
            pipeline.gateway,
        )
        write_artifact(args.output, publication)
        print(f"deep-read sample written to {args.output}")
    finally:
        await pipeline.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--site-root", type=Path, required=True)
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--count", type=int, choices=(1, 2), default=2)
    parser.add_argument(
        "--arxiv-id",
        action="append",
        help="deep-read this selected arXiv ID; repeat to choose two",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
