from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ai_daily.models import Event
from ai_daily.persona_models import EditorialMemory, MemoryKind, RetrievedMemory

TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "模型与平台": ("模型", "api", "平台", "openai", "anthropic", "gemini", "deepseek"),
    "Agent工具链": ("agent", "智能体", "mcp", "skill", "工具调用", "编排"),
    "AI产品分发": ("分发", "用户", "订阅", "应用", "商店", "流量", "增长"),
    "评测与可靠性": ("评测", "eval", "基准", "可靠", "准确", "幻觉", "安全"),
    "成本与定价": ("价格", "定价", "成本", "token", "免费", "订阅"),
    "监管与合规": ("监管", "政策", "法规", "合规", "版权", "隐私"),
    "开发工具": ("coding", "代码", "开发者", "ide", "github", "开源"),
    "多模态": ("图像", "视频", "音频", "语音", "多模态"),
}

KIND_QUOTA: dict[str, int] = {
    "experience": 4,
    "principle": 4,
    "decision": 4,
    "style": 4,
}


def constitution_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_memories(path: Path, root: Path) -> list[EditorialMemory]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("memories"), list):
        raise ValueError("persona memories must contain a memories list")
    memories = [EditorialMemory.model_validate(item) for item in payload["memories"]]
    _validate_sources(memories, root)
    _validate_unique_ids(memories)
    return memories


def _validate_sources(memories: list[EditorialMemory], root: Path) -> None:
    contents: dict[Path, tuple[str, str]] = {}
    for memory in memories:
        source = root / memory.source_path
        if source not in contents:
            raw = source.read_bytes()
            contents[source] = (hashlib.sha256(raw).hexdigest(), raw.decode("utf-8"))
        digest, text = contents[source]
        if digest != memory.source_artifact_id:
            raise ValueError(f"memory source hash mismatch: {memory.memory_id}")
        if memory.source_excerpt not in text:
            raise ValueError(f"memory source excerpt missing: {memory.memory_id}")


def _validate_unique_ids(memories: list[EditorialMemory]) -> None:
    ids = [item.memory_id for item in memories]
    if len(ids) != len(set(ids)):
        raise ValueError("persona memory ids must be unique")


def controlled_topics(events: list[Event]) -> set[str]:
    text = " ".join(f"{event.title} {event.summary}" for event in events).lower()
    return {
        topic for topic, terms in TOPIC_TERMS.items() if any(term.lower() in text for term in terms)
    }


def retrieve_memories(
    memories: list[EditorialMemory],
    events: list[Event],
    target_date: date,
    limit: int = 16,
) -> tuple[list[RetrievedMemory], bool]:
    topics = controlled_topics(events)
    scored: list[tuple[EditorialMemory, int]] = []
    for memory in memories:
        if not _production_eligible(memory, target_date):
            continue
        score = _score(memory, topics, target_date)
        scored.append((memory, score))
    scored.sort(key=lambda item: (-item[1], -item[0].valid_from.toordinal(), item[0].memory_id))
    selected = _apply_kind_quotas(scored, limit)
    conflicts = _has_unresolved_conflicts([memory for memory, _ in selected])
    rows = [RetrievedMemory(memory_id=item.memory_id, score=score) for item, score in selected]
    return rows, conflicts


def _production_eligible(memory: EditorialMemory, target_date: date) -> bool:
    if memory.status != "approved" or memory.publicity != "public":
        return False
    if memory.valid_from > target_date:
        return False
    return memory.valid_until is None or memory.valid_until >= target_date


def _score(memory: EditorialMemory, topics: set[str], target_date: date) -> int:
    topic_overlap = len(set(memory.topics) & topics)
    audience_overlap = bool(set(memory.audiences) & {"ai产品构建者", "专业从业者"})
    stage_overlap = bool(set(memory.product_stages) & {"构建", "验证", "上线"})
    explicit = memory.confidence == "explicit"
    recency = 0
    if memory.kind in {MemoryKind.DECISION, MemoryKind.OUTCOME}:
        age = (target_date - memory.valid_from).days
        recency = 2 if age <= 90 else 1 if age <= 365 else 0
    return (
        4 * topic_overlap
        + 2 * int(audience_overlap)
        + int(stage_overlap)
        + 2 * int(explicit)
        + recency
    )


def _quota_group(memory: EditorialMemory) -> str:
    if memory.kind == MemoryKind.EXPERIENCE:
        return "experience"
    if memory.kind in {MemoryKind.PRINCIPLE, MemoryKind.PREFERENCE}:
        return "principle"
    if memory.kind in {MemoryKind.DECISION, MemoryKind.OUTCOME}:
        return "decision"
    return "style"


def _apply_kind_quotas(
    scored: list[tuple[EditorialMemory, int]], limit: int
) -> list[tuple[EditorialMemory, int]]:
    grouped: dict[str, list[tuple[EditorialMemory, int]]] = defaultdict(list)
    for item in scored:
        grouped[_quota_group(item[0])].append(item)
    selected: list[tuple[EditorialMemory, int]] = []
    for group in ("experience", "principle", "decision", "style"):
        selected.extend(grouped[group][: KIND_QUOTA[group]])
    selected.sort(key=lambda item: (-item[1], -item[0].valid_from.toordinal(), item[0].memory_id))
    return selected[:limit]


def _has_unresolved_conflicts(memories: list[EditorialMemory]) -> bool:
    groups: dict[str, list[EditorialMemory]] = defaultdict(list)
    for memory in memories:
        if memory.conflict_group_id:
            groups[memory.conflict_group_id].append(memory)
    return any(len(items) > 1 and not _has_superseding_item(items) for items in groups.values())


def _has_superseding_item(items: list[EditorialMemory]) -> bool:
    ids = {item.memory_id for item in items}
    return any(ids & set(item.supersedes) for item in items)


def memories_by_id(memories: list[EditorialMemory]) -> dict[str, EditorialMemory]:
    return {item.memory_id: item for item in memories}


def memory_prompt_rows(
    retrieved: list[RetrievedMemory], memories: dict[str, EditorialMemory]
) -> list[dict[str, Any]]:
    return [
        {
            "memory_id": item.memory_id,
            "kind": memories[item.memory_id].kind.value,
            "statement": memories[item.memory_id].statement,
            "source_context": memories[item.memory_id].source_context[:500],
            "score": item.score,
        }
        for item in retrieved
    ]
