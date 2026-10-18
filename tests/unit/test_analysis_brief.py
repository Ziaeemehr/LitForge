from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from lens_fakes import gateway, seed

from litforge.analysis.ask import ask
from litforge.analysis.brief import write_brief


def test_brief_provenance_and_refusal(conn: sqlite3.Connection, tmp_path: Path) -> None:
    span = seed(conn)
    gw, _ = gateway(conn)
    answer = ask(conn, gw, "gain", project_id="proj")
    out = tmp_path / "brief.md"
    assert write_brief(conn, answer.run_id, out=out) == out
    text = out.read_text()
    for part in [span.id, "fake", "ask-v3", "lexical", "gain", "Audit", "Review", "p.1"]:
        assert part in text
    conn.execute("UPDATE answer_sentences SET evidence_ids_json='[]' WHERE run_id=?", (answer.run_id,))
    with pytest.raises(ValueError, match="evidence|label"):
        write_brief(conn, answer.run_id, out=out)
    conn.execute("UPDATE answer_sentences SET kind='interpretation' WHERE run_id=?", (answer.run_id,))
    write_brief(conn, answer.run_id, out=out)
    assert "interpretation — needs review" in out.read_text()


def test_brief_records_matrix_prompt(conn: sqlite3.Connection, tmp_path: Path) -> None:
    from litforge.analysis.matrix import MatrixColumn, create_matrix, extract_matrix

    seed(conn)
    gw, fake = gateway(conn)
    answer = ask(conn, gw, "gain")
    matrix = create_matrix(conn, "proj", "m", "gain",
                           [MatrixColumn(key="gain", label="Gain", instruction="gain", type="number")])
    fake.answer = lambda _: {"status": "not_reported"}
    extract_matrix(conn, gw, matrix.id)
    out = write_brief(conn, answer.run_id, matrix.id, out=tmp_path / "brief.md")
    assert "matrix-v2" in out.read_text() and "(not_reported)" in out.read_text()


@pytest.mark.parametrize("verdict", ["partial", "unsupported", "contradicted"])
def test_brief_labels_failed_audit(conn: sqlite3.Connection, tmp_path: Path, verdict: str) -> None:
    seed(conn)
    gw, _ = gateway(conn)
    answer = ask(conn, gw, "gain")
    conn.execute("UPDATE answer_sentences SET audit_verdict=?", (verdict,))
    text = write_brief(conn, answer.run_id, out=tmp_path / "brief.md").read_text()
    assert f"*(audit: {verdict} — needs review)*" in text.split("## Audit")[0]


def test_brief_rejected_and_edited_sentences(conn: sqlite3.Connection, tmp_path: Path) -> None:
    from litforge.analysis.review import record_review

    span = seed(conn)
    gw, _ = gateway(conn, {"sentences": [
        {"text": "Reject this.", "kind": "reported_fact", "evidence_ids": [span.id]},
        {"text": "Old text.", "kind": "reported_fact", "evidence_ids": [span.id]}], "insufficient": False})
    answer = ask(conn, gw, "gain")
    record_review(conn, "sentence", answer.sentences[0].id, "reject")
    record_review(conn, "sentence", answer.sentences[1].id, "edit", "Edited text.")
    text = write_brief(conn, answer.run_id, out=tmp_path / "brief.md").read_text()
    section = text.split("## Answer")[1].split("## ")[0]
    assert "Reject this." not in section and "Edited text." in section and "Old text." not in text
    assert "Reject this." in text.split("## Rejected by reviewer")[1]


def test_brief_untrusted_text_and_quote_limit(conn: sqlite3.Connection, tmp_path: Path) -> None:
    payload = "<script> [click](https://evil) `code` | **bold**"
    span = seed(conn, text="Results gain " + payload + " word" * 150)
    gw, _ = gateway(conn, {"sentences": [{"text": payload, "kind": "reported_fact", "evidence_ids": [span.id]}],
                           "insufficient": False})
    text = write_brief(conn, ask(conn, gw, "gain").run_id, out=tmp_path / "brief.md").read_text()
    assert "<script>" not in text and "[click](" not in text and "`code`" not in text
    quote = text.split(" — \"")[-1].rstrip().removesuffix("\"")
    assert quote.endswith("…") and not quote.endswith("wor…")
    assert len(quote) < 500


def test_brief_verified_quotes_are_under_sentence_and_escaped(conn: sqlite3.Connection, tmp_path: Path) -> None:
    from litforge.analysis.audit import audit_answer
    from litforge.analysis.markdown_safe import inert

    quote = '<script> [click](https://evil) `code` **bold**'
    span = seed(conn, text="Results gain " + quote)
    gw, _ = gateway(conn, {"sentences": [{"text": "Gain improves outcomes.", "kind": "reported_fact",
        "evidence_ids": [span.id], "quotes": [{"evidence_id": span.id, "text": quote}]}], "insufficient": False})
    answer = ask(conn, gw, "gain")
    auditor, _ = gateway(conn, {"verdict": "supported", "note": "checked"})
    audited = audit_answer(conn, auditor, answer.run_id)
    assert audited[0].quotes == answer.sentences[0].quotes
    text = write_brief(conn, answer.run_id, out=tmp_path / "brief.md").read_text().split("## Audit")[0]
    assert text.index("Gain improves outcomes.") < text.index("> " + inert(quote))
    assert f"[^{span.id}]" in text.split("\n> ", 1)[1]
    assert "<script>" not in text and "[click](" not in text
