from __future__ import annotations

import io
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from litforge.config import Paths
from litforge.projects import create_project
from litforge.storage.db import open_db
from litforge.writing import documents as wd

TEX = b"\\documentclass{article}\n\\begin{document}Hello\\end{document}"


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "data", exports_root=tmp_path / "exports")
    paths.ensure()
    conn = open_db(paths.db)
    create_project(conn, paths, "demo")
    yield conn, paths
    conn.close()


def packed(files, compression=zipfile.ZIP_DEFLATED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=compression) as z:
        for name, data in files:
            z.writestr(name, data)
    return out.getvalue()


def load(env, data):
    from litforge.writing.archive import import_zip
    conn, paths = env
    return import_zip(conn, paths, "demo", "Imported Paper", data)


def test_round_trip(env):
    from litforge.writing.archive import export_zip
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "Original")
    (doc.folder / "figures" / "plot.png").write_bytes(b"image")
    (doc.folder / ".build").mkdir()
    (doc.folder / ".build" / "out.pdf").write_bytes(b"hidden")
    (doc.folder / "link.tex").symlink_to(doc.folder / "main.tex")
    data = export_zip(doc)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.namelist() == sorted(f["name"] for f in wd.list_files(doc))
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in z.infolist())
    imported = load(env, data)
    assert imported.template == "imported"
    assert (imported.folder / ".snapshots").is_dir()
    assert {f["name"]: (imported.folder / f["name"]).read_bytes() for f in wd.list_files(imported)} == {
        f["name"]: (doc.folder / f["name"]).read_bytes() for f in wd.list_files(doc)}


@pytest.mark.parametrize("prefix", ["", "repo-main/"])
@pytest.mark.parametrize("main", ["main.tex", "paper.tex", "chapters/paper.tex"])
def test_import_layout_and_main(env, prefix, main):
    doc = load(env, packed([(prefix + main, TEX), (prefix + "section.tex", b"body"),
                            ("__MACOSX/._main.tex", b"junk"), (prefix + ".DS_Store", b"junk")]))
    assert doc.main_file == main
    assert {f["name"] for f in wd.list_files(doc)} == {main, "section.tex", "references.bib"}
    assert (doc.folder / "references.bib").read_bytes() == b""


def test_main_preferred(env):
    assert load(env, packed([("a.tex", TEX), ("main.tex", TEX)])).main_file == "main.tex"


@pytest.mark.parametrize("name", ["../escape.tex", "/absolute.tex", "C:/drive.tex", "a\\b.tex", "a/../b.tex"])
def test_unsafe_paths(env, name):
    with pytest.raises(wd.DocumentError):
        load(env, packed([("main.tex", TEX), (name, TEX)]))


def test_symlink(env):
    info = zipfile.ZipInfo("link.tex")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(wd.DocumentError, match="symbolic"):
        load(env, packed([("main.tex", TEX), (info, b"/tmp/outside")]))


@pytest.mark.parametrize("data, message", [(b"bad", "ZIP"), (packed([("a.txt", b"x")]), "tex"),
                                           (packed([("a.tex", b"body")]), "documentclass")])
def test_invalid_and_cleanup(env, data, message):
    with pytest.raises(wd.DocumentError, match=message):
        load(env, data)
    conn, paths = env
    assert wd.list_documents(conn, paths, "demo") == []
    root = wd.writing_root(paths, "demo")
    assert not root.exists() or list(root.iterdir()) == []


def test_entry_limit(env):
    with pytest.raises(wd.DocumentError, match="2000"):
        load(env, packed([(f"f{i}.tex", TEX) for i in range(2001)]))


def test_ratio(env):
    with pytest.raises(wd.DocumentError, match="ratio"):
        load(env, packed([("main.tex", TEX + b"x" * (1024 * 1024 + 1))]))


def altered_size(data, size):
    data = bytearray(data)
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 24, size)
    struct.pack_into("<I", data, 22, size)
    return bytes(data)


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_lied_size(env, compression):
    with pytest.raises(wd.DocumentError):
        load(env, altered_size(packed([("main.tex", TEX)], compression), 1))


