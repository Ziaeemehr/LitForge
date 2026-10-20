"""Writing Studio documents: plain folders under the project, atomic saves, snapshots and figures."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from litforge.config import Paths
from litforge.projects import get_project_id
from litforge.storage.db import new_id, now, transaction

TEMPLATES = {"article": "English article", "persian": "Persian article (xepersian)"}
TEXT_SUFFIXES = {".tex", ".bib", ".sty", ".cls", ".bst", ".txt", ".md"}
FIGURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg"}
MAX_FIGURE_BYTES = 25 * 1024 * 1024
MAX_TEXT_BYTES = 5 * 1024 * 1024
SNAPSHOT_EVERY_S = 120
SNAPSHOT_KEEP = 30
_SAFE_PART = re.compile(r"^[^\x00-\x1f/\\:*?\"<>|]+$")
_locks: dict[str, threading.RLock] = {}  # reentrant: a restore takes a safety snapshot while holding it
_locks_guard = threading.Lock()


class DocumentError(ValueError):
    pass


class SaveConflict(DocumentError):
    """The file changed on disk since the editor read it."""

    def __init__(self, current_hash: str | None) -> None:
        super().__init__("the file changed since it was opened; reload it or keep your version")
        self.current_hash = current_hash


@dataclass(frozen=True)
class Document:
    id: str
    project_id: str
    project: str
    name: str
    slug: str
    template: str
    engine: str
    folder: Path
    created_at: str
    updated_at: str
    main_file: str = "main.tex"
    draft: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "project": self.project, "name": self.name, "slug": self.slug,
                "template": self.template, "engine": self.engine, "created_at": self.created_at,
                "updated_at": self.updated_at, "main_file": self.main_file, "draft": self.draft}

    @property
    def output_stem(self) -> str:
        """The name the engine gives its output: .build/<stem>.pdf, .log and .synctex.gz."""
        return Path(self.main_file).stem


def _lock(doc_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(doc_id, threading.RLock())


def text_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def writing_root(paths: Paths, project: str) -> Path:
    return paths.project_dir(project) / "writing"


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50].rstrip("-")
    return slug or "document"


def resolve_inside(folder: Path, name: str, *, allow_hidden: bool = False) -> Path:
    """Resolve a relative file name inside `folder`; refuses absolute paths, `..`, odd characters and symlinks."""
    if not name or "\\" in name or "\x00" in name:
        raise DocumentError("invalid file name")
    rel = PurePosixPath(name)
    if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts) or len(rel.parts) > 4:
        raise DocumentError("the file must be inside the document folder")
    if not allow_hidden and any(part.startswith(".") for part in rel.parts):
        raise DocumentError("hidden files are managed by LitForge")
    if not all(_SAFE_PART.match(part) for part in rel.parts):
        raise DocumentError("invalid file name")
    root = folder.resolve()
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise DocumentError("symbolic links are not followed")
    target = (root / Path(*rel.parts)).resolve()
    if not target.is_relative_to(root):
        raise DocumentError("the file must be inside the document folder")
    return target


def _row_to_doc(paths: Paths, row: sqlite3.Row) -> Document:
    return Document(id=row["id"], project_id=row["project_id"], project=row["project"], name=row["name"],
                    slug=row["slug"], template=row["template"], engine=row["engine"],
                    folder=writing_root(paths, row["project"]) / row["slug"], created_at=row["created_at"],
                    updated_at=row["updated_at"], main_file=row["main_file"], draft=bool(row["draft"]))


_SELECT = """SELECT d.*, p.name AS project FROM writing_documents d JOIN projects p ON p.id=d.project_id"""


def get_document(conn: sqlite3.Connection, paths: Paths, doc_id: str) -> Document:
    row = conn.execute(_SELECT + " WHERE d.id=?", (doc_id,)).fetchone()
    if row is None:
        raise LookupError("document not found")
    return _row_to_doc(paths, row)


def list_documents(conn: sqlite3.Connection, paths: Paths, project: str) -> list[Document]:
    pid = get_project_id(conn, project)
    rows = conn.execute(_SELECT + " WHERE d.project_id=? ORDER BY d.updated_at DESC", (pid,)).fetchall()
    return [_row_to_doc(paths, r) for r in rows]


def create_document(conn: sqlite3.Connection, paths: Paths, project: str, name: str,
                    template: str = "article") -> Document:
    name = name.strip()
    if not name or len(name) > 200:
        raise DocumentError("give the document a name (at most 200 characters)")
    if template not in TEMPLATES:
        raise DocumentError(f"template must be one of {sorted(TEMPLATES)}")
    pid = get_project_id(conn, project)
    root = writing_root(paths, project)
    root.mkdir(parents=True, exist_ok=True)
    base = _slug(name)
    doc_id = new_id()
    ts = now()
    with transaction(conn, immediate=True):
        taken = {str(r[0]) for r in conn.execute("SELECT slug FROM writing_documents WHERE project_id=?", (pid,))}
        slug, n = base, 1
        while slug in taken or (root / slug).exists():
            n += 1
            slug = f"{base}-{n}"
        folder = root / slug
        source = resources.files("litforge.writing") / "templates" / template
        with resources.as_file(source) as src:
            shutil.copytree(src, folder, ignore=shutil.ignore_patterns("__pycache__", "__init__.py"))
        (folder / "figures").mkdir(exist_ok=True)
        (folder / "references.bib").touch()
        (folder / ".snapshots").mkdir(exist_ok=True)
        conn.execute(
            """INSERT INTO writing_documents(id, project_id, name, slug, template, engine, created_at, updated_at)
               VALUES (?,?,?,?,?,'tectonic',?,?)""", (doc_id, pid, name, slug, template, ts, ts))
    return get_document(conn, paths, doc_id)


def rename_document(conn: sqlite3.Connection, paths: Paths, doc_id: str, name: str) -> Document:
    name = name.strip()
    if not name or len(name) > 200:
        raise DocumentError("give the document a name (at most 200 characters)")
    get_document(conn, paths, doc_id)
    conn.execute("UPDATE writing_documents SET name=?, updated_at=? WHERE id=?", (name, now(), doc_id))
    return get_document(conn, paths, doc_id)


def set_engine(conn: sqlite3.Connection, paths: Paths, doc_id: str, engine: str) -> Document:
    if engine not in {"tectonic", "xelatex"}:
        raise DocumentError("engine must be tectonic or xelatex")
    get_document(conn, paths, doc_id)
    conn.execute("UPDATE writing_documents SET engine=? WHERE id=?", (engine, doc_id))
    return get_document(conn, paths, doc_id)


def set_settings(conn: sqlite3.Connection, paths: Paths, doc_id: str, *, engine: str | None = None,
                 main_file: str | None = None, draft: bool | None = None) -> Document:
    """Change the compile settings given; the others stay."""
    doc = get_document(conn, paths, doc_id)
    if engine is not None:
        doc = set_engine(conn, paths, doc_id, engine)
    if main_file is not None:
        target = resolve_inside(doc.folder, main_file)
        if target.suffix.lower() != ".tex" or not target.is_file():
            raise DocumentError("the main file must be a .tex file in the document")
        conn.execute("UPDATE writing_documents SET main_file=? WHERE id=?",
                     (target.relative_to(doc.folder.resolve()).as_posix(), doc_id))
    if draft is not None:
        conn.execute("UPDATE writing_documents SET draft=? WHERE id=?", (int(bool(draft)), doc_id))
    return get_document(conn, paths, doc_id)


_DOCUMENTCLASS = re.compile(r"^(?:[^%\n]|\\%)*?\\documentclass", re.M)


def main_candidates(doc: Document) -> list[str]:
    """The .tex files that can be compiled on their own (they have an uncommented \\documentclass)."""
    names = []
    for item in list_files(doc):
        if item["kind"] == "text" and item["name"].lower().endswith(".tex"):
            text = (doc.folder / item["name"]).read_text(encoding="utf-8", errors="replace")[:200_000]
            if _DOCUMENTCLASS.search(text):
                names.append(item["name"])
    return sorted(names, key=lambda n: (n != "main.tex", n.count("/"), n))


def delete_document(conn: sqlite3.Connection, paths: Paths, doc_id: str) -> Path:
    """Move the document folder to the project's trash folder and forget the row."""
    doc = get_document(conn, paths, doc_id)
    trash = paths.project_dir(doc.project) / "trash"
    trash.mkdir(parents=True, exist_ok=True)
    target = trash / f"{doc.slug}-{time.strftime('%Y%m%d-%H%M%S')}"
    with _lock(doc.id):
        if doc.folder.exists():
            os.replace(doc.folder, target)
        conn.execute("DELETE FROM writing_documents WHERE id=?", (doc.id,))
    return target


