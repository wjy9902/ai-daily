from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import factories
import httpx
import pytest
from pydantic import HttpUrl

from ai_daily import cli, persona_cli
from ai_daily.artifacts import write_artifact
from ai_daily.budget import BudgetLedger
from ai_daily.config import Secrets, load_config
from ai_daily.content import evidence_bundle
from ai_daily.persona_content import build_shortlist
from ai_daily.persona_models import (
    AnalysisClaim,
    AnalysisItem,
    AnalystOutput,
    ClaimQuote,
    Critique,
    CritiqueFinding,
    EditionDraft,
    FinalizerOutput,
    FinalizerResolution,
    OperationReceipt,
    PersonaEdition,
    PersonaPlan,
    PlanSelection,
    PublicTextBlock,
    RenderReceipt,
    UpstreamSnapshot,
    WechatTarget,
    sha256_payload,
)
from ai_daily.persona_pipeline import (
    PersonaPipeline,
    _analyst_event_row,
    _json_prompt,
    _validate_critique,
    _validate_finalizer_changes,
)
from ai_daily.persona_render import render_persona
from ai_daily.persona_replay import _copy_replay_inputs, freeze_replay_dataset, run_replay
from ai_daily.persona_snapshot import (
    activate_upstream_snapshot,
    load_upstream_snapshot,
    persist_upstream_snapshot,
)
from ai_daily.persona_verifier import (
    VerificationScope,
    normalize_analysis_item,
    verify_analysis_item,
    verify_edition,
)
from ai_daily.persona_wechat import (
    PublicationSlots,
    WechatAPIError,
    WechatClient,
    WechatHTTPError,
    WechatPublicationError,
    WechatPublicationUnknown,
    WechatResponseError,
    account_fingerprint,
    attest_release,
    publish_draft,
    reconcile_draft,
    sign_authorization,
    verify_attestation,
    verify_authorization,
)
from ai_daily.publication import PublicationLevel
from ai_daily.site_publisher import (
    PublicationRefused,
    SiteLayout,
    publish_site,
    recent_persona_editions,
)

TARGET = date(2026, 8, 27)
QUOTE = "Evidence for story 0."
AUTH_KEY = "ab" * 32
RELEASE_KEY = "cd" * 32


def _scope() -> VerificationScope:
    return VerificationScope(
        memories={},
        baseline_evidence={},
        current_ids_by_event={"event-0": {"event-0-1"}},
        baseline_ids_by_event={},
        memory_ids_by_event={},
    )


def _authorization(**updates: Any) -> Any:
    now = datetime.now(UTC)
    payload = {
        "schema_version": 1,
        "authorization_id": "auth-test",
        "issuer": "jesse",
        "column_id": "jiayu-editorial",
        "account_stable_id": "account-1",
        "environment": "production",
        "allowed_actions": ["create_draft", "reconcile_draft"],
        "valid_from": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "revoked_at": None,
        "key_id": "auth-v1",
        **updates,
    }
    return sign_authorization(payload, AUTH_KEY)


def _snapshot(layout: SiteLayout) -> UpstreamSnapshot:
    event = factories.event(0)
    unsigned = UpstreamSnapshot(
        target_date=TARGET,
        publication_level=PublicationLevel.L0,
        publication_marker="a" * 64,
        events=[event],
        evidence_bundles=[evidence_bundle(event)],
        decisions=[factories.judge_decision(0)],
        editorial_plan=None,
        snapshot_sha256="0" * 64,
    )
    snapshot = unsigned.model_copy(
        update={"snapshot_sha256": sha256_payload(unsigned.canonical_payload())}
    )
    write_artifact(layout.upstream_object_path(snapshot.publication_marker), snapshot)
    return snapshot


def test_analyst_prompt_drops_nested_raw_items_and_stays_bounded() -> None:
    event = factories.event(0)
    raw = event.items[0].model_copy(update={"summary": "长原文。" * 2_500})
    event = event.model_copy(update={"summary": "事件摘要。" * 1_500, "items": [raw] * 8})
    bundle = evidence_bundle(event)
    evidence = bundle.evidence[0]
    bundle = bundle.model_copy(
        update={
            "evidence": [
                evidence.model_copy(
                    update={
                        "evidence_id": f"event-0-{index}",
                        "excerpt": "可核验的证据句。" * 500,
                    }
                )
                for index in range(1, 4)
            ]
        }
    )
    unsigned = UpstreamSnapshot(
        target_date=TARGET,
        publication_level=PublicationLevel.L0,
        publication_marker="a" * 64,
        events=[event],
        evidence_bundles=[bundle],
        decisions=[factories.judge_decision(0)],
        editorial_plan=None,
        snapshot_sha256="0" * 64,
    )
    selection = PlanSelection(
        event_id="event-0",
        grade="S",
        importance_reason="这项变化影响 AI 产品决策。",
        evidence_ids=["event-0-1", "event-0-2", "event-0-3"],
    )

    row = _analyst_event_row(unsigned, selection)
    prompt = _json_prompt(
        selection=selection.model_dump(mode="json"),
        event=row,
        memories=[
            {
                "memory_id": f"memory-{index}",
                "kind": "experience",
                "statement": "个人经验。" * 52,
                "source_context": "来源背景。" * 36,
            }
            for index in range(4)
        ],
        baseline={
            "event_id": "event-0",
            "matched_event_id": "historical-event",
            "baseline_evidence_ids": ["historical-evidence"],
            "confidence": 0.9,
            "reasoning": "同一实体的同一指标。" * 40,
        },
    )

    assert "items" not in row["event"]
    assert len(prompt) <= 10_000


@pytest.mark.asyncio
async def test_analysts_run_in_settled_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = object.__new__(PersonaPipeline)
    cast(Any, pipeline).persona = SimpleNamespace(analyst_concurrency=2)
    selections = [
        PlanSelection(
            event_id=f"event-{index}",
            grade="S" if index == 0 else "A",
            importance_reason="important",
            evidence_ids=[f"event-{index}-1"],
        )
        for index in range(5)
    ]
    plan = PersonaPlan(
        edition_type="standard",
        today_thesis="important changes",
        selections=selections,
        watchlist_event_ids=[],
        omitted=[],
    )
    entered = {selection.event_id: asyncio.Event() for selection in selections}
    releases = {selection.event_id: asyncio.Event() for selection in selections}

    async def analyze_one(
        snapshot: object,
        selection: PlanSelection,
        memories: dict[str, Any],
        baseline: object,
        scope: VerificationScope,
    ) -> AnalysisItem:
        entered[selection.event_id].set()
        await releases[selection.event_id].wait()
        return _standard_item(selection.event_id, 0)

    monkeypatch.setattr(pipeline, "_analyze_one", analyze_one)
    running = asyncio.create_task(
        pipeline._analyze(None, plan, {}, {}, cast(VerificationScope, None))
    )

    await asyncio.gather(entered["event-0"].wait(), entered["event-1"].wait())
    releases["event-0"].set()
    await asyncio.sleep(0)
    assert not entered["event-2"].is_set()
    releases["event-1"].set()
    await asyncio.gather(entered["event-2"].wait(), entered["event-3"].wait())
    releases["event-2"].set()
    await asyncio.sleep(0)
    assert not entered["event-4"].is_set()
    releases["event-3"].set()
    await entered["event-4"].wait()
    releases["event-4"].set()

    results = await running
    assert [item.event_id for item in results] == [selection.event_id for selection in selections]


def _claim(claim_id: str, path: str, text: str) -> AnalysisClaim:
    return AnalysisClaim(
        claim_id=claim_id,
        field_path=path,
        text=text,
        claim_type="inference",
        current_evidence_ids=["event-0-1"],
    )


