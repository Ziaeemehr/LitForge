from __future__ import annotations

import json
import sqlite3

import pytest
from lens_fakes import evidence_id, gateway, seed

from litforge.analysis.ask import ask
from litforge.analysis.matrix import MatrixColumn, create_matrix, extract_matrix, get_matrix
from litforge.analysis.review import record_review, review_queue


def test_review_status_history_and_isolation(conn: sqlite3.Connection) -> None:
    seed(conn)
    gw, fake = gateway(conn, {"sentences": [{"text": "Unknown.", "kind": "unsupported", "evidence_ids": []}],
                             "insufficient": True})
    answer = ask(conn, gw, "gain", project_id="proj")
    assert len(review_queue(conn, "proj")) == 1 and review_queue(conn, "absent") == []
    target = answer.sentences[0].id
    record_review(conn, "sentence", target, "accept", reason="checked")
    assert review_queue(conn) == []
    record_review(conn, "sentence", target, "reject")
    record_review(conn, "sentence", target, "edit", new_value="Still unknown.")
    fake.answer = lambda m: {"status": "reported", "value": 99, "unit": None,
                             "evidence_ids": [evidence_id(m)], "note": ""}
    matrix = create_matrix(conn, "proj", "m", "gain", [MatrixColumn(key="gain", label="Gain", instruction="gain",
                                                                                type="number")])
    cell = extract_matrix(conn, gw, matrix.id).cells[0]
    assert len(review_queue(conn)) == 1
    record_review(conn, "cell", cell.id, "edit", new_value=3.25, reason="corrected")
    current = get_matrix(conn, matrix.id).cells[0]
    assert current.value == 3.25 and current.human_value == 3.25 and current.review_status == "edited"
    extract_matrix(conn, gw, matrix.id)
    assert get_matrix(conn, matrix.id).cells[0].value == 3.25
    history = conn.execute("SELECT * FROM human_reviews WHERE target_id=?", (cell.id,)).fetchone()
    assert json.loads(history["old_json"])["value"] == 99
    for sql in ["UPDATE human_reviews SET reason=\"forged\"", "DELETE FROM human_reviews",
                "INSERT OR REPLACE INTO human_reviews SELECT * FROM human_reviews"]:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)
    assert conn.execute("SELECT COUNT(*) FROM human_reviews").fetchone()[0] == 4
    with pytest.raises(KeyError):
        record_review(conn, "cell", "missing", "accept")
    with pytest.raises(ValueError):
        record_review(conn, "sentence", target, "edit")


def test_edits_invalidate_sentence_audit_and_keep_cell_override(conn: sqlite3.Connection) -> None:
    from litforge.analysis.ask import get_answer
    from litforge.analysis.audit import audit_answer

    seed(conn)
    gw, fake = gateway(conn)
    answer = ask(conn, gw, "gain")
    fake.answer = lambda _: {"verdict": "supported", "note": "checked"}
    audit_answer(conn, gw, answer.run_id)
    record_review(conn, "sentence", answer.sentences[0].id, "edit", new_value="Gain 99.")
    edited = get_answer(conn, answer.run_id).sentences[0]
    assert edited.audit_verdict is None and edited.flags == ["number_not_in_evidence"]
    fake.answer = lambda m: {"status": "reported", "value": 99, "evidence_ids": [evidence_id(m)]}
    matrix = create_matrix(conn, "proj", "m", "gain", [MatrixColumn(key="gain", label="Gain", instruction="gain",
                                                                                type="number")])
    cell = extract_matrix(conn, gw, matrix.id).cells[0]
    record_review(conn, "cell", cell.id, "edit", new_value=3.25)
    record_review(conn, "cell", cell.id, "accept")
    assert get_matrix(conn, matrix.id).cells[0].value == 3.25
    extract_matrix(conn, gw, matrix.id, force=True)
    assert get_matrix(conn, matrix.id).cells[0].value == 3.25


def test_new_extraction_does_not_accept_a_new_model_value(conn: sqlite3.Connection) -> None:
    seed(conn)
    gw, fake = gateway(conn)
    fake.answer = lambda m: {"status": "reported", "value": 3.25, "evidence_ids": [evidence_id(m)]}
    matrix = create_matrix(conn, "proj", "m", "gain",
                           [MatrixColumn(key="gain", label="Gain", instruction="gain", type="number")])
    cell = extract_matrix(conn, gw, matrix.id).cells[0]
    record_review(conn, "cell", cell.id, "accept")
    fake.answer = lambda m: {"status": "reported", "value": 99, "evidence_ids": [evidence_id(m)]}
    cell = extract_matrix(conn, gw, matrix.id, force=True).cells[0]
    assert cell.review_status == "open" and cell.needs_review


def test_queue_is_newest_first_even_with_equal_timestamps(conn: sqlite3.Connection) -> None:
    seed(conn)
    gw, _ = gateway(conn, {"sentences": [{"text": "Unknown.", "kind": "unsupported"}], "insufficient": True})
    first = ask(conn, gw, "gain")
    second = ask(conn, gw, "gain")
    conn.execute("UPDATE answer_sentences SET id='z_old',created_at='same' WHERE run_id=?", (first.run_id,))
    conn.execute("UPDATE answer_sentences SET id='a_new',created_at='same' WHERE run_id=?", (second.run_id,))
    assert [i.target_id for i in review_queue(conn)] == ["a_new", "z_old"]
