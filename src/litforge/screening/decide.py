"""Deterministic screening decision from the model's structured answer.

- exclude   if an exclusion criterion is `violated` with a verified quote and an abstract;
- uncertain if exclusions are violated without a verified quote or without an abstract;
- include   if no inclusion criterion is `not_met` and relevance >= 3;
- uncertain otherwise.
score = 0.5 * relevance/5 + 0.5 * inclusion fraction (met 1, unclear 0.5, not_met 0); relevance/5 alone
when there are no inclusion criteria. Evidence quotes are checked against title + abstract.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from litforge.screening.criteria import Criteria

INCLUSION = ("met", "not_met", "unclear")
EXCLUSION = ("violated", "not_violated", "unclear")
INCLUDE_MIN_RELEVANCE = 3
_MET_VALUE = {"met": 1.0, "unclear": 0.5, "not_met": 0.0}


class CriterionResult(BaseModel):
    criterion: str
    status: str
    evidence: str = ""
    verified: bool | None = None  # None: no evidence given


class Decision(BaseModel):
    decision: str  # include | uncertain | exclude
    score: float
    relevance: int
    inclusion: list[CriterionResult]
    exclusion: list[CriterionResult]
    reason: str
    error: str | None = None


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _items(raw: Any, criteria: list[str], allowed: tuple[str, ...], source: str) -> list[CriterionResult]:
    """One result per criterion, in order: missing or malformed entries become `unclear`."""
    entries = raw if isinstance(raw, list) else []
    out = []
    for i, criterion in enumerate(criteria):
        entry = entries[i] if i < len(entries) and isinstance(entries[i], dict) else {}
        status = entry.get("status")
        evidence = str(entry.get("evidence") or "").strip()
        out.append(CriterionResult(
            criterion=criterion, status=status if status in allowed else "unclear", evidence=evidence,
            verified=(_norm(evidence) in source) if _norm(evidence) else None))
    return out


def decide(raw: dict[str, Any], c: Criteria, title: str | None, abstract: str | None) -> Decision:
    source = _norm(f"{title or ''} {abstract or ''}")
    try:
        relevance = max(0, min(5, int(raw.get("relevance", 0))))
    except (TypeError, ValueError, OverflowError):  # also NaN / Infinity, which json.loads accepts
        relevance = 0
    inclusion = _items(raw.get("inclusion"), c.include, INCLUSION, source)
    exclusion = _items(raw.get("exclusion"), c.exclude, EXCLUSION, source)
    reason = str(raw.get("reason") or "").strip()
    if any(r.status == "violated" for r in exclusion):
        verified = any(r.status == "violated" and r.verified is True for r in exclusion)
        decision = "exclude" if verified and (abstract or "").strip() else "uncertain"
        if not verified:
            reason = (reason + " Exclusion quote not verified.").strip()
    elif all(r.status != "not_met" for r in inclusion) and relevance >= INCLUDE_MIN_RELEVANCE:
        decision = "include"
    else:
        decision = "uncertain"
    score = relevance / 5
    if inclusion:
        score = 0.5 * score + 0.5 * sum(_MET_VALUE[r.status] for r in inclusion) / len(inclusion)
    return Decision(decision=decision, score=round(score, 4), relevance=relevance, inclusion=inclusion,
                    exclusion=exclusion, reason=reason)


def failed_decision(c: Criteria, error: str) -> Decision:
    """Recorded when the model gave no usable answer: uncertain, so a person looks at it."""
    return Decision(decision="uncertain", score=0.0, relevance=0,
                    inclusion=[CriterionResult(criterion=x, status="unclear") for x in c.include],
                    exclusion=[CriterionResult(criterion=x, status="unclear") for x in c.exclude],
                    reason="", error=error)
