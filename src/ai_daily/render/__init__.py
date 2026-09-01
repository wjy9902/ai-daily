"""Static site render layer: HTML pages and the RSS feed.

Every renderer reads :class:`~ai_daily.publication.DailyPublication` and
nothing else, so a page can be rebuilt from ``published/<date>.json`` without
re-running collection or the models.
"""

from __future__ import annotations

from .papers import render_papers_index, render_papers_issue, render_papers_rss
from .site import (
    ArchiveEntry,
    render_archive,
    render_daily,
    render_fallback,
    render_index,
    render_issue,
    render_rss,
)

__all__ = [
    "ArchiveEntry",
    "render_archive",
    "render_daily",
    "render_fallback",
    "render_index",
    "render_issue",
    "render_papers_index",
    "render_papers_issue",
    "render_papers_rss",
    "render_rss",
]
