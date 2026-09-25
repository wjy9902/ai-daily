"""Short ids the planner can copy back without mangling.

Event ids are 16 hex characters of a URL hash. The planner reads about 200k
tokens of candidates and has to copy some thirty of those ids, plus their
evidence ids, back exactly - and on 2026-09-25 it could not: it returned
``fdaccec41435e8`` for ``fdaccec414cc35e8`` and ``04a9ab4c43d9fe8`` for
``04a9ab4c43d9fe8f``, both attempts failed validation, and the issue fell to
brief-only. Random hex has nothing for the model to hold on to; ``c37`` does.

The prompt carries only aliases, the output is translated back before any
validation, and validator messages are translated forward so a retry names
ids the model has actually seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ai_daily.models import Event

_EVIDENCE_ALIAS_RE = re.compile(r"(c\d+)(-\d+)")


@dataclass(frozen=True)
class PlanningIds:
    to_alias: dict[str, str]
    to_event: dict[str, str]

    @classmethod
    def for_events(cls, events: list[Event]) -> PlanningIds:
        to_alias = {event.event_id: f"c{index}" for index, event in enumerate(events, 1)}
        return cls(to_alias, {alias: event_id for event_id, alias in to_alias.items()})

    def alias_evidence(self, event_id: str, evidence_id: str) -> str:
        """Evidence ids are ``<event_id>-<n>``; the alias keeps the ``-<n>``."""

        return self.to_alias[event_id] + evidence_id.removeprefix(event_id)

    def restore(self, output: BaseModel) -> BaseModel:
        """Translate an aliased plan back to real ids.

        An alias that does not exist is left as written, so the plan validator
        rejects it exactly as it would reject any other unknown id.
        """

        value = output.model_dump()
        for field in ("lead", "follow", "brief"):
            for choice in value[field]:
                choice["event_id"] = self.to_event.get(choice["event_id"], choice["event_id"])
                choice["evidence_ids"] = self._restore_evidence(choice["evidence_ids"])
        for insight in value["editor_viewpoint"]:
            insight["evidence_ids"] = self._restore_evidence(insight["evidence_ids"])
        return type(output).model_validate(value)

    def aliased_message(self, message: str) -> str:
        """Rewrite real ids in a validator message into the aliases the model saw."""

        if not self.to_alias:
            return message
        # Longest first, so ``event-10`` is not read as ``event-1`` + ``0``.
        pattern = "|".join(
            re.escape(event_id) for event_id in sorted(self.to_alias, key=len, reverse=True)
        )
        return re.sub(pattern, lambda match: self.to_alias[match.group()], message)

    def _restore_evidence(self, evidence_ids: list[Any]) -> list[Any]:
        restored: list[Any] = []
        for evidence_id in evidence_ids:
            match = _EVIDENCE_ALIAS_RE.fullmatch(str(evidence_id))
            if match and match.group(1) in self.to_event:
                restored.append(self.to_event[match.group(1)] + match.group(2))
            else:
                restored.append(evidence_id)
        return restored
