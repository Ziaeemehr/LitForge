from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from litforge.storage import recovery
from litforge.storage.db import available_migrations, migrate, schema_version


def test_current_process_identity_is_stable() -> None:
    identity = recovery.process_started_at(os.getpid())
    if sys.platform.startswith(("linux", "darwin", "freebsd", "openbsd", "netbsd", "win")):
        assert identity
    assert recovery.process_started_at(os.getpid()) == identity


def test_linux_starttime_handles_parentheses_in_process_name(monkeypatch) -> None:
    monkeypatch.setattr(recovery.sys, "platform", "linux")
    stat = "123 (a process (with parentheses)) " + " ".join(["S", *(["0"] * 18), "98765", "0"])
    monkeypatch.setattr(Path, "read_text", lambda self: stat)
    assert recovery.process_started_at(123) == "98765"


def test_bsd_starttime_uses_argv_and_strips_ps_output(monkeypatch) -> None:
    monkeypatch.setattr(recovery.sys, "platform", "darwin")
    def run(argv, **kwargs):
        assert argv == ["ps", "-o", "lstart=", "-p", "123"]
        assert kwargs.get("timeout") is not None and not kwargs.get("shell")
        return subprocess.CompletedProcess(argv, 0, "  Sun Oct 18 10:20:30 2026\n")
    monkeypatch.setattr(recovery.subprocess, "run", run)
    assert recovery.process_started_at(123) == "Sun Oct 18 10:20:30 2026"


def test_windows_starttime_closes_handle(monkeypatch) -> None:
    import ctypes
    monkeypatch.setattr(recovery.sys, "platform", "win32")
    def times(handle, created, exited, kernel, user):
        created._obj.dwHighDateTime = 1
        created._obj.dwLowDateTime = 2
        return 1
    dll = SimpleNamespace(OpenProcess=Mock(return_value=42), GetProcessTimes=Mock(side_effect=times),
                          CloseHandle=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=dll), raising=False)
    assert recovery.process_started_at(123) == str((1 << 32) + 2)
    dll.CloseHandle.assert_called_once_with(42)


def test_identity_lookup_failure_falls_back_to_pid(monkeypatch) -> None:
    monkeypatch.setattr(recovery.sys, "platform", "darwin")
    monkeypatch.setattr(recovery.subprocess, "run", Mock(side_effect=OSError("unavailable")))
    assert recovery.process_started_at(os.getpid()) is None
    assert recovery.owner_alive(os.getpid(), "old-identity")


@pytest.mark.parametrize("old_v22", [True, False])
def test_identity_migration_supports_existing_v22_and_fresh_schema(old_v22) -> None:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    for version, _, sql in available_migrations():
        if version > 22:
            continue
        if old_v22:
            sql = "\n".join(line for line in sql.splitlines() if "owner_process_started_at" not in line)
            # The new column may follow the previous final column of CREATE TABLE.
            sql = sql.replace("owner_started_at TEXT NOT NULL,", "owner_started_at TEXT NOT NULL")
        conn.executescript(sql)
    conn.execute("PRAGMA user_version=22")
    conn.execute("INSERT INTO model_call_reservations(id,cost_usd,created_at,owner_pid,owner_started_at) "
                 "VALUES ('existing',1,'2026-10-18',123,'2026-10-18')")
    migrate(conn)
    assert schema_version(conn) == 24
    for table in ("watch_runs", "console_runs", "model_call_reservations"):
        assert "owner_process_started_at" in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    assert conn.execute("SELECT id FROM model_call_reservations").fetchone()[0] == "existing"
    assert migrate(conn) == []
    conn.close()
