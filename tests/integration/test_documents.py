"""Documents other than PDF (Word, PowerPoint, Excel, HTML, EPUB, Markdown, text): read with MarkItDown into the
same canonical document as a PDF, so search, export and the AI features work on them unchanged."""

from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path

import pytest

from litforge.config import Config, Paths
from litforge.export.markdown import export_markdown
from litforge.parsing.canonical import read_canonical
from litforge.parsing.markitdown_parser import document_from_markdown, is_document
from litforge.pipeline import add_pdfs
from litforge.projects import create_project
from litforge.ui import api

pytest.importorskip("markitdown")

MD = """# Coupling in whole-brain models

## Abstract

We tune the global coupling gain.

## Results

The gain was 3.25 across 40 subjects.

| Gain | Fit |
|---|---|
| 3.25 | 0.61 |
"""


def _docx(path: Path, paragraphs: list[tuple[str, str]]) -> Path:
    """A minimal Word file: (style, text) paragraphs, style 'Title', 'Heading1' or ''."""
    body = "".join(
        f'<w:p>{f"<w:pPr><w:pStyle w:val=\"{style}\"/></w:pPr>" if style else ""}<w:r><w:t>{text}</w:t></w:r></w:p>'
        for style, text in paragraphs)
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'relationships"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/'
                   '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {ns}><w:body>{body}</w:body></w:document>')
    return path


def test_markdown_becomes_sections_with_a_title() -> None:
    doc, title = document_from_markdown(MD, "markitdown", "0")
    assert title == "Coupling in whole-brain models"
    assert [(s.heading, s.kind) for s in doc.sections] == [
        ("Coupling in whole-brain models", "front"), ("Abstract", "abstract"), ("Results", "body")]
    results = doc.sections[2].paragraphs
    assert results[0].text == "The gain was 3.25 across 40 subjects."
    assert results[1].text.startswith("| Gain | Fit |")  # a table stays one block
    assert doc.page_count == 1 and all(p.page == 1 for s in doc.sections for p in s.paragraphs)


def test_document_types() -> None:
    assert all(is_document(Path(f"a{s}")) for s in (".docx", ".pptx", ".xlsx", ".html", ".epub", ".md", ".txt"))
    assert not is_document(Path("a.pdf")) and not is_document(Path("a.exe"))


def test_documents_are_added_read_exported_and_not_served_as_pdf(conn: sqlite3.Connection, paths: Paths,
                                                                 config: Config, tmp_path: Path) -> None:
    pid = create_project(conn, paths, "docs")
    folder = tmp_path / "mine"
    folder.mkdir()
    (folder / "notes.md").write_text(MD, encoding="utf-8")
    _docx(folder / "draft.docx", [("Title", "Neural mass models"), ("Heading1", "Methods"),
                                  ("", "We fitted the model to the recordings.")])
    (folder / "page.html").write_text("<html><head><title>A web page</title></head><body><h1>A web page</h1>"
                                      "<p>Text from the web.</p></body></html>", encoding="utf-8")
    (folder / "program.exe").write_bytes(b"MZ")  # not a document: ignored

    report, results, parsed = add_pdfs(conn, paths, config, pid, [folder], "my documents")
    assert sorted(Path(r.path).name for r in results) == ["draft.docx", "notes.md", "page.html"]
    assert {p.status for p in parsed} == {"ok"} and {p.parser for p in parsed} == {"markitdown"}
    titles = sorted(r["title"] for r in conn.execute(
        "SELECT p.title FROM project_papers pp JOIN papers p ON p.id = pp.paper_id WHERE pp.project_id=?", (pid,)))
    assert titles == ["A web page", "Coupling in whole-brain models", "Neural mass models"]

    row = conn.execute("""SELECT a.paper_id, a.pdf_path, r.output_dir FROM artifacts a
                          JOIN parse_runs r ON r.artifact_id = a.id AND r.is_current = 1
                          JOIN papers p ON p.id = a.paper_id WHERE p.title = 'Neural mass models'""").fetchone()
    assert row["pdf_path"].endswith(".docx")  # kept with its own extension
    assert "We fitted the model" in read_canonical(paths.root / row["output_dir"]).full_text()
    assert api._pdf_path(conn, paths, row["paper_id"]) is None  # never served or shown as a PDF

    out, count = export_markdown(conn, paths, "docs", pid, config=config)
    assert count == 3
    notes = "".join(p.read_text(encoding="utf-8") for p in (out / "knowledge_base").glob("*.md"))
    assert "We fitted the model to the recordings." in notes and "Text from the web." in notes
    assert "None" not in notes and "None" not in (out / "README.md").read_text()  # an unknown version is left out
    assert not list((out.parent / "pdfs").glob("*.pdf"))  # no Word file renamed to .pdf
    assert len(list((out.parent / "pdfs").glob("*.docx"))) == 1

    again, results2, _ = add_pdfs(conn, paths, config, pid, [folder / "notes.md"], "again")
    assert [r.status for r in results2] == ["duplicate"]
