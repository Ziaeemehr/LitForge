from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from lens_fakes import evidence_id, seed

from litforge.analysis.claim_graph import candidate_relation, claim_landscape, classify_relations, cluster_claims
from litforge.analysis.claims import canonical_text, extract_claims, review_claim
from litforge.analysis.evaluation import RetrievalBenchmark, evaluate_screening
from litforge.analysis.gateway import ModelGateway
from litforge.config import Config, Paths
from litforge.screening.active import review_queue
from litforge.screening.criteria import Criteria, save_criteria
from litforge.screening.fulltext import screen_fulltext
from litforge.screening.llm import FakeLlm


def setup(conn: sqlite3.Connection) -> None:
    seed(conn)
    seed(conn, "q")
    save_criteria(conn, "proj", Criteria(question="gain", include=["participants"], exclude=["animal only"]))


def model(conn: sqlite3.Connection, answer: Any) -> tuple[ModelGateway, FakeLlm]:
    fake = FakeLlm(answer)
    return ModelGateway(conn, Config(), providers={"local": fake}), fake


def claims_reply(messages: list[dict[str, str]]) -> dict[str, Any]:
    base = {"text": "Gain improves outcomes.", "type": "finding", "polarity": "positive", "outcome": "Gain",
            "evidence_ids": [evidence_id(messages), "invented"]}
    return {"claims": [base, {**base, "text": "  GAIN improves outcomes! "},
                       {**base, "text": "uncited", "evidence_ids": ["invented"]}]}


def test_canonical_claims_cache_dedupe_and_reviews(conn: sqlite3.Connection) -> None:
    setup(conn)
    assert canonical_text("  ＧＡＩＮ\n improves outcomes… ") == "gain improves outcomes"
    gw, fake = model(conn, claims_reply)
    result = extract_claims(conn, gw, "proj")
    assert len(result.claims) == 2 and result.uncited_dropped == 2 and fake.calls == 2
    assert all(len(c.evidence_ids) == 1 for c in result.claims)
    assert extract_claims(conn, gw, "proj").cached == 2 and fake.calls == 2
    c = result.claims[0]
    review_claim(conn, c.id, "edit", "Edited claim.")
    assert conn.execute("SELECT status FROM claims WHERE id=?", (c.id,)).fetchone()[0] == "edited"
    extract_claims(conn, gw, "proj", force=True)
    assert conn.execute("SELECT text FROM claims WHERE id=?", (c.id,)).fetchone()[0] == "Edited claim."
    assert conn.execute("SELECT COUNT(*) FROM model_calls WHERE run_id IS NULL OR prompt_version!='claims-v1'") \
        .fetchone()[0] == 0


def test_clusters_relations_hypotheses_landscape(conn: sqlite3.Connection) -> None:
    setup(conn)
    gw, _ = model(conn, claims_reply)
    extract_claims(conn, gw, "proj")
    first = cluster_claims(conn, "proj")
    assert len(first) == 1 and cluster_claims(conn, "proj") == first
    assert candidate_relation("positive", "null", "Gain", " gain ") == "contradicts"
    assert candidate_relation("positive", "negative", "gain", "loss") is None
    conn.execute("UPDATE papers SET year=2020,cited_by_count=100 WHERE id='p'")
    conn.execute("UPDATE papers SET year=2022,cited_by_count=7 WHERE id='q'")
    gw, fake = model(conn, lambda m: {"relation": "replicates", "explanation": "Same result",
                                     "possible_causes": [{"factor": "dataset", "detail": "Maybe", "evidence_ids": []}]})
    result = classify_relations(conn, gw, "proj")
    assert fake.calls == 1 and result.relations[0].causes[0].unsupported_hypothesis
    landscape = claim_landscape(conn, "proj")
    assert landscape[0].replication_count == 1 and not landscape[0].single_source
    assert landscape[0].strength == 2 and landscape[0].cited_by_count == 107
    assert landscape[0].origin_paper_id == "p"
    assert classify_relations(conn, gw, "proj").relations and fake.calls == 1


def test_same_paper_never_links(conn: sqlite3.Connection) -> None:
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="gain"))
    gw, _ = model(conn, lambda m: {"claims": [
        {"text": text, "type": "finding", "polarity": "positive", "evidence_ids": [evidence_id(m)]}
        for text in ["Gain improves outcomes", "Gain improves outcomes greatly"]]})
    extract_claims(conn, gw, "proj")
    assert len(cluster_claims(conn, "proj", threshold=0)) == 2


