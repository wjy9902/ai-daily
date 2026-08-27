from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from ai_daily.content import QUOTE_MIN_CHARS, normalize_quote_text, quote_supports
from ai_daily.persona_models import (
    AnalysisClaim,
    AnalysisItem,
    ClaimQuote,
    EditionDraft,
    EditorialMemory,
    PersonaEdition,
    PersonaRuntimeConfig,
    PublicTextBlock,
    UpstreamSnapshot,
)

FIRST_PERSON_RE = re.compile(r"(?:^|[^你他她它])(?:我|我的|我们|咱们|本人)")
FORBIDDEN_STYLE_RE = re.compile(
    r"(?:颠覆一切|震撼发布|炸裂|王炸|遥遥领先|史诗级|必然取代|彻底改变世界)"
)
INTERPRETIVE_PREFIX = {
    "inference": "判断：",
    "recommendation": "建议：",
    "uncertainty": "不确定性：",
}
# Mechanical grounding is reliable for numbers and version-like tokens. Plain
# Latin words include generic product vocabulary (API, ROI, SDK) and treating
# every one as a named entity creates false positives without protecting the
# equivalent Chinese text.
ANCHOR_RE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z][A-Za-z._-]*\d[A-Za-z0-9._-]*|\d[\d.]*%?)")
ANALYSIS_BLOCK_NAMES = (
    "headline_block",
    "confirmed_change_block",
    "delta_from_before_block",
    "importance_block",
    "product_implication_block",
    "recommended_action_block",
    "counter_case_block",
    "watch_signal_block",
)


@dataclass(frozen=True)
class VerificationScope:
    memories: dict[str, EditorialMemory]
    baseline_evidence: dict[str, str]
    current_ids_by_event: dict[str, set[str]]
    baseline_ids_by_event: dict[str, set[str]]
    memory_ids_by_event: dict[str, set[str]]


def verify_edition(
    draft: EditionDraft,
    snapshot: UpstreamSnapshot,
    scope: VerificationScope,
    config: PersonaRuntimeConfig,
) -> PersonaEdition:
    if draft.input_marker != snapshot.publication_marker:
        raise ValueError("edition input marker does not match upstream snapshot")
    claims = _claim_map(draft.claims)
    current_by_event = {
        bundle.event_id: {evidence.evidence_id: evidence.excerpt for evidence in bundle.evidence}
        for bundle in snapshot.evidence_bundles
    }
    current_evidence = {
        evidence_id: excerpt
        for evidence_by_id in current_by_event.values()
        for evidence_id, excerpt in evidence_by_id.items()
    }
    blocks = list(_blocks(draft))
    for path, block in blocks:
        _verify_block(path, block, claims)
        if FORBIDDEN_STYLE_RE.search(block.text):
            raise ValueError(f"forbidden hype at {path}")
        if FIRST_PERSON_RE.search(block.text) and not _first_person_allowed(
            block, claims, scope.memories
        ):
            raise ValueError(f"unapproved first-person voice at {path}")
    _verify_claim_inventory(blocks, claims)
    _verify_item_sources(draft, claims, current_by_event, scope)
    for claim in claims.values():
        event_id = _claim_event_id(claim, draft)
        allowed_ids = _allowed_for_event(scope.current_ids_by_event, event_id)
        allowed_current = {
            evidence_id: excerpt
            for evidence_id, excerpt in current_evidence.items()
            if evidence_id in allowed_ids
        }
        _verify_claim(claim, allowed_current, scope, event_id)
    _verify_source_links(draft, snapshot, claims)
    _verify_length(draft, blocks, config)
    payload = draft.model_dump(mode="json")
    edition = PersonaEdition.model_validate({**payload, "payload_sha256": "0" * 64})
    return edition.model_copy(update={"payload_sha256": edition.compute_payload_sha256()})