def list_files(doc: Document) -> list[dict[str, Any]]:
    out = []
    root = doc.folder.resolve()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(p.startswith(".") for p in rel.parts) or path.is_symlink() or not path.is_file():
            continue
        suffix = path.suffix.lower()
        kind = "text" if suffix in TEXT_SUFFIXES else "figure" if suffix in FIGURE_SUFFIXES else "other"
        out.append({"name": rel.as_posix(), "kind": kind, "size": path.stat().st_size})
    return out


def read_file(doc: Document, name: str) -> tuple[str, str]:
    """Return (text, hash) of a text file in the document."""
    path = resolve_inside(doc.folder, name)
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise DocumentError("only text files open in the editor")
    if not path.is_file():
        raise LookupError(f"{name} not found")
    data = path.read_bytes()
    return data.decode("utf-8", errors="replace"), text_hash(data)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def save_file(conn: sqlite3.Connection, doc: Document, name: str, text: str, expected_hash: str | None) -> str:
    """Write the file atomically. `expected_hash` is the hash the editor read; None creates a new file.
    Raises SaveConflict when the file on disk is not what the editor read."""
    path = resolve_inside(doc.folder, name)
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise DocumentError("only text files can be saved")
    data = text.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise DocumentError("the file is too large")
    with _lock(doc.id):
        current = text_hash(path.read_bytes()) if path.exists() else None
        if current != expected_hash:
            if current == text_hash(data):
                return current  # the same text is already saved (a retried request)
            raise SaveConflict(current)
        _atomic_write(path, data)
        conn.execute("UPDATE writing_documents SET updated_at=? WHERE id=?", (now(), doc.id))
        _maybe_snapshot(doc)
    return text_hash(data)


