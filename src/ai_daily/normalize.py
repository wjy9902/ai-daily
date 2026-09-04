from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl

from ai_daily.history import HistoricalIndex, HistoricalStory
from ai_daily.models import Event, RawItem, SourceChannel

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}
LATIN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+-]{1,}", re.IGNORECASE)
CHINESE_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
STRONG_IDENTIFIER_RE = re.compile(r"\b(?:CVE-\d{4}-\d+|GHSA-[a-z0-9-]+)\b", re.IGNORECASE)
STOPWORDS = {
    "about",
    "after",
    "announcing",
    "introducing",
    "launches",
    "new",
    "release",
    "released",
    "the",
    "with",
    "发布",
    "推出",
    "正式",
}
HISTORICAL_STOPWORDS = STOPWORDS | {
    "a",
    "about",
    "according",
    "ai",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "but",
    "by",
    "can",
    "company",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "get",
    "had",
    "has",
    "have",
    "how",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "make",
    "may",
    "model",
    "models",
    "more",
    "new",
    "news",
    "not",
    "now",
    "of",
    "on",
    "only",
    "or",
    "other",
    "our",
    "out",
    "over",
    "report",
    "reported",
    "reports",
    "rt",
    "said",
    "says",
    "some",
    "system",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "today",
    "too",
    "under",
    "up",
    "very",
    "via",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
    "一个",
    "一种",
    "今日",
    "公司",
    "已经",
    "消息",
    "模型",
    "目前",
    "相关",
    "研究",
    "表示",
    "进行",
    "通过",
}
AI_TOKENS = {
    "agent",
    "agentic",
    "agents",
    "ai",
    "anthropic",
    "chatbot",
    "chatgpt",
    "claude",
    "codex",
    "copilot",
    "cuda",
    "cursor",
    "deepmind",
    "deepseek",
    "diffusion",
    "gemini",
    "gemma",
    "genai",
    "glm",
    "gpt",
    "grok",
    "hunyuan",
    "inference",
    "jax",
    "kimi",
    "llama",
    "llm",
    "llms",
    "manus",
    "mcp",
    "midjourney",
    "minimax",
    "mistral",
    "moonshot",
    "multimodal",
    "nemotron",
    "notebooklm",
    "ollama",
    "openai",
    "openrouter",
    "perplexity",
    "pytorch",
    "qwen",
    "rag",
    "robotics",
    "sora",
    "tensorflow",
    "transformer",
    "veo",
    "vllm",
    "worldclaw",
    "xai",
    "zhipu",
}
AI_PHRASES = (
    "artificial intelligence",
    "foundation model",
    "hugging face",
    "large language model",
    "machine learning",
    "neural network",
    "model context protocol",
    "vibe coding",
    "人工智能",
    "大模型",
    "大语言模型",
    "多模态",
    "开源模型",
    "推理模型",
    "文生图",
    "文生视频",
    "智谱",
    "月之暗面",
    "具身智能",
    "智能体",
    "机器学习",
    "模型推理",
    "生成式",
    "算力",
    "语音模型",
    "视频模型",
)
# Registrable-domain suffixes that span more than one label. Kept as an explicit
# list on purpose: a public-suffix dependency is not worth taking for the
# handful of multi-part suffixes this feed actually sees.
MULTI_LABEL_PUBLIC_SUFFIXES = frozenset(
    {
        "com.cn",
        "net.cn",
        "org.cn",
        "gov.cn",
        "edu.cn",
        "ac.cn",
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "me.uk",
        "net.uk",
        "co.jp",
        "or.jp",
        "ne.jp",
        "ac.jp",
        "go.jp",
        "com.au",
        "net.au",
        "org.au",
        "edu.au",
        "gov.au",
        "co.kr",
        "or.kr",
        "com.hk",
        "org.hk",
        "com.tw",
        "org.tw",
        "com.sg",
        "com.br",
        "com.mx",
        "co.in",
        "co.nz",
        "co.za",
    }
)
FIRST_PARTY_CHANNELS = frozenset({SourceChannel.OFFICIAL, SourceChannel.RELEASE})
#: RSSHub renders a retweet as "RT <display name>: <the other account's post>".
#: The separator is what keeps RTX, RTFM and a product called RT-2 out.
_RETWEET_RE = re.compile(r"^RT[ @:]")