def verify_analysis_item(
    item: AnalysisItem,
    snapshot: UpstreamSnapshot,
    scope: VerificationScope,
) -> None:
    """Apply edition evidence rules before an analyst result can be reused."""
    current_by_event = {
        bundle.event_id: {evidence.evidence_id: evidence.excerpt for evidence in bundle.evidence}
        for bundle in snapshot.evidence_bundles
    }
    current = current_by_event.get(item.event_id, {})
    if not item.evidence_ids or not set(item.evidence_ids) <= set(current):
        raise ValueError(f"item {item.event_id} has out-of-scope current evidence")
    if len(item.evidence_ids) != len(set(item.evidence_ids)):
        raise ValueError(f"item {item.event_id} contains duplicate current evidence")
    if not set(item.memory_ids) <= scope.memory_ids_by_event.get(item.event_id, set()):
        raise ValueError(f"item {item.event_id} has out-of-scope memory")

    claims = _claim_map(item.claims)
    blocks = list(_analysis_blocks(item))
    for path, block in blocks:
        _verify_block(path, block, claims)
    _verify_claim_inventory(blocks, claims)
    for claim in claims.values():
        if not set(claim.current_evidence_ids) <= set(item.evidence_ids):
            raise ValueError(f"item {item.event_id} claim used undeclared evidence")
        if not set(claim.experience_memory_ids) <= set(item.memory_ids):
            raise ValueError(f"item {item.event_id} claim used undeclared memory")
        _verify_claim(claim, current, scope, item.event_id)

    confirmed = [claims[claim_id] for claim_id in item.confirmed_change_block.claim_ids]
    if any(claim.claim_type != "current_fact" for claim in confirmed):
        raise ValueError(f"item {item.event_id} confirmed change must be current facts")
    if item.delta_from_before_block is not None:
        delta = [claims[claim_id] for claim_id in item.delta_from_before_block.claim_ids]
        if not all(claim.current_evidence_ids and claim.baseline_evidence_ids for claim in delta):
            raise ValueError(f"item {item.event_id} delta lacks current or baseline evidence")


def normalize_analysis_item(
    item: AnalysisItem,
    snapshot: UpstreamSnapshot,
    scope: VerificationScope,
) -> AnalysisItem:
    """Canonicalize evidence-bound structure without rewriting model judgments."""
    path_by_claim: dict[str, str] = {}
    for path, block in _analysis_blocks(item):
        for claim_id in block.claim_ids:
            previous = path_by_claim.setdefault(claim_id, path)
            if previous != path:
                raise ValueError(f"claim {claim_id} is reused across public blocks")

    claims: list[AnalysisClaim] = []
    current = {
        evidence.evidence_id: evidence.excerpt
        for bundle in snapshot.evidence_bundles
        if bundle.event_id == item.event_id
        for evidence in bundle.evidence
    }
    sources: dict[
        str,
        tuple[
            Literal["current_evidence", "baseline_evidence", "experience_memory"],
            dict[str, str],
        ],
    ] = {
        "current_fact": ("current_evidence", current),
        "baseline_fact": ("baseline_evidence", scope.baseline_evidence),
        "experience_fact": (
            "experience_memory",
            {key: value.source_excerpt for key, value in scope.memories.items()},
        ),
    }
    source_maps = {
        "current_evidence": current,
        "baseline_evidence": scope.baseline_evidence,
        "experience_memory": {key: value.source_excerpt for key, value in scope.memories.items()},
    }
    for claim in item.claims:
        claim_updates: dict[str, str] = {}
        if claim.claim_id in path_by_claim:
            claim_updates["field_path"] = path_by_claim[claim.claim_id]
        quote_updates: dict[str, object] = {}
        if claim.claim_type in sources:
            source_kind, excerpts = sources[claim.claim_type]
            source_ids = {
                "current_fact": claim.current_evidence_ids,
                "baseline_fact": claim.baseline_evidence_ids,
                "experience_fact": claim.experience_memory_ids,
            }[claim.claim_type]
            quotes = [
                ClaimQuote(
                    source_kind=source_kind,
                    source_id=source_id,
                    quote=_verbatim_excerpt(excerpts[source_id]),
                )
                for source_id in source_ids
                if source_id in excerpts
            ]
            quote_updates = {"quotes": quotes}
            claim_updates["text"] = "；".join(quote.quote for quote in quotes)
        elif (prefix := INTERPRETIVE_PREFIX.get(claim.claim_type)) is not None:
            evidence = _claim_evidence_text(claim, source_maps)
            text = claim.text
            for token in sorted(_ungrounded_anchors(text, prefix, evidence), key=len, reverse=True):
                replacement = "相关指标" if token[0].isdigit() else "相关版本"
                text = text.replace(token, replacement)
            claim_updates["text"] = text
        claims.append(claim.model_copy(update={**claim_updates, **quote_updates}))

    claims_by_id = {claim.claim_id: claim for claim in claims}
    item_updates: dict[str, object] = {"claims": claims}
    for name in ANALYSIS_BLOCK_NAMES:
        block = getattr(item, name)
        if block is None:
            continue
        text = "".join(claims_by_id[claim_id].text for claim_id in block.claim_ids)
        item_updates[name] = block.model_copy(update={"text": text})
    return item.model_copy(update=item_updates)