@pytest.mark.parametrize(("evidence", "expected"), [("missing", "uncertain"), ("wrong", "uncertain"),
                                                   ("valid", "exclude")])
def test_fulltext_requires_cited_verified_quote(conn: sqlite3.Connection, evidence: str, expected: str) -> None:
    setup(conn)
    def reply(m: list[dict[str, str]]) -> dict[str, Any]:
        return {"relevance": 4, "inclusion": [{"status": "met", "evidence": {"evidence_id": evidence_id(m),
                  "quote": "participants"}}], "exclusion": [{"status": "violated", "evidence": {
                  "evidence_id": "invented" if evidence == "missing" else evidence_id(m),
                  "quote": "invented quote" if evidence == "wrong" else "1,200 participants"}}], "reason": "Check"}
    gw, fake = model(conn, reply)
    report = screen_fulltext(conn, gw, "proj")
    assert report.counts == {expected: 2}
    assert conn.execute("SELECT COUNT(*) FROM screening_history WHERE stage='S2'").fetchone()[0] == 2
    assert screen_fulltext(conn, gw, "proj").cached == 2 and fake.calls == 2
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM screening_history")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE screening_history SET reason='changed'")


def test_queue_components_needs_pdf_and_evaluation(conn: sqlite3.Connection) -> None:
    setup(conn)
    conn.execute("INSERT INTO papers(id,title,created_at,updated_at) VALUES ('missing','gain','now','now')")
    conn.execute("INSERT INTO project_papers VALUES ('proj','missing','manual','now')")
    gw, _ = model(conn, lambda m: {"relevance": 3, "inclusion": [], "exclusion": [], "reason": "borderline"})
    report = screen_fulltext(conn, gw, "proj")
    assert report.needs_pdf == ["missing"]
    queue = review_queue(conn, "proj")
    assert len(queue) == 3
    for item in queue:
        assert item.info == pytest.approx(.4 * item.uncertainty + .3 * item.disagreement +
                                          .2 * item.boundary + .1 * item.coverage)
        assert item.reason
    assert [i.info for i in queue] == sorted([i.info for i in queue], reverse=True)
    bench = RetrievalBenchmark(name="screen", screening=[{"paper": "p", "relevant": True},
                                                        {"paper": "q", "relevant": False}])
    result = evaluate_screening(conn, bench, "proj")
    assert result.stages["S2"].shortlist_recall == 1 and result.passed
    conn.execute("UPDATE fulltext_screening SET decision='exclude' WHERE paper_id='p'")
    result = evaluate_screening(conn, bench, "proj")
    assert result.stages["S2"].false_negative_rate == 1 and not result.passed
    stored = conn.execute("SELECT result_json FROM analysis_runs WHERE id=?", (result.run_id,)).fetchone()[0]
    assert not json.loads(stored)["passed"]


def test_fulltext_parse_without_matching_chunks_stays_uncertain(conn: sqlite3.Connection) -> None:
    setup(conn)
    conn.execute("DELETE FROM chunks")
    gw, fake = model(conn, lambda m: pytest.fail("empty evidence must not call a model"))
    report = screen_fulltext(conn, gw, "proj")
    assert report.counts == {"uncertain": 2} and fake.calls == 0


def test_screen_run_and_marks_append_history_without_output_changes(conn: sqlite3.Connection) -> None:
    from litforge.screening.run import screen_run
    from litforge.ui.api import set_marks
    setup(conn)
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,status,started_at) "
                 "VALUES ('d','proj','search','ok','now')")
    conn.execute("INSERT INTO discovery_results(run_id,paper_id,score,n_hits,new_to_project) VALUES ('d','p',1,1,1)")
    fake = FakeLlm(lambda m: {"relevance": 4, "inclusion": [], "exclusion": [], "reason": "S1 reason"})
    report = screen_run(conn, fake, "d", 1)
    assert report.screened == 1 and report.counts == {"include": 1}
    assert screen_run(conn, fake, "d", 1).cached == 1
    set_marks(conn, "demo", ["p"], "relevant")
    set_marks(conn, "demo", ["p"], "irrelevant")
    set_marks(conn, "demo", ["p"], None)
    rows = conn.execute("SELECT stage,source,decision,reason,criteria_hash FROM screening_history "
                        "ORDER BY rowid").fetchall()
    assert [(r["stage"], r["source"], r["decision"]) for r in rows] == [
        ("S1", "model", "include"), ("S3", "human", "include"),
        ("S3", "human", "exclude"), ("S3", "human", "uncertain")]
    assert all(r["reason"] and r["criteria_hash"] for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM model_calls WHERE task='screen' AND run_id IS NOT NULL "
                        "AND prompt_version='screen-v2'").fetchone()[0] == 1