def is_amplified(item: RawItem) -> bool:
    """True when ``item`` is an account repeating someone else's post.

    A vendor account carries two kinds of traffic and this project was reading
    them as one. Its own posts are the newsmaker speaking: first-party, Tier A,
    the thing lead_is_corroborated exists to find. Its retweets are the
    newsmaker pointing at a stranger - on 2026-09-04 x-openai-devs amplified 91
    of them, among the "Astra absolutely crushes" and "one-shotted this tool"
    genre - and reading those as OpenAI stating a fact meant a launch could
    qualify to lead an issue on nothing but praise the vendor liked.

    They stay in the event, because an official amplification is still a signal
    that the story is real. They just stop being the evidence for it, and stop
    being eligible to represent the cluster they sit in.
    """

    return bool(_RETWEET_RE.match(item.title.strip()))


CHANNEL_PRIORITY = {
    SourceChannel.OFFICIAL: 0,
    SourceChannel.NEWS: 1,
    SourceChannel.COMMUNITY: 2,
    SourceChannel.RELEASE: 3,
    SourceChannel.RESEARCH: 4,
}
COMMUNITY_DISCOVERY_SCORE = 100


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


#: Dashes that outlets typeset inside a product name, written as escapes
#: because that is what they are being read as. Simon Willison filed the GPT-6
#: launch with U+2011 between the name and the version, which LATIN_TOKEN_RE
#: splits into "gpt" and a bare "6" it then drops for being one character - so
#: the title that named the product yielded no identifier at all and the launch
#: never merged with anyone else's report of it. Typography is not a difference
#: between stories.
_UNICODE_DASHES = str.maketrans(
    dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe63\uff0d", "-")
)


def normalize_dashes(text: str) -> str:
    return text.translate(_UNICODE_DASHES)


def title_tokens(title: str) -> set[str]:
    latin = {token.lower() for token in LATIN_TOKEN_RE.findall(normalize_dashes(title))} - STOPWORDS
    chinese: set[str] = set()
    for sequence in CHINESE_RE.findall(title):
        if sequence in STOPWORDS:
            continue
        chinese.update(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return (latin | chinese) - STOPWORDS


def story_identifiers(item: RawItem) -> set[str]:
    text = f"{item.title} {item.summary[:1200]}"
    return {value.lower() for value in STRONG_IDENTIFIER_RE.findall(text)}


def is_ai_related(item: RawItem) -> bool:
    if item.source_ai_focused:
        return True
    text = f"{item.title} {item.summary[:1500]}".lower()
    tokens = {token.lower() for token in LATIN_TOKEN_RE.findall(text)}
    has_ai_signal = bool(tokens & AI_TOKENS) or any(phrase in text for phrase in AI_PHRASES)
    return has_ai_signal or _is_high_signal_community_item(item)


def _is_high_signal_community_item(item: RawItem) -> bool:
    if item.source_channel != SourceChannel.COMMUNITY:
        return False
    score = item.metrics.get("score", 0)
    return isinstance(score, (int, float)) and score >= COMMUNITY_DISCOVERY_SCORE


#: Punctuation stripped when normalizing a product identifier, so that
#: "GLM-5.3-Flash", "GLM 5.3 Flash" and "glm5.3flash" all collapse to the
#: same key.
_PRODUCT_ID_STRIP = re.compile(r"[.\-+_]")
#: A roundup title enumerates many products ("Sonnet 4.5、GLM-4.6、Ring-1T…").
#: Using its identifiers for merging would chain unrelated stories together
#: through the roundup, so such titles contribute none. Counted over *direct*
#: identifiers only: a roundup is a title carrying many real product names, and
#: the joined rule below answers for its own noise.
_PRODUCT_ID_ROUNDUP_LIMIT = 3
#: The joined rule glues a name to a following number, and a number-heavy
#: headline offers many pairs that are not names. Techmeme filed the GPT-6
#: launch as "OpenAI prices GPT-6 Astra at $10/1M … vs $40" and "scores 62.7%
#: … and 99.9%", which produced at10, vs40, scores627 and and999 - enough
#: junk to break the roundup limit, so the guard threw away the real gpt6
#: anchor along with it and three reports of the day's biggest launch stayed
#: three separate one-source events. Real names produce one or two pairs.
_PRODUCT_ID_JOINED_LIMIT = 2
#: A version number is not a quantity. Money and percentages are the two the
#: headlines above actually collide with, and both are marked in the text.
_CURRENCY_CHARS = frozenset("$¥€£₩₽¢￥")
#: LATIN_TOKEN_RE needs two characters, so a lone version digit is not a token
#: at all: "Grok 4" never produced grok4 despite the docstring below promising
#: it, and "GPT 6" would not either. Anchors are the one place a single
#: character carries meaning, so they tokenize for themselves.
_PRODUCT_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+-]*", re.IGNORECASE)