def _verbatim_excerpt(excerpt: str, limit: int = 300) -> str:
    compact = " ".join(excerpt.split()).strip()
    if len(normalize_quote_text(compact)) < QUOTE_MIN_CHARS:
        raise ValueError("evidence excerpt is too short for a factual quote")
    if len(compact) <= limit:
        return compact
    prefix = compact[:limit]
    boundary = max(prefix.rfind(mark) for mark in ("。", ".", "\uff01", "!", "\uff1f", "?"))
    return prefix[: boundary + 1] if boundary >= QUOTE_MIN_CHARS else prefix


def _analysis_blocks(item: AnalysisItem) -> Iterable[tuple[str, PublicTextBlock]]:
    for name in ANALYSIS_BLOCK_NAMES:
        block = getattr(item, name)
        if block is not None:
            yield f"items[0].{name}", block


def _claim_map(claims: list[AnalysisClaim]) -> dict[str, AnalysisClaim]:
    result = {claim.claim_id: claim for claim in claims}
    if len(result) != len(claims):
        raise ValueError("edition contains duplicate claim ids")
    return result


def _blocks(draft: EditionDraft) -> Iterable[tuple[str, PublicTextBlock]]:
    yield "title_block", draft.title_block
    yield "digest_block", draft.digest_block
    yield "thesis_block", draft.thesis_block
    for index, item in enumerate(draft.items):
        base = f"items[{index}]"
        for name in (
            "headline_block",
            "confirmed_change_block",
            "delta_from_before_block",
            "importance_block",
            "product_implication_block",
            "recommended_action_block",
            "counter_case_block",
            "watch_signal_block",
        ):
            block = getattr(item, name)
            if block is not None:
                yield f"{base}.{name}", block
    for index, block in enumerate(draft.watchlist_blocks):
        yield f"watchlist_blocks[{index}]", block


def _verify_block(path: str, block: PublicTextBlock, claims: dict[str, AnalysisClaim]) -> None:
    if len(set(block.claim_ids)) != len(block.claim_ids):
        raise ValueError(f"duplicate claim ids at {path}")
    try:
        referenced = [claims[claim_id] for claim_id in block.claim_ids]
    except KeyError as error:
        raise ValueError(f"unknown claim id at {path}: {error.args[0]}") from error
    if any(claim.field_path != path for claim in referenced):
        raise ValueError(f"claim field_path mismatch at {path}")
    if block.text != "".join(claim.text for claim in referenced):
        raise ValueError(f"public text is not exact claim assembly at {path}")