def test_queue_mark_neighbors_and_stage_disagreement(conn: sqlite3.Connection) -> None:
    from litforge.ui.api import set_marks
    setup(conn)
    seed(conn, "t")
    conn.execute("UPDATE papers SET title='gain improves outcomes',abstract='gain improves outcomes'")
    set_marks(conn, "demo", ["p"], "relevant")
    set_marks(conn, "demo", ["q"], "irrelevant")
    gw, _ = model(conn, lambda m: {"relevance": 3, "inclusion": [], "exclusion": [], "reason": "S2"})
    screen_fulltext(conn, gw, "proj")
    conn.execute("INSERT INTO screening_results(project_id,paper_id,decision,score,result_json,model,"
                     "prompt_version,"
                 "criteria_hash,fingerprint,screened_at) VALUES ('proj','t','exclude',0,'{}','fake','v','h','f','now')")
    item = review_queue(conn, "proj")[0]
    assert item.paper_id == "t" and item.stage == "S2"
    assert item.predicted_relevant == .5 and item.boundary == 1 and item.disagreement == 1
    assert item.uncertainty == pytest.approx(.9) and item.coverage == 0


@pytest.mark.parametrize(("text", "expected"), [("A\tB\nC!!!", "a b c"), ("Café.", "café"),
    ("Cafe\u0301", "café"), (" Effect (n=2) ", "effect (n=2"), ("x-y, z", "x-y, z")])
def test_canonical_unicode_whitespace_and_trailing_punctuation(text: str, expected: str) -> None:
    assert canonical_text(text) == expected


def test_clustering_boost_threshold_transitivity_and_label(conn: sqlite3.Connection) -> None:
    setup(conn)
    seed(conn, "t")
    gw, _ = model(conn, lambda m: {"claims": [{"text": "Result " + evidence_id(m), "type": "finding",
                    "polarity": "positive", "outcome": " Gain. ", "evidence_ids": [evidence_id(m)]}]})
    extract_claims(conn, gw, "proj")
    conn.execute("UPDATE papers SET cited_by_count=500 WHERE id='q'")
    assert len(cluster_claims(conn, "proj", .55, similarity=lambda a, b: .44)) == 3
    clusters = cluster_claims(conn, "proj", .55, similarity=lambda a, b: .45)
    assert len(clusters) == 1
    assert clusters[0].label == conn.execute("SELECT text FROM claims WHERE paper_id='q'").fetchone()[0]
    assert len(cluster_claims(conn, "proj", 1, similarity=lambda a, b: .99)) == 1
    # Two edges can connect three papers; the absent third edge does not split the cluster.
    texts = {r["paper_id"]: canonical_text(r["text"]) for r in conn.execute("SELECT * FROM claims")}
    assert len(cluster_claims(conn, "proj", .8, similarity=lambda a, b:
        0 if {a, b} == {texts["p"], texts["t"]} else .9)) == 1