def _is_quantity(text: str, start: int, end: int) -> bool:
    """Whether the token spanning ``[start, end)`` is money or a percentage."""

    before = text[start - 1] if start else ""
    return before in _CURRENCY_CHARS or text[end : end + 1] == "%"


def title_product_identifiers(title: str, lexicon: frozenset[str] = frozenset()) -> set[str]:
    """Versioned product names from a TITLE, as cross-outlet merge anchors.

    Different outlets word the same announcement differently — and a Chinese
    report shares almost no tokens with the vendor's English post — but both
    keep the product name verbatim: "GLM-5.3-Flash", "Qwen3.8", "Grok 4".
    A token qualifies when it mixes letters and digits ("glm-5.3-flash");
    a bare name followed by a version ("Grok 4", "GPT 5.5") is joined into
    one identifier. Titles only: summaries (and digest-style titles, see the
    roundup limit) mention many products and would over-merge.

    ``lexicon`` carries the day's bare-word product names - Astra, Sol, Mythos
    - which the digit rule above cannot see at all. See product_lexicon for
    where they come from and why they are safe to anchor on.

    Only the products the title is *about* count. A headline that prices one
    model against another names both, and merging on the loser chains two
    launches into one event: on 2026-09-04 "OpenAI prices GPT-6 Astra … vs
    $5/$40 for GPT-5.6 Sol" and a tweet reading "Muse Spark 1.3 >> Fable 5"
    were enough to weld the Astra, Muse Spark and Fable stories into a single
    29-item cluster that a Meta retweet then got to name. Anything past a
    comparison or a second clause is context, so it is dropped.
    """

    anchors = _title_anchors(title)
    if len(anchors.direct) > _PRODUCT_ID_ROUNDUP_LIMIT:
        return set()
    joined = {} if len(anchors.joined) > _PRODUCT_ID_JOINED_LIMIT else anchors.joined
    lexical = {token: index for index, token in enumerate(anchors.tokens) if token in lexicon}
    positions = anchors.direct | joined | lexical
    if not positions:
        return set()
    subject_ends = min(positions.values()) + _SUBJECT_SPAN
    return {name for name, index in positions.items() if index <= subject_ends}


@dataclass(frozen=True)
class _TitleAnchors:
    """Product identifiers in one title, with the token index each starts at."""

    tokens: list[str]
    direct: dict[str, int]
    joined: dict[str, int]
    #: Token index -> the bare name that opens a joined identifier. "Fable" in
    #: "Claude Fable 5.1" is as much the product's name as "GPT-6" is, and it
    #: is the form the lexicon has to learn from vendors that space the version.
    joined_names: dict[int, str]


def _title_anchors(title: str) -> _TitleAnchors:
    text = normalize_dashes(title)
    boundary = _comparison_boundary(text)
    matches = [match for match in _PRODUCT_TOKEN_RE.finditer(text) if match.start() < boundary]
    tokens = [match.group(0).lower() for match in matches]
    direct: dict[str, int] = {}
    joined: dict[str, int] = {}
    joined_names: dict[int, str] = {}
    for index, token in enumerate(tokens):
        normalized = _PRODUCT_ID_STRIP.sub("", token)
        has_alpha = any(ch.isalpha() for ch in normalized)
        has_digit = any(ch.isdigit() for ch in normalized)
        if _is_direct_identifier(normalized):
            direct.setdefault(normalized, index)
        # "Kimi K3" / "Grok 4": the name alone is generic and the version
        # alone is noise, but joined they are as specific as a long id. A
        # function word is never the name, and a price or a percentage is
        # never the version.
        if index + 1 < len(tokens) and has_alpha and not has_digit:
            following_match = matches[index + 1]
            following = _PRODUCT_ID_STRIP.sub("", tokens[index + 1])
            candidate = normalized + following
            if (
                token not in HISTORICAL_STOPWORDS
                and any(ch.isdigit() for ch in following)
                and not _is_quantity(text, *following_match.span())
                and len(candidate) >= 4
                and not _is_year_number(candidate)
            ):
                joined.setdefault(candidate, index)
                joined_names.setdefault(index, normalized)
    return _TitleAnchors(tokens=tokens, direct=direct, joined=joined, joined_names=joined_names)