def _verify_claim(
    claim: AnalysisClaim,
    current: dict[str, str],
    scope: VerificationScope,
    event_id: str | None,
) -> None:
    if claim.artifact_ids:
        raise ValueError(f"claim {claim.claim_id} referenced unsupported artifacts")
    baseline_ids = _allowed_for_event(scope.baseline_ids_by_event, event_id)
    memory_ids = _allowed_for_event(scope.memory_ids_by_event, event_id)
    declared = {
        "current_evidence": set(claim.current_evidence_ids),
        "baseline_evidence": set(claim.baseline_evidence_ids),
        "experience_memory": set(claim.experience_memory_ids),
    }
    _reject_duplicate_sources(claim)
    if not declared["current_evidence"] <= set(current):
        raise ValueError(f"claim {claim.claim_id} referenced out-of-scope current evidence")
    if not declared["baseline_evidence"] <= baseline_ids:
        raise ValueError(f"claim {claim.claim_id} referenced out-of-scope baseline evidence")
    if not declared["experience_memory"] <= memory_ids:
        raise ValueError(f"claim {claim.claim_id} referenced out-of-scope memory")
    if any(
        memory_id not in scope.memories
        or scope.memories[memory_id].status != "approved"
        or scope.memories[memory_id].publicity != "public"
        for memory_id in declared["experience_memory"]
    ):
        raise ValueError(f"claim {claim.claim_id} referenced non-public memory")
    if claim.claim_type == "experience_fact" and any(
        scope.memories[memory_id].usage != "first_person_allowed"
        for memory_id in declared["experience_memory"]
    ):
        raise ValueError(f"claim {claim.claim_id} experience is not approved for publication")
    _verify_factual_text(claim)
    source_maps = {
        "current_evidence": current,
        "baseline_evidence": scope.baseline_evidence,
        "experience_memory": {key: value.source_excerpt for key, value in scope.memories.items()},
    }
    _verify_interpretive_text(claim, declared, source_maps)
    for quote in claim.quotes:
        if quote.source_id not in declared[quote.source_kind]:
            raise ValueError(f"claim {claim.claim_id} quote source is not declared")
        excerpt = source_maps[quote.source_kind].get(quote.source_id)
        if excerpt is None or not quote_supports(quote.quote, excerpt):
            raise ValueError(f"claim {claim.claim_id} quote is not supported")


def _verify_factual_text(claim: AnalysisClaim) -> None:
    if claim.claim_type not in {"current_fact", "baseline_fact", "experience_fact"}:
        return
    expected = "；".join(quote.quote for quote in claim.quotes)
    if claim.text != expected:
        raise ValueError(f"claim {claim.claim_id} factual text must be the exact verified quote")


def _verify_interpretive_text(
    claim: AnalysisClaim,
    declared: dict[str, set[str]],
    source_maps: dict[str, dict[str, str]],
) -> None:
    prefix = INTERPRETIVE_PREFIX.get(claim.claim_type)
    if prefix is None:
        return
    if not claim.text.startswith(prefix):
        raise ValueError(f"claim {claim.claim_id} {claim.claim_type} must start with {prefix}")
    evidence = _claim_evidence_text(claim, source_maps)
    ungrounded = _ungrounded_anchors(claim.text, prefix, evidence)
    if ungrounded:
        raise ValueError(
            f"claim {claim.claim_id} contains ungrounded entity/version/number anchors"
        )


def _claim_evidence_text(
    claim: AnalysisClaim,
    source_maps: dict[str, dict[str, str]],
) -> str:
    declared = {
        "current_evidence": claim.current_evidence_ids,
        "baseline_evidence": claim.baseline_evidence_ids,
        "experience_memory": claim.experience_memory_ids,
    }
    return " ".join(
        source_maps[kind][source_id]
        for kind, source_ids in declared.items()
        for source_id in source_ids
        if source_id in source_maps[kind]
    ).lower()


def _ungrounded_anchors(text: str, prefix: str, evidence: str) -> set[str]:
    return {
        token
        for token in ANCHOR_RE.findall(text.removeprefix(prefix))
        if token.lower() not in evidence
    }


def _allowed_for_event(values: dict[str, set[str]], event_id: str | None) -> set[str]:
    if event_id is not None:
        return values.get(event_id, set())
    return set().union(*values.values()) if values else set()


def _reject_duplicate_sources(claim: AnalysisClaim) -> None:
    for values in (
        claim.current_evidence_ids,
        claim.baseline_evidence_ids,
        claim.experience_memory_ids,
        claim.artifact_ids,
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"claim {claim.claim_id} contains duplicate source ids")


def _claim_event_id(claim: AnalysisClaim, draft: EditionDraft) -> str | None:
    match = re.match(r"items\[(\d+)]\.", claim.field_path)
    if match is None:
        return None
    index = int(match.group(1))
    if index >= len(draft.items):
        raise ValueError(f"claim {claim.claim_id} references a missing item")
    return draft.items[index].event_id


def _verify_claim_inventory(
    blocks: list[tuple[str, PublicTextBlock]], claims: dict[str, AnalysisClaim]
) -> None:
    referenced = {claim_id for _, block in blocks for claim_id in block.claim_ids}
    if referenced != set(claims):
        raise ValueError("edition claim inventory does not match public blocks")


