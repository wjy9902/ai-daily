from __future__ import annotations

import argparse
import re
from pathlib import Path

TITLE_RE = re.compile(r'^title = "(\d{4}-\d{2}-\d{2})"$', re.MULTILINE)
DATE_RE = re.compile(r'^date = "([^"]+)"$', re.MULTILINE)
MARKER_RE = re.compile(r"<!-- ai-daily:(\d{4}-\d{2}-\d{2}):v\d+(?::sha256=[a-f0-9]{64})? -->")
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
    marker = MARKER_RE.search(document)
    if marker is None:
        return document
    parts = document.split("+++", maxsplit=2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError("marked daily issue has invalid frontmatter delimiters")
    frontmatter = parts[1]
    title = TITLE_RE.search(frontmatter)
    created = DATE_RE.search(frontmatter)
    if not title or not created:
        raise ValueError("marked daily issue has invalid frontmatter")
    if title.group(1) != marker.group(1):
        raise ValueError("marked daily issue title does not match its marker")
    target_date = title.group(1)
    frontmatter = DATE_RE.sub(f'date = "{target_date}T00:00:00+08:00"', frontmatter, count=1)
    if "created_at =" not in frontmatter:
        if EXTRA_RE.search(frontmatter) is None:
            raise ValueError("marked daily issue is missing its extra section")
        frontmatter = EXTRA_RE.sub(
            f'[extra]\ncreated_at = "{created.group(1)}"', frontmatter, count=1
        )
    return f"+++{frontmatter}+++{parts[2]}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("content_dir", type=Path)
    args = parser.parse_args()
    normalize_daily_dates(args.content_dir)


if __name__ == "__main__":
    main()
