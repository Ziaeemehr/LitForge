"""Screening decision rules (pure functions, no model)."""

from __future__ import annotations

import pytest

from litforge.screening.criteria import Criteria, criteria_hash
from litforge.screening.decide import decide, failed_decision
from litforge.screening.prompt import PROMPT_VERSION, build_messages, fingerprint, response_schema

ABSTRACT = "We fit a Jansen-Rit neural mass model to EEG recordings with simulation-based inference."
C = Criteria(question="Inference for neural mass models", include=["uses a neural mass model", "uses EEG or MEG"],
             exclude=["animal study only"])


def raw(relevance: int = 4, inc: tuple[str, ...] = ("met", "met"), exc: tuple[str, ...] = ("not_violated",),
        ev: str = "Jansen-Rit neural mass model") -> dict:
    return {"relevance": relevance,
            "inclusion": [{"status": s, "evidence": ev} for s in inc],
            "exclusion": [{"status": s, "evidence": ""} for s in exc],
            "reason": "fits"}


def test_include_when_all_met_and_relevant() -> None:
    d = decide(raw(), C, "Title", ABSTRACT)
    assert d.decision == "include"
    assert d.score == pytest.approx(0.9)


def test_exclude_when_any_exclusion_violated() -> None:
    assert decide(raw(exc=("violated",)), C, "Title", ABSTRACT).decision == "exclude"


def test_uncertain_when_inclusion_not_met_or_low_relevance() -> None:
    assert decide(raw(inc=("met", "not_met")), C, "T", ABSTRACT).decision == "uncertain"
    assert decide(raw(relevance=2), C, "T", ABSTRACT).decision == "uncertain"


def test_unclear_counts_half_in_score() -> None:
    d = decide(raw(relevance=5, inc=("met", "unclear")), C, "T", ABSTRACT)
    assert d.decision == "include" and d.score == pytest.approx(0.875)


def test_missing_abstract_is_never_excluded() -> None:
    assert decide(raw(exc=("violated",)), C, "Title", None).decision == "uncertain"
    assert decide(raw(exc=("violated",)), C, "Title", "  ").decision == "uncertain"


def test_wrong_counts_unknown_status_and_bad_relevance_are_normalized() -> None:
    d = decide({"relevance": 9, "inclusion": [{"status": "yes"}], "exclusion": [], "reason": 3}, C, "T", ABSTRACT)
    assert d.relevance == 5
    assert [r.status for r in d.inclusion] == ["unclear", "unclear"]
    assert [r.status for r in d.exclusion] == ["unclear"]
    assert [r.criterion for r in d.inclusion] == C.include
    assert d.reason == "3"
    d = decide({"relevance": "high"}, C, "T", ABSTRACT)
    assert d.relevance == 0 and d.decision == "uncertain"


def test_evidence_verified_against_title_and_abstract() -> None:
    d = decide(raw(ev="neural   mass MODEL to EEG"), C, "T", ABSTRACT)
    assert d.inclusion[0].verified is True
    d = decide(raw(ev="fMRI connectivity"), C, "T", ABSTRACT)
    assert d.inclusion[0].verified is False
    assert d.exclusion[0].verified is None  # empty evidence is not checked


def test_no_inclusion_criteria_scores_relevance_only() -> None:
    c = Criteria(question="q")
    d = decide({"relevance": 3, "inclusion": [], "exclusion": [], "reason": ""}, c, "T", ABSTRACT)
    assert d.decision == "include" and d.score == pytest.approx(3 / 5)


def test_failed_decision_is_uncertain_with_error() -> None:
    d = failed_decision(C, "bad json")
    assert d.decision == "uncertain" and d.score == 0 and d.error == "bad json"
    assert len(d.inclusion) == 2 and len(d.exclusion) == 1


def test_criteria_from_text_and_hash() -> None:
    c = Criteria.from_text(" q ", "a\n\n b \n", "")
    assert c == Criteria(question="q", include=["a", "b"], exclude=[])
    assert not c.is_empty() and Criteria(question=" ").is_empty()
    assert criteria_hash(c) == criteria_hash(Criteria.from_text("q", "a\nb", ""))
    assert criteria_hash(c) != criteria_hash(Criteria.from_text("q", "a", ""))


def test_schema_has_one_entry_per_criterion() -> None:
    s = response_schema(C)
    assert s["properties"]["inclusion"]["minItems"] == 2 == s["properties"]["inclusion"]["maxItems"]
    assert s["properties"]["exclusion"]["maxItems"] == 1
    assert set(s["required"]) == {"relevance", "inclusion", "exclusion", "reason"}


def test_messages_number_criteria_and_mark_missing_abstract() -> None:
    msgs = build_messages(C, "A title", None, 2020, "J Neuro")
    user = msgs[-1]["content"]
    assert "I1. uses a neural mass model" in user and "E1. animal study only" in user
    assert "(no abstract available)" in user and "A title" in user and "2020" in user


def test_fingerprint_changes_with_model_criteria_and_text() -> None:
    base = fingerprint(C, "qwen3:4b", "t", "a")
    assert base == fingerprint(C, "qwen3:4b", "t", "a")
    assert base != fingerprint(C, "qwen3:8b", "t", "a")
    assert base != fingerprint(C, "qwen3:4b", "t", "a2")
    assert base != fingerprint(Criteria(question="other"), "qwen3:4b", "t", "a")
    assert PROMPT_VERSION


def test_infinite_relevance_does_not_crash() -> None:
    assert decide({"relevance": float("inf")}, C, "T", ABSTRACT).relevance == 0
    assert decide({"relevance": float("nan")}, C, "T", ABSTRACT).relevance == 0