def upload_figure(doc: Document, name: str, data: bytes) -> str:
    base = PurePosixPath(name.replace("\\", "/")).name
    stem, dot, suffix = base.rpartition(".")
    if not dot or f".{suffix.lower()}" not in FIGURE_SUFFIXES:
        raise DocumentError("figures must be PNG, JPEG, PDF, EPS or SVG")
    clean = re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-") or "figure"
    if len(data) > MAX_FIGURE_BYTES:
        raise DocumentError("the figure is larger than 25 MB")
    rel = f"figures/{clean}.{suffix.lower()}"
    _atomic_write(resolve_inside(doc.folder, rel), data)
    return rel


# ---- snapshots ------------------------------------------------------------------------------------------


def _snapshot_dir(doc: Document) -> Path:
    path = doc.folder / ".snapshots"
    if path.is_symlink():
        raise DocumentError("symbolic links are not followed")
    path.mkdir(exist_ok=True)
    return path


def _text_files(doc: Document) -> list[str]:
    return [f["name"] for f in list_files(doc) if f["kind"] == "text"]


def create_snapshot(doc: Document, note: str = "", name: str = "") -> str:
    # Under the document's lock: rotation must not remove a version that is being named, read or restored.
    with _lock(doc.id):
        snap_id = time.strftime("%Y%m%dT%H%M%S") + f"-{time.time_ns() % 1_000_000_000:09d}"
        target = _snapshot_dir(doc) / snap_id
        target.mkdir()
        files = _text_files(doc)
        for file_name in files:
            dest = target / file_name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolve_inside(doc.folder, file_name), dest)
        meta = {"created_at": now(), "note": note, "name": _clean_version_name(name), "files": files}
        (target / ".meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        _rotate(doc)
        return snap_id


def _maybe_snapshot(doc: Document) -> None:
    snaps = list_snapshots(doc)
    if snaps and time.time() - snaps[0]["mtime"] < SNAPSHOT_EVERY_S:
        return
    create_snapshot(doc, "auto")


def _rotate(doc: Document) -> None:
    for snap in [s for s in list_snapshots(doc) if not s["name"]][SNAPSHOT_KEEP:]:
        shutil.rmtree(_snapshot_dir(doc) / snap["id"], ignore_errors=True)


def list_snapshots(doc: Document) -> list[dict[str, Any]]:
    out = []
    for path in _snapshot_dir(doc).iterdir():
        meta_file = path / ".meta.json"
        if path.is_symlink() or not meta_file.is_file():
            continue
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"id": path.name, "created_at": meta.get("created_at"), "note": meta.get("note", ""),
                    "name": meta.get("name", ""), "files": meta.get("files", []), "mtime": meta_file.stat().st_mtime})
    out.sort(key=lambda s: s["id"], reverse=True)
    return out


