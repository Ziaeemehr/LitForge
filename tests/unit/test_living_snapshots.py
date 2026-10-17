from __future__ import annotations

import json
import sqlite3

import pytest
from lens_fakes import gateway, seed

from litforge.analysis.ask import ask
from litforge.analysis.claim_graph import classify_relations, cluster_claims
from litforge.analysis.claims import extract_claims
from litforge.analysis.matrix import MatrixColumn, create_matrix, extract_matrix
from litforge.analysis.snapshots import diff_snapshots, get_snapshot, significance, take_snapshot
from litforge.screening.criteria import Criteria, save_criteria


def test_snapshot_content_hash_scope_and_immutability(conn: sqlite3.Connection) -> None:
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="gain"))
    gw, _ = gateway(conn)
    ask(conn, gw, "gain", "proj")
    a = take_snapshot(conn, "proj", "baseline")
    b = take_snapshot(conn, "proj", "again")
    first = get_snapshot(conn, a)
    assert first.content_hash == get_snapshot(conn, b).content_hash
    assert first.data.papers[0].id == "p" and first.data.answers[0].question == "gain"
    for sql in ["UPDATE project_snapshots SET label='changed'", "DELETE FROM project_snapshots",
                "INSERT OR REPLACE INTO project_snapshots SELECT * FROM project_snapshots",
                "INSERT OR REPLACE INTO project_snapshots(rowid,id,project_id,label,created_at,content_hash,data_json) "
                "SELECT rowid,'replacement',project_id,label,created_at,content_hash,data_json "
                "FROM project_snapshots LIMIT 1"]:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(sql)
    assert significance(diff_snapshots(first, get_snapshot(conn, b))) == []
    with pytest.raises(KeyError):
        take_snapshot(conn, "absent")


def test_papers_screening_priority_and_no_count_signal(conn: sqlite3.Connection) -> None:
    seed(conn)
    a = get_snapshot(conn, take_snapshot(conn, "proj"))
    seed(conn, "q")
    b = get_snapshot(conn, take_snapshot(conn, "proj"))
    assert [p.id for p in diff_snapshots(a, b).added_papers] == ["q"]
    assert significance(diff_snapshots(a, b)) == []
    conn.execute("INSERT INTO paper_marks VALUES ('proj','q','relevant','now')")
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,status,started_at) "
                 "VALUES ('d','proj','search','done','now')")
    conn.executemany("INSERT INTO discovery_results(run_id,paper_id,score,n_hits,new_to_project,priority) "
                     "VALUES ('d',?,1,1,1,?)", [("p", .2), ("q", .9)])
    c = get_snapshot(conn, take_snapshot(conn, "proj"))
    assert [s.kind for s in significance(diff_snapshots(a, c))] == ["important_paper"]
    assert diff_snapshots(b, c).screening_changes[0].paper_id == "q"
    assert significance(diff_snapshots(b, c)) == []  # existing paper newly marked is not a new paper
    conn.execute("DELETE FROM project_papers WHERE paper_id='p'")
    assert [p.id for p in diff_snapshots(c, get_snapshot(conn, take_snapshot(conn, "proj"))).removed_papers] == ["p"]


def test_matrix_and_answer_evidence_changes(conn: sqlite3.Connection) -> None:
    span = seed(conn)
    matrix = create_matrix(conn, "proj", "m", "gain", [MatrixColumn(
        key="gain", label="Gain", instruction="gain", type="text")])
    gw, _ = gateway(conn, {"status": "reported", "value": "gain", "evidence_ids": [span.id]})
    extract_matrix(conn, gw, matrix.id)
    gw, _ = gateway(conn)
    answer = ask(conn, gw, "gain", "proj")
    a = get_snapshot(conn, take_snapshot(conn, "proj"))
    conn.execute("UPDATE matrix_cells SET value_json='\"different\"'")
    conn.execute("UPDATE answer_sentences SET evidence_ids_json='[]' WHERE run_id=?", (answer.run_id,))
    b = get_snapshot(conn, take_snapshot(conn, "proj"))
    delta = diff_snapshots(a, b)
    assert delta.matrix_changes[0].column_key == "gain"
    assert delta.answer_changes[0].question == "gain"
    assert {s.kind for s in significance(delta)} == {"matrix_value", "answer_evidence"}
    conn.execute("UPDATE matrix_cells SET evidence_ids_json='[]'")
    c = get_snapshot(conn, take_snapshot(conn, "proj"))
    assert diff_snapshots(b, c).matrix_changes and not significance(diff_snapshots(b, c))


