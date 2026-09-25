"""The September 23 launch sources must remain separate editorial events."""

import json
from pathlib import Path

from ai_daily.content import evidence_bundle
from ai_daily.models import RawItem
from ai_daily.normalize import (
    _ordered_event_items,
    cluster_items,
    product_lexicon,
    title_product_identifiers,
)
from ai_daily.planning import lead_is_corroborated

FIXTURE = Path("tests/fixtures/model-launch-cluster-2026-09-23.json")


def _sources() -> list[RawItem]:
    return [RawItem.model_validate(item) for item in json.loads(FIXTURE.read_text())]


def test_launches_do_not_merge_through_a_price_war_story() -> None:
    sources = _sources()
    events = cluster_items(sources, lexicon=product_lexicon(sources))
    by_source = {item.source: event for event in events for item in event.items}

    opus = by_source["anthropic-news"]
    gpt = by_source["openai-news"]
    jev = by_source["toms-hardware"]
    assert len({opus.event_id, gpt.event_id, jev.event_id}) == 3
    assert opus.primary_item.source == "anthropic-news"
    assert gpt.primary_item.source == "openai-news"
    assert evidence_bundle(opus).evidence[0].source == "Anthropic"
    assert evidence_bundle(gpt).evidence[0].source == "OpenAI"
    assert lead_is_corroborated(opus)
    assert lead_is_corroborated(gpt)
    assert any(item.source == "x-anthropic" for item in opus.items)
    assert by_source["36kr-newsflashes"].event_id != opus.event_id
    assert by_source["axios"].event_id not in {opus.event_id, gpt.event_id}


def test_numeric_multipliers_do_not_teach_generic_product_names() -> None:
    lexicon = product_lexicon(_sources())
    assert "cheaper" not in lexicon
    assert not title_product_identifiers("193x faster and 445x cheaper", lexicon)


def test_short_official_launch_prefers_rich_same_topic_support() -> None:
    sources = _sources()
    by_source = {item.source: item for item in sources}
    primary = by_source["openai-news"]
    support = by_source["aws-ml-blog"]
    comparison = by_source["testingcatalog"]
    other_model = support.model_copy(update={"title": "GPT-6 Astra is now available on AWS"})
    ordered = _ordered_event_items(primary, [primary, comparison, other_model, support])

    assert [item.source for item in ordered] == [
        "openai-news",
        "aws-ml-blog",
        "testingcatalog",
        "aws-ml-blog",
    ]
    event = next(
        event
        for event in cluster_items(sources, lexicon=product_lexicon(sources))
        if event.primary_item.source == "openai-news"
    )
    assert evidence_bundle(event.model_copy(update={"items": ordered})).evidence[1].source == (
        "AWS Machine Learning Blog"
    )


def test_bare_product_name_before_version_remains_an_anchor() -> None:
    assert {"astra", "gpt6"} <= title_product_identifiers(
        "Astra (GPT-6) launches", frozenset({"astra"})
    )


def test_same_launch_still_merges_across_languages() -> None:
    sources = _sources()
    events = cluster_items(sources, lexicon=product_lexicon(sources))
    by_source = {item.source: event for event in events for item in event.items}
    assert any(
        item.source == "ithome" and "Opus 5.5" in item.title
        for item in by_source["anthropic-news"].items
    )
    assert any(
        item.source == "ithome" and "GPT-6" in item.title for item in by_source["openai-news"].items
    )