def _edition_draft(marker: str, target_date: date = TARGET) -> EditionDraft:
    thesis = "今天没有足够大的能力、成本或分发变化值得扩写。" * 10
    watch = "继续观察价格、可靠性和真实用户采用是否出现可验证变化。" * 3
    rows = [
        ("claim-title", "title_block", "判断：今天没有必须追的大更新"),
        ("claim-digest", "digest_block", "判断：重要新闻不是越多越好，证据不足就不硬凑结论。"),
        ("claim-thesis", "thesis_block", "判断：" + thesis),
        ("claim-watch", "watchlist_blocks[0]", "判断：" + watch),
    ]
    claims = [_claim(*row) for row in rows]
    blocks = {
        path: PublicTextBlock(
            block_id=f"block-{claim_id.removeprefix('claim-')}",
            block_type=path,
            text=text,
            claim_ids=[claim_id],
        )
        for claim_id, path, text in rows
    }
    return EditionDraft(
        column_id="jiayu-editorial",
        target_date=target_date,
        edition_type="no_major_update",
        title_block=blocks["title_block"],
        digest_block=blocks["digest_block"],
        thesis_block=blocks["thesis_block"],
        items=[],
        watchlist_blocks=[blocks["watchlist_blocks[0]"]],
        source_links=[HttpUrl("https://example.test/story-0")],
        ai_disclosure="本文由 AI 参与资料整理和初稿生成，并经过证据约束与自动审稿。",
        input_marker=marker,
        claims=claims,
    )


def _edition(marker: str, target_date: date = TARGET) -> PersonaEdition:
    draft = _edition_draft(marker, target_date)
    unsigned = PersonaEdition.model_validate(
        {**draft.model_dump(mode="json"), "payload_sha256": "0" * 64}
    )
    return unsigned.model_copy(update={"payload_sha256": unsigned.compute_payload_sha256()})


def _standard_item(event_id: str, index: int) -> AnalysisItem:
    fields = (
        "headline_block",
        "confirmed_change_block",
        "importance_block",
        "product_implication_block",
        "counter_case_block",
        "watch_signal_block",
    )
    claims = []
    blocks: dict[str, PublicTextBlock] = {}
    for position, field in enumerate(fields):
        path = f"items[{index}].{field}"
        claim_id = f"claim-{event_id}-{position}"
        if field == "confirmed_change_block":
            text = f"Evidence for story {event_id.removeprefix('event-')}."
            claim = AnalysisClaim(
                claim_id=claim_id,
                field_path=path,
                text=text,
                claim_type="current_fact",
                current_evidence_ids=[f"{event_id}-1"],
                quotes=[
                    ClaimQuote(
                        source_kind="current_evidence",
                        source_id=f"{event_id}-1",
                        quote=text,
                    )
                ],
            )
        else:
            text = "判断：" + "这项产品判断明确说明变化、影响与边界条件。" * 3
            claim = _claim(claim_id, path, text).model_copy(
                update={"current_evidence_ids": [f"{event_id}-1"]}
            )
        claims.append(claim)
        blocks[field] = PublicTextBlock(
            block_id=f"block-{event_id}-{position}",
            block_type=field,
            text=text,
            claim_ids=[claim_id],
        )
    return AnalysisItem(
        event_id=event_id,
        grade="S" if event_id == "event-0" else "A",
        headline_block=blocks["headline_block"],
        confirmed_change_block=blocks["confirmed_change_block"],
        importance_block=blocks["importance_block"],
        product_implication_block=blocks["product_implication_block"],
        counter_case_block=blocks["counter_case_block"],
        watch_signal_block=blocks["watch_signal_block"],
        claims=claims,
        evidence_ids=[f"{event_id}-1"],
        analysis_confidence=0.9,
    )


def _standard_draft(marker: str) -> EditionDraft:
    top_rows = [
        ("claim-standard-title", "title_block", "判断：两项变化值得 AI 产品团队今天处理"),
        (
            "claim-standard-digest",
            "digest_block",
            "判断：能力与分发同时变化，重点是验证真实产品收益。",
        ),
        (
            "claim-standard-thesis",
            "thesis_block",
            "判断："
            + "今天的重要变化不在演示效果，而在产品团队能否用可复现证据改善成本、可靠性与分发。"
            * 4,
        ),
    ]
    top_claims = [_claim(*row) for row in top_rows]
    top_blocks = {
        path: PublicTextBlock(
            block_id=f"block-standard-{position}",
            block_type=path,
            text=text,
            claim_ids=[claim_id],
        )
        for position, (claim_id, path, text) in enumerate(top_rows)
    }
    items = [_standard_item("event-0", 0), _standard_item("event-1", 1)]
    return EditionDraft(
        column_id="jiayu-editorial",
        target_date=TARGET,
        edition_type="standard",
        title_block=top_blocks["title_block"],
        digest_block=top_blocks["digest_block"],
        thesis_block=top_blocks["thesis_block"],
        items=items,
        watchlist_blocks=[],
        source_links=[
            HttpUrl("https://example.test/story-0"),
            HttpUrl("https://example.test/story-1"),
        ],
        ai_disclosure="本文由 AI 参与资料整理和初稿生成，并经过证据约束与自动审稿。",
        input_marker=marker,
        claims=top_claims + [claim for item in items for claim in item.claims],
    )


