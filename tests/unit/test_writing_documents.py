from __future__ import annotations

import os
from pathlib import Path

import pytest

from litforge.config import Paths
from litforge.projects import create_project
from litforge.storage.db import open_db
from litforge.writing import documents as wd
from litforge.writing.documents import DocumentError, SaveConflict


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "data", exports_root=tmp_path / "exports")
    paths.ensure()
    conn = open_db(paths.db)
    create_project(conn, paths, "demo")
    yield conn, paths
    conn.close()


@pytest.mark.parametrize("template", ["article", "persian"])
def test_create_from_templates(env, template: str) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "My Paper", template)
    assert doc.folder == paths.project_dir("demo") / "writing" / "my-paper"
    names = {f["name"] for f in wd.list_files(doc)}
    assert {"main.tex", "references.bib"} <= names
    if template == "persian":
        assert "fonts/Vazirmatn-Regular.ttf" in names
        assert "xepersian" in wd.read_file(doc, "main.tex")[0]
        # English references must run left to right inside Persian text (found in the real run, 2026-10-19).
        assert "\\bibliographystyle{plain-fa}" in wd.read_file(doc, "main.tex")[0]
    second = wd.create_document(conn, paths, "demo", "My Paper", template)
    assert second.slug == "my-paper-2"
    assert [d.id for d in wd.list_documents(conn, paths, "demo")].count(doc.id) == 1


def test_persian_name_gets_fallback_slug(env) -> None:
    conn, paths = env
    assert wd.create_document(conn, paths, "demo", "مقاله‌ی من", "persian").slug == "document"


def test_save_conflict_detection(env) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "d")
    text, h = wd.read_file(doc, "main.tex")
    h2 = wd.save_file(conn, doc, "main.tex", text + "% tab one\n", h)
    with pytest.raises(SaveConflict) as err:
        wd.save_file(conn, doc, "main.tex", text + "% tab two\n", h)  # second tab still holds the old hash
    assert err.value.current_hash == h2
    assert wd.read_file(doc, "main.tex")[0].endswith("% tab one\n")
    assert wd.save_file(conn, doc, "main.tex", text + "% tab one\n", h) == h2  # retried identical save is fine
    assert wd.save_file(conn, doc, "notes.tex", "x", None)
    with pytest.raises(SaveConflict):
        wd.save_file(conn, doc, "notes.tex", "y", None)


def test_atomic_save_keeps_old_text_on_failure(env, monkeypatch) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "d")
    text, h = wd.read_file(doc, "main.tex")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(wd.os, "replace", boom)
    with pytest.raises(OSError):
        wd.save_file(conn, doc, "main.tex", "new", h)
    monkeypatch.undo()
    assert wd.read_file(doc, "main.tex") == (text, h)
    assert not [p for p in doc.folder.iterdir() if p.name.endswith(".tmp")]


def test_snapshot_rotation_and_restore(env, monkeypatch) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "d")
    monkeypatch.setattr(wd, "SNAPSHOT_EVERY_S", 0)
    text, h = wd.read_file(doc, "main.tex")
    for i in range(35):
        h = wd.save_file(conn, doc, "main.tex", f"v{i}", h)
    snaps = wd.list_snapshots(doc)
    assert len(snaps) == wd.SNAPSHOT_KEEP
    target = next(s for s in snaps if (doc.folder / ".snapshots" / s["id"] / "main.tex").read_text() == "v10")
    wd.restore_snapshot(conn, doc, target["id"])
    assert wd.read_file(doc, "main.tex")[0] == "v10"
    newest = wd.list_snapshots(doc)[0]
    assert newest["note"].startswith("before restoring")
    assert (doc.folder / ".snapshots" / newest["id"] / "main.tex").read_text() == "v34"
    with pytest.raises(DocumentError):
        wd.restore_snapshot(conn, doc, "../../x")


def test_snapshot_throttle(env) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "d")
    _, h = wd.read_file(doc, "main.tex")
    for i in range(5):
        h = wd.save_file(conn, doc, "main.tex", f"v{i}", h)
    assert len(wd.list_snapshots(doc)) == 1


@pytest.mark.parametrize("name", ["../x.tex", "/etc/passwd", "a/../../x.tex", ".snapshots/x.tex", "a\\b.tex",
                                  "", "figures/../../../x.tex"])
def test_traversal_rejected(env, name: str) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "d")
    with pytest.raises(DocumentError):
        wd.save_file(conn, doc, name, "x", None)


def test_symlink_rejected(env, tmp_path: Path) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "d")
    outside = tmp_path / "outside.tex"
    outside.write_text("secret")
    os.symlink(outside, doc.folder / "link.tex")
    os.symlink(tmp_path, doc.folder / "dir")
    for name in ("link.tex", "dir/outside.tex"):
        with pytest.raises(DocumentError):
            wd.read_file(doc, name)
        with pytest.raises(DocumentError):
            wd.save_file(conn, doc, name, "x", None)
    assert outside.read_text() == "secret"


def test_upload_figure(env) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "d")
    assert wd.upload_figure(doc, "../../My Plot!.PNG", b"\x89PNG") == "figures/My-Plot.png"
    assert (doc.folder / "figures" / "My-Plot.png").read_bytes() == b"\x89PNG"
    with pytest.raises(DocumentError):
        wd.upload_figure(doc, "evil.sh", b"x")


def test_delete_moves_to_trash(env) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "d")
    target = wd.delete_document(conn, paths, doc.id)
    assert target.parent == paths.project_dir("demo") / "trash" and (target / "main.tex").is_file()
    assert not doc.folder.exists() and wd.list_documents(conn, paths, "demo") == []


def test_restoring_the_oldest_snapshot_survives_rotation(env, monkeypatch) -> None:
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "d")
    monkeypatch.setattr(wd, "SNAPSHOT_EVERY_S", 0)
    _, h = wd.read_file(doc, "main.tex")
    for i in range(40):
        h = wd.save_file(conn, doc, "main.tex", f"v{i}", h)
    oldest = wd.list_snapshots(doc)[-1]
    expected = (doc.folder / ".snapshots" / oldest["id"] / "main.tex").read_text()
    assert "main.tex" in wd.restore_snapshot(conn, doc, oldest["id"])
    assert wd.read_file(doc, "main.tex")[0] == expected