def test_cluster_growth_matches_members_despite_id_change(conn: sqlite3.Connection) -> None:
    from lens_fakes import evidence_id
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="gain"))
    gw, _ = gateway(conn, {"claims": [{"text": "Gain improves outcomes", "type": "finding",
                                     "polarity": "positive", "evidence_ids": []}]})
    from litforge.screening.llm import FakeLlm
    gw.providers["local"] = FakeLlm(lambda m: {"claims": [{"text": "Gain improves outcomes", "type": "finding",
        "polarity": "positive", "evidence_ids": [evidence_id(m)]}]})
    extract_claims(conn, gw, "proj")
    cluster_claims(conn, "proj")
    a = get_snapshot(conn, take_snapshot(conn, "proj"))
    assert len(diff_snapshots(a, a).single_source_claims) == 1
    seed(conn, "q")
    extract_claims(conn, gw, "proj", ["q"])
    cluster_claims(conn, "proj")
    gw, _ = gateway(conn, {"relation": "replicates", "explanation": "Same"})
    classify_relations(conn, gw, "proj")
    b = get_snapshot(conn, take_snapshot(conn, "proj"))
    delta = diff_snapshots(a, b)
    assert delta.cluster_changes[0].direction == "strengthened"
    assert delta.cluster_changes[0].strength_after - delta.cluster_changes[0].strength_before == 1
    assert [s.kind for s in significance(delta)] == ["cluster_strength"]
    conn.execute("UPDATE claim_relations SET relation='contradicts'")
    c = get_snapshot(conn, take_snapshot(conn, "proj"))
    delta = diff_snapshots(b, c)
    assert delta.cluster_changes[0].direction == "weakened"
    assert {s.kind for s in significance(delta)} == {"cluster_strength", "contradiction"}
    assert json.loads(c.model_dump_json())["data"]["relations"]


def test_reported_cell_addition_removal_and_status_transitions(conn: sqlite3.Connection) -> None:
    span = seed(conn)
    a = get_snapshot(conn, take_snapshot(conn, "proj"))
    matrix = create_matrix(conn, "proj", "m", "gain", [MatrixColumn(
        key="gain", label="Gain", instruction="gain", type="text")])
    gw, _ = gateway(conn, {"status": "reported", "value": "gain", "evidence_ids": [span.id]})
    extract_matrix(conn, gw, matrix.id)
    b = get_snapshot(conn, take_snapshot(conn, "proj"))
    assert [s.kind for s in significance(diff_snapshots(a, b))] == ["matrix_value"]
    conn.execute("DELETE FROM matrix_cells")
    c = get_snapshot(conn, take_snapshot(conn, "proj"))
    assert [s.kind for s in significance(diff_snapshots(b, c))] == ["matrix_value"]


def test_answer_reorder_is_not_evidence_change_and_removal_is_correct(conn: sqlite3.Connection) -> None:
    from litforge.analysis.snapshots import AnswerState, SentenceState
    seed(conn)
    a = get_snapshot(conn, take_snapshot(conn, "proj"))
    sentences = [SentenceState(text="A", kind="reported_fact", evidence_ids=["ev_a"]),
                 SentenceState(text="B", kind="reported_fact", evidence_ids=["ev_b"])]
    a.data.answers = [AnswerState(question="Q", sentences=sentences)]
    b = a.model_copy(deep=True)
    b.data.answers[0].sentences.reverse()
    assert not diff_snapshots(a, b).answer_changes
    b.data.answers[0].sentences = [sentences[1]]
    changed = diff_snapshots(a, b).answer_changes
    assert len(changed) == 1 and changed[0].text == "A" and changed[0].before == ["ev_a"]


