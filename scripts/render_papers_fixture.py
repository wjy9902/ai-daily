from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from ai_daily.papers_models import PapersPublication
from ai_daily.render import render_papers_index, render_papers_issue, render_papers_rss

DEFAULT_SITE_BASE_URL = "https://daily.jiayutool.cn"
REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--site", type=Path, required=True)
    parser.add_argument("--site-base-url", default=DEFAULT_SITE_BASE_URL)
    args = parser.parse_args()

    publication = PapersPublication.model_validate_json(
        args.fixture.read_text(encoding="utf-8")
    ).signed()
    papers = args.site / "papers"
    issue = papers / publication.target_date.isoformat()
    assets = args.site / "assets"
    issue.mkdir(parents=True, exist_ok=True)
    assets.mkdir(parents=True, exist_ok=True)
    (papers / "index.html").write_text(
        render_papers_index(publication, [publication], args.site_base_url), encoding="utf-8"
    )
    (issue / "index.html").write_text(
        render_papers_issue(publication, args.site_base_url), encoding="utf-8"
    )
    (papers / "rss.xml").write_text(
        render_papers_rss([publication], args.site_base_url), encoding="utf-8"
    )
    shutil.copyfile(REPO_ROOT / "static" / "site.css", assets / "site.css")
    print(f"papers preview written to {papers / 'index.html'}")


if __name__ == "__main__":
    main()