#: How far past the first product name the title is still naming the same
#: thing. "Claude Fable 5.1 and Claude Mythos 5.1" is one announcement of two
#: models and spans four tokens; "GPT-6 Astra … matching Anthropic's pricing
#: for Claude Fable 5.1" names a second product twenty tokens later and is a
#: comparison, not a joint launch. Cheaper and steadier than enumerating every
#: verb an outlet can compare with.
_SUBJECT_SPAN = 4
#: Where a title stops being about its own subject: a comparison, or the
#: semicolon Techmeme uses to staple a second story onto the first.
_COMPARISON_BOUNDARY_RE = re.compile(
    r"(?:[;；]|>>"
    r"|\b(?:vs|versus|than|compared|beats|outperforms)\b"
    r"|对比|相比|超过|超越|击败|力压|优于|不敌"
    # A bare 超 is comparative only in front of a name; inside a word it is
    # 超大规模 or 超算, and cutting there would drop the title's own subject.
    r"|超(?=\s*[A-Za-z0-9]))",
    re.IGNORECASE,
)


def _comparison_boundary(text: str) -> int:
    match = _COMPARISON_BOUNDARY_RE.search(text)
    return match.start() if match else len(text)


def _is_direct_identifier(normalized: str) -> bool:
    """Whether a punctuation-stripped token is a product name on its own."""

    return (
        any(ch.isalpha() for ch in normalized)
        and any(ch.isdigit() for ch in normalized)
        and len(normalized) >= 4
        and not _is_year_number(normalized)
    )


def _is_year_number(identifier: str) -> bool:
    """True when the identifier's digits are just a calendar year.

    "WRC 2026" or "CES 2027" name an event, not a product: two stories
    sharing them are two announcements from the same venue, and merging on
    them chains a whole conference into one event.
    """

    digits = "".join(ch for ch in identifier if ch.isdigit())
    return bool(re.fullmatch(r"20[2-3]\d", digits))


#: How many independent publishers have to write a word beside a version
#: before it counts as a product name. One is an incidental proper noun - a
#: customer, a programme, a place that happened to sit next to a number.
_LEXICON_MIN_PUBLISHERS = 2


def product_lexicon(items: list[RawItem]) -> frozenset[str]:
    """Bare-word product names, learned from what first-party sources call them.

    Merge anchors needed a digit, which is how the 2026-09-04 launch was lost:
    "OpenAI launches Astra, its powerful (and controversial) new model" and
    "OpenAI's Astra crosses Critical cybersecurity threshold" contain no digit
    at all, so TechCrunch and TestingCatalog could never join the story they
    were reporting. Model names are increasingly bare words - Astra, Sol,
    Mythos, Fable - and no digit is coming.

    A word earns its place by belonging to a versioned identifier: standing
    next to one ("GPT-6 Astra", "Astra (GPT-6)") or opening one that the version
    is spaced away from ("Claude Fable 5.1", "Grok 4"). That is what a product
    name looks like when anyone writes the full name out, and it is narrow
    where the obvious signals are not. Capitalisation says nothing - AWS,
    NVIDIA and Google headline in title case, so "Intelligence", "Cloud" and
    "Series" are capitalised too, and a lexicon built that way merged 98
    unrelated items into one event on the replay of 2026-09-04.

    Two further guards:

    * **Two publishers.** Registrable domains, so one outlet's habit is not a
      vocabulary; "overview" and "Legora" sat beside GPT-6 once each and stay
      out. It is deliberately not restricted to first-party posts: at 04:20
      that day the only first-party mentions were "Path to Astra" and a tweet
      that promised the model without naming a version, so a first-party rule
      would have learned the name hours after the story needed it.
    * **Not a vendor or a generic.** The day OpenAI ships two unrelated things,
      "OpenAI" must not merge them. That exclusion is the source display names
      plus AI_TOKENS, both already maintained, so no new roster appears here.

    Freshness deliberately does not apply: an older post naming the product
    still teaches the name today's coverage is reported under.
    """

    excluded: set[str] = set()
    attestations: dict[str, set[str]] = defaultdict(set)
    for item in items:
        excluded.update(token.lower() for token in LATIN_TOKEN_RE.findall(item.source_label))
        try:
            publisher = registrable_domain(str(item.url))
        except ValueError:
            continue
        anchors = _title_anchors(item.title)
        versioned = set(anchors.direct.values())
        candidates = dict(anchors.joined_names)
        for index in versioned:
            for neighbour in (index - 1, index + 1):
                if 0 <= neighbour < len(anchors.tokens) and neighbour not in versioned:
                    candidates.setdefault(neighbour, anchors.tokens[neighbour])
        for token in candidates.values():
            if len(token) >= 4 and token.isalpha():
                attestations[token].add(publisher)
    excluded |= AI_TOKENS | HISTORICAL_STOPWORDS | STOPWORDS
    return frozenset(
        token
        for token, publishers in attestations.items()
        if len(publishers) >= _LEXICON_MIN_PUBLISHERS and token not in excluded
    )


