from pathlib import Path

import pytest

from ai_daily.site_dates import normalize_daily_dates


def test_daily_site_date_uses_issue_title_in_beijing_timezone(tmp_path: Path) -> None:
    post = tmp_path / "issue-3.md"
    post.write_text(
        """+++
title = "2026-08-13"
date = "2026-08-12T20:20:00Z"
[extra]
issue_url = "https://example.com/3"
+++
<!-- ai-daily:2026-08-13:v2 -->
body
"""
    )

    assert normalize_daily_dates(tmp_path) == 1
    value = post.read_text()
    assert 'date = "2026-08-13T00:00:00+08:00"' in value
    assert 'created_at = "2026-08-12T20:20:00Z"' in value


def test_site_date_rejects_mismatched_marked_issue(tmp_path: Path) -> None:
    post = tmp_path / "issue-4.md"
    original = """+++
title = "2026-08-13"
date = "2026-08-12T20:20:00Z"
[extra]
+++
<!-- ai-daily:2026-08-12:v2 -->
"""
    post.write_text(original)

    with pytest.raises(ValueError, match="does not match"):
        normalize_daily_dates(tmp_path)
    assert post.read_text() == original


def test_site_date_ignores_unmarked_issue(tmp_path: Path) -> None:
    post = tmp_path / "issue-5.md"
    original = """+++
title = "legacy"
date = "2026-08-12T20:20:00Z"
+++
body
"""
    post.write_text(original)

    assert normalize_daily_dates(tmp_path) == 0
    assert post.read_text() == original


def test_site_date_rejects_marked_issue_without_extra_section(tmp_path: Path) -> None:
    post = tmp_path / "issue-6.md"
    post.write_text(
        """+++
title = "2026-08-13"
date = "2026-08-12T20:20:00Z"
+++
<!-- ai-daily:2026-08-13:v2 -->
"""
    )

    with pytest.raises(ValueError, match="extra section"):
        normalize_daily_dates(tmp_path)


def test_site_date_rejects_fields_that_exist_only_in_body(tmp_path: Path) -> None:
    post = tmp_path / "issue-7.md"
    post.write_text(
        """+++
[extra]
+++
title = "2026-08-13"
date = "2026-08-12T20:20:00Z"
<!-- ai-daily:2026-08-13:v2 -->
"""
    )

    with pytest.raises(ValueError, match="invalid frontmatter"):
        normalize_daily_dates(tmp_path)
