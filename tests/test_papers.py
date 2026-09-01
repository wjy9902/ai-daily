from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import factories
import httpx
import pytest
from pydantic import HttpUrl

from ai_daily.budget import BudgetLedger, BudgetStage
from ai_daily.models import BudgetConfig, RawItem, SourceConfig, SourceTier
from ai_daily.papers import (
    PapersPipeline,
    apply_cross_mentions,
    arxiv_id,
    build_candidates,
    historical_paper_keys,
    normalize_title,
    publication_gate,
    score_signals,
    select_papers,
    supplement_floor,
    validate_deep_read,
)
from ai_daily.papers_config import load_papers_config
from ai_daily.papers_fulltext import clean_arxiv_html, fetch_full_papers
from ai_daily.papers_models import (
    DeepRead,
    ExperimentClaim,
    PaperCandidate,
    PaperCard,
    PaperSignals,
    PapersPublication,
    SupplementBatch,
    SupplementChoice,
    TopicBatch,
    TopicDecision,
    load_papers_publication,
)
from ai_daily.render import render_papers_index, render_papers_issue, render_papers_rss
from ai_daily.site_publisher import (
    PublicationRefused,
    SiteLayout,
    publication_lock,
    publish_papers_site,
    publish_site,
    render_release,
)
from ai_daily.sources import Collector


def candidate(index: int, score: float = 5, *, hf: bool = True) -> PaperCandidate:
    identifier = f"2609.{index:05d}"
    return PaperCandidate(
        arxiv_id=identifier,
        title_key=f"candidatepaper{index:030d}",
        title=f"Candidate paper {index}",
        abstract="An abstract about agent reasoning.",
        submitted_at=datetime(2026, 9, 1, tzinfo=UTC),
        arxiv_url=HttpUrl(f"https://arxiv.org/abs/{identifier}"),
        signals=PaperSignals(
            hf_listed=hf,
            upvotes=1 if hf and score >= 4 else 0,
            organization="Example Lab" if not hf else None,
            org_tier=0.4 if not hf else 0,
            base_score=score,
            final_score=score,
        ),
    )


def deep_read() -> DeepRead:
    quote = "Accuracy | Baseline 41.2 | Ours 56.8"
    return DeepRead(
        positioning="A positioning statement.",
        background="Background.",
        mechanism="Mechanism.",
        experiment_summary="Results support the mechanism without numeric prose.",
        experiments=[ExperimentClaim(claim="Accuracy improves to 56.8%.", quote=quote)],
        novelty="Novelty.",
        soundness="Soundness.",
        significance="Significance.",
        limitations="Limitations.",
        follow_up="Follow up.",
    )


def card(index: int, *, deep: bool = True, title: str | None = None) -> PaperCard:
    item = candidate(index)
    return PaperCard(
        arxiv_id=item.arxiv_id,
        title=title or item.title,
        abstract=item.abstract,
        arxiv_url=item.arxiv_url,
        signals=item.signals,
        topic="agent",
        deep_read=deep_read() if deep else None,
        fallback_reason=None if deep else "HTML unavailable",
    )


def papers_publication(
    target: date = date(2026, 9, 1), cards: list[PaperCard] | None = None
) -> PapersPublication:
    return PapersPublication(
        target_date=target,
        generated_at=datetime(2026, 9, 1, 1, tzinfo=UTC),
        papers=cards or [card(1), card(2)],
    ).signed()


class PromptGateway:
    def __init__(self, supplement: str | None = None, fail_topic_call: int | None = None) -> None:
        self.supplement = supplement
        self.fail_topic_call = fail_topic_call
        self.topic_calls = 0

    async def generate(self, role: str, output_type: type[Any], **kwargs: Any) -> Any:
        del role
        if output_type is TopicBatch:
            self.topic_calls += 1
            if self.fail_topic_call == self.topic_calls:
                raise RuntimeError("one batch failed")
            rows = json.loads(cast(str, kwargs["prompt"]))
            output = TopicBatch(
                decisions=[
                    TopicDecision(candidate_id=row["candidate_id"], topic="其他") for row in rows
                ]
            )
        elif output_type is SupplementBatch:
            choices = (
                [SupplementChoice(candidate_id=self.supplement, reason="cold but credible")]
                if self.supplement
                else []
            )
            output = SupplementBatch(choices=choices)
        else:
            raise AssertionError(output_type)
        validator = kwargs.get("validator")
        return validator(output) if validator else output