def test_snapshot_requires_explicit_activation(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    snapshot = _snapshot(layout)
    assert not layout.upstream_pointer_path(TARGET).exists()

    activate_upstream_snapshot(layout, TARGET, snapshot.publication_marker)

    assert layout.upstream_pointer_path(TARGET).exists()


def test_marker_snapshot_is_immutable_and_reusable(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    publication = factories.publication(target_date=TARGET)
    original = persist_upstream_snapshot(
        layout,
        publication,
        [factories.event(0)],
        [factories.judge_decision(0)],
        None,
    )
    path = layout.upstream_object_path(publication.marker)
    original_bytes = path.read_bytes()

    reused = persist_upstream_snapshot(
        layout,
        publication,
        [factories.event(0)],
        [factories.judge_decision(0)],
        None,
    )

    assert reused == original
    assert path.read_bytes() == original_bytes
    with pytest.raises(ValueError, match="conflicting upstream snapshots"):
        persist_upstream_snapshot(
            layout,
            publication,
            [factories.event(1)],
            [factories.judge_decision(1)],
            None,
        )


def test_replay_inputs_are_copied_without_mutating_production(tmp_path: Path) -> None:
    source = SiteLayout(tmp_path / "production")
    destination = SiteLayout(tmp_path / "replay")
    source.ensure()
    destination.ensure()
    snapshot = _snapshot(source)
    activate_upstream_snapshot(source, TARGET, snapshot.publication_marker)
    object_before = source.upstream_object_path(snapshot.publication_marker).read_bytes()
    pointer_before = source.upstream_pointer_path(TARGET).read_bytes()

    _copy_replay_inputs(
        source,
        destination,
        [
            {
                "target_date": TARGET.isoformat(),
                "publication_marker": snapshot.publication_marker,
                "snapshot_sha256": snapshot.snapshot_sha256,
            }
        ],
    )

    assert source.upstream_object_path(snapshot.publication_marker).read_bytes() == object_before
    assert source.upstream_pointer_path(TARGET).read_bytes() == pointer_before
    assert load_upstream_snapshot(destination, TARGET) == snapshot


@pytest.mark.asyncio
async def test_replay_refuses_runtime_manifest_drift_before_model_calls(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path / "production")
    layout.ensure()
    snapshot = _snapshot(layout)
    activate_upstream_snapshot(layout, TARGET, snapshot.publication_marker)
    config = load_config(Path("config"))
    dataset = tmp_path / "replay.json"
    freeze_replay_dataset(layout, [TARGET], dataset, config, Path.cwd())
    payload = json.loads(dataset.read_text(encoding="utf-8"))
    payload["runtime_manifest"]["persona_config_sha256"] = "0" * 64
    dataset.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="runtime inputs drifted"):
        await run_replay(config, Secrets(), layout, Path.cwd(), dataset)


def test_shortlist_records_every_candidate(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    snapshot = _snapshot(layout)

    shortlist = build_shortlist(snapshot)

    assert shortlist.event_ids == ["event-0"]
    assert shortlist.audit[0].selected is True
    assert shortlist.audit[0].phase == "category_round_robin"


def test_shortlist_uses_locked_category_order_caps_and_stable_ties() -> None:
    categories = [
        "前沿研究",
        "模型与平台",
        "行业动态",
        "国内 AI",
        "值得试的项目",
        "前瞻与传闻",
    ]
    events = [factories.event(index, score=50) for index in range(12)]
    decisions = [
        factories.judge_decision(index, relevance=80).model_copy(
            update={"category": categories[index % len(categories)]}
        )
        for index in range(12)
    ]
    unsigned = UpstreamSnapshot(
        target_date=TARGET,
        publication_level=PublicationLevel.L0,
        publication_marker="d" * 64,
        events=events,
        evidence_bundles=[evidence_bundle(event) for event in events],
        decisions=decisions,
        editorial_plan=None,
        snapshot_sha256="0" * 64,
    )
    snapshot = unsigned.model_copy(
        update={"snapshot_sha256": sha256_payload(unsigned.canonical_payload())}
    )

    shortlist = build_shortlist(snapshot, limit=6)

    assert shortlist.event_ids == [
        "event-1",
        "event-2",
        "event-3",
        "event-10",
        "event-0",
        "event-11",
    ]


def test_shortlist_sorts_editorial_tiers_by_importance_then_event_id() -> None:
    events = [factories.event(index) for index in range(3)]
    plan = factories.plan(
        [
            factories.selection(2, importance=80),
            factories.selection(1, importance=90),
            factories.selection(0, importance=90),
        ]
    )
    unsigned = UpstreamSnapshot(
        target_date=TARGET,
        publication_level=PublicationLevel.L0,
        publication_marker="e" * 64,
        events=events,
        evidence_bundles=[evidence_bundle(event) for event in events],
        decisions=[factories.judge_decision(index) for index in range(3)],
        editorial_plan=plan,
        snapshot_sha256="0" * 64,
    )
    snapshot = unsigned.model_copy(
        update={"snapshot_sha256": sha256_payload(unsigned.canonical_payload())}
    )

    assert build_shortlist(snapshot).event_ids == ["event-0", "event-1", "event-2"]


def test_verifier_accepts_exact_claim_assembly_and_rejects_first_person(
    tmp_path: Path,
) -> None:
    config = load_config(Path("config")).persona
    assert config is not None
    layout = SiteLayout(tmp_path)
    layout.ensure()
    snapshot = _snapshot(layout)
    draft = _edition_draft(snapshot.publication_marker)

    edition = verify_edition(draft, snapshot, _scope(), config)
    assert edition.hash_is_valid()

    bad_claim = draft.claims[2].model_copy(update={"text": "我认为" + draft.claims[2].text})
    bad = draft.model_copy(
        update={
            "claims": [draft.claims[0], draft.claims[1], bad_claim, draft.claims[3]],
            "thesis_block": draft.thesis_block.model_copy(update={"text": bad_claim.text}),
        }
    )
    with pytest.raises(ValueError, match="first-person"):
        verify_edition(bad, snapshot, _scope(), config)


def test_verifier_rejects_fabricated_provenance_and_source_links(tmp_path: Path) -> None:
    config = load_config(Path("config")).persona
    assert config is not None
    layout = SiteLayout(tmp_path)
    layout.ensure()
    snapshot = _snapshot(layout)
    draft = _edition_draft(snapshot.publication_marker)

    bad_claim = draft.claims[2].model_copy(
        update={
            "claim_type": "recommendation",
            "current_evidence_ids": [],
            "quotes": [],
            "artifact_ids": ["fake-source"],
        }
    )
    bad_provenance = draft.model_copy(
        update={
            "claims": [draft.claims[0], draft.claims[1], bad_claim, draft.claims[3]],
            "thesis_block": draft.thesis_block.model_copy(update={"text": bad_claim.text}),
        }
    )
    with pytest.raises(ValueError, match="unsupported artifacts"):
        verify_edition(bad_provenance, snapshot, _scope(), config)

    bad_link = draft.model_copy(
        update={"source_links": [HttpUrl("https://attacker.example/phishing")]}
    )
    with pytest.raises(ValueError, match="source links"):
        verify_edition(bad_link, snapshot, _scope(), config)


def test_verifier_rejects_paraphrase_disguised_as_verified_fact(tmp_path: Path) -> None:
    config = load_config(Path("config")).persona
    assert config is not None
    layout = SiteLayout(tmp_path)
    layout.ensure()
    snapshot = _snapshot(layout)
    draft = _edition_draft(snapshot.publication_marker)
    bad_fact = draft.claims[2].model_copy(
        update={
            "claim_type": "current_fact",
            "text": "这是一条与引文无关、却伪装成已确认事实的结论。",
            "quotes": [
                ClaimQuote(
                    source_kind="current_evidence",
                    source_id="event-0-1",
                    quote=QUOTE,
                )
            ],
        }
    )
    bad = draft.model_copy(
        update={
            "claims": [draft.claims[0], draft.claims[1], bad_fact, draft.claims[3]],
            "thesis_block": draft.thesis_block.model_copy(update={"text": bad_fact.text}),
        }
    )

    with pytest.raises(ValueError, match="exact verified quote"):
        verify_edition(bad, snapshot, _scope(), config)


def test_analyst_result_is_evidence_verified_before_editing(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    snapshot = _snapshot(layout)
    item = _standard_item("event-0", 0)

    verify_analysis_item(item, snapshot, _scope())

    fact = item.claims[1].model_copy(update={"text": "并非证据原文的事实改写。"})
    bad = item.model_copy(
        update={
            "claims": [item.claims[0], fact, *item.claims[2:]],
            "confirmed_change_block": item.confirmed_change_block.model_copy(
                update={"text": fact.text}
            ),
        }
    )
    with pytest.raises(ValueError, match="exact verified quote"):
        verify_analysis_item(bad, snapshot, _scope())

    wrong_path = item.claims[0].model_copy(
        update={
            "field_path": "items[1].headline_block",
            "text": "判断：GPT-6 将成本降低 90%。",
            "quotes": [
                ClaimQuote(
                    source_kind="current_evidence",
                    source_id="event-0-1",
                    quote="This unsupported quote should be discarded.",
                )
            ],
        }
    )
    bad_path = item.model_copy(update={"claims": [wrong_path, *item.claims[1:]]})
    with pytest.raises(ValueError, match="field_path mismatch"):
        verify_analysis_item(bad_path, snapshot, _scope())

    unsupported_quote = ClaimQuote(
        source_kind="current_evidence",
        source_id="event-0-1",
        quote="This sentence is not present in the cited evidence.",
    )
    unsupported_fact = bad.claims[1].model_copy(update={"quotes": [unsupported_quote]})
    normalized = normalize_analysis_item(
        bad_path.model_copy(update={"claims": [wrong_path, unsupported_fact, *bad.claims[2:]]}),
        snapshot,
        _scope(),
    )
    verify_analysis_item(normalized, snapshot, _scope())
    assert normalized.claims[1].text == normalized.claims[1].quotes[0].quote
    assert normalized.claims[1].quotes[0].quote == QUOTE
    assert normalized.claims[0].field_path == "items[0].headline_block"
    assert "GPT-6" not in normalized.claims[0].text
    assert "90%" not in normalized.claims[0].text
    assert normalized.claims[0].quotes == []


def test_analysis_normalization_gives_reused_claims_stable_unique_ids(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    snapshot = _snapshot(layout)
    item = _standard_item("event-0", 0)
    reused_id = item.headline_block.claim_ids[0]
    reused = item.model_copy(
        update={
            "importance_block": item.importance_block.model_copy(
                update={"text": item.headline_block.text, "claim_ids": [reused_id]}
            )
        }
    )

    normalized = normalize_analysis_item(reused, snapshot, _scope())
    repeated = normalize_analysis_item(reused, snapshot, _scope())

    verify_analysis_item(normalized, snapshot, _scope())
    assert normalized.headline_block.claim_ids != normalized.importance_block.claim_ids
    assert normalized == repeated
    assert len(normalized.claims) == len(
        {claim.claim_id for claim in normalized.claims}
    )


def test_verifier_rejects_fabricated_fact_labeled_as_inference(tmp_path: Path) -> None:
    config = load_config(Path("config")).persona
    assert config is not None
    layout = SiteLayout(tmp_path)
    layout.ensure()
    snapshot = _snapshot(layout)
    draft = _edition_draft(snapshot.publication_marker)
    fabricated = draft.claims[2].model_copy(
        update={"text": "判断：OpenAI 已经秘密发布 GPT-6，并将 API 价格降低 90%。"}
    )
    bad = draft.model_copy(
        update={
            "claims": [
                draft.claims[0],
                draft.claims[1],
                fabricated,
                draft.claims[3],
            ],
            "thesis_block": draft.thesis_block.model_copy(update={"text": fabricated.text}),
        }
    )

    with pytest.raises(ValueError, match="ungrounded"):
        verify_edition(bad, snapshot, _scope(), config)


@pytest.mark.parametrize(
    ("updates", "kwargs", "message"),
    [
        ({"revoked_at": datetime.now(UTC).isoformat()}, {}, "inactive"),
        (
            {"valid_from": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
            {},
            "inactive",
        ),
        (
            {"expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
            {},
            "inactive",
        ),
        ({"environment": "staging"}, {}, "not for production"),
        ({"column_id": "other-column"}, {}, "scope mismatch"),
        ({"account_stable_id": "other-account"}, {}, "scope mismatch"),
        ({"allowed_actions": ["reconcile_draft"]}, {}, "not authorized"),
    ],
)
def test_authorization_denied_cases(
    updates: dict[str, Any], kwargs: dict[str, Any], message: str
) -> None:
    record = _authorization(**updates)
    with pytest.raises(WechatPublicationError, match=message):
        verify_authorization(
            record,
            AUTH_KEY,
            column_id="jiayu-editorial",
            account_stable_id="account-1",
            action="create_draft",
            **kwargs,
        )


@pytest.mark.asyncio
async def test_wechat_http_error_never_exposes_access_token() -> None:
    sentinel = "SENSITIVE_TOKEN"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("stable_token"):
            return httpx.Response(200, json={"access_token": sentinel}, request=request)
        return httpx.Response(500, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WechatClient("app", "secret", http)
        with pytest.raises(WechatPublicationError) as caught:
            await client.probe()

    assert sentinel not in str(caught.value)
    assert sentinel not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_render_and_unified_site_include_persona_page(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    publication = factories.publication(target_date=TARGET)
    edition = _edition(publication.marker)
    write_artifact(layout.persona_edition_path(TARGET), edition)

    release = publish_site(layout, publication, factories.SITE)

    page = release / "jiayu" / "index.html"
    assert edition.payload_sha256 in page.read_text(encoding="utf-8")
    assert "甲鱼主编版" in (release / "index.html").read_text(encoding="utf-8")


def test_persona_site_has_placeholder_before_first_edition(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    publication = factories.publication(target_date=TARGET)

    release = publish_site(layout, publication, factories.SITE)

    page = (release / "jiayu" / "index.html").read_text(encoding="utf-8")
    assert "首期正在准备中" in page
    assert "site-header" in page
    assert "site-footer" in page


def test_same_day_daily_upgrade_is_rejected_after_persona_freeze(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    original = factories.publication(target_date=TARGET, level=PublicationLevel.L1)
    edition = _edition(original.marker)
    write_artifact(layout.persona_edition_path(TARGET), edition)
    publish_site(layout, original, factories.SITE)
    upgraded = factories.publication(
        target_date=TARGET,
        level=PublicationLevel.L0,
        highlight="同日升级后的新基础日报。",
    )

    with pytest.raises(PublicationRefused, match="persona edition is already frozen"):
        publish_site(layout, upgraded, factories.SITE)

    committed = json.loads(layout.publication_path(TARGET).read_text(encoding="utf-8"))
    assert committed["marker"] == original.marker
    assert edition.payload_sha256 in (layout.current.resolve() / "jiayu" / "index.html").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_cli_never_rewrites_a_frozen_persona_date_after_marker_change(
    tmp_path: Path,
) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    snapshot = _snapshot(layout)
    activate_upstream_snapshot(layout, TARGET, snapshot.publication_marker)
    stale = _edition("b" * 64)
    write_artifact(layout.persona_edition_path(TARGET), stale)
    original = layout.persona_edition_path(TARGET).read_bytes()

    result = await cli._persona_run_unlocked(
        SimpleNamespace(
            config_dir="config",
            site_root=str(layout.root),
            date=TARGET.isoformat(),
            mode="dry-run",
            authorization=None,
            execute=False,
        )
    )

    assert result == 1
    assert layout.persona_edition_path(TARGET).read_bytes() == original
    status = json.loads(layout.persona_status_file.read_text(encoding="utf-8"))
    assert status["action"] == "persona_immutable_date_held"
    assert "marker changed" in status["reason"]


def test_persona_archive_keeps_pages_older_than_thirty_days(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    oldest = TARGET - timedelta(days=30)
    for offset in range(30, 0, -1):
        day = TARGET - timedelta(days=offset)
        publication = factories.publication(target_date=day)
        write_artifact(layout.publication_path(day), publication)
        write_artifact(layout.persona_edition_path(day), _edition(publication.marker, day))
    newest = factories.publication(target_date=TARGET)
    write_artifact(layout.persona_edition_path(TARGET), _edition(newest.marker))

    release = publish_site(layout, newest, factories.SITE)

    assert (release / "jiayu" / f"{oldest.isoformat()}.html").exists()
    index = (release / "jiayu" / "index.html").read_text(encoding="utf-8")
    assert f"{oldest.isoformat()}.html" in index
    assert f'datetime="{TARGET.isoformat()}"' in index
    assert 'style="color:#20201e' not in index


def test_publishing_an_old_persona_edition_does_not_roll_back_homepage(
    tmp_path: Path,
) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    old_publication = factories.publication(target_date=TARGET)
    new_publication = factories.publication(target_date=TARGET + timedelta(days=1))
    publish_site(layout, old_publication, factories.SITE)
    publish_site(layout, new_publication, factories.SITE)
    old_edition = _edition(old_publication.marker)
    write_artifact(layout.persona_edition_path(TARGET), old_edition)

    cli._publish_persona_site(load_config(Path("config")), layout, old_edition)

    current = layout.current.resolve()
    latest_page = current / "daily" / new_publication.target_date.isoformat() / "index.html"
    assert new_publication.marker in latest_page.read_text(encoding="utf-8")
    assert old_edition.payload_sha256 in (current / "jiayu" / "index.html").read_text(
        encoding="utf-8"
    )


def test_snapshot_pointer_repair_closes_post_commit_crash_gap(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path)
    layout.ensure()
    publication = factories.publication(target_date=TARGET)
    event = factories.event(0)
    persist_upstream_snapshot(
        layout,
        publication,
        [event],
        [factories.judge_decision(0)],
        None,
    )
    publish_site(layout, publication, factories.SITE)

    assert cli._repair_snapshot_pointer(layout, TARGET, publication.marker) is True
    assert load_upstream_snapshot(layout, TARGET).publication_marker == publication.marker


def test_hmac_authorization_attestation_and_slot_idempotency(tmp_path: Path) -> None:
    auth_key = AUTH_KEY
    release_key = RELEASE_KEY
    now = datetime.now(UTC)
    authorization = sign_authorization(
        {
            "schema_version": 1,
            "authorization_id": "auth-1",
            "issuer": "jesse",
            "column_id": "jiayu-editorial",
            "account_stable_id": "account-1",
            "environment": "production",
            "allowed_actions": ["create_draft"],
            "valid_from": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
            "revoked_at": None,
            "key_id": "auth-v1",
        },
        auth_key,
    )
    verify_authorization(
        authorization,
        auth_key,
        column_id="jiayu-editorial",
        account_stable_id="account-1",
        action="create_draft",
    )
    tampered = authorization.model_copy(update={"issuer": "mallory"})
    with pytest.raises(WechatPublicationError, match="signature"):
        verify_authorization(
            tampered,
            auth_key,
            column_id="jiayu-editorial",
            account_stable_id="account-1",
            action="create_draft",
        )

    edition = _edition("a" * 64)
    rendered = render_persona(edition, factories.SITE)
    attestation = attest_release(
        edition,
        rendered.receipt,
        authorization,
        account_fingerprint("app", "account-1"),
        "release-v1",
        release_key,
    )
    verify_attestation(attestation, edition, rendered.receipt, release_key)

    slots = PublicationSlots(tmp_path / "slots.sqlite3")
    slots.claim(attestation.publication_slot, "attempt-1")
    slots.update(attestation.publication_slot, "sending", attempt_id="attempt-1")
    with pytest.raises(WechatPublicationError, match="already claimed"):
        slots.claim(attestation.publication_slot, "attempt-2")
    slots.update(attestation.publication_slot, "failed", retryable=True)
    slots.claim(attestation.publication_slot, "attempt-3")
    assert slots.get(attestation.publication_slot)["attempt_id"] == "attempt-3"  # type: ignore[index]


class _ReadTimeoutWechatClient:
    app_id = "app"

    async def access_token(self) -> str:
        return "token"

    async def create_draft(self, article: dict[str, Any], token: str) -> tuple[str, dict[str, Any]]:
        raise httpx.ReadTimeout("remote result unknown")


class _VerifyTimeoutWechatClient(_ReadTimeoutWechatClient):
    async def create_draft(self, article: dict[str, Any], token: str) -> tuple[str, dict[str, Any]]:
        return "remote-media-id", {"articles": [article]}

    async def verify_draft(
        self, media_id: str, expected: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        raise httpx.ConnectError("readback unavailable")


class _VerifyMismatchWechatClient(_VerifyTimeoutWechatClient):
    async def verify_draft(
        self, media_id: str, expected: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        raise WechatPublicationError("remote draft HTML does not match submitted HTML")


class _SuccessfulWechatClient(_VerifyTimeoutWechatClient):
    async def verify_draft(
        self, media_id: str, expected: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        return {"media_id": media_id, "news_item": [expected]}


class _TokenFailureWechatClient(_ReadTimeoutWechatClient):
    async def access_token(self) -> str:
        raise httpx.ConnectError("token endpoint unavailable")


class _ConnectFailureWechatClient(_ReadTimeoutWechatClient):
    async def create_draft(self, article: dict[str, Any], token: str) -> tuple[str, dict[str, Any]]:
        raise httpx.ConnectError("draft endpoint unavailable")


class _HTTPFailureWechatClient(_ReadTimeoutWechatClient):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    async def create_draft(self, article: dict[str, Any], token: str) -> tuple[str, dict[str, Any]]:
        raise WechatHTTPError(self.status_code, "draft/add")


class _RemoteProtocolFailureWechatClient(_ReadTimeoutWechatClient):
    async def create_draft(self, article: dict[str, Any], token: str) -> tuple[str, dict[str, Any]]:
        raise httpx.RemoteProtocolError("connection closed after request")


class _DecodeFailureWechatClient(_ReadTimeoutWechatClient):
    async def create_draft(self, article: dict[str, Any], token: str) -> tuple[str, dict[str, Any]]:
        raise ValueError("invalid response JSON")


class _APIFailureWechatClient(_ReadTimeoutWechatClient):
    def __init__(self, errcode: int) -> None:
        self.errcode = errcode

    async def create_draft(self, article: dict[str, Any], token: str) -> tuple[str, dict[str, Any]]:
        raise WechatAPIError(self.errcode)


class _MissingMediaWechatClient(_ReadTimeoutWechatClient):
    async def create_draft(self, article: dict[str, Any], token: str) -> tuple[str, dict[str, Any]]:
        raise WechatResponseError("draft/add returned no media_id")


class _ReconcileWechatClient:
    app_id = "app"

    def __init__(self, media_id: str | None) -> None:
        self.media_id = media_id
        self.find_calls = 0
        self.verify_calls = 0

    async def find_draft_by_marker(self, marker: str) -> str | None:
        self.find_calls += 1
        return self.media_id

    async def verify_draft(
        self, media_id: str, expected: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        self.verify_calls += 1
        return {"media_id": media_id, "news_item": [expected]}


def _wechat_publish_inputs(tmp_path: Path) -> dict[str, Any]:
    authorization = _authorization(allowed_actions=["create_draft"])
    edition = _edition("a" * 64)
    rendered = render_persona(edition, factories.SITE)
    attestation = attest_release(
        edition,
        rendered.receipt,
        authorization,
        account_fingerprint("app", "account-1"),
        "release-v1",
        RELEASE_KEY,
    )
    return {
        "slots": PublicationSlots(tmp_path / "slots.sqlite3"),
        "edition": edition,
        "html": rendered.html,
        "receipt": rendered.receipt,
        "authorization": authorization,
        "attestation": attestation,
        "auth_key": AUTH_KEY,
        "release_key": RELEASE_KEY,
        "account_stable_id": "account-1",
        "cover_media_id": "cover-1",
        "author": "甲鱼",
    }


@pytest.mark.asyncio
async def test_wechat_successfully_creates_and_verifies_one_draft(tmp_path: Path) -> None:
    inputs = _wechat_publish_inputs(tmp_path)

    receipt = await publish_draft(client=cast(Any, _SuccessfulWechatClient()), **inputs)

    slot = inputs["slots"].get(inputs["attestation"].publication_slot)
    assert receipt.state == "verified"
    assert receipt.remote_id == "remote-media-id"
    assert slot is not None and slot["state"] == "verified"
    assert slot["remote_id"] == "remote-media-id"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("client", "expected_state", "retryable", "error_type"),
    [
        (_TokenFailureWechatClient(), "failed", True, WechatPublicationError),
        (_ConnectFailureWechatClient(), "failed", True, WechatPublicationError),
        (_HTTPFailureWechatClient(400), "failed", False, WechatHTTPError),
        (_HTTPFailureWechatClient(503), "unknown", False, WechatPublicationUnknown),
        (_RemoteProtocolFailureWechatClient(), "unknown", False, WechatPublicationUnknown),
        (_DecodeFailureWechatClient(), "unknown", False, WechatPublicationUnknown),
        (_APIFailureWechatClient(40014), "failed", True, WechatAPIError),
        (_APIFailureWechatClient(-1), "failed", True, WechatAPIError),
        (_APIFailureWechatClient(40003), "failed", False, WechatAPIError),
        (_MissingMediaWechatClient(), "unknown", False, WechatPublicationUnknown),
    ],
)
async def test_wechat_send_failures_have_explicit_retry_semantics(
    tmp_path: Path,
    client: Any,
    expected_state: str,
    retryable: bool,
    error_type: type[Exception],
) -> None:
    inputs = _wechat_publish_inputs(tmp_path)

    with pytest.raises(error_type):
        await publish_draft(client=client, **inputs)

    slot = inputs["slots"].get(inputs["attestation"].publication_slot)
    assert slot is not None and slot["state"] == expected_state
    assert slot["retryable"] is retryable


@pytest.mark.asyncio
async def test_prepared_slot_is_safely_reclaimed_before_send(tmp_path: Path) -> None:
    inputs = _wechat_publish_inputs(tmp_path)
    slot_id = inputs["attestation"].publication_slot
    inputs["slots"].claim(slot_id, "crashed-before-token")

    receipt = await publish_draft(client=cast(Any, _SuccessfulWechatClient()), **inputs)

    slot = inputs["slots"].get(slot_id)
    assert receipt.state == "verified"
    assert slot is not None and slot["state"] == "verified"
    assert slot["attempt_id"] != "crashed-before-token"


@pytest.mark.asyncio
async def test_reconcile_rejects_tampered_edition_before_remote_access(tmp_path: Path) -> None:
    inputs = _wechat_publish_inputs(tmp_path)
    slot_id = inputs["attestation"].publication_slot
    inputs["slots"].claim(slot_id, "attempt-unknown")
    inputs["slots"].update(slot_id, "unknown")
    client = _ReconcileWechatClient("remote-media-id")
    tampered = inputs["edition"].model_copy(update={"payload_sha256": "f" * 64})

    with pytest.raises(
        WechatPublicationError,
        match=r"bind rendered bytes|edition hash is invalid",
    ):
        await reconcile_draft(
            client=cast(Any, client),
            slots=inputs["slots"],
            edition=tampered,
            html=inputs["html"],
            receipt=inputs["receipt"],
            authorization=_authorization(allowed_actions=["reconcile_draft"]),
            attestation=inputs["attestation"],
            auth_key=AUTH_KEY,
            release_key=RELEASE_KEY,
            account_stable_id="account-1",
            cover_media_id="cover-1",
            author="甲鱼",
        )

    assert client.find_calls == 0
    assert client.verify_calls == 0


@pytest.mark.asyncio
async def test_wechat_read_timeout_persists_reconcilable_unknown_receipt(
    tmp_path: Path,
) -> None:
    auth_key = AUTH_KEY
    release_key = RELEASE_KEY
    now = datetime.now(UTC)
    authorization = sign_authorization(
        {
            "schema_version": 1,
            "authorization_id": "auth-timeout",
            "issuer": "jesse",
            "column_id": "jiayu-editorial",
            "account_stable_id": "account-1",
            "environment": "production",
            "allowed_actions": ["create_draft"],
            "valid_from": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
            "revoked_at": None,
            "key_id": "auth-v1",
        },
        auth_key,
    )
    edition = _edition("a" * 64)
    rendered = render_persona(edition, factories.SITE)
    attestation = attest_release(
        edition,
        rendered.receipt,
        authorization,
        account_fingerprint("app", "account-1"),
        "release-v1",
        release_key,
    )
    slots = PublicationSlots(tmp_path / "slots.sqlite3")

    with pytest.raises(WechatPublicationUnknown) as caught:
        await publish_draft(
            client=cast(Any, _ReadTimeoutWechatClient()),
            slots=slots,
            edition=edition,
            html=rendered.html,
            receipt=rendered.receipt,
            authorization=authorization,
            attestation=attestation,
            auth_key=auth_key,
            release_key=release_key,
            account_stable_id="account-1",
            cover_media_id="cover-1",
            author="甲鱼",
        )

    assert caught.value.receipt is not None
    assert caught.value.receipt.state == "unknown"
    assert slots.get(attestation.publication_slot)["state"] == "unknown"  # type: ignore[index]


@pytest.mark.asyncio
async def test_wechat_readback_connection_failure_is_unknown_not_retryable(
    tmp_path: Path,
) -> None:
    authorization = _authorization(allowed_actions=["create_draft"])
    edition = _edition("a" * 64)
    rendered = render_persona(edition, factories.SITE)
    attestation = attest_release(
        edition,
        rendered.receipt,
        authorization,
        account_fingerprint("app", "account-1"),
        "release-v1",
        RELEASE_KEY,
    )
    slots = PublicationSlots(tmp_path / "slots.sqlite3")

    with pytest.raises(WechatPublicationUnknown) as caught:
        await publish_draft(
            client=cast(Any, _VerifyTimeoutWechatClient()),
            slots=slots,
            edition=edition,
            html=rendered.html,
            receipt=rendered.receipt,
            authorization=authorization,
            attestation=attestation,
            auth_key=AUTH_KEY,
            release_key=RELEASE_KEY,
            account_stable_id="account-1",
            cover_media_id="cover-1",
            author="甲鱼",
        )

    slot = slots.get(attestation.publication_slot)
    assert caught.value.receipt is not None
    assert caught.value.receipt.remote_id == "remote-media-id"
    assert slot is not None and slot["state"] == "unknown"
    assert slot["retryable"] is False


@pytest.mark.asyncio
async def test_wechat_readback_mismatch_is_reconcilable_and_never_retryable(
    tmp_path: Path,
) -> None:
    authorization = _authorization(allowed_actions=["create_draft"])
    edition = _edition("a" * 64)
    rendered = render_persona(edition, factories.SITE)
    attestation = attest_release(
        edition,
        rendered.receipt,
        authorization,
        account_fingerprint("app", "account-1"),
        "release-v1",
        RELEASE_KEY,
    )
    slots = PublicationSlots(tmp_path / "slots.sqlite3")

    with pytest.raises(WechatPublicationError, match="does not match"):
        await publish_draft(
            client=cast(Any, _VerifyMismatchWechatClient()),
            slots=slots,
            edition=edition,
            html=rendered.html,
            receipt=rendered.receipt,
            authorization=authorization,
            attestation=attestation,
            auth_key=AUTH_KEY,
            release_key=RELEASE_KEY,
            account_stable_id="account-1",
            cover_media_id="cover-1",
            author="甲鱼",
        )

    slot = slots.get(attestation.publication_slot)
    assert slot is not None
    assert slot["state"] == "remote_mismatch"
    assert slot["retryable"] is False
    assert slot["remote_id"] == "remote-media-id"


def test_corrupt_old_publication_does_not_block_current_persona_archive(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    old_date = date(2026, 8, 26)
    old_publication = factories.publication(target_date=old_date)
    write_artifact(
        layout.persona_edition_path(old_date), _edition(old_publication.marker, old_date)
    )
    layout.publication_path(old_date).write_text("{not-json", encoding="utf-8")

    current = factories.publication(target_date=TARGET)
    write_artifact(layout.persona_edition_path(TARGET), _edition(current.marker, TARGET))
    release = publish_site(layout, current, factories.SITE)

    assert (release / "jiayu" / f"{TARGET.isoformat()}.html").exists()
    assert not (release / "jiayu" / f"{old_date.isoformat()}.html").exists()
    assert [item.target_date for item in recent_persona_editions(layout)] == [TARGET]


@pytest.mark.asyncio
@pytest.mark.parametrize("media_id", ["found-media-id", None])
async def test_unknown_draft_reconciliation_never_creates_a_second_draft(
    tmp_path: Path, media_id: str | None
) -> None:
    authorization = _authorization()
    edition = _edition("a" * 64)
    rendered = render_persona(edition, factories.SITE)
    attestation = attest_release(
        edition,
        rendered.receipt,
        authorization,
        account_fingerprint("app", "account-1"),
        "release-v1",
        RELEASE_KEY,
    )
    slots = PublicationSlots(tmp_path / "slots.sqlite3")
    slots.claim(attestation.publication_slot, "attempt-unknown")
    slots.update(attestation.publication_slot, "unknown")
    client = _ReconcileWechatClient(media_id)
    current_authorization = _authorization(authorization_id="auth-current")
    call = reconcile_draft(
        client=cast(Any, client),
        slots=slots,
        edition=edition,
        receipt=rendered.receipt,
        authorization=current_authorization,
        attestation=attestation,
        auth_key=AUTH_KEY,
        release_key=RELEASE_KEY,
        account_stable_id="account-1",
        html=rendered.html,
        cover_media_id="cover-1",
        author="甲鱼",
    )

    if media_id is None:
        with pytest.raises(WechatPublicationUnknown, match="no matching"):
            await call
        assert slots.get(attestation.publication_slot)["state"] == "unknown"  # type: ignore[index]
    else:
        receipt = await call
        assert receipt.state == "verified"
        assert receipt.remote_id == media_id
        assert slots.get(attestation.publication_slot)["state"] == "verified"  # type: ignore[index]
    assert client.find_calls == 1
    assert client.verify_calls == int(media_id is not None)


@pytest.mark.asyncio
async def test_wechat_reconciliation_marker_is_in_structured_source_metadata() -> None:
    edition = _edition("a" * 64)
    rendered = render_persona(edition, factories.SITE)
    payload = persona_cli.draft_article_payload(
        edition,
        rendered.html,
        "cover-1",
        "甲鱼",
    )

    assert payload["content_source_url"].endswith(f"#jiayu-{edition.payload_sha256}")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("stable_token"):
            return httpx.Response(200, json={"access_token": "token"}, request=request)
        if request.url.path.endswith("draft/batchget"):
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "item": [
                        {
                            "media_id": "remote-media-id",
                            "content": {
                                "news_item": [
                                    {
                                        "content": "comment removed by platform",
                                        "content_source_url": payload["content_source_url"],
                                    }
                                ]
                            },
                        }
                    ],
                },
                request=request,
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WechatClient("app", "secret", http)
        assert await client.find_draft_by_marker(edition.payload_sha256) == "remote-media-id"


@pytest.mark.asyncio
async def test_existing_wechat_slot_automatically_reconciles_instead_of_creating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    authorization = _authorization()
    edition = _edition("a" * 64)
    rendered = render_persona(edition, factories.SITE)
    attestation = attest_release(
        edition,
        rendered.receipt,
        authorization,
        account_fingerprint("app", "account-1"),
        "release-v1",
        RELEASE_KEY,
    )
    target = WechatTarget(
        edition=edition,
        html=rendered.html,
        render_receipt=rendered.receipt,
        authorization=authorization,
        attestation=attestation,
        cover_media_id="cover-1",
        author="甲鱼",
        request_sha256="e" * 64,
    )
    slots = PublicationSlots(layout.root / "wechat-slots.sqlite3")
    slots.claim(attestation.publication_slot, "attempt-existing")
    slots.update(attestation.publication_slot, "unknown")
    calls = {"reconcile": 0, "publish": 0}
    sentinel = cast(Any, object())

    class FakeClient:
        app_id = "app"

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    async def fake_reconcile(**kwargs: Any) -> Any:
        calls["reconcile"] += 1
        return sentinel

    async def fake_publish(**kwargs: Any) -> Any:
        calls["publish"] += 1
        raise AssertionError("an occupied slot must never create another draft")

    monkeypatch.setattr(persona_cli, "WechatClient", FakeClient)
    monkeypatch.setattr(persona_cli, "reconcile_draft", fake_reconcile)
    monkeypatch.setattr(persona_cli, "publish_draft", fake_publish)
    monkeypatch.setenv("WECHAT_APP_ID", "app")
    monkeypatch.setenv("WECHAT_APP_SECRET", "secret")
    monkeypatch.setenv("WECHAT_ACCOUNT_STABLE_ID", "account-1")
    monkeypatch.setenv("AI_DAILY_AUTH_HMAC_KEY", AUTH_KEY)
    monkeypatch.setenv("AI_DAILY_RELEASE_HMAC_KEY", RELEASE_KEY)

    result = await persona_cli._execute_target(layout, target)

    assert result is sentinel
    assert calls == {"reconcile": 1, "publish": 0}


@pytest.mark.asyncio
async def test_persona_draft_writes_prepared_and_verified_manifests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    edition = _edition("a" * 64)
    authorization_path = tmp_path / "authorization.json"
    write_artifact(authorization_path, _authorization())
    for name, value in {
        "WECHAT_APP_ID": "app",
        "WECHAT_ACCOUNT_STABLE_ID": "account-1",
        "AI_DAILY_AUTH_HMAC_KEY": AUTH_KEY,
        "AI_DAILY_RELEASE_HMAC_KEY": RELEASE_KEY,
        "WECHAT_COVER_MEDIA_ID": "cover-1",
    }.items():
        monkeypatch.setenv(name, value)
    args = SimpleNamespace(authorization=authorization_path, execute=False)

    assert await persona_cli.persona_draft(args, load_config(Path("config")), layout, edition) == 0
    manifest_path = layout.persona_manifest_path(TARGET)
    prepared = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert prepared["wechat_state"] == "not_attempted"
    render_receipt = RenderReceipt.model_validate_json(
        Path(prepared["render_receipt_path"]).read_text(encoding="utf-8")
    )
    assert render_receipt.edition_payload_sha256 == edition.payload_sha256
    target = WechatTarget.model_validate_json(
        layout.wechat_target_path(TARGET).read_text(encoding="utf-8")
    )

    async def verified(*args: Any, **kwargs: Any) -> OperationReceipt:
        return OperationReceipt(
            kind="wechat_draft",
            attempt_id="attempt-verified",
            publication_slot=target.attestation.publication_slot,
            operation="create_draft",
            state="verified",
            request_sha256=target.request_sha256,
            created_at=datetime.now(UTC),
            account_fingerprint=target.attestation.account_fingerprint,
            response_sha256="f" * 64,
            remote_id="remote-media-id",
        )

    monkeypatch.setattr(persona_cli, "_execute_target", verified)
    args.execute = True
    assert await persona_cli.persona_draft(args, load_config(Path("config")), layout, edition) == 0
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed["wechat_state"] == "draft_verified"
    assert Path(completed["wechat_receipt_path"]).exists()


@pytest.mark.asyncio
async def test_persona_draft_persists_unknown_receipt_and_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    edition = _edition("a" * 64)
    authorization_path = tmp_path / "authorization.json"
    write_artifact(authorization_path, _authorization())
    for name, value in {
        "WECHAT_APP_ID": "app",
        "WECHAT_ACCOUNT_STABLE_ID": "account-1",
        "AI_DAILY_AUTH_HMAC_KEY": AUTH_KEY,
        "AI_DAILY_RELEASE_HMAC_KEY": RELEASE_KEY,
        "WECHAT_COVER_MEDIA_ID": "cover-1",
    }.items():
        monkeypatch.setenv(name, value)
    target_box: dict[str, WechatTarget] = {}
    original_build = persona_cli._build_target

    def capture_target(*args: Any, **kwargs: Any) -> tuple[WechatTarget, Path]:
        target, path = original_build(*args, **kwargs)
        target_box["target"] = target
        return target, path

    async def unknown(*args: Any, **kwargs: Any) -> OperationReceipt:
        target = target_box["target"]
        receipt = OperationReceipt(
            kind="wechat_draft",
            attempt_id="attempt-unknown",
            publication_slot=target.attestation.publication_slot,
            operation="create_draft",
            state="unknown",
            request_sha256=target.request_sha256,
            created_at=datetime.now(UTC),
            account_fingerprint=target.attestation.account_fingerprint,
            error_code="ReadTimeout",
        )
        raise WechatPublicationUnknown("result unknown", receipt)

    monkeypatch.setattr(persona_cli, "_build_target", capture_target)
    monkeypatch.setattr(persona_cli, "_execute_target", unknown)
    args = SimpleNamespace(authorization=authorization_path, execute=True)

    assert await persona_cli.persona_draft(args, load_config(Path("config")), layout, edition) == 2
    manifest = json.loads(layout.persona_manifest_path(TARGET).read_text(encoding="utf-8"))
    assert manifest["wechat_state"] == "unknown"
    assert Path(manifest["wechat_receipt_path"]).exists()


def test_short_hmac_keys_are_rejected() -> None:
    with pytest.raises(WechatPublicationError, match="64 hex characters"):
        sign_authorization(
            _authorization().model_dump(mode="json", exclude={"signature"}),
            "too-short",
        )


@pytest.mark.asyncio
async def test_hmac_keys_are_compared_as_decoded_bytes(tmp_path: Path) -> None:
    authorization = _authorization(allowed_actions=["create_draft"])
    edition = _edition("a" * 64)
    rendered = render_persona(edition, factories.SITE)
    with pytest.raises(WechatPublicationError, match="keys must differ"):
        await publish_draft(
            client=cast(Any, _SuccessfulWechatClient()),
            slots=PublicationSlots(tmp_path / "slots.sqlite3"),
            edition=edition,
            html=rendered.html,
            receipt=rendered.receipt,
            authorization=authorization,
            attestation=attest_release(
                edition,
                rendered.receipt,
                authorization,
                account_fingerprint("app", "account-1"),
                "release-v1",
                AUTH_KEY,
            ),
            auth_key=AUTH_KEY,
            release_key=AUTH_KEY.upper(),
            account_stable_id="account-1",
            cover_media_id="cover-1",
            author="甲鱼",
        )


def test_finalizer_must_declare_real_public_field_changes() -> None:
    draft = _edition_draft("a" * 64)
    resolution = FinalizerResolution(
        blocker_id="blocker-thesis",
        resolution="收紧原来的判断。",
        changed_fields=["thesis_block"],
    )
    unchanged = FinalizerOutput(draft=draft, resolutions=[resolution])
    with pytest.raises(ValueError, match="exactly match"):
        _validate_finalizer_changes(draft, unchanged)

    old_claim = draft.claims[2]
    new_claim = old_claim.model_copy(update={"text": old_claim.text + "仍需继续验证。"})
    changed = draft.model_copy(
        update={
            "claims": [draft.claims[0], draft.claims[1], new_claim, draft.claims[3]],
            "thesis_block": draft.thesis_block.model_copy(update={"text": new_claim.text}),
        }
    )
    _validate_finalizer_changes(draft, FinalizerOutput(draft=changed, resolutions=[resolution]))


class _FakeGateway:
    def __init__(self, draft: EditionDraft) -> None:
        config = load_config(Path("config")).persona
        assert config is not None
        self.ledger = BudgetLedger(config.budget)
        self.runs: list[Any] = []
        self.draft = draft

    async def generate(self, role: str, output_type: Any, *args: Any, **kwargs: Any) -> Any:
        if role == "persona_planner":
            value: Any = PersonaPlan(
                edition_type="no_major_update",
                today_thesis="今天没有需要展开的重大更新。",
                selections=[
                    PlanSelection(
                        event_id="event-0",
                        grade="B",
                        importance_reason="证据真实，但变化幅度不够大。",
                        evidence_ids=["event-0-1"],
                    )
                ],
                watchlist_event_ids=[],
                omitted=[],
            )
        elif role == "persona_edition_editor":
            value = self.draft
        elif role == "persona_critic":
            prompt = json.loads(args[1])
            value = Critique(
                draft_sha256=sha256_payload(prompt["draft"]),
                review_round=1,
                findings=[],
            )
        else:
            raise AssertionError(role)
        validator = kwargs.get("validator")
        if validator:
            validator(value)
        return cast(Any, value)


class _StandardGateway:
    def __init__(self, draft: EditionDraft) -> None:
        config = load_config(Path("config")).persona
        assert config is not None
        self.ledger = BudgetLedger(config.budget)
        self.runs: list[Any] = []
        self.draft = draft
        self.active_analysts = 0
        self.maximum_analysts = 0

    async def generate(self, role: str, output_type: Any, *args: Any, **kwargs: Any) -> Any:
        if role == "persona_planner":
            value: Any = PersonaPlan(
                edition_type="standard",
                today_thesis="两项变化值得展开。",
                selections=[
                    PlanSelection(
                        event_id=f"event-{index}",
                        grade="S" if index == 0 else "A",
                        importance_reason="这项变化会影响 AI 产品决策。",
                        evidence_ids=[f"event-{index}-1"],
                    )
                    for index in range(2)
                ],
                watchlist_event_ids=[],
                omitted=[],
            )
        elif role == "persona_analyst":
            prompt = json.loads(args[1])
            event_id = str(prompt["selection"]["event_id"])
            self.active_analysts += 1
            self.maximum_analysts = max(self.maximum_analysts, self.active_analysts)
            await asyncio.sleep(0.01)
            self.active_analysts -= 1
            value = AnalystOutput(item=_standard_item(event_id, 0))
        elif role == "persona_edition_editor":
            value = self.draft
        elif role == "persona_critic":
            prompt = json.loads(args[1])
            value = Critique(
                draft_sha256=sha256_payload(prompt["draft"]),
                review_round=1,
                findings=[],
            )
        else:
            raise AssertionError(role)
        validator = kwargs.get("validator")
        if validator:
            validator(value)
        return cast(Any, value)


class _FinalizerGateway:
    def __init__(self, before: EditionDraft, second_round_blocker: bool) -> None:
        config = load_config(Path("config")).persona
        assert config is not None
        self.ledger = BudgetLedger(config.budget)
        self.runs: list[Any] = []
        self.before = before
        self.second_round_blocker = second_round_blocker
        self.critic_calls = 0
        old_claim = before.claims[2]
        new_claim = old_claim.model_copy(update={"text": old_claim.text + "仍需观察。"})
        self.after = before.model_copy(
            update={
                "claims": [before.claims[0], before.claims[1], new_claim, before.claims[3]],
                "thesis_block": before.thesis_block.model_copy(update={"text": new_claim.text}),
            }
        )

    async def generate(self, role: str, output_type: Any, *args: Any, **kwargs: Any) -> Any:
        prompt = json.loads(args[1])
        if role == "persona_critic":
            self.critic_calls += 1
            round_number = self.critic_calls
            findings = []
            if round_number == 1 or self.second_round_blocker:
                findings = [
                    CritiqueFinding(
                        blocker_id=f"blocker-round-{round_number}",
                        severity="blocker",
                        field_path="thesis_block",
                        issue_type="unsupported_entailment",
                        explanation="这项判断需要进一步收紧。",
                        status="open",
                    )
                ]
            value: Any = Critique(
                draft_sha256=sha256_payload(prompt["draft"]),
                review_round=round_number,
                findings=findings,
            )
        elif role == "persona_finalizer":
            value = FinalizerOutput(
                draft=self.after,
                resolutions=[
                    FinalizerResolution(
                        blocker_id="blocker-round-1",
                        resolution="收紧判断并保留观察边界。",
                        changed_fields=["thesis_block"],
                    )
                ],
            )
        else:
            raise AssertionError(role)
        validator = kwargs.get("validator")
        if validator:
            validator(value)
        return cast(Any, value)


@pytest.mark.asyncio
async def test_persona_pipeline_runs_end_to_end_without_provider(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    snapshot = _snapshot(layout)
    activate_upstream_snapshot(layout, TARGET, snapshot.publication_marker)
    draft = _edition_draft(snapshot.publication_marker)
    gateway = _FakeGateway(draft)
    pipeline = PersonaPipeline(
        load_config(Path("config")),
        Secrets(),
        layout,
        Path.cwd(),
        TARGET,
        gateway=cast(Any, gateway),
    )

    result = await pipeline.run(TARGET)

    assert result.editorial_state == "ready", result.reason
    persisted = PersonaEdition.model_validate_json(
        layout.persona_edition_path(TARGET).read_text(encoding="utf-8")
    )
    assert persisted.hash_is_valid()


@pytest.mark.asyncio
async def test_pipeline_does_not_freeze_stale_edition_if_marker_changes_before_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    original = _snapshot(layout)
    activate_upstream_snapshot(layout, TARGET, original.publication_marker)
    config = load_config(Path("config"))
    pipeline = PersonaPipeline(
        config,
        Secrets(),
        layout,
        Path.cwd(),
        TARGET,
        gateway=cast(Any, _FakeGateway(_edition_draft(original.publication_marker))),
    )
    replacement_unsigned = original.model_copy(
        update={
            "publication_marker": "b" * 64,
            "snapshot_sha256": "0" * 64,
        }
    )
    replacement = replacement_unsigned.model_copy(
        update={"snapshot_sha256": sha256_payload(replacement_unsigned.canonical_payload())}
    )
    write_artifact(layout.upstream_object_path(replacement.publication_marker), replacement)

    async def stale_candidate(target_date: date, run_dir: Path) -> PersonaEdition:
        del target_date, run_dir
        activate_upstream_snapshot(layout, TARGET, replacement.publication_marker)
        return _edition(original.publication_marker)

    monkeypatch.setattr(pipeline, "_produce", stale_candidate)

    result = await pipeline.run(TARGET)

    assert result.editorial_state == "held"
    assert "marker changed" in str(result.reason)
    assert not layout.persona_edition_path(TARGET).exists()


@pytest.mark.asyncio
async def test_standard_pipeline_analyzes_multiple_events_concurrently(
    tmp_path: Path,
) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    events = [factories.event(0), factories.event(1)]
    unsigned = UpstreamSnapshot(
        target_date=TARGET,
        publication_level=PublicationLevel.L0,
        publication_marker="c" * 64,
        events=events,
        evidence_bundles=[evidence_bundle(event) for event in events],
        decisions=[factories.judge_decision(index) for index in range(2)],
        editorial_plan=None,
        snapshot_sha256="0" * 64,
    )
    snapshot = unsigned.model_copy(
        update={"snapshot_sha256": sha256_payload(unsigned.canonical_payload())}
    )
    write_artifact(layout.upstream_object_path(snapshot.publication_marker), snapshot)
    activate_upstream_snapshot(layout, TARGET, snapshot.publication_marker)
    gateway = _StandardGateway(_standard_draft(snapshot.publication_marker))
    pipeline = PersonaPipeline(
        load_config(Path("config")),
        Secrets(),
        layout,
        Path.cwd(),
        TARGET,
        gateway=cast(Any, gateway),
    )

    result = await pipeline.run(TARGET)

    assert result.editorial_state == "ready", result.reason
    assert gateway.maximum_analysts == 2
    persisted = PersonaEdition.model_validate_json(
        layout.persona_edition_path(TARGET).read_text(encoding="utf-8")
    )
    assert [item.event_id for item in persisted.items] == ["event-0", "event-1"]
    assert persisted.hash_is_valid()


@pytest.mark.asyncio
async def test_blocker_runs_finalizer_and_clean_second_critic(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    snapshot = _snapshot(layout)
    draft = _edition_draft(snapshot.publication_marker)
    gateway = _FinalizerGateway(draft, second_round_blocker=False)
    pipeline = PersonaPipeline(
        load_config(Path("config")),
        Secrets(),
        layout,
        Path.cwd(),
        TARGET,
        gateway=cast(Any, gateway),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = await pipeline._review(snapshot, draft, _scope(), run_dir)

    assert result == gateway.after
    assert (run_dir / "finalizer.json").exists()
    assert (run_dir / "critique-2.json").exists()


@pytest.mark.asyncio
async def test_second_critic_blocker_keeps_edition_held(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    snapshot = _snapshot(layout)
    draft = _edition_draft(snapshot.publication_marker)
    gateway = _FinalizerGateway(draft, second_round_blocker=True)
    pipeline = PersonaPipeline(
        load_config(Path("config")),
        Secrets(),
        layout,
        Path.cwd(),
        TARGET,
        gateway=cast(Any, gateway),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    with pytest.raises(ValueError, match="round 2 still has open blockers"):
        await pipeline._review(snapshot, draft, _scope(), run_dir)


def test_critic_cannot_self_resolve_a_finding() -> None:
    critique = Critique(
        draft_sha256="a" * 64,
        review_round=1,
        findings=[
            CritiqueFinding(
                blocker_id="blocker-self-resolved",
                severity="blocker",
                field_path="thesis_block",
                issue_type="unsupported_entailment",
                explanation="模型不能自己宣告问题已经解决。",
                status="resolved",
            )
        ],
    )

    with pytest.raises(ValueError, match="must remain open"):
        _validate_critique(critique, "a" * 64, 1)
