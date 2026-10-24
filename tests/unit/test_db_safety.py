"""Upgrade safety against real SQLite files and the bundled schema."""

import sqlite3
from pathlib import Path

import pytest

from litforge.storage import db


def test_newer_schema_is_untouched(tmp_path: Path) -> None:
    path = tmp_path / "papers.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(f"PRAGMA user_version={db.latest_version() + 1}")
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    with pytest.raises(db.SchemaTooNew) as error:
        db.open_db(path)
    message = str(error.value)
    assert str(path) in message and str(tmp_path / "backups") in message
    assert str(db.latest_version() + 1) in message and str(db.latest_version()) in message
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
    assert list(tmp_path.iterdir()) == [path]


def test_upgrade_backs_up_wal_and_retains_only_five(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "papers.sqlite"
    conn = db.open_db(path)
    assert not (tmp_path / "backups").exists()
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO papers(id, created_at, updated_at) VALUES ('safe', 'now', 'now')")
    migrations = db.available_migrations()
    old = db.latest_version()
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(6):
        (backups / f"papers-v1-20200101T00000{i}000000Z.sqlite").touch()
    unrelated = backups / "personal.sqlite"
    unrelated.touch()
    monkeypatch.setattr(db, "available_migrations", lambda: [
        *migrations, (old + 1, "next.sql", "CREATE TABLE upgrade_test(id TEXT);")])
    upgraded = db.open_db(path)
    assert db.schema_version(upgraded) == old + 1
    snapshots = sorted(backups.glob("papers-v*.sqlite"))
    assert len(snapshots) == 5 and unrelated.exists()
    assert not (backups / "papers-v1-20200101T000000000000Z.sqlite").exists()
    snapshot = next(backups.glob(f"papers-v{old}-*.sqlite"))
    with sqlite3.connect(snapshot) as saved:
        assert db.schema_version(saved) == old
        assert saved.execute("SELECT id FROM papers").fetchone()[0] == "safe"
        assert saved.execute("SELECT name FROM sqlite_master WHERE name='upgrade_test'").fetchone() is None
    upgraded.close()
    conn.close()


def test_backup_failure_prevents_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "papers.sqlite"
    conn = db.open_db(path)
    old = db.schema_version(conn)
    conn.close()
    migrations = db.available_migrations()
    monkeypatch.setattr(db, "available_migrations", lambda: [*migrations, (old + 1, "next.sql", "SELECT 1;")])
    (tmp_path / "backups").write_text("blocked", encoding="utf-8")
    with pytest.raises(OSError):
        db.open_db(path)
    with sqlite3.connect(path) as check:
        assert db.schema_version(check) == old


def test_memory_database_has_no_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    conn = db.open_db(Path(":memory:"))
    assert db.schema_version(conn) == db.latest_version()
    conn.close()
    assert list(tmp_path.iterdir()) == []


def test_cli_refusal_is_plain_message(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from litforge.cli import app
    from litforge.config import Paths

    paths = Paths(tmp_path)
    paths.ensure()
    with sqlite3.connect(paths.db) as conn:
        conn.execute(f"PRAGMA user_version={db.latest_version() + 1}")
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "desktop-server"])
    assert result.exit_code == 1
    assert "This library" in result.output
    assert "Traceback" not in result.output


def test_ui_refuses_before_starting_and_on_reopen(tmp_path: Path) -> None:
    from litforge.config import Paths, load_config
    from litforge.ui.server import App, start_server

    paths = Paths(tmp_path)
    application = App(paths, load_config())
    with sqlite3.connect(paths.db) as conn:
        conn.execute(f"PRAGMA user_version={db.latest_version() + 1}")
    with pytest.raises(db.SchemaTooNew, match="This library"):
        application.db()
    with pytest.raises(db.SchemaTooNew, match="This library"):
        start_server(paths, load_config(), desktop=True)


def test_cli_database_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import typer

    from litforge.cli import State
    from litforge.config import Paths, load_config
    from litforge.observability import EventLog

    paths = Paths(tmp_path)
    paths.ensure()
    with sqlite3.connect(paths.db) as conn:
        conn.execute(f"PRAGMA user_version={db.latest_version() + 1}")
    state = State(paths, load_config(), EventLog(paths.logs))
    with pytest.raises(typer.Exit) as error:
        state.db()
    assert error.value.exit_code == 1
    assert "This library" in capsys.readouterr().err


def test_desktop_error_reaches_host_without_traceback(tmp_path: Path) -> None:
    import sys

    from litforge.config import Paths
    from litforge.desktop.host import Backend, BackendError

    paths = Paths(tmp_path / "data")
    paths.ensure()
    with sqlite3.connect(paths.db) as conn:
        conn.execute(f"PRAGMA user_version={db.latest_version() + 1}")
    backend = Backend(
        [sys.executable, "-c", "from litforge.cli import app; app()",
         "--data-dir", str(paths.root), "desktop-server"],
        tmp_path / "desktop.log",
    )
    with pytest.raises(BackendError) as error:
        backend.start(timeout=10)
    assert "This library" in str(error.value)
    assert str(paths.db) in str(error.value)
    assert "Traceback" not in str(error.value)


def test_backup_api_failure_removes_partial_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "papers.sqlite"
    db.open_db(path).close()
    old = db.latest_version()
    migrations = db.available_migrations()
    monkeypatch.setattr(db, "available_migrations", lambda: [*migrations, (old + 1, "next.sql", "SELECT 1;")])
    original = sqlite3.connect

    class BrokenBackup(sqlite3.Connection):
        def backup(self, *args: object, **kwargs: object) -> None:
            raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(db.sqlite3, "connect", lambda *args, **kwargs: original(*args, **kwargs, factory=BrokenBackup))
    with pytest.raises(sqlite3.OperationalError, match="disk full"):
        db.open_db(path)
    assert list((tmp_path / "backups").iterdir()) == []
    with original(path) as check:
        assert db.schema_version(check) == old
