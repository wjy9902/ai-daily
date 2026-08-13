from pathlib import Path

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


def test_site_date_ignores_unmarked_or_mismatched_issue(tmp_path: Path) -> None:
    post = tmp_path / "issue-4.md"
    original = """+++
title = "2026-08-13"
date = "2026-08-12T20:20:00Z"
[extra]
+++
<!-- ai-daily:2026-08-12:v2 -->
"""
    post.write_text(original)

    assert normalize_daily_dates(tmp_path) == 0
    assert post.read_text() == original
