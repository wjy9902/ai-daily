from __future__ import annotations

import html
import re
from collections import Counter
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ai_daily.content import evidence_bundle
from ai_daily.models import (
    DraftItem,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    Event,
    Evidence,
)
from ai_daily.site_trust import daily_marker, story_title_marker

WEEKDAYS = "一二三四五六日"
MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_[\]<>])")
BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def assemble_markdown(
    target_date: date,
    plan: EditorialPlan,
    drafts: list[DraftItem],
    events: list[Event],
) -> str:
    events_by_id = {event.event_id: event for event in events}
    drafts_by_id = {draft.event_id: draft for draft in drafts}
    details = [item for item in plan.selections if item.tier != EditorialTier.BRIEF]
    if set(drafts_by_id) != {item.event_id for item in details}:
        raise ValueError("drafts do not match the detailed editorial plan")
    evidence = {
        item.evidence_id: item
        for selection in plan.selections
        for item in evidence_bundle(events_by_id[selection.event_id]).evidence
    }
    lines = _header(target_date, plan)
    lines.extend(_contents(plan.selections))
    lines.extend(["---", ""])
    for selection in details:
        lines.extend(
            _detail(
                selection,
                drafts_by_id[selection.event_id],
                evidence,
                events_by_id[selection.event_id].published_at,
            )
        )
    briefs = [item for item in plan.selections if item.tier == EditorialTier.BRIEF]
    lines.extend(_briefs(briefs, evidence, events_by_id))
    lines.extend(_viewpoint(plan, evidence))
    lines.extend(
        ["---", "", "本期由自动化编辑流水线整理；事实与数据请以链接中的原始来源为准。", ""]
    )
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > 60_000:
        raise ValueError("digest exceeds the safe GitHub Issue body limit")
    return body


def _header(target_date: date, plan: EditorialPlan) -> list[str]:
    weekday = WEEKDAYS[target_date.weekday()]
    return [
        daily_marker(target_date),
        f'<p class="digest-kicker">AI 日报 · 周{weekday}</p>',
        "",
        (
            '<p class="digest-highlight"><strong>今日亮点：</strong> '
            f"{html.escape(plan.today_highlight)}</p>"
        ),
        "",
    ]


def _contents(selections: list[EditorialSelection]) -> list[str]:
    leads = [item for item in selections if item.tier == EditorialTier.LEAD]
    lines = ["## 今日必读", ""]
    lines.extend(f"- [{_markdown_text(item.headline)}](#story-{item.event_id})" for item in leads)
    lines.append("")
    remaining = [item for item in selections if item.tier != EditorialTier.LEAD]
    if not remaining:
        return lines
    lines.extend(
        [
            '<details class="digest-index">',
            f"<summary>展开其余 {len(remaining)} 条目录</summary>",
            '<div class="digest-index__content">',
        ]
    )
    for tier, title in (
        (EditorialTier.FOLLOW, "值得关注"),
        (EditorialTier.BRIEF, "快讯"),
    ):
        values = [item for item in selections if item.tier == tier]
        if not values:
            continue
        lines.extend([f"<h3>{title}</h3>", "<ul>"])
        lines.extend(
            (
                f'<li><a href="#story-{html.escape(item.event_id, quote=True)}">'
                f"{html.escape(item.headline)}</a></li>"
            )
            for item in values
        )
        lines.append("</ul>")
    lines.extend(["</div>", "</details>", ""])
    return lines


