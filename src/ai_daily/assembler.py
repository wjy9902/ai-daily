from __future__ import annotations

from datetime import date

from ai_daily.content import evidence_bundle, group_drafts
from ai_daily.models import DraftItem, Event

MARKER_VERSION = "v1"
CATEGORY_ORDER = ["模型与平台", "前沿研究", "值得试的项目", "行业动态", "国内 AI", "快讯"]


def marker(target_date: date) -> str:
    return f"<!-- ai-daily:{target_date.isoformat()}:{MARKER_VERSION} -->"


def assemble_markdown(target_date: date, drafts: list[DraftItem], events: list[Event]) -> str:
    if not drafts:
        raise ValueError("cannot assemble an empty digest")
    events_by_id = {event.event_id: event for event in events}
    grouped = group_drafts(drafts)
    lines = [marker(target_date), f"# AI 日报 {target_date.isoformat()}", ""]
    lines.extend(["## 速览", ""])
    lines.extend(f"- {draft.title}" for draft in drafts)
    lines.append("")
    for category in CATEGORY_ORDER:
        category_drafts = grouped.get(category, [])
        if not category_drafts:
            continue
        lines.extend([f"## {category}", ""])
        for draft in category_drafts:
            event = events_by_id[draft.event_id]
            bundle = evidence_bundle(event)
            evidence_by_id = {item.evidence_id: item for item in bundle.evidence}
            sources = " · ".join(
                f"[{evidence_by_id[evidence_id].source}]({evidence_by_id[evidence_id].url})"
                for evidence_id in draft.evidence_ids
            )
            lines.extend(
                [
                    f"### {draft.title}",
                    "",
                    f"**TL;DR：** {draft.tldr}",
                    "",
                    *[f"- {fact}" for fact in draft.facts],
                    "",
                    f"**为什么重要：** {draft.why_it_matters}",
                    "",
                    f"**今日行动：** {draft.action}",
                    "",
                    f"**来源：** {sources}",
                ]
            )
            if draft.caveat:
                lines.extend(["", f"**局限：** {draft.caveat}"])
            lines.append("")
    lines.extend(["---", "", "内容由自动化系统辅助整理，请以原始来源为准。", ""])
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > 60_000:
        raise ValueError("digest exceeds the safe GitHub Issue body limit")
    return body