def test_percentile_boundary_marks_precedence_and_cross_project(conn: sqlite3.Connection) -> None:
    from litforge.analysis.snapshots import PaperState
    seed(conn)
    a = get_snapshot(conn, take_snapshot(conn, "proj"))
    b = a.model_copy(deep=True)
    b.data.papers = [PaperState(id=str(i), priority=p, s1="include", s2=s2, mark=mark)
                     for i, (p, s2, mark) in enumerate([
                         (.1, None, None), (.2, None, None), (.3, None, None), (.4, "exclude", None)])]
    delta = diff_snapshots(a, b)
    assert delta.priority_p75 == .3
    assert len(significance(delta)) == 1 and "2" in significance(delta)[0].message
    b.data.papers[-1].mark = "relevant"
    assert len(significance(diff_snapshots(a, b))) == 2
    b.project_id = "other"
    with pytest.raises(ValueError, match="same project"):
        diff_snapshots(a, b)


def test_removed_project_paper_no_longer_contributes_cluster_strength(conn: sqlite3.Connection) -> None:
    from lens_fakes import evidence_id

    from litforge.screening.llm import FakeLlm
    seed(conn)
    seed(conn, "q")
    save_criteria(conn, "proj", Criteria(question="gain"))
    gw, _ = gateway(conn)
    gw.providers["local"] = FakeLlm(lambda m: {"claims": [{"text": "Gain improves outcomes", "type": "finding",
        "polarity": "positive", "evidence_ids": [evidence_id(m)]}]})
    extract_claims(conn, gw, "proj")
    cluster_claims(conn, "proj")
    gw, _ = gateway(conn, {"relation": "replicates", "explanation": "Same"})
    classify_relations(conn, gw, "proj")
    a = get_snapshot(conn, take_snapshot(conn, "proj"))
    conn.execute("DELETE FROM project_papers WHERE paper_id='q'")
    b = get_snapshot(conn, take_snapshot(conn, "proj"))
    assert len(b.data.claims) == 1 and b.data.clusters[0].supporting == 1
    assert diff_snapshots(a, b).cluster_changes[0].direction == "weakened"


def test_merging_single_source_clusters_is_regrouping_not_weakening(conn: sqlite3.Connection) -> None:
    from lens_fakes import evidence_id

    from litforge.screening.llm import FakeLlm
    first, second = seed(conn, "p"), seed(conn, "q")
    save_criteria(conn, "proj", Criteria(question="gain"))
    names = {first.id: "First finding", second.id: "Second finding"}
    gw, _ = gateway(conn)
    gw.providers["local"] = FakeLlm(lambda m: {"claims": [{"text": names[evidence_id(m)], "type": "finding",
        "polarity": "positive", "evidence_ids": [evidence_id(m)]}]})
    extract_claims(conn, gw, "proj")
    cluster_claims(conn, "proj", similarity=lambda a, b: 0)
    a = get_snapshot(conn, take_snapshot(conn, "proj"))
    bridge = seed(conn, "t")
    names[bridge.id] = "Bridge finding"
    extract_claims(conn, gw, "proj", ["t"])
    cluster_claims(conn, "proj", similarity=lambda a, b: 1 if "bridge" in a or "bridge" in b else 0)
    delta = diff_snapshots(a, get_snapshot(conn, take_snapshot(conn, "proj")))
    assert [c.direction for c in delta.cluster_changes] == ["regrouped"]
    assert significance(delta) == []
