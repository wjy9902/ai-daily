from __future__ import annotations

import html
import re
from datetime import date

from ai_daily.content import evidence_bundle
from ai_daily.models import (
    DraftItem,
    EditorialPlan,
    EditorialSelection,
    EditorialTier,
    Event,
    Evidence,
)
from ai_daily.site_trust import story_title_marker

MARKER_VERSION = "v2"
WEEKDAYS = "一二三四五六日"
MARKDOWN_ESCAPE_RE = re.compile(r"([\\`*_[\]<>])")


def marker(target_date: date) -> str:
    return f"<!-- ai-daily:{target_date.isoformat()}:{MARKER_VERSION} -->"


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
        lines.extend(_detail(selection, drafts_by_id[selection.event_id], evidence, target_date))
    briefs = [item for item in plan.selections if item.tier == EditorialTier.BRIEF]
    lines.extend(_briefs(briefs, evidence))
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
        marker(target_date),
        f"# AI 日报 · 周{weekday}",
        "",
        f"> **今日亮点：** {_markdown_text(plan.today_highlight)}",
        "",
    ]


def _contents(selections: list[EditorialSelection]) -> list[str]:
    lines = ["## 速览目录", ""]
    sections = (
        (EditorialTier.LEAD, "今日重点"),
        (EditorialTier.FOLLOW, "值得关注"),
        (EditorialTier.BRIEF, "快讯"),
    )
    for tier, title in sections:
        values = [item for item in selections if item.tier == tier]
        lines.extend([f"### {title}", ""])
        lines.extend(
            f"- [{_markdown_text(item.headline)}](#story-{item.event_id})" for item in values
        )
        lines.append("")
    return lines


def _detail(
    selection: EditorialSelection,
    draft: DraftItem,
    evidence: dict[str, Evidence],
    target_date: date,
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
        f"> **TL;DR：** {_markdown_text(draft.tldr)}",
        ">",
        f"> **来源：** {_source_links(_detail_evidence_ids(selection, draft), evidence)}",
        ">",
        "> **核心事实：**",
        *[f"> - {_markdown_text(fact)}" for fact in draft.facts],
        ">",
        f"> **为什么重要：** {_markdown_text(draft.why_it_matters)}",
    ]
    if draft.action:
        lines.extend([">", f"> **可以怎么用：** {_markdown_text(draft.action)}"])
    if draft.caveat:
        lines.extend([">", f"> **局限/争议：** {_markdown_text(draft.caveat)}"])
    lines.extend(["", f"`{target_date.isoformat()}` · {selection.category}", ""])
    return lines


def _briefs(selections: list[EditorialSelection], evidence: dict[str, Evidence]) -> list[str]:
    lines = ['<a id="quick-news"></a>', "## 快讯", "", '<ol class="brief-list">']
    for selection in selections:
        sources = _source_links_html(selection.evidence_ids, evidence)
        event_id = html.escape(selection.event_id, quote=True)
        headline = html.escape(selection.headline)
        category = html.escape(selection.category)
        brief = html.escape(selection.brief)
        lines.append(story_title_marker(selection.headline))
        lines.append(
            f'<li id="story-{event_id}"><strong>{headline}</strong>'
            f'<span class="brief-category">{category}</span>'
            f'<p>{brief}</p><div class="brief-sources">{sources}</div></li>'
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
    unique = dict(values)
    return " · ".join(
        f'<a href="{html.escape(url, quote=True)}">{html.escape(source)}</a>'
        for source, url in unique.items()
    )


def _detail_evidence_ids(selection: EditorialSelection, draft: DraftItem) -> list[str]:
    return list(dict.fromkeys([*selection.evidence_ids, *draft.evidence_ids]))


def _source_links_html(evidence_ids: list[str], evidence: dict[str, Evidence]) -> str:
    values = [_evidence_fields(evidence[evidence_id]) for evidence_id in evidence_ids]
    unique = dict(values)
    return " · ".join(
        f'<a href="{html.escape(url, quote=True)}">{html.escape(source)}</a>'
        for source, url in unique.items()
    )


def _evidence_fields(value: Evidence) -> tuple[str, str]:
    return value.source, str(value.url)


def _markdown_text(value: str) -> str:
    single_line = " ".join(value.split())
    return MARKDOWN_ESCAPE_RE.sub(r"\\\1", single_line)
