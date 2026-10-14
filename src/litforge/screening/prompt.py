"""Prompt and response schema for title/abstract screening. Bump PROMPT_VERSION on any wording change:
it is part of every result's fingerprint, so changed prompts re-screen papers."""

from __future__ import annotations

import hashlib
from typing import Any

from litforge.screening.criteria import Criteria
from litforge.screening.decide import EXCLUSION, INCLUSION

PROMPT_VERSION = "screen-v1"

SYSTEM = (
    "You screen scientific papers for a literature review using only the title and abstract given. "
    "For each inclusion criterion answer met, not_met or unclear; for each exclusion criterion answer "
    "violated, not_violated or unclear. Answer not_met or violated only when the text states it; when the "
    "text does not say, answer unclear. Evidence must be a short exact quote from the title or abstract, "
    "or empty. relevance is 0 (unrelated) to 5 (directly answers the research question). "
    "reason is one or two sentences. Reply with JSON only."
)


def response_schema(c: Criteria) -> dict[str, Any]:
    def items(statuses: tuple[str, ...], n: int) -> dict[str, Any]:
        return {"type": "array", "minItems": n, "maxItems": n, "items": {
            "type": "object", "required": ["status", "evidence"],
            "properties": {"status": {"type": "string", "enum": list(statuses)}, "evidence": {"type": "string"}}}}

    return {"type": "object", "required": ["relevance", "inclusion", "exclusion", "reason"], "properties": {
        "relevance": {"type": "integer", "minimum": 0, "maximum": 5},
        "inclusion": items(INCLUSION, len(c.include)),
        "exclusion": items(EXCLUSION, len(c.exclude)),
        "reason": {"type": "string"}}}


def build_messages(c: Criteria, title: str | None, abstract: str | None, year: int | None,
                   venue: str | None) -> list[dict[str, str]]:
    inc = "\n".join(f"I{i}. {x}" for i, x in enumerate(c.include, 1)) or "(none)"
    exc = "\n".join(f"E{i}. {x}" for i, x in enumerate(c.exclude, 1)) or "(none)"
    meta = " · ".join(str(x) for x in (year, venue) if x)
    user = (f"Research question: {c.question}\n\nInclusion criteria:\n{inc}\n\nExclusion criteria:\n{exc}\n\n"
            f"Paper title: {title or '(no title)'}\n" + (f"Published: {meta}\n" if meta else "") +
            f"Abstract: {(abstract or '').strip() or '(no abstract available)'}")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def fingerprint(c: Criteria, model: str, title: str | None, abstract: str | None) -> str:
    parts = [PROMPT_VERSION, model, c.model_dump_json(), title or "", abstract or ""]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