def _same_story_by_product(
    left_title: str, right_title: str, lexicon: frozenset[str] = frozenset()
) -> bool:
    """Whether two titles report the same story, anchored on a product name.

    A shared versioned product name is necessary but not sufficient: on launch
    day the same model appears in a release story, a pricing story and a
    partner-platform story, and those must stay distinct events. Two cases:

    * **Different scripts** (one title Chinese, one Latin): word overlap is
      structurally impossible, so the shared product name is the whole
      signal. This is the case the plain similarity clause could never merge.
    * **Same script**: demand the titles also agree beyond the bare version —
      either one title is essentially just the product name (a Hacker News
      style bare link), or the titles share at least one digit-free word
      ("granite", "transcribe", "千问"). Two same-language stories whose only
      common ground is a version number ("GPT-5.6 API 上线" vs "GPT-5.6
      更新数据政策") are different stories about the same model.

    The shared anchors themselves are excluded from that overlap. A lexicon
    anchor is a digit-free word, so counting it would let the second clause
    answer itself and every story about a product would merge — and when the
    anchor carries no version at all, one word of agreement is not enough.
    """

    shared = title_product_identifiers(left_title, lexicon) & title_product_identifiers(
        right_title, lexicon
    )
    if not shared:
        return False
    left_cjk = bool(CHINESE_RE.search(left_title))
    right_cjk = bool(CHINESE_RE.search(right_title))
    if left_cjk != right_cjk:
        return True
    left_tokens = title_tokens(left_title)
    right_tokens = title_tokens(right_title)
    if min(len(left_tokens), len(right_tokens)) <= 2:
        return True
    digit_free_overlap = {
        token
        for token in (left_tokens & right_tokens) - shared
        if not any(ch.isdigit() for ch in token)
    }
    # A bare name is weaker evidence than a version, so it has to be paid for
    # twice. "RT shirish: …Meta's new model is already ranked above Fable 5"
    # shares only the word Fable with Anthropic's launch, and on one word it
    # welded the Fable and Muse Spark stories into a single 25-item cluster.
    if any(any(ch.isdigit() for ch in name) for name in shared):
        return bool(digit_free_overlap)
    return len(digit_free_overlap) >= 2


def _similar(left: RawItem, right: RawItem, window: timedelta, lexicon: frozenset[str]) -> bool:
    left_time = left.published_at or left.discovered_at
    right_time = right.published_at or right.discovered_at
    if abs(left_time - right_time) > window:
        return False
    identifiers = story_identifiers(left) & story_identifiers(right)
    if identifiers:
        return True
    if _same_story_by_product(left.title, right.title, lexicon):
        return True
    if left.source == right.source and left.source_channel == SourceChannel.RELEASE:
        return False
    left_tokens = title_tokens(f"{left.title} {left.summary[:300]}")
    right_tokens = title_tokens(f"{right.title} {right.summary[:300]}")
    if not left_tokens or not right_tokens:
        return False
    intersection = len(left_tokens & right_tokens)
    jaccard = intersection / len(left_tokens | right_tokens)
    containment = intersection / min(len(left_tokens), len(right_tokens))
    return jaccard >= 0.38 and containment >= 0.55


def registrable_domain(url: str) -> str:
    """The eTLD+1 of ``url``: the unit of source independence.

    ``www.example.com`` and ``news.example.com`` are the same publisher, so
    they collapse to ``example.com``. Multi-label public suffixes such as
    ``com.cn`` or ``co.uk`` keep one more label so that ``example.com.cn`` does
    not collapse to the suffix itself.
    """

    host = (urlsplit(canonicalize_url(url)).hostname or "").strip(".")
    if not host:
        raise ValueError(f"cannot determine a registrable domain for url={url}")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in MULTI_LABEL_PUBLIC_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def cluster_items(
    items: list[RawItem],
    window_hours: int = 48,
    lexicon: frozenset[str] = frozenset(),
) -> list[Event]:
    by_url: dict[str, list[RawItem]] = defaultdict(list)
    for item in items:
        by_url[canonicalize_url(str(item.url))].append(item)

    groups = _connected_groups(list(by_url.values()), timedelta(hours=window_hours), lexicon)

    events = []
    for group in groups:
        primary = min(
            group,
            key=lambda item: (
                # A retweet is the weakest possible representative of a cluster:
                # its title and summary are a stranger's words, and the event
                # takes both. 2026-09-04's launch cluster held 36 items across
                # 10 domains and led with "RT Matthew Berman: Astra (GPT-6) is
                # here!!!", so the write-up quoted an early tester's enthusiasm
                # while the pricing and benchmark reports sat unread beneath it.
                #
                # This outranks tier and channel rather than breaking ties
                # inside them, for the same reason lead_is_corroborated ignores
                # amplification: a retweet is Tier A official only because of
                # whose account it sits on. Ranking it that way put "RT Hebbia:
                # Astra set a new high in our evals" ahead of TechCrunch's
                # launch report in the same cluster.
                is_amplified(item),
                item.source_tier.value,
                CHANNEL_PRIORITY[item.source_channel],
                -len(item.summary),
            ),
        )
        ordered = [primary, *(item for item in group if item is not primary)]
        canonical = canonicalize_url(str(primary.url))
        event_id = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        latest_dated_item = max(
            (item for item in group if item.published_at),
            key=lambda item: cast(datetime, item.published_at),
            default=None,
        )
        events.append(
            Event(
                event_id=event_id,
                canonical_url=HttpUrl(canonical),
                title=primary.title,
                summary=primary.summary,
                published_at=(latest_dated_item.published_at if latest_dated_item else None),
                source_time_kind=(
                    latest_dated_item.source_time_kind
                    if latest_dated_item
                    else primary.source_time_kind
                ),
                items=ordered,
            )
        )
    return events