def test_score_formula_preserves_every_signal_component() -> None:
    signals = score_signals(
        PaperSignals(
            hf_listed=True,
            upvotes=7,
            organization="OpenAI",
            org_tier=1,
            github_repo="https://github.com/example/repo",
            github_stars=15,
            cross_mentions=2,
            agent_bonus=1,
        )
    )
    expected = 3 + 1.5 * math.log2(8) + 2 + 1 + 0.5 * math.log2(16) + 2
    assert signals.base_score == expected
    assert signals.final_score == expected + 1


def test_arxiv_versions_and_title_punctuation_are_normalized() -> None:
    assert arxiv_id("https://arxiv.org/pdf/2401.12345v3") == "2401.12345"
    assert normalize_title("A Paper: Why?") == normalize_title("a-paper why")


async def test_main_channel_gate_ignores_supplements() -> None:
    config = load_papers_config()
    selected, reasons = await select_papers(
        [candidate(1), candidate(2), candidate(3, score=1, hf=False)],
        config,
        cast(Any, PromptGateway(supplement="2609.00003")),
    )
    assert selected == []
    assert "minimum is 3" in reasons[-1]


async def test_supplement_bypasses_score_but_must_pass_floor() -> None:
    config = load_papers_config()
    cold = candidate(9, score=1, hf=False)
    assert supplement_floor(cold)
    selected, _ = await select_papers(
        [candidate(1), candidate(2), candidate(3), cold],
        config,
        cast(Any, PromptGateway(supplement="2609.00009")),
    )
    assert selected[-1].supplement is True
    assert selected[-1].signals.final_score < config.score_threshold


async def test_topic_batches_isolate_one_failure() -> None:
    config = load_papers_config()
    values = [candidate(index) for index in range(1, 24)]
    selected, reasons = await select_papers(
        values, config, cast(Any, PromptGateway(fail_topic_call=1))
    )
    assert len(selected) == 8
    assert reasons == ["topic batch 1: RuntimeError"]


def test_cross_mentions_match_id_title_and_unique_sources(tmp_path: Path) -> None:
    target = date(2026, 9, 1)
    run = tmp_path / target.isoformat() / "run-1"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}", encoding="utf-8")
    value = candidate(1)
    rows = {
        "items": [
            {"source": "x-one", "url": "https://arxiv.org/abs/2609.00001", "summary": ""},
            {"source": "x-one", "url": "https://arxiv.org/pdf/2609.00001", "summary": ""},
            {"source": "hn", "title": value.title_key, "url": "https://news.ycombinator.com"},
        ]
    }
    (run / "sources.json").write_text(json.dumps(rows), encoding="utf-8")
    result = apply_cross_mentions([value], tmp_path, target)
    assert result[0].signals.cross_mentions == 2


def test_arxiv_category_configuration_does_not_change_daily_default() -> None:
    papers = SourceConfig(
        name="papers",
        kind="arxiv",
        url="https://export.arxiv.org/api/query",
        tier=SourceTier.A,
        arxiv_categories=["cs.AI", "cs.MA"],
    )
    daily = SourceConfig(
        name="daily", kind="arxiv", url="https://export.arxiv.org/api/query", tier=SourceTier.A
    )
    assert papers.arxiv_categories == ["cs.AI", "cs.MA"]
    assert daily.arxiv_categories is None


def test_relative_papers_artifacts_live_under_writable_site_root(tmp_path: Path) -> None:
    config = load_papers_config()
    config.artifacts_dir = "artifacts"
    layout = SiteLayout(tmp_path / "site")

    pipeline = PapersPipeline(
        config,
        Path("config"),
        layout,
        collector=cast(Any, object()),
        gateway=cast(Any, PromptGateway()),
    )

    assert pipeline.artifacts_dir == layout.root / "artifacts"


