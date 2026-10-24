"""Doctor reports database compatibility without refusing to open it."""

import sqlite3
from pathlib import Path

import pytest

from litforge import doctor
from litforge.config import Paths
from litforge.storage import db


@pytest.mark.parametrize(("offset", "level"), [(-1, "warn"), (0, "ok"), (1, "error")])
def test_doctor_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offset: int, level: str,
) -> None:
    paths = Paths(tmp_path)
    paths.ensure()
    latest = db.latest_version()
    current = latest + offset
    with sqlite3.connect(paths.db) as conn:
        conn.execute(f"PRAGMA user_version={current}")
    monkeypatch.setattr(doctor, "_ollama", lambda _: doctor.Check(name="ollama", level="warn", detail="offline"))
    monkeypatch.setattr(doctor, "_gpu", lambda: doctor.Check(name="gpu", level="warn", detail="unavailable"))
    check = next(check for check in doctor.run_checks(paths) if check.name == "database")
    assert check.level == level
    if offset > 0:
        assert check.detail == (
            f"schema v{current} is newer than this LitForge (v{latest}); "
            f"update LitForge or restore a backup from {paths.db.parent / 'backups'}"
        )
    elif offset < 0:
        assert "run any command to migrate" in check.detail
    else:
        assert check.detail == f"schema v{current} (latest v{latest})"
