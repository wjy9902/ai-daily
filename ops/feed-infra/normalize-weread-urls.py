#!/usr/bin/env python3
"""Repair the WeChat short links we-mp-rss derives from WeRead ids.

WeRead's own ids carry ``~`` where the real mp.weixin.qq.com short-link token
carries ``_``. we-mp-rss builds the article URL from that id and deliberately
keeps the ``~`` (``quote(original_id, safe="~")`` in
``core/wx/model/weread_mp.py``), on the stated belief that WeChat 302s the
``_`` form away. Measured from the production egress on 2026-08-29, one article
in all three forms:

    .../s/h77la5tspxS7XmeElGjC~w     200,      31612 bytes, "参数错误"
    .../s/h77la5tspxS7XmeElGjC%7Ew   200,      31612 bytes, "参数错误"
    .../s/h77la5tspxS7XmeElGjC_w     200,  3_294_942 bytes, the article

The ``_`` form does 302, but to ``..._w?nwr_flag=1#wechat_redirect`` - the
underscore survives, WeChat only appends its non-WeChat-referer flag. So the
redirect that motivated keeping ``~`` is not WeChat rewriting the token.

Affected articles fetch no body at all, and since nothing reports a collection
error the feed just serves a title-shaped item. This runs on a timer because
new articles keep arriving with ``~``; it is a patch over an upstream bug, not
a fix. Delete it once we-mp-rss stops producing ``~`` URLs.

Note ``scripts/fix_weread_mp_urls.py`` inside the container does the opposite -
it rewrites ``_`` back to ``~``. Never run it.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path("/www/wwwroot/ai-daily/feed-infra/data/we-mp-rss/db.db")
# The value working rows carry; DATA_STATUS.ACTIVE in the container's models.
STATUS_ACTIVE = 1


def normalize(db_path: Path) -> int:
    """Rewrite ~ to _ and let the content backfill retry only those rows."""
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT id, url FROM articles WHERE url LIKE '%~%'")
        rows = cursor.fetchall()
        for article_id, url in rows:
            cursor.execute(
                "UPDATE articles SET url = ? WHERE id = ?",
                (url.replace("~", "_"), article_id),
            )
        # Only the rows just repaired are unparked. Resetting every bodyless
        # article would re-queue genuinely dead ones forever and defeat
        # gather.content_max_failures.
        repaired = [article_id for article_id, _ in rows]
        if repaired:
            placeholders = ",".join("?" * len(repaired))
            cursor.execute(
                "UPDATE articles SET fix_fail_count = 0, status = ?, "
                f"fetch_started_at = NULL WHERE has_content = 0 AND id IN ({placeholders})",
                (STATUS_ACTIVE, *repaired),
            )
        connection.commit()
        return len(rows)
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    arguments = parser.parse_args()
    if not arguments.db.is_file():
        print(f"database not found: {arguments.db}", file=sys.stderr)
        return 1
    repaired = normalize(arguments.db)
    # Silent on the common no-op so the journal only shows real repairs.
    if repaired:
        print(f"normalized {repaired} weread article urls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