def _connected_groups(
    groups: list[list[RawItem]], window: timedelta, lexicon: frozenset[str]
) -> list[list[RawItem]]:
    parents = list(range(len(groups)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for right in range(len(groups)):
        for left in range(right):
            if any(_similar(a, b, window, lexicon) for a in groups[left] for b in groups[right]):
                parents[root(right)] = root(left)
    components: dict[int, list[RawItem]] = defaultdict(list)
    for index, group in enumerate(groups):
        components[root(index)].extend(group)
    return list(components.values())


#: Terms that mark an event as something a reader can act on today.
#:
#: The original list only recognised launches and price cuts, which scored a
#: model release highly but left the changes readers of an AI daily actually
#: react to — a quota reset, a free window opening, a feature flag flipping —
#: on the same footing as a conference recap. The 2026-08-28 coverage audit
#: against the benchmark digest turned on exactly this vocabulary: quota
#: resets, reserve allowances, limited-time free access and gray releases were
#: its most frequent story shape and our lowest-scoring one.
ACTION_TERMS = (
    # shipping and availability
    "api",
    "available",
    "general availability",
    "launch",
    "model",
    "open source",
    "release",
    "rollout",
    "发布",
    "上线",
    "开源",
    "模型",
    "推出",
    # quota, rate limits and usage
    "credits",
    "quota",
    "rate limit",
    "reset",
    "usage limit",
    "额度",
    "用量",
    "限额",
    "限流",
    "重置",
    "配额",
    # price and access changes
    "discount",
    "free",
    "pricing",
    "price",
    "涨价",
    "降价",
    "调价",
    "限免",
    "免费",
    "折扣",
    "优惠",
    # gradual and ending availability
    "beta",
    "deprecat",
    "preview",
    "sunset",
    "waitlist",
    "内测",
    "公测",
    "灰度",
    "下线",
    "停用",
)
#: ``api`` used to fire inside capital, rapid and therapies, so four of every
#: seven English matches were false and the term stopped separating anything.
#: Latin terms now need word boundaries, with the inflections an English
#: headline actually uses - which is also what lets "deprecat" reach
#: deprecated and deprecation. CJK has no such boundary, so it stays a
#: substring test; it never had the collision problem.
_ACTION_LATIN_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(term) for term in ACTION_TERMS if term.isascii())
    + r")(?:s|es|ed|ing|d|ion)?\b"
)
_ACTION_CJK_TERMS = tuple(term for term in ACTION_TERMS if not term.isascii())


def _is_actionable(text: str) -> bool:
    return bool(_ACTION_LATIN_RE.search(text)) or any(term in text for term in _ACTION_CJK_TERMS)


def _corroboration(event: Event) -> int:
    """How many independent publishers, other than the newsmaker, carried this.

    Counting configured sources measured how many feeds this project happens to
    point at one company: an OpenAI launch reaches seven of them and scored the
    full bonus for one party talking about itself, while a smaller lab with a
    single feed scored nothing for the same news. Publishers are registrable
    domains - the definition lead_is_corroborated already uses - and first-party
    items are the claim rather than evidence for it, so they do not count here.
    Being the newsmaker is already paid for in the channel score.
    """

    publishers = {
        registrable_domain(str(item.url))
        for item in event.items
        if item.source_channel not in FIRST_PARTY_CHANNELS
    }
    return min(18, max(0, (len(publishers) - 1) * 7))


