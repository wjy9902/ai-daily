from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl

from ai_daily.models import Event, RawItem

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{1,}|[\u4e00-\u9fff]{2,}", re.IGNORECASE)


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, urlencode(query), ""))


def title_tokens(title: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(title)}


def _similar(left: RawItem, right: RawItem, window: timedelta) -> bool:
    left_time = left.published_at or left.discovered_at
    right_time = right.published_at or right.discovered_at
    if abs(left_time - right_time) > window:
        return False
    left_tokens = title_tokens(left.title)
    right_tokens = title_tokens(right.title)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    return overlap >= 0.72


def cluster_items(items: list[RawItem], window_hours: int = 48) -> list[Event]:
    by_url: dict[str, list[RawItem]] = defaultdict(list)
    for item in items:
        by_url[canonicalize_url(str(item.url))].append(item)

    groups: list[list[RawItem]] = list(by_url.values())
    merged: list[list[RawItem]] = []
    window = timedelta(hours=window_hours)
    for group in groups:
        for existing in merged:
            if _similar(group[0], existing[0], window):
                existing.extend(group)
                break
        else:
            merged.append(group)

    events = []
    for group in merged:
        primary = min(group, key=lambda item: (item.source_tier.value, -len(item.summary)))
        canonical = canonicalize_url(str(primary.url))
        event_id = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        events.append(
            Event(
                event_id=event_id,
                canonical_url=HttpUrl(canonical),
                title=primary.title,
                summary=primary.summary,
                published_at=primary.published_at,
                items=group,
            )
        )
    return events


def score_events(events: list[Event], now: datetime) -> list[Event]:
    scored = []
    for event in events:
        tiers = {item.source_tier.value for item in event.items}
        source_score = 30 if "A" in tiers else 20 if "B" in tiers else 10
        age = now - (event.published_at or event.items[0].discovered_at)
        recency = max(0, 25 - age.total_seconds() / 3600)
        corroboration = min(20, (len({item.source for item in event.items}) - 1) * 10)
        text = f"{event.title} {event.summary}".lower()
        action_terms = ("release", "launch", "api", "model", "open source", "发布", "开源", "模型")
        actionability = 15 if any(term in text for term in action_terms) else 5
        metrics = sum(
            float(value)
            for item in event.items
            for value in item.metrics.values()
            if isinstance(value, (int, float))
        )
        popularity = min(10, metrics / 50)
        scored.append(
            event.model_copy(
                update={
                    "score": min(
                        100, source_score + recency + corroboration + actionability + popularity
                    )
                }
            )
        )
    return sorted(scored, key=lambda event: event.score, reverse=True)


def remove_historical(events: list[Event], historical_urls: set[str]) -> list[Event]:
    canonical_history = {canonicalize_url(url) for url in historical_urls}
    return [
        event
        for event in events
        if canonicalize_url(str(event.canonical_url)) not in canonical_history
    ]