def test_relation_candidates_first_and_budget_stop(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm.budget import BudgetExceeded
    setup(conn)
    seed(conn, "t")
    gw, _ = model(conn, claims_reply)
    extract_claims(conn, gw, "proj")
    conn.execute("UPDATE claims SET polarity='negative' WHERE paper_id='t'")
    cluster_claims(conn, "proj")
    seen: list[dict[str, Any]] = []
    def reply(m: list[dict[str, str]]) -> dict[str, Any]:
        pair = json.loads(m[-1]["content"].split("\n")[0])
        seen.append(pair)
        return {"relation": "contradicts", "explanation": "Different", "possible_causes": [
            {"factor": "population", "detail": "Maybe", "evidence_ids": [evidence_id(m), "invented"]}]}
    gw, _ = model(conn, reply)
    result = classify_relations(conn, gw, "proj", max_pairs=1)
    assert len(result.relations) == 1 and seen[0]["candidate"] == "contradicts"
    cause = result.relations[0].causes[0]
    assert cause.hypothesis and not cause.unsupported_hypothesis and len(cause.evidence_ids) == 1
    before = conn.execute("SELECT COUNT(*) FROM claim_relations").fetchone()[0]
    def budget(*args: Any, **kwargs: Any) -> Any:
        raise BudgetExceeded("budget")
    monkeypatch.setattr(gw, "call", budget)
    result = classify_relations(conn, gw, "proj")
    assert result.budget_stopped and conn.execute("SELECT COUNT(*) FROM claim_relations").fetchone()[0] == before
    assert conn.execute("SELECT status FROM analysis_runs WHERE id=?", (result.run_id,)).fetchone()[0] == "ok"


def test_landscape_citations_origin_contradiction_and_rejected_claims(conn: sqlite3.Connection) -> None:
    setup(conn)
    gw, _ = model(conn, claims_reply)
    extract_claims(conn, gw, "proj")
    conn.execute("UPDATE papers SET year=2000,cited_by_count=100 WHERE id='p'")
    conn.execute("UPDATE papers SET year=2001,cited_by_count=200 WHERE id='q'")
    conn.execute("INSERT INTO paper_relations(citing_id,cited_id,source,created_at) VALUES ('q','p','fake','now')")
    cluster_claims(conn, "proj")
    gw, _ = model(conn, lambda m: {"relation": "contradicts", "explanation": "Opposite"})
    classify_relations(conn, gw, "proj")
    c = claim_landscape(conn, "proj")[0]
    assert c.supporting_papers == c.contradicting_papers == 1 and c.strength == 0 and c.cited_by_count == 300
    assert c.origin_paper_id == "p" and c.later_citing_papers == ["q"] and c.replication_count == 0
    q = conn.execute("SELECT id FROM claims WHERE paper_id='q'").fetchone()[0]
    review_claim(conn, q, "reject")
    c = claim_landscape(conn, "proj")[0]
    assert c.single_source and c.strength == 1 and c.cited_by_count == 100


@pytest.mark.parametrize("invalid", ["wrong_id", "another_span", "truncated", "empty", "string"])
def test_s2_only_violated_criterion_quote_can_authorize_exclusion(conn: sqlite3.Connection, invalid: str) -> None:
    from litforge.analysis.ledger import to_packet
    from litforge.screening.fulltext import fulltext_decision
    span = seed(conn, text="Results: animal only. " + "x " * 1100 + "Faraway quote")
    packet = to_packet([span])
    c = Criteria(question="gain", exclude=["animal only", "other"])
    evidence: Any = {"evidence_id": span.id, "quote": "animal only"}
    if invalid in ("wrong_id", "another_span"):
        evidence["evidence_id"] = "other"
    elif invalid == "truncated":
        evidence["quote"] = "Faraway quote"
    elif invalid == "empty":
        evidence["quote"] = ""
    else:
        evidence = "animal only"
    raw = {"relevance": 5, "exclusion": [{"status": "violated", "evidence": evidence},
           {"status": "not_violated", "evidence": {"evidence_id": span.id, "quote": "animal only"}}]}
    assert fulltext_decision(raw, c, packet).decision == "uncertain"
    raw["exclusion"][0]["evidence"] = {"evidence_id": span.id, "quote": "animal only"}
    assert fulltext_decision(raw, c, packet).decision == "exclude"


def test_claim_cache_model_parse_and_empty_extraction(conn: sqlite3.Connection) -> None:
    setup(conn)
    gw, fake = model(conn, lambda m: {"claims": []})
    assert extract_claims(conn, gw, "proj").claims == [] and fake.calls == 2
    assert extract_claims(conn, gw, "proj").cached == 2 and fake.calls == 2
    gw.config.models.local.model = "another-model"
    extract_claims(conn, gw, "proj")
    assert fake.calls == 4
    conn.execute("UPDATE parse_runs SET is_current=0 WHERE id='rp'")
    conn.execute("INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,created_at,"
                 "is_current,output_dir) VALUES ('rp2','ap','fake','2','h','ok','later',1,'parsed')")
    conn.execute("UPDATE chunks SET parse_run_id='rp2' WHERE paper_id='p'")
    extract_claims(conn, gw, "proj")
    assert fake.calls == 5


def test_history_insert_or_replace_is_refused(conn: sqlite3.Connection) -> None:
    from litforge.ui.api import set_marks
    setup(conn)
    set_marks(conn, "demo", ["p"], "relevant")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("INSERT OR REPLACE INTO screening_history SELECT * FROM screening_history")


def test_s2_cache_criteria_and_budget(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    from litforge.llm.budget import BudgetExceeded
    setup(conn)
    gw, fake = model(conn, lambda m: {"relevance": 4, "reason": "match"})
    screen_fulltext(conn, gw, "proj", top=1)
    assert fake.calls == 1
    save_criteria(conn, "proj", Criteria(question="gain", include=["new criterion"]))
    screen_fulltext(conn, gw, "proj", top=1)
    assert fake.calls == 2
    def budget(*args: Any, **kwargs: Any) -> Any:
        raise BudgetExceeded("budget")
    monkeypatch.setattr(gw, "call", budget)
    result = screen_fulltext(conn, gw, "proj", force=True)
    assert result.budget_stopped and result.screened == 0
    assert conn.execute("SELECT COUNT(*) FROM screening_history").fetchone()[0] == 2


def test_claim_edits_invalidate_model_relations_and_update_identity(conn: sqlite3.Connection) -> None:
    setup(conn)
    gw, _ = model(conn, claims_reply)
    result = extract_claims(conn, gw, "proj")
    cluster_claims(conn, "proj")
    relation_gw, fake = model(conn, lambda m: {"relation": "replicates", "explanation": "Original"})
    classify_relations(conn, relation_gw, "proj")
    review_claim(conn, result.claims[0].id, "edit", "Gain improves outcomes in adults.")
    row = conn.execute("SELECT * FROM claims WHERE id=?", (result.claims[0].id,)).fetchone()
    assert row["canonical_text"] == "gain improves outcomes in adults"
    cluster_claims(conn, "proj", threshold=0)
    assert claim_landscape(conn, "proj")[0].replication_count == 0
    classify_relations(conn, relation_gw, "proj")
    assert fake.calls == 2
    assert len(extract_claims(conn, gw, "proj", force=True).claims) == 2


def test_project_delete_keeps_append_only_screening_history(conn: sqlite3.Connection, paths: Paths) -> None:
    from litforge.projects import delete_project
    setup(conn)
    gw, _ = model(conn, claims_reply)
    extract_claims(conn, gw, "proj")
    cluster_claims(conn, "proj")
    gw, _ = model(conn, lambda m: {"relevance": 4, "reason": "match"})
    screen_fulltext(conn, gw, "proj")
    delete_project(conn, paths, "demo")
    assert conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM screening_history").fetchone()[0] == 2


def test_section_retrieval_preferred_and_fts_fallback(conn: sqlite3.Connection) -> None:
    from litforge.analysis.claims import paper_spans
    setup(conn)
    conn.execute("UPDATE chunks SET section_path='Methods' WHERE paper_id='p'")
    conn.execute("INSERT INTO chunks(id,parse_run_id,paper_id,kind,order_idx,section_path,text,token_count,"
                 "page_start,page_end,hash) VALUES ('result','rp','p','text',1,'Discussion > Findings',"
                 "'A section with different words.',6,3,3,'h')")
    preferred = paper_spans(conn, "p", "rp", "gain")
    assert len(preferred) == 1 and preferred[0].chunk_id == "result" and preferred[0].page == 3
    conn.execute("UPDATE chunks SET section_path='Methods' WHERE id='result'")
    fallback = paper_spans(conn, "p", "rp", "gain")
    assert [s.chunk_id for s in fallback] == ["cp"]


def test_s2_retrieves_at_most_three_chunks_per_criterion(conn: sqlite3.Connection) -> None:
    setup(conn)
    for i in range(6):
        conn.execute("INSERT INTO chunks(id,parse_run_id,paper_id,kind,order_idx,text,token_count,"
                     "page_start,page_end,hash) VALUES (?, 'rp','p','text',?, ?,10,1,1,'h')",
                     ("extra" + str(i), i + 1, "animal only participants gain " * (i + 1)))
    from litforge.screening.fulltext import plan_calls
    gw, _ = model(conn, lambda m: {})
    plans = plan_calls(conn, gw, "proj", paper_ids=["p"])
    assert len(plans) == 1 and len(plans[0].evidence_ids) <= 6
    assert set(plans[0].evidence_ids) <= {r[0] for r in conn.execute(
        "SELECT id FROM evidence_spans WHERE paper_id='p'")}


def test_queue_coverage_uncertainty_mark_disagreement_and_limit(conn: sqlite3.Connection) -> None:
    from litforge.ui.api import set_marks
    setup(conn)
    conn.execute("UPDATE papers SET abstract='gain participants',title='gain participants'")
    conn.execute("UPDATE parse_runs SET is_current=0 WHERE artifact_id='aq'")
    set_marks(conn, "demo", ["p"], "irrelevant")
    gw, _ = model(conn, lambda m: {"relevance": 5})
    # q has no parse, so its S1 model suggestion is used.
    screen_fulltext(conn, gw, "proj")
    conn.execute("INSERT INTO screening_results(project_id,paper_id,decision,score,result_json,model,"
                     "prompt_version,"
                 "criteria_hash,fingerprint,screened_at) VALUES ('proj','q','include',1,'{}','fake','v','h','f','now')")
    item = review_queue(conn, "proj", 1)[0]
    assert item.stage == "S1" and item.predicted_relevant == 0 and item.disagreement == 1
    assert item.coverage == .5 and item.uncertainty == 0 and item.boundary == 0
    with pytest.raises(ValueError):
        review_queue(conn, "proj", 0)


def test_screening_evaluation_stage_rates_threshold_and_identifiers(conn: sqlite3.Connection) -> None:
    setup(conn)
    conn.execute("UPDATE papers SET doi='10.123/AbC' WHERE id='p'")
    for paper, decision in (("p", "exclude"), ("q", "uncertain")):
        conn.execute("INSERT INTO screening_results(project_id,paper_id,decision,score,result_json,model,"
                     "prompt_version,"
                     "criteria_hash,fingerprint,screened_at) VALUES ('proj',?,?,.5,'{}','fake','v','h','f','now')",
                     (paper, decision))
    bench = RetrievalBenchmark(name="rates", screening=[{"paper": "10.123/abc", "relevant": True},
                                                       {"paper": "q", "relevant": True}])
    report = evaluate_screening(conn, bench, "proj", threshold=.5)
    assert report.passed
    assert report.stages["S1"].false_negative_rate == report.stages["S1"].exclusion_rate == .5
    assert report.stages["S1"].uncertain_rate == report.stages["S1"].shortlist_recall == .5
    assert report.stages["S2"].uncertain_rate == 1 and report.stages["S2"].screened == 0
    assert not evaluate_screening(conn, bench, "proj", threshold=.49).passed
    with pytest.raises(ValueError, match="resolve"):
        evaluate_screening(conn, RetrievalBenchmark(name="bad", screening=[{"paper": "outside", "relevant": True}]),
                           "proj")


def test_claims_without_retrieved_evidence_are_not_counted_cached(conn: sqlite3.Connection) -> None:
    setup(conn)
    conn.execute("DELETE FROM chunks")
    gw, fake = model(conn, lambda m: pytest.fail("no evidence"))
    result = extract_claims(conn, gw, "proj")
    assert result.cached == 0 and not result.claims and fake.calls == 0


@pytest.mark.parametrize("question", ["", "unmatchedword"])
def test_claims_fallback_uses_first_content_then_front_matter(conn: sqlite3.Connection, question: str) -> None:
    from litforge.analysis.claims import paper_spans
    setup(conn)
    conn.execute("UPDATE chunks SET section_path='Front matter' WHERE paper_id='p'")
    for i in range(10):
        conn.execute("INSERT INTO chunks(id,parse_run_id,paper_id,kind,order_idx,section_path,text,token_count,"
                     "page_start,page_end,hash) "
                     "VALUES (?, 'rp','p','text',?,'Methods','Useful paper text',4,1,1,'h')", (f"body{i}", i + 1))
    assert [s.chunk_id for s in paper_spans(conn, "p", "rp", question)] == [f"body{i}" for i in range(8)]
    conn.execute("DELETE FROM chunks WHERE section_path='Methods'")
    assert [s.chunk_id for s in paper_spans(conn, "p", "rp", question)] == ["cp"]


def test_claims_report_no_text_including_stale_cache(conn: sqlite3.Connection) -> None:
    setup(conn)
    gw, _ = model(conn, claims_reply)
    extract_claims(conn, gw, "proj")
    conn.execute("DELETE FROM chunks")
    result = extract_claims(conn, gw, "proj")
    assert result.no_text == 2 and result.cached == 0


def test_fulltext_filters_unsent_criterion_evidence(conn: sqlite3.Connection) -> None:
    from litforge.ui.api import fulltext_result
    setup(conn)
    def reply(m: list[dict[str, str]]) -> dict[str, Any]:
        return {"relevance": 5, "inclusion": [{"status": "met", "evidence": {
            "evidence_id": evidence_id(m), "quote": "participants"}}], "exclusion": [
            {"status": "violated", "evidence": {"evidence_id": "unsent-id", "quote": "participants"}}]}
    gw, _ = model(conn, reply)
    screen_fulltext(conn, gw, "proj")
    for row in conn.execute("SELECT result_json FROM fulltext_screening"):
        assert "unsent-id" not in row[0]
        result = json.loads(row[0])
        assert result["invalid_citations"] == 1
        assert result["criterion_evidence"]["inclusion"][0]["evidence"]["evidence_id"]
    assert "unsent-id" not in json.dumps(fulltext_result(conn, "proj", "p"))


def test_claims_api_supplies_paper_titles_and_years(conn: sqlite3.Connection) -> None:
    from litforge.ui.api import lens_claims
    setup(conn)
    conn.execute("UPDATE papers SET year=2020 WHERE id='p'")
    gw, _ = model(conn, claims_reply)
    extract_claims(conn, gw, "proj")
    cluster_claims(conn, "proj")
    data = lens_claims(conn, "proj")
    assert data["papers"]["p"] == {"title": "Title p", "year": 2020}


def test_claim_command_module_has_content_name() -> None:
    from importlib import import_module
    assert callable(import_module("litforge.analysis.claim_commands").register)


@pytest.mark.parametrize("table", ["evidence_spans", "human_reviews", "screening_history"])
@pytest.mark.parametrize("rowid_alias", ["rowid", "_rowid_", "oid"])
def test_immutable_tables_refuse_rowid_replacement(conn: sqlite3.Connection, table: str, rowid_alias: str) -> None:
    from litforge.ui.api import set_marks
    setup(conn)
    set_marks(conn, "demo", ["p"], "relevant")
    conn.execute("INSERT INTO human_reviews VALUES ('review','cell','target','accept','{}','{}','','now')")
    original = dict(conn.execute(f"SELECT rowid,* FROM {table} LIMIT 1").fetchone())
    columns = list(original)
    values = list(original.values())
    columns[0] = rowid_alias
    values[1] = "replacement-id"
    with pytest.raises(sqlite3.IntegrityError, match="immutable|append-only"):
        conn.execute(f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) "
                     f"VALUES ({','.join('?' for _ in values)})", values)
    assert dict(conn.execute(f"SELECT rowid,* FROM {table} WHERE rowid=?", (original['rowid'],)).fetchone()) == original


@pytest.mark.parametrize("change", ["polarity", "outcome", "evidence", "population", "method", "type"])
def test_reextraction_invalidates_changed_inputs(conn: sqlite3.Connection, change: str) -> None:
    setup(conn)
    gw, _ = model(conn, claims_reply)
    extract_claims(conn, gw, "proj")
    cluster_claims(conn, "proj")
    relation_gw, fake = model(conn, lambda m: {"relation": "replicates", "explanation": "Original"})
    classify_relations(conn, relation_gw, "proj")
    if change == "evidence":
        conn.execute("UPDATE chunks SET text='Results: a new cited finding' WHERE paper_id='p'")
    def reply(m: list[dict[str, str]]) -> dict[str, Any]:
        draft = claims_reply(m)["claims"][0]
        if change != "evidence":
            draft[change] = {"polarity": "negative", "outcome": "Loss", "population": "Adults",
                             "method": "Trial", "type": "method"}[change]
        return {"claims": [draft]}
    gw, _ = model(conn, reply)
    extract_claims(conn, gw, "proj", paper_ids=["p"], force=True)
    assert conn.execute("SELECT status FROM claim_relations").fetchone()[0] == "stale"
    if change == "outcome":
        assert conn.execute("SELECT COUNT(*) FROM claims WHERE cluster_id IS NOT NULL").fetchone()[0] == 0
    cluster_claims(conn, "proj", threshold=0)
    assert claim_landscape(conn, "proj")[0].replication_count == 0
    classify_relations(conn, relation_gw, "proj")
    assert fake.calls == 2


@pytest.mark.parametrize("empty", [False, True])
def test_reextraction_reconciles_proposals_preserves_reviews(conn: sqlite3.Connection, empty: bool) -> None:
    setup(conn)
    gw, _ = model(conn, lambda m: {"claims": [
        {"text": text, "type": "finding", "polarity": "positive", "evidence_ids": [evidence_id(m)]}
        for text in ["Obsolete finding", "Accepted finding", "Rejected finding", "Edited finding"]]})
    extracted = extract_claims(conn, gw, "proj")
    reviewed = []
    for c in extracted.claims:
        if c.paper_id == "p" and c.text != "Obsolete finding":
            action = {"Accepted finding": "accept", "Rejected finding": "reject", "Edited finding": "edit"}[c.text]
            reviewed.append(review_claim(conn, c.id, action, "Human text" if action == "edit" else None))
    cluster_claims(conn, "proj", threshold=0)
    relation_gw, _ = model(conn, lambda m: {"relation": "replicates", "explanation": "Original"})
    classify_relations(conn, relation_gw, "proj")
    gw, _ = model(conn, lambda m: {"claims": [] if empty else [
        {"text": "Replacement finding", "type": "finding", "polarity": "positive",
         "evidence_ids": [evidence_id(m)]}]})
    result = extract_claims(conn, gw, "proj", paper_ids=["p"], force=True)
    proposals = [c for c in result.claims if c.paper_id == "p" and c.status == "proposed"]
    assert [c.text for c in proposals] == ([] if empty else ["Replacement finding"])
    assert all(c in result.claims for c in reviewed)
    assert len([c for c in result.claims if c.paper_id == "q"]) == 4
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE cluster_id IS NOT NULL").fetchone()[0] == 0
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    cluster_claims(conn, "proj", threshold=0)
    assert "Obsolete finding" not in [c.label for c in claim_landscape(conn, "proj")]


@pytest.mark.parametrize("conflict", [False, True])
def test_screening_evaluation_resolves_alias_duplicates(conn: sqlite3.Connection, conflict: bool) -> None:
    setup(conn)
    conn.execute("UPDATE papers SET doi='10.1234/gain',arxiv_id='1234.5678' WHERE id='p'")
    conn.execute("INSERT INTO screening_results(project_id,paper_id,decision,score,result_json,model,"
                 "prompt_version,criteria_hash,fingerprint,screened_at) "
                 "VALUES ('proj','q','exclude',0,'{}','fake','v','h','f','now')")
    bench = RetrievalBenchmark(name="aliases", screening=[
        {"paper": "p", "relevant": True}, {"paper": "10.1234/GAIN", "relevant": not conflict},
        {"paper": "1234.5678", "relevant": True}, {"paper": "q", "relevant": True}])
    if conflict:
        with pytest.raises(ValueError, match="conflicting.*label"):
            evaluate_screening(conn, bench, "proj", threshold=.4)
    else:
        report = evaluate_screening(conn, bench, "proj", threshold=.4)
        assert report.stages["S1"].papers == 2
        assert report.stages["S1"].false_negative_rate == .5
        assert not report.passed


def test_landscape_markdown_origin_title_and_year(conn: sqlite3.Connection) -> None:
    from litforge.analysis.claim_graph import landscape_markdown
    setup(conn)
    conn.execute("UPDATE papers SET year=2020 WHERE id='p'")
    gw, _ = model(conn, claims_reply)
    extract_claims(conn, gw, "proj")
    cluster_claims(conn, "proj")
    assert "origin Title p (2020)" in landscape_markdown(conn, "proj")


def test_unchanged_reextraction_preserves_clusters_and_relations(conn: sqlite3.Connection) -> None:
    setup(conn)
    gw, _ = model(conn, claims_reply)
    extract_claims(conn, gw, "proj")
    clusters = cluster_claims(conn, "proj")
    relation_gw, fake = model(conn, lambda m: {"relation": "replicates", "explanation": "Original"})
    classify_relations(conn, relation_gw, "proj")
    extract_claims(conn, gw, "proj", force=True)
    assert {r[0] for r in conn.execute("SELECT cluster_id FROM claims")} == {c.id for c in clusters}
    assert claim_landscape(conn, "proj")[0].replication_count == 1
    classify_relations(conn, relation_gw, "proj")
    assert fake.calls == 1