def restore_snapshot(conn: sqlite3.Connection, doc: Document, snap_id: str) -> list[str]:
    """Bring back a snapshot's text files; the current text is snapshotted first so nothing is lost."""
    source = _version_folder(doc, snap_id)
    meta = json.loads((source / ".meta.json").read_text(encoding="utf-8"))
    with _lock(doc.id):
        # Read first: the safety snapshot below may rotate this (oldest) snapshot away.
        contents = {name: path.read_bytes() for name in meta.get("files", [])
                    if (path := resolve_inside(source, name)).is_file()}
        when = str(meta.get("created_at") or snap_id).replace("T", " ")[:16]
        create_snapshot(doc, f"before restoring the version of {when}")
        restored = []
        for name, data in contents.items():
            _atomic_write(resolve_inside(doc.folder, name), data)
            restored.append(name)
        conn.execute("UPDATE writing_documents SET updated_at=? WHERE id=?", (now(), doc.id))
    return restored



def _clean_version_name(name: str) -> str:
    return "".join(c for c in name if unicodedata.category(c) != "Cc").strip()[:120]


def _version_folder(doc: Document, version: str) -> Path:
    if not re.fullmatch(r"\d{8}T\d{6}-\d{9}", version):
        raise DocumentError("unknown snapshot")
    source = _snapshot_dir(doc) / version
    if source.is_symlink() or not (source / ".meta.json").is_file():
        raise DocumentError("unknown snapshot")
    return source


