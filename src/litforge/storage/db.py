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
_BACKUP_RE = re.compile(r"^papers-v\d+-(\d{8}T\d{12}Z)\.sqlite$")


class SchemaTooNew(RuntimeError):
    """The library needs a newer LitForge installation."""


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
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except BaseException:
        conn.close()
        raise
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


def _check_schema(conn: sqlite3.Connection, db_path: Path) -> None:
    current, latest = schema_version(conn), latest_version()
    if current > latest:
        raise SchemaTooNew(
            f"This library at {db_path} was upgraded by a newer LitForge "
            f"(database schema {current}, this version knows up to {latest}). "
            f"Update LitForge, or restore a backup from {db_path.parent / 'backups'}."
        )


def _backup(conn: sqlite3.Connection, db_path: Path, version: int) -> None:
    folder = db_path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = folder / f"papers-v{version}-{timestamp}.sqlite"
    # Exclusive creation prevents overwriting an existing snapshot.
    with target.open("xb"):
        pass
    try:
        saved = sqlite3.connect(target)
        try:
            conn.backup(saved)
        finally:
            saved.close()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    snapshots = [(match[1], path) for path in folder.iterdir()
                 if (match := _BACKUP_RE.fullmatch(path.name)) and path.is_file() and not path.is_symlink()]
    for _, path in sorted(snapshots, reverse=True)[5:]:
        path.unlink()


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations; each runs in its own transaction. Returns applied names."""
    applied = []
    current = schema_version(conn)
    for version, name, sql in available_migrations():
        if version <= current:
            continue
        if version == 23:
            # 0021/0022 were amended before release; existing branch v22 DBs still need these columns.
            statements = []
            for statement in sql.split(";"):
                match = re.fullmatch(r"\s*ALTER TABLE (\w+) ADD COLUMN (\w+) TEXT\s*", statement)
                if match and match[2] in {r[1] for r in conn.execute(f"PRAGMA table_info({match[1]})")}:
                    continue
                statements.append(statement)
            sql = ";".join(statements)
        # executescript commits implicitly, so wrap the script and the version bump together.
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        applied.append(name)
    return applied


def open_db(db_path: Path) -> sqlite3.Connection:
    # Inspect existing files before connect() can change their journal mode.
    if str(db_path) != ":memory:" and db_path.is_file():
        probe = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            _check_schema(probe, db_path)
        finally:
            probe.close()
    conn = connect(db_path)
    try:
        current = schema_version(conn)
        if 0 < current < latest_version() and str(db_path) != ":memory:" and db_path.is_file():
            _backup(conn, db_path, current)
        migrate(conn)
        return conn
    except BaseException:
        conn.close()
        raise


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
