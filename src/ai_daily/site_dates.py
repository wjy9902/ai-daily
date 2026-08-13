from __future__ import annotations

import argparse
import re
from pathlib import Path

TITLE_RE = re.compile(r'^title = "(\d{4}-\d{2}-\d{2})"$', re.MULTILINE)
DATE_RE = re.compile(r'^date = "([^"]+)"$', re.MULTILINE)
MARKER_RE = re.compile(r"<!-- ai-daily:(\d{4}-\d{2}-\d{2}):v\d+ -->")
EXTRA_RE = re.compile(r"^\[extra\]$", re.MULTILINE)


def normalize_daily_dates(content_dir: Path) -> int:
    changed = 0
    for path in content_dir.glob("issue-*.md"):
        original = path.read_text(encoding="utf-8")
        normalized = _normalize_document(original)
        if normalized == original:
            continue
        path.write_text(normalized, encoding="utf-8")
        changed += 1
    return changed


def _normalize_document(document: str) -> str:
    title = TITLE_RE.search(document)
    marker = MARKER_RE.search(document)
    created = DATE_RE.search(document)
    if not title or not marker or not created or title.group(1) != marker.group(1):
        return document
    target_date = title.group(1)
    normalized = DATE_RE.sub(f'date = "{target_date}T00:00:00+08:00"', document, count=1)
    parts = normalized.split("+++", maxsplit=2)
    if len(parts) != 3:
        return document
    frontmatter = parts[1]
    if "created_at =" not in frontmatter:
        normalized = EXTRA_RE.sub(
            f'[extra]\ncreated_at = "{created.group(1)}"', normalized, count=1
        )
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("content_dir", type=Path)
    args = parser.parse_args()
    normalize_daily_dates(args.content_dir)


if __name__ == "__main__":
    main()
