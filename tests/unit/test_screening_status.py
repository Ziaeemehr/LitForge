"""Final decisions use the migrated library and exact public status values."""
import json

import pytest

from litforge.analysis.runs import start_run
from litforge.screening.status import PaperStatus, final_status


def project(conn):
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('p','Review','now')")
    return "p"


def paper(conn, pid, source="discovery"):
    conn.execute("INSERT INTO papers(id,created_at,updated_at) VALUES (?,'now','now')", (pid,))
    conn.execute("INSERT INTO project_papers VALUES ('p',?,?,'now')", (pid, source))


def result(conn, pid, decision, stage="S1", reason="model reason", exclusion=None):
    payload = json.dumps(dict(decision=decision, score=0.5, relevance=3, inclusion=[],
                              exclusion=exclusion or [], reason=reason))
    if stage == "S1":
        conn.execute("INSERT INTO screening_results(project_id,paper_id,decision,score,result_json,model,"
                     "prompt_version,criteria_hash,fingerprint,screened_at) "
                     "VALUES ('p',?,?,0.5,?,'fake','v','c','f','now')", (pid, decision, payload))
    else:
        run = start_run(conn, "screen_fulltext", "p", {}, {})
        conn.execute("INSERT INTO fulltext_screening(project_id,paper_id,decision,score,result_json,"
                     "evidence_ids_json,model,prompt_version,criteria_hash,fingerprint,run_id,screened_at) "
                     "VALUES ('p',?,?,0.5,?,'[]','fake','v','c','f',?,'now')", (pid, decision, payload, run))


def mark(conn, pid, value):
    conn.execute("INSERT INTO paper_marks VALUES ('p',?,?,'now')", (pid, value))


def versions(conn, primary, other):
    for pid in (primary, other):
        conn.execute("INSERT INTO paper_versions VALUES ('group',?,'unknown','user',1,?,0,'now')",
                     (pid, int(pid == primary)))


def test_human_relevant_wins_s1(conn):
    project(conn)
    paper(conn, "a")
    result(conn, "a", "exclude")
    mark(conn, "a", "relevant")
    assert final_status(conn, "p") == {"a": PaperStatus(status="included", stage="S1", source="human",
                                                      reason="included by reviewer")}


def test_human_irrelevant_wins_s2(conn):
    project(conn)
    paper(conn, "a")
    result(conn, "a", "include", "S2")
    mark(conn, "a", "irrelevant")
    assert final_status(conn, "p") == {"a": PaperStatus(status="excluded", stage="S2", source="human",
                                                      reason="excluded by reviewer")}


def test_s2_wins_s1(conn):
    project(conn)
    paper(conn, "a")
    result(conn, "a", "exclude")
    result(conn, "a", "uncertain", "S2")
    assert final_status(conn, "p") == {"a": PaperStatus(status="uncertain", stage="S2", source="model",
                                                      reason="model reason")}


def test_no_results_unscreened(conn):
    project(conn)
    paper(conn, "a")
    assert final_status(conn, "p") == {"a": PaperStatus(status="unscreened", stage="none", source="none", reason="")}


def test_only_primary_version(conn):
    project(conn)
    paper(conn, "a")
    paper(conn, "b")
    versions(conn, "a", "b")
    mark(conn, "b", "relevant")
    assert final_status(conn, "p") == {"a": PaperStatus(status="unscreened", stage="none", source="none", reason="")}


@pytest.mark.parametrize("stage", ["S1", "S2"])
@pytest.mark.parametrize("exclusion,reason,expected", [
    ([{"criterion": "unverified", "status": "violated", "verified": False},
      {"criterion": "wrong population", "status": "violated", "verified": True}], "fallback", "wrong population"),
    ([], "fallback", "fallback"), ([], "", "excluded"),
])
def test_exclusion_reason(conn, stage, exclusion, reason, expected):
    project(conn)
    paper(conn, "a")
    result(conn, "a", "exclude", stage, reason, exclusion)
    assert final_status(conn, "p")["a"] == PaperStatus(status="excluded", stage=stage, source="model", reason=expected)