def _detail(
    selection: EditorialSelection,
    draft: DraftItem,
    evidence: dict[str, Evidence],
    published_at: datetime | None,
) -> list[str]:
    tier_label = "今日重点" if selection.tier == EditorialTier.LEAD else "值得关注"
    tier_class = "lead" if selection.tier == EditorialTier.LEAD else "follow"
    lines = [
        story_title_marker(selection.headline),
        f'<a id="story-{selection.event_id}"></a>',
        (
            f'## <span class="story-tier story-tier--{tier_class}">{tier_label}</span> '
            f"{_markdown_text(selection.headline)}"
        ),
        "",
        f'<div class="story-card story-card--{tier_class}">',
        f'<p class="story-tldr"><strong>TL;DR：</strong> {html.escape(draft.tldr)}</p>',
        (
            '<p class="story-sources"><strong>来源：</strong> '
            f"{_source_links(_detail_evidence_ids(selection, draft), evidence)}</p>"
        ),
        '<div class="story-facts"><strong>核心事实：</strong><ul>',
        *[f"<li>{html.escape(fact)}</li>" for fact in draft.facts],
        "</ul></div>",
        (
            '<p class="story-importance"><strong>为什么重要：</strong> '
            f"{html.escape(draft.why_it_matters)}</p>"
        ),
    ]
    if draft.action:
        lines.append(
            f'<p class="story-action"><strong>可以怎么用：</strong> {html.escape(draft.action)}</p>'
        )
    if draft.caveat:
        lines.append(
            f'<p class="story-caveat"><strong>局限/争议：</strong> {html.escape(draft.caveat)}</p>'
        )
    publication_time = _publication_time(published_at)
    lines.extend(
        [
            "</div>",
            (
                '<p class="story-meta"><time datetime="'
                f'{publication_time.isoformat()}">来源发布：'
                f"{publication_time.strftime('%m-%d %H:%M')} 北京时间</time> · "
                f"{html.escape(selection.category)}</p>"
            ),
            "",
        ]
    )
    return lines


def _briefs(
    selections: list[EditorialSelection],
    evidence: dict[str, Evidence],
    events_by_id: dict[str, Event],
) -> list[str]:
    lines = ['<a id="quick-news"></a>', "## 快讯", "", '<ol class="brief-list">']
    for selection in selections:
        sources = _source_links_html(selection.evidence_ids, evidence)
        event_id = html.escape(selection.event_id, quote=True)
        headline = html.escape(selection.headline)
        category = html.escape(selection.category)
        brief = html.escape(selection.brief)
        publication_time = _publication_time(events_by_id[selection.event_id].published_at)
        lines.append(story_title_marker(selection.headline))
        lines.append(
            f'<li id="story-{event_id}"><strong>{headline}</strong>'
            f'<span class="brief-category">{category}</span>'
            f'<p>{brief}</p><div class="brief-sources">{sources} · '
            f'<time datetime="{publication_time.isoformat()}">来源发布：'
            f"{publication_time.strftime('%m-%d %H:%M')} 北京时间</time></div></li>"
        )
    lines.extend(["</ol>", ""])
    return lines


def _viewpoint(plan: EditorialPlan, evidence: dict[str, Evidence]) -> list[str]:
    lines = ["## 编辑观点", ""]
    for insight in plan.editor_viewpoint:
        lines.append(
            f"- {_markdown_text(insight.text)} — {_source_links(insight.evidence_ids, evidence)}"
        )
    lines.append("")
    return lines


def _source_links(evidence_ids: list[str], evidence: dict[str, Evidence]) -> str:
    values = [_evidence_fields(evidence[evidence_id]) for evidence_id in evidence_ids]
    unique = list(dict.fromkeys(values))
    source_counts = Counter(source for source, _, _ in unique)
    source_positions: Counter[str] = Counter()
    links: list[str] = []
    for source, title, url in unique:
        source_positions[source] += 1
        label = source
        title_attribute = ""
        if source_counts[source] > 1:
            label = f"{source} {source_positions[source]}/{source_counts[source]}"
            title_attribute = f' title="{html.escape(title, quote=True)}"'
        links.append(
            f'<a href="{html.escape(url, quote=True)}"{title_attribute}>{html.escape(label)}</a>'
        )
    return " · ".join(links)


def _detail_evidence_ids(selection: EditorialSelection, draft: DraftItem) -> list[str]:
    return list(dict.fromkeys([*selection.evidence_ids, *draft.evidence_ids]))


def _source_links_html(evidence_ids: list[str], evidence: dict[str, Evidence]) -> str:
    return _source_links(evidence_ids, evidence)


def _evidence_fields(value: Evidence) -> tuple[str, str, str]:
    return value.source, value.title, str(value.url)


def _publication_time(value: datetime | None) -> datetime:
    if value is None or value.utcoffset() is None:
        raise ValueError("selected event requires a verified publication time")
    return value.astimezone(BEIJING_TIMEZONE)


def _markdown_text(value: str) -> str:
    single_line = " ".join(value.split())
    return MARKDOWN_ESCAPE_RE.sub(r"\\\1", single_line)