def name_snapshot(doc: Document, snap_id: str, name: str) -> str:
    """Set or clear a snapshot's display name without replacing its other metadata."""
    clean = _clean_version_name(name)
    with _lock(doc.id):
        source = _version_folder(doc, snap_id) / ".meta.json"
        meta = json.loads(source.read_text(encoding="utf-8"))
        meta["name"] = clean
        _atomic_write(source, json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    return clean


def version_text(doc: Document, version: str, name: str) -> str:
    folder = doc.folder if version == "current" else _version_folder(doc, version)
    path = resolve_inside(folder, name)
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise DocumentError("only text files open in the editor")
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _line_opcodes(left: list[str], right: list[str]) -> list[tuple[str, int, int, int, int]]:
    """difflib opcodes for two lists of lines, fast on long files: the common start and end are taken out first
    (edits are usually local), so the quadratic matcher only sees the changed middle; a very large middle uses
    difflib's junk heuristic, which stays fast at some cost in precision."""
    start = 0
    while start < len(left) and start < len(right) and left[start] == right[start]:
        start += 1
    end = 0
    while end < len(left) - start and end < len(right) - start and left[-1 - end] == right[-1 - end]:
        end += 1
    mid_a, mid_b = left[start:len(left) - end], right[start:len(right) - end]
    ops: list[tuple[str, int, int, int, int]] = [("equal", 0, start, 0, start)] if start else []
    if mid_a or mid_b:
        big = len(mid_a) * len(mid_b) > 4_000_000
        for tag, i, j, k, m in difflib.SequenceMatcher(None, mid_a, mid_b, autojunk=big).get_opcodes():
            ops.append((tag, i + start, j + start, k + start, m + start))
    if end:
        ops.append(("equal", len(left) - end, len(left), len(right) - end, len(right)))
    return ops


def _grouped(ops: list[tuple[str, int, int, int, int]], context: int) -> list[list[tuple[str, int, int, int, int]]]:
    """difflib.SequenceMatcher.get_grouped_opcodes for a ready list of opcodes."""
    if not ops or all(op[0] == "equal" for op in ops):
        return []
    codes = list(ops)
    tag, i1, i2, j1, j2 = codes[0]
    if tag == "equal":
        codes[0] = (tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2)
    tag, i1, i2, j1, j2 = codes[-1]
    if tag == "equal":
        codes[-1] = (tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context))
    groups: list[list[tuple[str, int, int, int, int]]] = []
    group: list[tuple[str, int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > context * 2:
            group.append((tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)))
            groups.append(group)
            group = []
            i1, j1 = max(i1, i2 - context), max(j1, j2 - context)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        groups.append(group)
    return groups


def diff_versions(doc: Document, a: str, b: str, name: str = "main.tex", context: int = 3) -> dict[str, Any]:
    left = version_text(doc, a, name).splitlines()
    right = version_text(doc, b, name).splitlines()
    ops = _line_opcodes(left, right)
    added, removed = _change_counts(ops)
    hunks: list[dict[str, Any]] = []
    total = 0
    truncated = False
    for group in _grouped(ops, max(0, context)):
        first, last = group[0], group[-1]
        a_lines, b_lines = last[2] - first[1], last[4] - first[3]
        hunk: dict[str, Any] = {
            "a_start": first[1] + (1 if a_lines else 0), "a_lines": a_lines,
            "b_start": first[3] + (1 if b_lines else 0), "b_lines": b_lines, "lines": []}
        for tag, i, j, k, end in group:
            pieces = ([(" ", left[i:j])] if tag == "equal" else
                      [("-", left[i:j]), ("+", right[k:end])])
            for op, lines in pieces:
                for text in lines:
                    if total == 4000:
                        truncated = True
                        break
                    hunk["lines"].append({"op": op, "text": text})
                    total += 1
                if truncated:
                    break
            if truncated:
                break
        if hunk["lines"]:
            hunks.append(hunk)
        if truncated:
            break
    return {"file": name, "a": a, "b": b, "added": added, "removed": removed,
            "truncated": truncated, "hunks": hunks}


def _change_counts(ops: list[tuple[str, int, int, int, int]]) -> tuple[int, int]:
    added = removed = 0
    for tag, i, j, k, end in ops:
        if tag != "equal":
            added += end - k
            removed += j - i
    return added, removed


def snapshot_summaries(doc: Document) -> list[dict[str, Any]]:
    with _lock(doc.id):  # rotation cannot remove a version between listing and reading it
        return _summaries(doc)


def _summaries(doc: Document) -> list[dict[str, Any]]:
    snapshots = list_snapshots(doc)
    older: dict[str, str] = {}
    for snapshot in reversed(snapshots):
        texts = {name: version_text(doc, snapshot["id"], name) for name in snapshot["files"]
                 if Path(name).suffix.lower() in TEXT_SUFFIXES}
        added = removed = 0
        for name in texts.keys() | older.keys():
            before, after = older.get(name, ""), texts.get(name, "")
            if before != after:
                counts = _change_counts(_line_opcodes(before.splitlines(), after.splitlines()))
                added += counts[0]
                removed += counts[1]
        snapshot.update(added=added, removed=removed)
        older = texts
    return snapshots