@pytest.mark.parametrize("nested", [True, False])
async def test_hf_payload_shapes_and_missing_fields_are_defensive(
    nested: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def allow_test_dns(value: str) -> None:
        del value

    monkeypatch.setattr("ai_daily.sources._validate_public_dns", allow_test_dns)
    paper = {
        "id": "2609.00001",
        "title": "Paper",
        "summary": "Abstract",
        "publishedAt": "2026-09-01T00:00:00Z",
        "organization": {"name": "Example Lab"},
        "githubRepo": "https://github.com/example/repo",
        "githubStars": 12,
        "numComments": 3,
        "authors": [{"name": "Alice"}],
    }
    value = {"paper": paper, "upvotes": 7} if nested else {**paper, "numUpvotes": 7}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[value]))
    source = SourceConfig(
        name="papers-hf-daily",
        kind="huggingface",
        url="https://huggingface.co/api/daily_papers",
        tier=SourceTier.A,
    )
    items, health = await Collector(transport).collect([source])
    assert health[0].status == "ok"
    assert items[0].metrics["upvotes"] == 7
    assert items[0].metrics["organization"] == "Example Lab"
    assert items[0].author == "Alice"

    minimal = {"id": "2609.00002", "title": "Minimal", "publishedAt": "2026-09-01"}
    collector = Collector(httpx.MockTransport(lambda request: httpx.Response(200, json=[minimal])))
    items, _ = await collector.collect([source])
    assert items[0].metrics["github_stars"] == 0
    assert items[0].metrics["organization"] == ""


