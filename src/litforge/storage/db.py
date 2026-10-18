"""SQLite connection, migrations and small shared helpers."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

_MIGRATION_RE = re.compile(r"^(\d{4})_.+\.sql$")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)  # explicit transactions only
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def available_migrations() -> list[tuple[int, str, str]]:
    """Return (version, name, sql) for bundled migration files, sorted by version."""
    out = []
    for entry in resources.files("litforge.storage.migrations").iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if match:
            out.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    out.sort()
    for (a, name_a, _), (b, name_b, _) in zip(out, out[1:], strict=False):
        if a == b:  # migrate() would skip the second file silently
            raise RuntimeError(f"duplicate migration number: {name_a} and {name_b}")
    return out


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def latest_version() -> int:
    migrations = available_migrations()
    return migrations[-1][0] if migrations else 0


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations; each runs in its own transaction. Returns applied names."""
    applied = []
    current = schema_version(conn)
    for version, name, sql in available_migrations():
        if version <= current:
            continue
        # executescript commits implicitly, so wrap the script and the version bump together.
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        applied.append(name)
    return applied


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn


def fts5_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False


def start_job(conn: sqlite3.Connection, kind: str, target_id: str | None) -> str:
    job_id = new_id()
    conn.execute(
        "INSERT INTO jobs(id, kind, target_id, status, started_at) VALUES (?,?,?,?,?)",
        (job_id, kind, target_id, "running", now()),
    )
    return job_id


def finish_job(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    detail: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE jobs SET status=?, detail_json=?, error=?, finished_at=? WHERE id=?",
        (status, dumps(detail or {}), error, now(), job_id),
    )
