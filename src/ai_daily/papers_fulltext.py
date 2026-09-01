"""arXiv full-text retrieval and deterministic HTML cleaning."""

from __future__ import annotations

import re
from dataclasses import dataclass

import feedparser
from lxml import etree, html  # type: ignore[import-untyped]

from .sources import Collector, SourceCollectionError

FULL_TEXT_LIMIT = 60_000
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_HTML = "https://arxiv.org/html"


@dataclass(frozen=True)
class FullPaper:
    arxiv_id: str
    version: int
    authors: str
    text: str | None
    failure: str | None = None


def _clean(value: str) -> str:
    return " ".join(value.split())


def _section_kind(heading: str) -> str:
    lowered = heading.lower()
    if any(word in lowered for word in ("experiment", "evaluation", "result", "analysis")):
        return "experiments"
    if any(word in lowered for word in ("method", "approach", "algorithm", "framework")):
        return "method"
    if any(word in lowered for word in ("limitation", "discussion", "conclusion")):
        return "limitation"
    if any(word in lowered for word in ("appendix", "supplement")):
        return "appendix"
    if any(word in lowered for word in ("related", "preliminar", "background")):
        return "related"
    return "intro"


def _table_text(table: html.HtmlElement) -> list[str]:
    rows: list[str] = []
    for row in table.xpath(".//tr"):
        cells = [_clean(" ".join(cell.itertext())) for cell in row.xpath("./th | ./td")]
        if any(cells):
            rows.append(" | ".join(cells))
    return rows


def _blocks(document: html.HtmlElement) -> list[tuple[str, str]]:
    body = document.xpath("//article | //main | //body")
    root = body[0] if body else document
    blocks: list[tuple[str, str]] = []
    kind = "intro"
    for element in root.xpath(".//h1 | .//h2 | .//h3 | .//h4 | .//p | .//table"):
        tag = str(element.tag).lower()
        if tag.startswith("h"):
            heading = _clean(" ".join(element.itertext()))
            if heading:
                kind = _section_kind(heading)
                blocks.append((kind, heading))
            continue
        if tag == "table":
            blocks.extend((kind, row) for row in _table_text(element))
            continue
        text = _clean(" ".join(element.itertext()))
        if text:
            blocks.append((kind, text))
    return blocks


def _fallback_truncate(text: str, limit: int) -> str:
    head = text[:50_000]
    remaining = text[50_000:]
    windows = [remaining[start : start + 10_000] for start in range(0, len(remaining), 10_000)]
    numeric = max(
        windows,
        key=lambda value: sum(character.isdigit() for character in value),
        default="",
    )
    return (head + "\n" + numeric)[:limit]


def _truncate_blocks(blocks: list[tuple[str, str]], limit: int) -> str:
    if not blocks:
        return ""
    joined = "\n".join(text for _, text in blocks)
    if len(joined) <= limit:
        return joined
    if not any(kind in {"experiments", "method"} for kind, _ in blocks):
        return _fallback_truncate(joined, limit)

    kept = list(blocks)
    for kind in ("related", "appendix", "intro", "limitation"):
        while len("\n".join(text for _, text in kept)) > limit:
            index = next((i for i in range(len(kept) - 1, -1, -1) if kept[i][0] == kind), None)
            if index is None:
                break
            kept.pop(index)
    while len("\n".join(text for _, text in kept)) > limit:
        index = next((i for i in range(len(kept) - 1, -1, -1) if kept[i][0] == "method"), None)
        if index is None:
            break
        kept.pop(index)
    result = "\n".join(text for _, text in kept)
    if len(result) > limit:
        # Experiments are never selectively discarded; only the final character
        # ceiling applies if experiments alone exceed the model's safe context.
        return result[:limit]
    return result


def clean_arxiv_html(content: bytes, limit: int = FULL_TEXT_LIMIT) -> str:
    try:
        document = html.fromstring(content)
    except (etree.ParserError, ValueError, TypeError) as error:
        raise SourceCollectionError("invalid arXiv HTML") from error
    for element in document.xpath(
        "//script | //style | //nav | //header | //footer | //aside | //form | "
        "//*[contains(@class, 'ltx_bibliography')] | //*[@id='references']"
    ):
        element.drop_tree()
    return _truncate_blocks(_blocks(document), limit)


async def fetch_full_papers(collector: Collector, arxiv_ids: list[str]) -> dict[str, FullPaper]:
    if not arxiv_ids:
        return {}
    response = await collector._request(
        ARXIV_API,
        params={"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)},
        timeout=30,
    )
    response.raise_for_status()
    feed = feedparser.parse(response.content)
    versions: dict[str, tuple[int, str]] = {}
    for entry in feed.entries:
        match = re.search(r"(\d{4}\.\d{4,5})v(\d+)$", str(entry.id))
        if not match:
            continue
        authors = ", ".join(author.name for author in entry.get("authors", []))
        versions[match.group(1)] = (int(match.group(2)), authors)

    results: dict[str, FullPaper] = {}
    for arxiv_id in arxiv_ids:
        metadata = versions.get(arxiv_id)
        if metadata is None:
            results[arxiv_id] = FullPaper(
                arxiv_id, 0, "", None, "arXiv version metadata unavailable"
            )
            continue
        version, authors = metadata
        try:
            html_response = await collector._request(
                f"{ARXIV_HTML}/{arxiv_id}v{version}", timeout=45
            )
            if html_response.status_code == 404:
                results[arxiv_id] = FullPaper(
                    arxiv_id, version, authors, None, "arXiv HTML unavailable"
                )
                continue
            html_response.raise_for_status()
            text = clean_arxiv_html(html_response.content)
            if len(text) < 1000:
                raise SourceCollectionError("arXiv HTML yielded insufficient text")
            results[arxiv_id] = FullPaper(arxiv_id, version, authors, text)
        except Exception as error:
            results[arxiv_id] = FullPaper(
                arxiv_id, version, authors, None, f"{type(error).__name__}: {error}"
            )
    return results
