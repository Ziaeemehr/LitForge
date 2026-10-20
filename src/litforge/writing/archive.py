"""Portable document archives, with bounded extraction into a new document only."""

from __future__ import annotations

import copy
import io
import re
import sqlite3
import stat
import zipfile
import zlib
from pathlib import PurePosixPath

from litforge.config import Paths
from litforge.writing.documents import (
    Document,
    DocumentError,
    _create_document,
    _lock,
    list_files,
    main_candidates,
    set_settings,
)

MAX_ENTRIES = 2000
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_RATIO = 200


def export_zip(doc: Document) -> bytes:
    out = io.BytesIO()
    with _lock(doc.id), zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in list_files(doc):
            path = doc.folder / item["name"]
            if any(part.is_symlink() for part in (path, *path.parents) if part != doc.folder):
                continue
            archive.write(path, item["name"])
    return out.getvalue()


def _members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise DocumentError("ZIP has more than 2000 entries")
    total = 0
    members = []
    for info in infos:
        name = info.orig_filename  # filename is silently truncated at NUL by zipfile.
        parts = name.rstrip("/").split("/")
        if (not name or "\x00" in name or "\\" in name or name.startswith("/")
                or any(p == ".." or re.match(r"^[A-Za-z]:", p) for p in parts)):
            raise DocumentError("unsafe path in ZIP")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise DocumentError("ZIP contains a symbolic link")
        total += info.file_size
        if info.file_size > MAX_FILE_BYTES:
            raise DocumentError("ZIP file is larger than 50 MB")
        if total > MAX_TOTAL_BYTES:
            raise DocumentError("ZIP uncompressed total exceeds 200 MB")
        _check_ratio(info.file_size, info.compress_size)
        if any(p.startswith(".") or p == "__MACOSX" for p in parts):
            continue
        members.append((info, name))
    files = [name for info, name in members if not info.is_dir()]
    if files and all("/" in name for name in files) and len({name.split("/")[0] for name in files}) == 1:
        prefix = files[0].split("/")[0] + "/"
        if all(name == prefix or name.startswith(prefix) for _, name in members):
            members = [(info, name[len(prefix):]) for info, name in members if name != prefix]
    return members


def _check_ratio(size: int, compressed: int) -> None:
    if size > 1024 * 1024 and size > MAX_RATIO * compressed:
        raise DocumentError("ZIP member compression ratio exceeds 200")


def import_zip(conn: sqlite3.Connection, paths: Paths, project: str, name: str, data: bytes) -> Document:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = _members(archive)
            if not any(PurePosixPath(n).suffix.lower() == ".tex" for i, n in members if not i.is_dir()):
                raise DocumentError("ZIP has no .tex file")

            def populate(doc: Document) -> None:
                total = 0
                for info, rel in members:
                    target = doc.folder / rel
                    if not target.resolve().is_relative_to(doc.folder.resolve()):
                        raise DocumentError("unsafe path in ZIP")
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # zipfile truncates to file_size; allow one extra byte to detect a forged header.
                    bounded = copy.copy(info)
                    bounded.file_size = info.file_size + 1
                    size = 0
                    with archive.open(bounded) as source, target.open("xb") as dest:
                        while chunk := source.read(64 * 1024):
                            size += len(chunk)
                            total += len(chunk)
                            if size > info.file_size or size > MAX_FILE_BYTES:
                                raise DocumentError("ZIP member exceeds its declared size or 50 MB")
                            if total > MAX_TOTAL_BYTES:
                                raise DocumentError("ZIP uncompressed total exceeds 200 MB")
                            _check_ratio(size, info.compress_size)
                            dest.write(chunk)
                    if size != info.file_size:
                        raise DocumentError("ZIP member does not match its declared size")
                candidates = main_candidates(doc)
                if not candidates:
                    raise DocumentError(r"no .tex file with \documentclass")
                set_settings(conn, paths, doc.id, main_file=candidates[0])

            return _create_document(conn, paths, project, name, "imported", populate)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, NotImplementedError,
            EOFError, UnicodeError, zlib.error) as exc:
        raise DocumentError(f"invalid ZIP: {exc}") from exc
    except OSError as exc:
        raise DocumentError(f"could not import ZIP: {exc}") from exc
