from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from litforge.cli import app
from litforge.config import Paths
from litforge.export.markdown import export_markdown
from litforge.parsing import run
from litforge.ui import api


@pytest.mark.parametrize("installed", ["none", "mineru", "docling"])
def test_cli_parser_status_display(conn: sqlite3.Connection, paths: Paths, corpus: Path,
                                   monkeypatch: pytest.MonkeyPatch, installed: str) -> None:
    # Force structural routing while keeping the fast output, so all three commands see it.
    (paths.root / "config.yaml").write_text(
        "parsing:\n  complex_fallback: none\n  router:\n    max_table_count: 0\n")
    from litforge.parsing import docling_parser, mineru_parser
    monkeypatch.setattr(mineru_parser, "find_command", lambda *a: "fake-mineru" if installed == "mineru" else None)
    monkeypatch.setattr(docling_parser, "docling_available", lambda: installed == "docling")
    monkeypatch.setattr(run, "find_command", mineru_parser.find_command)
    monkeypatch.setattr(run, "docling_available", docling_parser.docling_available)
    runner = CliRunner()
    base = ["--data-dir", str(paths.root)]
    commands = [["add", str(corpus / "single_column.pdf")], ["parse"], ["status"]]
    for command in commands:
        result = runner.invoke(app, base + command)
        assert result.exit_code == 0, result.output
        assert "needs accurate parser" in result.output
        assert "needs_docling" not in result.output
        hint = "install with `litforge parse install-mineru`"
        assert (hint in result.output) == (installed == "none")
    row = conn.execute("SELECT status,route FROM parse_runs WHERE is_current=1").fetchone()
    assert tuple(row) == ("needs_docling", "needs_docling")
    # The UI receives a display label while the underlying status stays compatible.
    paper_id = conn.execute("SELECT id FROM papers").fetchone()[0]
    detail = api.paper_detail(conn, paths, paper_id, None)
    assert detail["parse_status_label"] == "needs accurate parser"
    assert (hint in detail["parse_status_hint"]) == (installed == "none")
    project_id = conn.execute("SELECT id FROM projects").fetchone()[0]
    exported, count = export_markdown(conn, paths, "default", project_id)
    assert count == 1
    readme = (exported / "README.md").read_text()
    assert "needs accurate parser" in readme and "needs_docling" not in readme
    assert (hint in readme) == (installed == "none")
    # Front matter is machine-readable (vault queries), so it keeps the stored value.
    note = next(exported.glob("knowledge_base/*.md")).read_text()
    assert "parse_status: needs_docling" in note
    assert (hint in note) == (installed == "none")
