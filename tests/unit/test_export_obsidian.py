"""Obsidian exports link only selected notes and preserve the default export bytes."""
import json
from pathlib import Path

import pytest
import yaml

from litforge.export.markdown import export_markdown
from tests.unit.test_screening_status import mark, paper, project, result, versions


def seed(conn):
    project(conn)
    for pid, title in [("a", "Alpha"), ("b", "Beta"), ("c", "پژوهش"), ("d", None)]:
        paper(conn, pid)
        conn.execute("UPDATE papers SET title=? WHERE id=?", (title, pid))


def notes(conn, paths, **kwargs):
    root, _ = export_markdown(conn, paths, "Review", "p", **kwargs)
    manifest = json.loads((root / "manifest.json").read_text())
    return root, {r["id"]: (root / "knowledge_base" / (r["slug"] + ".md")).read_text() for r in manifest}


def test_obsidian_links_only_to_exported_notes(conn, paths):
    seed(conn)
    for a, b in [("a", "b"), ("b", "a"), ("a", "c"), ("a", "a")]:
        conn.execute("INSERT INTO paper_relations VALUES (?,?,'test','now')", (a, b))
    _, texts = notes(conn, paths, paper_ids=["a", "b", "d"], obsidian=True)
    assert texts["a"].split("## Links in this export\n\n")[1] == "[[beta-b]]\n"
    assert texts["b"].split("## Links in this export\n\n")[1] == "[[alpha-a]]\n"
    assert "## Links in this export" not in texts["d"]
    assert "[[paper-c]]" not in "".join(texts.values())


def test_obsidian_off_output_unchanged(conn, paths, tmp_path):
    seed(conn)
    # Frozen bytes from the exporter before the Obsidian option was introduced.
    baseline = Path(__file__).parent / "fixtures" / "export_markdown_default.json"
    expected = json.loads(baseline.read_text())
    for options in ({}, {"obsidian": False}):
        root, _ = notes(conn, paths, out_dir=tmp_path / "export", **options)
        actual = {str(p.relative_to(root)): p.read_bytes().decode("utf-8") for p in root.rglob("*") if p.is_file()}
        assert actual == expected


def test_status_tag_in_front_matter(conn, paths):
    seed(conn)
    result(conn, "a", "exclude")
    mark(conn, "a", "relevant")
    result(conn, "b", "include", "S2")
    mark(conn, "b", "irrelevant")
    result(conn, "c", "uncertain")
    paper(conn, "duplicate")
    versions(conn, "a", "duplicate")
    mark(conn, "duplicate", "relevant")
    _, texts = notes(conn, paths, all_versions=True, obsidian=True)
    for pid, status in [("a", "included"), ("b", "excluded"), ("c", "uncertain"),
                        ("d", "unscreened"), ("duplicate", "unscreened")]:
        front = yaml.safe_load(texts[pid].split("---\n")[1])
        assert front["tags"] == ["litforge/" + status]
        if pid in {"d", "duplicate"}:
            assert "aliases" not in front
        else:
            assert front["aliases"] == [front["title"]]


def test_shared_references_link(conn, paths):
    seed(conn)
    for pid, refs in [("a", ["external-1", "external-2", "external-3"]),
                      ("b", ["external-1", "external-2", "external-3"]),
                      ("c", ["external-1", "external-2"]),
                      ("d", ["external-1", "external-2", "external-3"])]:
        conn.executemany("INSERT INTO paper_references VALUES (?,?)", [(pid, ref) for ref in refs])
    _, texts = notes(conn, paths, paper_ids=["a", "b", "c"], obsidian=True)
    assert texts["a"].split("## Links in this export\n\n")[1] == "[[beta-b]]\n"
    assert texts["b"].split("## Links in this export\n\n")[1] == "[[alpha-a]]\n"
    assert "## Links in this export" not in texts["c"]


def test_readme_wiki_links(conn, paths):
    seed(conn)
    root, _ = notes(conn, paths, obsidian=True)
    text = (root / "README.md").read_text()
    manifest = json.loads((root / "manifest.json").read_text())
    for row in manifest:
        assert "[[" + row["slug"] + "]]" in text
    assert "](knowledge_base/" not in text


@pytest.mark.parametrize("fetch", [False, True])
def test_cli_obsidian_json(conn, paths, fetch):
    from typer.testing import CliRunner

    from litforge.cli import app

    seed(conn)
    conn.commit()
    args = ["--data-dir", str(paths.root), "export", "-p", "Review", "--obsidian", "--json"]
    response = CliRunner().invoke(app, [*args, "--fetch"] if fetch else args)
    assert response.exit_code == 0, response.output
    report = json.loads(response.stdout)
    assert report["papers"] == 4
    assert "[[alpha-a]]" in (Path(report["path"]) / "README.md").read_text()
    if fetch:
        assert "downloaded 0 of 4" in response.stderr