def _first_person_allowed(
    block: PublicTextBlock,
    claims: dict[str, AnalysisClaim],
    memories: dict[str, EditorialMemory],
) -> bool:
    memory_ids = {
        memory_id
        for claim_id in block.claim_ids
        for memory_id in claims[claim_id].experience_memory_ids
    }
    return bool(memory_ids) and all(
        memory_id in memories and memories[memory_id].usage == "first_person_allowed"
        for memory_id in memory_ids
    )


def _verify_item_sources(
    draft: EditionDraft,
    claims: dict[str, AnalysisClaim],
    current_by_event: dict[str, dict[str, str]],
    scope: VerificationScope,
) -> None:
    event_ids = [item.event_id for item in draft.items]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("edition contains duplicate events")
    for index, item in enumerate(draft.items):
        allowed_current = set(current_by_event.get(item.event_id, {}))
        if not item.evidence_ids or not set(item.evidence_ids) <= allowed_current:
            raise ValueError(f"item {item.event_id} has out-of-scope current evidence")
        if len(item.evidence_ids) != len(set(item.evidence_ids)):
            raise ValueError(f"item {item.event_id} contains duplicate current evidence")
        if not set(item.memory_ids) <= scope.memory_ids_by_event.get(item.event_id, set()):
            raise ValueError(f"item {item.event_id} has out-of-scope memory")
        if item.delta_from_before_block is not None:
            delta_claims = [claims[item_id] for item_id in item.delta_from_before_block.claim_ids]
            if not all(
                claim.current_evidence_ids and claim.baseline_evidence_ids for claim in delta_claims
            ):
                raise ValueError(f"item {item.event_id} delta lacks current or baseline evidence")
        confirmed_claims = [claims[item_id] for item_id in item.confirmed_change_block.claim_ids]
        if any(claim.claim_type != "current_fact" for claim in confirmed_claims):
            raise ValueError(f"item {item.event_id} confirmed change must be current facts")
        item_paths = {path for path, _ in _blocks(draft) if path.startswith(f"items[{index}].")}
        item_claim_ids = {claim.claim_id for claim in item.claims}
        public_claim_ids = {
            claim_id
            for path, block in _blocks(draft)
            if path in item_paths
            for claim_id in block.claim_ids
        }
        if item_claim_ids != public_claim_ids:
            raise ValueError(f"item {item.event_id} claim inventory mismatch")
        if any(claims[claim.claim_id] != claim for claim in item.claims):
            raise ValueError(f"item {item.event_id} claim objects do not match edition claims")
        for claim in item.claims:
            if not set(claim.current_evidence_ids) <= set(item.evidence_ids):
                raise ValueError(f"item {item.event_id} claim used another event's evidence")
            if not set(claim.experience_memory_ids) <= set(item.memory_ids):
                raise ValueError(f"item {item.event_id} claim used undeclared memory")


def _verify_source_links(
    draft: EditionDraft,
    snapshot: UpstreamSnapshot,
    claims: dict[str, AnalysisClaim],
) -> None:
    links = [str(url) for url in draft.source_links]
    used_ids = {
        evidence_id for claim in claims.values() for evidence_id in claim.current_evidence_ids
    }
    expected = {
        str(evidence.url)
        for bundle in snapshot.evidence_bundles
        for evidence in bundle.evidence
        if evidence.evidence_id in used_ids
    }
    if not links or len(links) != len(set(links)) or set(links) != expected:
        raise ValueError("edition source links do not match used current evidence URLs")


def _verify_length(
    draft: EditionDraft,
    blocks: list[tuple[str, PublicTextBlock]],
    config: PersonaRuntimeConfig,
) -> None:
    body_chars = sum(
        sum(1 for character in unicodedata.normalize("NFC", block.text) if not character.isspace())
        for path, block in blocks
        if path not in {"title_block", "digest_block"}
    )
    minimum, maximum = (
        (config.standard_min_chars, config.standard_max_chars)
        if draft.edition_type == "standard"
        else (config.no_major_min_chars, config.no_major_max_chars)
    )
    if not minimum <= body_chars <= maximum:
        raise ValueError(f"edition body length {body_chars} outside {minimum}-{maximum}")