def score_events(events: list[Event], now: datetime) -> list[Event]:
    scored = []
    for event in events:
        tiers = {item.source_tier.value for item in event.items}
        channels = {item.source_channel for item in event.items}
        tier_score = 12 if "A" in tiers else 8 if "B" in tiers else 4
        channel_score = max(_channel_score(channel) for channel in channels)
        age = now - (event.published_at or event.primary_item.discovered_at)
        recency = max(0, 20 - age.total_seconds() / 3600 / 2)
        corroboration = _corroboration(event)
        text = f"{event.title} {event.summary}".lower()
        actionability = 12 if _is_actionable(text) else 4
        popularity = min(6, math.log1p(_numeric_metrics(event)) / 1.2)
        scored.append(
            event.model_copy(
                update={
                    "score": min(
                        100,
                        max(
                            0,
                            tier_score
                            + channel_score
                            + recency
                            + corroboration
                            + actionability
                            + popularity,
                        ),
                    )
                }
            )
        )
    return sorted(scored, key=lambda event: event.score, reverse=True)


def select_candidate_pool(
    events: list[Event],
    limit: int,
    research_limit: int,
    release_limit: int,
) -> list[Event]:
    """Keep high-value general news visible during paper or release surges."""
    channel_limits = {
        SourceChannel.RESEARCH: research_limit,
        SourceChannel.RELEASE: release_limit,
    }
    channel_counts: dict[SourceChannel, int] = defaultdict(int)
    selected: list[Event] = []
    deferred: list[Event] = []
    for event in events:
        channel = _event_channel(event)
        channel_limit = channel_limits.get(channel)
        if channel_limit is not None and channel_counts[channel] >= channel_limit:
            deferred.append(event)
            continue
        selected.append(event)
        channel_counts[channel] += 1
        if len(selected) == limit:
            return selected
    selected.extend(deferred[: limit - len(selected)])
    return sorted(selected, key=lambda event: event.score, reverse=True)


def _event_channel(event: Event) -> SourceChannel:
    return min(
        (item.source_channel for item in event.items),
        key=lambda channel: CHANNEL_PRIORITY[channel],
    )


def _channel_score(channel: SourceChannel) -> int:
    """How much this channel alone says a story is worth carrying.

    Research and release sit low because this is a news digest: a paper or a
    version bump is not the day's story unless someone else made it one, and a
    cluster that also reaches news or official picks that channel up instead.

    Both used to carry a second penalty subtracted in score_events. It fired on
    exactly the condition that produced their number here - ``max()`` returns
    research's score only when research is the only channel - so it was one
    judgement written twice. Folded in at the magnitude it already had.
    """

    return {
        SourceChannel.OFFICIAL: 30,
        SourceChannel.NEWS: 24,
        SourceChannel.COMMUNITY: 16,
        SourceChannel.RELEASE: 14,
        SourceChannel.RESEARCH: 2,
    }[channel]


def _numeric_metrics(event: Event) -> float:
    return sum(
        float(value)
        for item in event.items
        for value in item.metrics.values()
        if isinstance(value, (int, float))
    )


def remove_historical(events: list[Event], history: HistoricalIndex) -> list[Event]:
    canonical_history = {canonicalize_url(url) for url in history.urls}
    return [
        event
        for event in events
        if canonicalize_url(str(event.canonical_url)) not in canonical_history
        and not any(_title_match(event.title, title) for title in history.titles)
        and not any(_historical_story_match(event, story) for story in history.stories)
    ]


def _historical_story_match(event: Event, story: HistoricalStory) -> bool:
    return any(
        _historical_text_match(current, previous)
        for current in _event_history_texts(event)
        for previous in story.texts
    )


def _event_history_texts(event: Event) -> tuple[str, ...]:
    """What the event is, for history to be matched against.

    Not every text every item carries. One stray sentence anywhere in a
    cluster deletes the whole event, and cross-outlet merging made that
    certain: the 2026-09-04 GPT-6 cluster offered 67 texts against 178
    remembered stories, and "OpenAI has just introduced GPT-6 Astra…" was
    struck down by "Amazon Bedrock processes inference requests and data
    within India" from 08-28 — four shared tokens over a six-token sentence
    clears containment against the shorter text. The bigger and better sourced
    the story, the likelier it was to be deleted.

    An event's identity is its own title and summary, which come from the
    representative item. The other items are corroboration: that a cluster
    contains one follow-up to yesterday's story does not make it yesterday's
    story.
    """

    values = (event.title, event.summary[:1600])
    return tuple(value for value in dict.fromkeys(values) if value.strip())


