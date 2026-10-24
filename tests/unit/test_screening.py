"""Screening decision rules (pure functions, no model)."""

from __future__ import annotations

import sqlite3

import pytest

from litforge.analysis.gateway import ModelGateway
from litforge.screening.criteria import Criteria, criteria_hash
from litforge.screening.decide import decide, failed_decision
from litforge.screening.llm import FakeLlm
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
    answer = raw(exc=("violated",))
    answer["exclusion"][0]["evidence"] = "neural mass model"
    assert decide(answer, C, "Title", ABSTRACT).decision == "exclude"


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


@pytest.mark.parametrize("quote", ["", "Invented animal experiment"])
def test_unverified_exclusion_is_uncertain_with_reason(quote: str) -> None:
    answer = raw(exc=("violated",))
    answer["exclusion"][0]["evidence"] = quote
    result = decide(answer, C, "Title", ABSTRACT)
    assert result.decision == "uncertain"
    assert "exclusion quote not verified" in result.reason.lower()


def test_verified_title_quote_does_not_override_missing_abstract() -> None:
    answer = raw(exc=("violated",))
    answer["exclusion"][0]["evidence"] = "Title"
    assert decide(answer, C, "Title", None).decision == "uncertain"


@pytest.fixture()
def borderline_library(conn: sqlite3.Connection) -> tuple[ModelGateway, FakeLlm]:
    from lens_fakes import gateway, seed

    from litforge.screening.criteria import save_criteria

    for paper, decision, score in [
        ("a", "exclude", 0.2), ("b", "uncertain", 0.3), ("c", "include", 0.55),
        ("d", "include", 0.9), ("e", "uncertain", 0.5),
    ]:
        seed(conn, paper)
        conn.execute(
            "INSERT INTO screening_results(project_id,paper_id,decision,score,result_json,model,"
            "prompt_version,criteria_hash,fingerprint,screened_at) VALUES ('proj',?,?,?,'{}','fake','v','h','f','now')",
            (paper, decision, score),
        )
    seed(conn, "unscreened")
    conn.execute("INSERT INTO paper_marks VALUES ('proj','e','irrelevant','now')")
    save_criteria(conn, "proj", Criteria(question="gain"))
    return gateway(conn, {"relevance": 3, "inclusion": [], "exclusion": [], "reason": "ok"})


def test_fulltext_borderline_selects_uncertain_and_mid_scores(
    conn: sqlite3.Connection, borderline_library: tuple[ModelGateway, FakeLlm]
) -> None:
    from litforge.screening.fulltext import screen_fulltext

    gw, _ = borderline_library
    result = screen_fulltext(conn, gw, "proj", only="borderline")
    assert result.screened == 2
    assert {r[0] for r in conn.execute("SELECT paper_id FROM fulltext_screening")} == {"b", "c"}
    assert conn.execute("SELECT mark FROM paper_marks WHERE paper_id='e'").fetchone()[0] == "irrelevant"


def test_fulltext_borderline_preview_counts_match_run(
    conn: sqlite3.Connection, borderline_library: tuple[ModelGateway, FakeLlm]
) -> None:
    from litforge.screening.fulltext import plan_calls, screen_fulltext

    gw, fake = borderline_library
    assert len(plan_calls(conn, gw, "proj", only="borderline")) == 2
    assert fake.calls == 0
    assert screen_fulltext(conn, gw, "proj", only="borderline").screened == 2
    assert fake.calls == 2
    assert plan_calls(conn, gw, "proj", only="borderline") == []
    assert screen_fulltext(conn, gw, "proj", only="borderline").cached == 2


def test_fulltext_borderline_explicit_paper_ids_ignore_filter(
    conn: sqlite3.Connection, borderline_library: tuple[ModelGateway, FakeLlm]
) -> None:
    from litforge.screening.fulltext import screen_fulltext

    gw, _ = borderline_library
    assert screen_fulltext(conn, gw, "proj", only="borderline", paper_ids=["a", "e"]).screened == 2
    assert {r[0] for r in conn.execute("SELECT paper_id FROM fulltext_screening")} == {"a", "e"}
    assert conn.execute("SELECT mark FROM paper_marks WHERE paper_id='e'").fetchone()[0] == "irrelevant"


def test_fulltext_borderline_top_limits_filtered_set(
    conn: sqlite3.Connection, borderline_library: tuple[ModelGateway, FakeLlm]
) -> None:
    from litforge.screening.fulltext import plan_calls, screen_fulltext

    gw, _ = borderline_library
    assert len(plan_calls(conn, gw, "proj", only="borderline", top=1)) == 1
    assert screen_fulltext(conn, gw, "proj", only="borderline", top=1).screened == 1
    assert [r[0] for r in conn.execute("SELECT paper_id FROM fulltext_screening")] == ["b"]


def test_fulltext_borderline_recorded_in_start_run_input(
    conn: sqlite3.Connection, borderline_library: tuple[ModelGateway, FakeLlm]
) -> None:
    from litforge.analysis.runs import input_hash
    from litforge.screening.fulltext import screen_fulltext

    gw, _ = borderline_library
    result = screen_fulltext(conn, gw, "proj", only="borderline")
    row = conn.execute("SELECT input_hash FROM analysis_runs WHERE id=?", (result.run_id,)).fetchone()
    assert row[0] == input_hash({"top": None, "paper_ids": None, "only": "borderline"})


def test_fulltext_borderline_custom_band(
    conn: sqlite3.Connection, borderline_library: tuple[ModelGateway, FakeLlm]
) -> None:
    from litforge.screening.fulltext import screen_fulltext

    gw, _ = borderline_library
    screen_fulltext(conn, gw, "proj", only="borderline", band=(0.7, 1.0))
    assert {r[0] for r in conn.execute("SELECT paper_id FROM fulltext_screening")} == {"b", "d"}


@pytest.mark.parametrize("band", [(-0.1, 0.7), (0.4, 1.1), (0.7, 0.4), (0.5, 0.5)])
def test_borderline_config_rejects_invalid_band(band: tuple[float, float]) -> None:
    from litforge.config import ScreeningConfig

    with pytest.raises(ValueError, match="borderline"):
        ScreeningConfig(borderline=band)


def test_fulltext_borderline_api_preview_uses_configured_band(
    conn: sqlite3.Connection, borderline_library: tuple[ModelGateway, FakeLlm]
) -> None:
    from litforge.ui.api import LensRequest, lens_plan

    gw, _ = borderline_library
    gw.config.screening.borderline = (0.85, 0.95)
    request = LensRequest(task="screen_fulltext", project="demo", only="borderline")
    assert lens_plan(conn, gw, request)["number_of_calls"] == 2
    with pytest.raises(ValueError):
        LensRequest(task="screen_fulltext", project="demo", only="invalid")


def test_fulltext_borderline_band_includes_low_excludes_high(
    conn: sqlite3.Connection, borderline_library: tuple[ModelGateway, FakeLlm]
) -> None:
    from litforge.screening.fulltext import screen_fulltext

    gw, _ = borderline_library
    conn.execute("UPDATE screening_results SET score=0.4 WHERE paper_id='a'")
    conn.execute("UPDATE screening_results SET score=0.7 WHERE paper_id='d'")
    screen_fulltext(conn, gw, "proj", only="borderline")
    assert {r[0] for r in conn.execute("SELECT paper_id FROM fulltext_screening")} == {"a", "b", "c"}