def test_header_file_limit(env):
    with pytest.raises(wd.DocumentError, match="50 MB"):
        load(env, altered_size(packed([("main.tex", TEX)]), 50 * 1024 * 1024 + 1))


def test_total_limit(env, monkeypatch):
    from litforge.writing import archive
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 100)
    with pytest.raises(wd.DocumentError, match="total"):
        load(env, packed([("main.tex", TEX), ("second.tex", TEX)]))


def test_nul_path(env):
    data = packed([("main.tex", TEX), ("badX.tex", TEX)]).replace(b"badX.tex", b"bad\x00.tex")
    with pytest.raises(wd.DocumentError, match="path"):
        load(env, data)


def test_total_header_limit(env):
    data = bytearray(packed([(f"file{i}.tex", TEX) for i in range(5)]))
    offset = 0
    for _ in range(5):
        offset = data.index(b"PK\x01\x02", offset)
        struct.pack_into("<I", data, offset + 24, 45 * 1024 * 1024)
        struct.pack_into("<I", data, offset + 20, 1024 * 1024)
        offset += 4
    with pytest.raises(wd.DocumentError, match="total"):
        load(env, bytes(data))


def test_cleanup_after_partial_extraction(env):
    with pytest.raises(wd.DocumentError):
        load(env, packed([("main.tex", TEX), ("a", b"file"), ("a/nested.txt", b"conflict")]))
    conn, paths = env
    assert wd.list_documents(conn, paths, "demo") == []
    assert list(wd.writing_root(paths, "demo").iterdir()) == []
    assert load(env, packed([("main.tex", TEX)])).slug == "imported-paper"


def test_existing_bibliography_and_unique_slug(env):
    data = packed([("main.tex", TEX), ("references.bib", b"@article{key}")])
    first = load(env, data)
    second = load(env, data)
    assert second.slug == first.slug + "-2"
    assert (second.folder / "references.bib").read_bytes() == b"@article{key}"


def test_crc_failure_cleanup(env):
    data = bytearray(packed([("main.tex", TEX)], zipfile.ZIP_STORED))
    data[data.index(TEX)] ^= 1
    with pytest.raises(wd.DocumentError, match="ZIP"):
        load(env, bytes(data))
    conn, paths = env
    assert wd.list_documents(conn, paths, "demo") == []
    assert list(wd.writing_root(paths, "demo").iterdir()) == []


def test_export_deep_files(env):
    from litforge.writing.archive import export_zip
    conn, paths = env
    doc = wd.create_document(conn, paths, "demo", "Deep")
    deep = doc.folder / "a/b/c/d/e.txt"
    deep.parent.mkdir(parents=True)
    deep.write_bytes(b"deep")
    with zipfile.ZipFile(io.BytesIO(export_zip(doc))) as archive:
        assert archive.read("a/b/c/d/e.txt") == b"deep"


def test_forged_size_and_crc_cannot_hide_extra_data(env):
    import zlib

    data = bytearray(altered_size(packed([("main.tex", TEX)]), 1))
    central = data.index(b"PK\x01\x02")
    crc = zlib.crc32(TEX[:1])
    struct.pack_into("<I", data, central + 16, crc)
    struct.pack_into("<I", data, 14, crc)
    # A normal ZipFile.read accepts the truncated prefix with these forged headers.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.read("main.tex") == TEX[:1]
    with pytest.raises(wd.DocumentError):
        load(env, bytes(data))


def test_non_candidate_main_uses_first_candidate(env):
    doc = load(env, packed([("main.tex", b"% section"), ("z.tex", TEX), ("a.tex", TEX)]))
    assert doc.main_file == "a.tex"


def test_invalid_utf8_filename(env):
    data = bytearray(packed([("main.tex", TEX)]))
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, 6, 0x800)
    struct.pack_into("<H", data, central + 8, 0x800)
    data[30] = 0xff
    data[central + 46] = 0xff
    with pytest.raises(wd.DocumentError, match="ZIP"):
        load(env, bytes(data))