async def test_arxiv_fetch_uses_configured_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow_test_dns(value: str) -> None:
        del value

    monkeypatch.setattr("ai_daily.sources._validate_public_dns", allow_test_dns)
    seen_query = ""
    feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'><title>x</title>
    <entry><id>https://arxiv.org/abs/2609.00001v1</id><title>Paper</title>
    <summary>Abstract</summary><published>2026-09-01T00:00:00Z</published>
    <link href='https://arxiv.org/abs/2609.00001v1'/></entry></feed>"""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_query
        seen_query = request.url.params["search_query"]
        return httpx.Response(200, content=feed)

    source = SourceConfig(
        name="papers-arxiv",
        kind="arxiv",
        url="https://export.arxiv.org/api/query",
        tier=SourceTier.A,
        arxiv_categories=["cs.AI", "cs.MA"],
    )
    items, _ = await Collector(httpx.MockTransport(handler)).collect([source])
    assert len(items) == 1
    assert seen_query == "cat:cs.AI OR cat:cs.MA"


def test_hf_and_arxiv_versions_merge_to_one_candidate() -> None:
    config = load_papers_config()
    hf = RawItem(
        source="papers-hf-daily",
        source_tier=SourceTier.A,
        source_item_id="2609.00001v2",
        url="https://huggingface.co/papers/2609.00001",
        title="Merged paper",
        summary="HF abstract",
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        discovered_at=datetime(2026, 9, 1, tzinfo=UTC),
        metrics={"upvotes": 8, "organization": "Example Lab"},
    )
    arxiv = hf.model_copy(
        update={
            "source": "papers-arxiv",
            "source_item_id": "https://arxiv.org/abs/2609.00001v3",
            "url": HttpUrl("https://arxiv.org/abs/2609.00001v3"),
            "summary": "Full arXiv abstract",
            "author": "Alice, Bob",
            "metrics": {},
        }
    )
    merged = build_candidates([hf, arxiv], config)
    assert len(merged) == 1
    assert merged[0].arxiv_id == "2609.00001"
    assert merged[0].signals.hf_listed is True
    assert merged[0].authors == "Alice, Bob"


async def test_missing_arxiv_html_becomes_a_simple_read_reason() -> None:
    feed = b"""<feed xmlns='http://www.w3.org/2005/Atom'><entry>
    <id>https://arxiv.org/abs/2609.00001v3</id><author><name>Alice</name></author>
    </entry></feed>"""
    paths: list[str] = []

    class FakeCollector:
        async def _request(self, url: str, **kwargs: object) -> httpx.Response:
            del kwargs
            paths.append(url)
            request = httpx.Request("GET", url)
            if "export.arxiv.org" in url:
                return httpx.Response(200, content=feed, request=request)
            return httpx.Response(404, request=request)

    result = await fetch_full_papers(cast(Any, FakeCollector()), ["2609.00001"])
    assert paths[-1].endswith("/2609.00001v3")
    assert result["2609.00001"].text is None
    assert result["2609.00001"].failure == "arXiv HTML unavailable"


def test_custom_papers_budget_shares_are_independent() -> None:
    ledger = BudgetLedger(
        BudgetConfig(
            request_limit=60,
            input_token_limit=3_000_000,
            output_token_limit=600_000,
            cost_cny_limit=8,
        ),
        request_shares={BudgetStage.JUDGE: 0.2, BudgetStage.PLAN: 0, BudgetStage.DRAFT: 0.8},
        cost_shares={BudgetStage.JUDGE: 0.2, BudgetStage.PLAN: 0, BudgetStage.DRAFT: 0.8},
    )
    assert ledger.stage_remaining_requests(BudgetStage.DRAFT) == 48
    assert ledger.stage_remaining_cost(BudgetStage.DRAFT) == 6.4


def test_html_cleaning_linearizes_tables_and_removes_references() -> None:
    raw = b"""<html><body><article><h2>Method</h2><p>State verifier mechanism.</p>
    <h2>Experiments</h2><table><tr><th>Model</th><th>Accuracy</th></tr>
    <tr><td>Ours</td><td>56.8</td></tr></table>
    <section class='ltx_bibliography'><p>Secret reference list</p></section>
    </article></body></html>"""
    text = clean_arxiv_html(raw)
    assert "Model | Accuracy" in text
    assert "Ours | 56.8" in text
    assert "Secret reference" not in text


def test_html_truncation_discards_related_work_before_experiments() -> None:
    related = "R" * 400
    experiments = "Experiment result 56.8 " * 20
    raw = (
        f"<article><h2>Related Work</h2><p>{related}</p>"
        f"<h2>Experiments</h2><p>{experiments}</p></article>"
    )
    text = clean_arxiv_html(raw.encode(), limit=500)
    assert "Experiment result" in text
    assert related not in text


def test_deep_read_claim_quotes_pass_and_numeric_summary_fails() -> None:
    value = deep_read()
    full = "Table 1\nAccuracy | Baseline 41.2 | Ours 56.8\nEnd."
    assert validate_deep_read(value, full) is value
    with pytest.raises(ValueError, match="summary"):
        validate_deep_read(value.model_copy(update={"experiment_summary": "Improved 100%."}), full)
    with pytest.raises(ValueError, match="substring"):
        validate_deep_read(
            value.model_copy(
                update={"experiments": [ExperimentClaim(claim="x", quote="fabricated quote text")]}
            ),
            full,
        )


@pytest.mark.parametrize(
    ("cards", "accepted"),
    [
        ([card(1), card(2)], True),
        ([card(1), card(2, deep=False)], False),
        ([card(1), card(2), card(3, deep=False), card(4, deep=False), card(5, deep=False)], False),
    ],
)
def test_publication_gate(cards: list[PaperCard], accepted: bool) -> None:
    assert publication_gate(papers_publication(cards=cards))[0] is accepted


def test_papers_record_checksum_detects_corruption() -> None:
    publication = papers_publication()
    assert load_papers_publication(publication.model_dump_json()).marker == publication.marker
    corrupted = publication.model_dump_json().replace("Candidate paper 1", "changed")
    with pytest.raises(ValueError, match="marker"):
        load_papers_publication(corrupted)


def test_render_has_papers_rss_prefix_xss_escaping_and_simple_label() -> None:
    publication = papers_publication(
        cards=[card(1, title='<script>alert("x")</script>'), card(2, deep=False)]
    )
    index = render_papers_index(publication, [publication], factories.SITE)
    issue = render_papers_issue(publication, factories.SITE)
    rss = render_papers_rss([publication], factories.SITE)
    assert 'href="../papers/rss.xml"' in index
    assert 'href="../../papers/rss.xml"' in issue
    assert "<script>" not in index
    assert "&lt;script&gt;" in index
    assert "未深读" in index
    assert f"{factories.SITE}/papers/2026-09-01/" in rss


async def test_papers_publish_renders_then_commits_and_refuses_same_day(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path / "site")
    publish_site(layout, factories.publication(), factories.SITE)
    publication = papers_publication()
    release = await publish_papers_site(layout, publication, factories.SITE, retry_seconds=0)
    assert layout.papers_publication_path(publication.target_date).exists()
    assert publication.marker in (release / "papers" / "index.html").read_text(encoding="utf-8")
    with pytest.raises(PublicationRefused, match="already published"):
        await publish_papers_site(layout, publication, factories.SITE, retry_seconds=0)


async def test_papers_publish_render_failure_does_not_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_daily import site_publisher

    layout = SiteLayout(tmp_path / "site")
    publish_site(layout, factories.publication(), factories.SITE)
    publication = papers_publication()
    monkeypatch.setattr(site_publisher, "render_release", lambda *args, **kwargs: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        await publish_papers_site(layout, publication, factories.SITE, retry_seconds=0)
    assert not layout.papers_publication_path(publication.target_date).exists()


async def test_papers_publish_gives_up_when_daily_holds_lock(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path / "site")
    publish_site(layout, factories.publication(), factories.SITE)
    with publication_lock(layout), pytest.raises(PublicationRefused, match="remained busy"):
        await publish_papers_site(
            layout, papers_publication(), factories.SITE, lock_attempts=2, retry_seconds=0
        )


async def test_activation_double_failure_leaves_record_for_next_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_daily import site_publisher

    layout = SiteLayout(tmp_path / "site")
    daily = factories.publication()
    publish_site(layout, daily, factories.SITE)
    publication = papers_publication()
    original_activate = site_publisher.activate_release
    monkeypatch.setattr(
        site_publisher,
        "activate_release",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("activation failed")),
    )
    with pytest.raises(OSError, match="activation failed"):
        await publish_papers_site(layout, publication, factories.SITE, retry_seconds=0)
    assert layout.papers_publication_path(publication.target_date).exists()

    monkeypatch.setattr(site_publisher, "activate_release", original_activate)
    release = render_release(layout, daily, factories.SITE, "self-heal")
    original_activate(layout, release)
    page = release / "papers" / publication.target_date.isoformat() / "index.html"
    assert publication.marker in page.read_text(encoding="utf-8")


def test_corrupt_historical_papers_record_is_not_swallowed(tmp_path: Path) -> None:
    layout = SiteLayout(tmp_path / "site")
    layout.ensure()
    publication = papers_publication()
    corrupted = publication.model_dump_json().replace("Candidate paper 1", "tampered")
    layout.papers_publication_path(publication.target_date).write_text(corrupted, encoding="utf-8")
    with pytest.raises(ValueError, match="marker"):
        historical_paper_keys(layout)


def test_systemd_contract_and_cli_have_no_papers_date_option() -> None:
    service = Path("ops/systemd/ai-daily-papers.service").read_text(encoding="utf-8")
    timer = Path("ops/systemd/ai-daily-papers.timer").read_text(encoding="utf-8")
    assert "TimeoutStartSec=3600" in service
    assert "ai-daily papers --mode publish" in service
    assert "OnCalendar=*-*-* 06:10:00" in timer
    from ai_daily.cli import build_parser

    args = build_parser().parse_args(["papers", "--mode", "dry-run"])
    assert args.command == "papers"
    assert not hasattr(args, "date")