def _historical_text_match(left: str, right: str) -> bool:
    left_tokens = _historical_tokens(left)
    right_tokens = _historical_tokens(right)
    shared = left_tokens & right_tokens
    if len(shared) < 4 or not (_historical_anchors(left) & _historical_anchors(right)):
        return False
    left_products = title_product_identifiers(left)
    right_products = title_product_identifiers(right)
    shared_products = left_products & right_products
    containment = len(shared) / min(len(left_tokens), len(right_tokens))
    if containment >= 0.6:
        if bool(left_products) == bool(right_products) or shared_products:
            return True
        # Exactly one side names a versioned product. Containment against the
        # shorter text is meant to catch a headline restated inside a longer
        # write-up, but a short old headline made of brand words is contained
        # in almost any launch from the same company: on 2026-09-02 "Claude in
        # Chrome is generally available | Claude by Anthropic" (five tokens)
        # matched "Anthropic unveils Claude Fable 5.1, which is generally
        # available…" on claude/anthropic/generally/available and deleted the
        # day's top story. A version number the other text never mentions is
        # evidence of a different story, so demand the overlap hold both ways.
        return len(shared) / max(len(left_tokens), len(right_tokens)) >= 0.6
    return bool(shared_products and len(shared) >= 5 and containment >= 0.45)


def _historical_tokens(value: str) -> set[str]:
    tokens = {_stem_history_token(token) for token in title_tokens(value)}
    return {token for token in tokens if len(token) >= 2 and token not in HISTORICAL_STOPWORDS}


def _historical_anchors(value: str) -> set[str]:
    anchors = set(title_product_identifiers(value))
    for raw in LATIN_TOKEN_RE.findall(value):
        token = _stem_history_token(raw)
        if token in HISTORICAL_STOPWORDS:
            continue
        looks_named = len(raw) >= 3 and (
            raw[0].isupper() or any(char.isupper() for char in raw[1:])
        )
        if raw.lower() in AI_TOKENS or looks_named:
            anchors.add(token)
    return anchors


def _stem_history_token(token: str) -> str:
    if not token.isascii():
        return token
    value = token.lower().strip(".+-")
    for suffix, replacement, minimum in (
        ("ies", "y", 6),
        ("ically", "ic", 8),
        ("edly", "", 7),
        ("ing", "", 7),
        ("ed", "", 6),
        ("ly", "", 6),
        ("es", "", 6),
        ("s", "", 5),
    ):
        if len(value) >= minimum and value.endswith(suffix):
            return value[: -len(suffix)] + replacement
    return value


def _title_match(left: str, right: str) -> bool:
    """Does ``left`` (a candidate's raw title) repeat ``right`` (a published headline)?

    The two sides are different kinds of string. ``right`` is our own headline -
    long, Chinese, and carrying the specs we put in it. ``left`` is whatever the
    outlet called it, often short and in English. Symmetric overlap is calibrated
    for comparing like with like and fails here: "Introducing Hy4 Preview" scored
    jaccard 0.12 against "腾讯发布开源旗舰 Hy4 preview：总参数 770B、激活 49B、
    上下文 1M" and ran again on 2026-08-31 as that day's lead detail, a day after
    the same release led 2026-08-30.

    So there is a second path for the case that actually matters: the candidate
    names the same product and says nothing the published headline did not
    already say. That is measured in one direction - how much of ``left`` the
    headline already covers - because ``min()`` measures the shorter title, and
    a follow-up ("OpenAI 调整 GPT-5 API 定价" against "OpenAI 发布 GPT-5") scores
    a perfect symmetric containment while plainly being new. Directed, the
    duplicate scores 1.00 and that follow-up 0.40.
    """

    left_product_ids = _product_identifiers(left)
    right_product_ids = _product_identifiers(right)
    if (left_product_ids or right_product_ids) and left_product_ids != right_product_ids:
        return False
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    intersection = len(left_tokens & right_tokens)
    containment = intersection / min(len(left_tokens), len(right_tokens))
    jaccard = intersection / len(left_tokens | right_tokens)
    if containment >= 0.65 and jaccard >= 0.5:
        return True
    same_product = bool(left_product_ids) and left_product_ids == right_product_ids
    return same_product and intersection / len(left_tokens) >= 0.65


def _product_identifiers(value: str) -> set[str]:
    """Versioned product names in a title: "Hy4", "GLM-5.3", "K3".

    A magnitude is not a product. "770B", "49B", "1M" and "4K" all mix letters
    and digits, so counting them here made a headline that quotes specs look
    like a different product from the same announcement without them - which is
    why {hy4} never equalled {hy4, 770b, 49b, 1m} and the release published
    twice. A product name leads with its letters; a magnitude leads with its
    number.
    """

    return {
        token.lower()
        for token in LATIN_TOKEN_RE.findall(value)
        if token[:1].isalpha() and any(character.isdigit() for character in token)
    }
