"""Content-addressed storage helpers: file hashing, PDF store and parse-cache keys."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from litforge.config import Paths


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def store_pdf(paths: Paths, source: Path, sha256: str) -> Path:
    """Copy `source` into the library as <sha256>.pdf (atomic). Never overwrites or deletes."""
    dest = paths.pdfs / f"{sha256}.pdf"
    if dest.exists():
        return dest
    paths.pdfs.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(f".tmp-{os.getpid()}")
    shutil.copy2(source, tmp)
    os.replace(tmp, dest)
    return dest


def parse_output_dir(paths: Paths, sha256: str, parser: str, version: str, cfg_hash: str) -> Path:
    safe_version = version.replace("/", "_")
    return paths.parsed / sha256 / f"{parser}-{safe_version}-{cfg_hash}"


def relative_to_root(paths: Paths, path: Path) -> str:
    return str(path.resolve().relative_to(paths.root))
