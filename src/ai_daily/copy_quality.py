"""Conservative copy checks, not a semantic completeness classifier."""

import re

from .publication import DailyPublication


def copy_problem(value: str, limit: int, original: str | None = None) -> str | None:
    text = value.strip()
    if not text:
        return "empty"
    if len(value) > limit:
        return "too_long"
    if text.endswith((",", "，", ":", "：", ";", "；", "、")):
        return "unfinished_ending"
    # Strip complete URLs before punctuation checks: paths may contain brackets.
    # Remove a full Markdown link as a unit, retaining unmatched syntax for checks.
    prose = re.sub(r"\[([^\]]+)\]\(https?://[^\s)]+\)", r"\1", text)
    prose = re.sub(
        r"https?://[^\s]+",
        lambda match: (
            "URL" + "".join(c for c in match[0] if c in '()[]“”「」『』《》"\uff08\uff09')
        ),
        prose,
    )
    if re.search(r"\[[^\]]*$|\[[^\]]*\]\([^)]*$", text):
        return "unfinished_markdown_link"
    for opening, closing in (
        ("(", ")"),
        ("\uff08", "\uff09"),
        ("[", "]"),
        ("【", "】"),
        ("“", "”"),
        ("「", "」"),
        ("『", "』"),
        ("《", "》"),
    ):
        if prose.count(opening) != prose.count(closing):
            return "unbalanced_delimiter"
    # Balanced quotes around numeric model names are valid. An odd quote after
    # a digit may be an inch mark; do not turn this ambiguity into a blocker.
    if prose.count('"') % 2 and not re.search(r'\d"', prose):
        return "unbalanced_quote"
    if original and len(text) < len(original) and original.startswith(text):
        if not re.search(r'[。\uff01\uff1f!?][”」』"]?$|\.(?:[”"])?$', text):
            return "cut_source_prefix"
    return None


def source_copy_ready(title: str, summary: str) -> bool:
    return (
        copy_problem(title, 100) is None
        and copy_problem(summary, 320) is None
        and re.search(r'[。\uff01\uff1f!?][”」』"]?$|\.(?:[”"])?$', summary.strip()) is not None
        and not summary.rstrip().endswith(("...", "…"))
    )


def validate_publication_copy(publication: DailyPublication) -> None:
    fields: list[tuple[str, str, str, int]] = []
    for card in publication.briefs:
        fields.extend(
            (
                (card.event_id, "headline", card.headline, 100),
                (card.event_id, "brief", card.brief, 320),
            )
        )
    for story in publication.details:
        fields.extend(
            (
                (story.event_id, "headline", story.headline, 100),
                (story.event_id, "tldr", story.tldr, 300),
                (story.event_id, "why_it_matters", story.why_it_matters, 500),
            )
        )
        fields.extend((story.event_id, "fact", fact.text, 500) for fact in story.facts)
        for name, value, limit in (("action", story.action, 400), ("caveat", story.caveat, 500)):
            if value is not None:
                fields.append((story.event_id, name, value, limit))
    if publication.highlight:
        fields.append(("issue", "highlight", publication.highlight, 300))
    fields.extend(("issue", "viewpoint", view.text, 300) for view in publication.viewpoints)
    for event_id, field, value, limit in fields:
        if problem := copy_problem(value, limit):
            raise ValueError(f"copy {event_id} {field}: {problem}")
