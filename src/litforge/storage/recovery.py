"""Release checkpoints left behind by dead processes or expired worker leases."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta

from litforge.storage.db import now

OWNER_LEASE_HOURS = 24


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def repair_abandoned_runs(conn: sqlite3.Connection) -> None:
    """Keep live owners; legacy runs without an owner expire under the same lease."""
    cutoff = (datetime.now(UTC) - timedelta(hours=OWNER_LEASE_HOURS)).isoformat(timespec="seconds")
    for table, status, timestamp, finished in (
        ("watch_runs", "error", "started_at", "finished_at"),
        ("console_runs", "failed", "updated_at", "updated_at"),
    ):
        rows = conn.execute(
            f"SELECT id,owner_pid,julianday(COALESCE(owner_started_at,{timestamp})) < julianday(?) AS expired "
            f"FROM {table} WHERE status='running'", (cutoff,)).fetchall()
        for row in rows:
            if row["expired"] or (row["owner_pid"] is not None and not _alive(row["owner_pid"])):
                conn.execute(f"UPDATE {table} SET status=?,{finished}=?,error=? WHERE id=? AND status='running'",
                             (status, now(), "interrupted: worker owner exited or its lease expired", row["id"]))
