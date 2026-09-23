import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import factories
import pytest

from ai_daily import brief_recovery
from ai_daily.brief_recovery import BriefBatch, RecoveredBrief, recover_briefs
from ai_daily.budget import BudgetExceeded
from ai_daily.composer import _story_to_brief
from ai_daily.copy_quality import copy_problem, source_copy_ready
from ai_daily.model_gateway import MissingProviderSecret, ModelInvocationFailed


def output(event):
    return RecoveredBrief(
        event_id=event.event_id,
        headline="模型发布更新",
        brief="该模型已发布更新，原文提供了具体更新说明。",
        evidence_id=event.event_id + "-1",
        quote=event.items[0].summary[:80],
    )


def pending(index=0):
    event = factories.event(index)
    event.summary = event.items[0].summary = "The source confirms a product update. " * 12
    return event


@pytest.mark.parametrize(
    "value",
    [
        "No final punctuation",
        "v3.5 costs $2.50",
        "It's available",
        'He said "ready".',
        "https://example.test/a/b",
        "字" * 240,
    ],
)
def test_conservative_checks_allow_valid_copy(value):
    assert copy_problem(value, 320) is None


@pytest.mark.parametrize(
    "value", [" ", "unfinished,", "尚未结束：", "[source](https://a.test", 'He said "']
)
def test_obvious_incomplete_copy_is_rejected(value):
    assert copy_problem(value, 320)


def test_actual_eight_truncations_are_rejected():
    samples = json.loads(Path("tests/fixtures/truncated-briefs-2026-09-23.json").read_text())
    assert len(samples) == 8
    for sample in samples:
        assert copy_problem(sample["broken"], 320, sample["summary"])
        assert not source_copy_ready(sample["title"], sample["summary"])


def test_valid_tldr_is_not_cut():
    story = factories.story_card().model_copy(update={"tldr": "事实已经确认。" * 40})
    assert len(story.tldr) > 240
    assert _story_to_brief(story).brief == story.tldr


async def test_direct_complete_source_needs_no_model():
    event = factories.event()
    gateway, enrich = AsyncMock(), AsyncMock()
    assert await recover_briefs([event], gateway, enrich, []) == [event]
    gateway.generate.assert_not_called()
    enrich.assert_not_called()


async def test_three_batches_max_and_partial_results_survive():
    events = [pending(i) for i in range(16)]
    responses = [
        BriefBatch(
            items=[
                output(events[0]),
                output(events[1]).model_copy(
                    update={"quote": "This quote was never in the evidence."}
                ),
            ]
        ),
        ModelInvocationFailed("safe"),
        BriefBatch(items=[output(events[8])]),
    ]
    gateway, enrich, audit = AsyncMock(), AsyncMock(), []
    gateway.generate.side_effect = responses
    result = await recover_briefs(events, gateway, enrich, audit)
    assert [e.event_id for e in result] == [events[0].event_id, events[8].event_id]
    assert gateway.generate.call_count == 3
    assert all(len(json.loads(c.kwargs["prompt"])) <= 4 for c in gateway.generate.call_args_list)
    assert any(a["reason"] == "invalid_evidence_quote" for a in audit)
    enrich.assert_not_called()


@pytest.mark.parametrize("error", [BudgetExceeded("limit"), MissingProviderSecret("missing")])
async def test_global_failure_stops_new_batches(error):
    gateway, audit = AsyncMock(), []
    gateway.generate.side_effect = error
    assert await recover_briefs([pending(i) for i in range(12)], gateway, AsyncMock(), audit) == []
    assert gateway.generate.call_count == 1
    assert len(audit) == 12


async def test_enrichment_once_before_generating():
    event = pending()
    event.summary = event.items[0].summary = "Short fragment"

    async def enrich(events):
        events[0].summary = events[0].items[0].summary = "The verified article explains the update."

    fetch = AsyncMock(side_effect=enrich)
    gateway = AsyncMock()

    async def generate(*args, **kwargs):
        return BriefBatch(items=[output(event)])

    gateway.generate.side_effect = generate
    result = await recover_briefs([event], gateway, fetch, [])
    assert len(result) == 1
    fetch.assert_awaited_once()


async def test_timeout_keeps_previous_batch(monkeypatch):
    monkeypatch.setattr(brief_recovery, "RECOVERY_SECONDS", 0.02)
    events = [pending(i) for i in range(8)]
    count = 0

    async def generate(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            return BriefBatch(items=[output(events[0])])
        await asyncio.sleep(1)

    gateway, audit = AsyncMock(), []
    gateway.generate.side_effect = generate
    result = await recover_briefs(events, gateway, AsyncMock(), audit)
    assert [e.event_id for e in result] == [events[0].event_id]
    assert any(a["reason"] == "recovery_timeout" for a in audit)


async def test_unknown_event_and_overlong_title_are_rejected_per_item():
    events = [pending(i) for i in range(3)]
    batch = BriefBatch(
        items=[
            output(events[0]),
            output(events[1]).model_copy(update={"headline": "长" * 101}),
            output(events[2]).model_copy(update={"event_id": "unknown"}),
        ]
    )
    gateway, audit = AsyncMock(), []
    gateway.generate.return_value = batch
    result = await recover_briefs(events, gateway, AsyncMock(), audit)
    assert [e.event_id for e in result] == [events[0].event_id]
    assert {a["reason"] for a in audit} >= {
        "too_long",
        "unknown_or_duplicate_event",
        "missing_output",
    }


async def test_failed_fetch_still_tries_existing_evidence():
    event = pending()
    event.summary = event.items[0].summary = "Source says the new product is available"
    # Collector reports individual fetch failures in its audit, not by raising.
    fetch = AsyncMock(return_value=None)
    gateway = AsyncMock()
    gateway.generate.return_value = BriefBatch(items=[output(event)])
    assert len(await recover_briefs([event], gateway, fetch, [])) == 1
    fetch.assert_awaited_once()


async def test_all_failed_recovery_becomes_l3():
    from ai_daily.composer import build_ranked_publication
    from ai_daily.degradation import DegradationTracker
    from ai_daily.publication import PublicationLevel

    gateway = AsyncMock()
    gateway.generate.side_effect = ModelInvocationFailed("failed")
    result = await recover_briefs([pending()], gateway, AsyncMock(), [])
    pub = build_ranked_publication(factories.TARGET_DATE, result, DegradationTracker())
    assert pub.level is PublicationLevel.L3
    assert not pub.briefs


def test_balanced_markdown_link_is_not_rejected():
    assert copy_problem("Read [source](https://example.test/a).", 320) is None


@pytest.mark.parametrize(
    "text",
    [
        "See (https://example.test/a).",
        'The display measures 5".',
        "https://example.test/item_(version)",
    ],
)
def test_urls_and_measurements_do_not_look_like_unclosed_quotes(text):
    assert copy_problem(text, 320) is None


@pytest.mark.parametrize(
    "text", ['发布新模型 "GPT-6"，现已开放。', '参数 "v2" 已生效。', 'The 5" display runs "v2".']
)
def test_numeric_quoted_names_are_not_inch_marks(text):
    assert copy_problem(text, 320) is None
