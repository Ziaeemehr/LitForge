from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from litforge.analysis.ledger import get_spans
from litforge.cli import app
from litforge.config import Paths
from litforge.indexing.fts import search
from litforge.llm.audit import ModelCallRecord, record_model_call


@pytest.fixture()
def library(paths: Paths, corpus: Path) -> tuple[CliRunner, list[str]]:
    runner = CliRunner()
    base = ["--data-dir", str(paths.root)]
    for args in [["new", "demo"], ["add", str(corpus / "single_column.pdf")]]:
        result = runner.invoke(app, base + args)
        assert result.exit_code == 0, result.output
    return runner, base


def test_ask_records_exact_evidence(conn: sqlite3.Connection, library: tuple[CliRunner, list[str]]) -> None:
    runner, base = library
    result = runner.invoke(app, base + ["ask", "Where is the zebrafinch marker reported?",
                                      "--evidence-only", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["spans"] and data["spans"][0]["page"] == 3
    span = get_spans(conn, [data["spans"][0]["id"]])[0]
    assert "zebrafinch" in span.text and span.parser and span.parser_version
    run = conn.execute("SELECT * FROM analysis_runs WHERE id=?", (data["run_id"],)).fetchone()
    assert run["task"] == "ask_evidence" and run["status"] == "ok" and run["project_id"]
    assert conn.execute("SELECT COUNT(*) FROM run_evidence WHERE run_id=?", (data["run_id"],)).fetchone()[0]
    text = runner.invoke(app, base + ["ask", "zebrafinch", "--evidence-only"])
    assert "[ev_" in text.output and "p.3" in text.output and "zebrafinch" in text.output
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
    assert runner.invoke(app, base + ["index", "--force"]).exit_code == 0
    assert get_spans(conn, [span.id])[0] == span


def test_ask_empty_and_phase_one(library: tuple[CliRunner, list[str]]) -> None:
    runner, base = library
    no_flag = runner.invoke(app, base + ["ask", "why?"])
    assert no_flag.exit_code == 0 and "No evidence found in the library" in no_flag.output
    result = runner.invoke(app, base + ["ask", "How is it?", "--evidence-only", "--json"])
    assert result.exit_code == 0 and json.loads(result.output)["spans"] == []


def test_eval_cli(conn: sqlite3.Connection, library: tuple[CliRunner, list[str]], tmp_path: Path) -> None:
    runner, base = library
    bench = tmp_path / "bench.yaml"
    bench.write_text('''name: synthetic
questions:
  - id: q1
    question: Where is the zebrafinch marker?
    relevant:
      - paper: 10.1234/SYNTHETIC.5678
        page: 3
        contains: zebrafinch
  - id: q2
    question: nonexistentxyz
    relevant:
      - paper: 10.1234/synthetic.5678
        page: 2
        contains: Jansen Rit
''')
    result = runner.invoke(app, base + ["eval", "retrieval", str(bench), "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["recall_at_k"] == report["hit_rate_at_k"] == report["mrr"] == 0.5
    assert conn.execute("SELECT status FROM analysis_runs WHERE id=?", (report["run_id"],)).fetchone()[0] == "ok"
    text = runner.invoke(app, base + ["eval", "retrieval", str(bench)])
    assert "0.500" in text.output and "q2" in text.output and "Jansen Rit" in text.output


def test_calls_cli(conn: sqlite3.Connection, paths: Paths) -> None:
    for run in ["r1", "r2"]:
        record_model_call(conn, ModelCallRecord(provider="fake", model="m", task="extract", input_hash="h",
                                               status="ok", run_id=run, input_tokens=4, output_tokens=2))
    runner = CliRunner()
    base = ["--data-dir", str(paths.root), "calls"]
    result = runner.invoke(app, base + ["--run", "r1", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1 and rows[0]["run_id"] == "r1" and rows[0]["input_tokens"] == 4
    assert len(json.loads(runner.invoke(app, base + ["--limit", "1", "--json"]).output)) == 1
    assert "fake" in runner.invoke(app, base).output


def test_question_retrieval_keeps_search_defaults(conn: sqlite3.Connection,
                                                library: tuple[CliRunner, list[str]]) -> None:
    from litforge.analysis.retrieve import retrieve_for_question

    runner, base = library
    question = "How are neural mass parameters inferred?"
    assert search(conn, question) == []
    assert 1 <= len(retrieve_for_question(conn, question, k=8, max_per_paper=1)) <= 1
    assert len(retrieve_for_question(conn, question, k=2)) == 2
    assert runner.invoke(app, base + ["new", "empty"]).exit_code == 0
    empty = runner.invoke(app, base + ["ask", question, "--evidence-only", "--json"])
    assert json.loads(empty.output)["spans"] == []
    explicit = runner.invoke(app, base + ["ask", question, "--evidence-only", "-p", "demo", "-k", "1", "--json"])
    assert len(json.loads(explicit.output)["spans"]) == 1


def test_invalid_benchmark_one_line_error(library: tuple[CliRunner, list[str]], tmp_path: Path) -> None:
    runner, base = library
    bench = tmp_path / "bad.yaml"
    bench.write_text("name: empty\nquestions: []\n")
    result = runner.invoke(app, base + ["eval", "retrieval", str(bench)])
    assert result.exit_code == 1 and len(result.output.splitlines()) == 1


def test_ask_uses_current_hit_page(conn: sqlite3.Connection, library: tuple[CliRunner, list[str]]) -> None:
    runner, base = library
    chunk = conn.execute("SELECT id FROM chunks LIMIT 1").fetchone()["id"]
    conn.execute("DELETE FROM chunks WHERE id != ?", (chunk,))
    conn.execute("UPDATE chunks SET text=?, page_start=1, page_end=2, bbox_json=? WHERE id=?",
                 ("alpha marker. beta marker.", json.dumps([
                     {"start": 0, "end": 14, "page": 1}, {"start": 14, "end": 26, "page": 2},
                 ]), chunk))
    ids = []
    for term, page in [("alpha", 1), ("beta", 2)]:
        result = runner.invoke(app, base + ["ask", term, "--evidence-only", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ids.append(data["spans"][0]["id"])
        assert data["spans"][0]["page"] == page
        run = conn.execute("SELECT result_json FROM analysis_runs WHERE id=?", (data["run_id"],)).fetchone()
        assert json.loads(run["result_json"]) == data
        linked = conn.execute("SELECT page FROM run_evidence WHERE run_id=?", (data["run_id"],)).fetchone()
        assert linked["page"] == page
        text = runner.invoke(app, base + ["ask", term, "--evidence-only"])
        assert text.exit_code == 0 and f"p.{page}" in text.output
    assert ids[0] == ids[1]
    assert get_spans(conn, [ids[0]])[0].page == 1
