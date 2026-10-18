"""Release checkpoints left behind by dead owners (or expired ownerless legacy rows)."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from litforge.storage.db import now

OWNER_LEASE_HOURS = 24


def process_started_at(pid: int) -> str | None:
    """OS process identity, separate from the time a run acquired its owner."""
    try:
        if sys.platform.startswith("linux"):
            # comm can contain spaces and parentheses; starttime is stat field 22.
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
            kernel.GetProcessTimes.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                created, exited, kernel_time, user = (wintypes.FILETIME() for _ in range(4))
                if kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                          ctypes.byref(kernel_time), ctypes.byref(user)):
                    return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
                return None
            finally:
                kernel.CloseHandle(handle)
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=2, check=False)
        return result.stdout.strip() or None if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def owner_alive(pid: int, started_at: str | None) -> bool:
    if not _alive(pid):
        return False
    actual = process_started_at(pid) if started_at is not None else None
    return started_at is None or actual is None or actual == started_at


def repair_model_reservations(conn: sqlite3.Connection) -> None:
    """Release crashed owners; callers serialize this with budget reservations."""
    rows = conn.execute(
        "SELECT id,owner_pid,owner_process_started_at FROM model_call_reservations").fetchall()
    for row in rows:
        if not owner_alive(row["owner_pid"], row["owner_process_started_at"]):
            conn.execute("DELETE FROM model_call_reservations WHERE id=?", (row["id"],))


def repair_abandoned_runs(conn: sqlite3.Connection) -> None:
    """Keep live owners; legacy runs without an owner expire under the same lease."""
    cutoff = (datetime.now(UTC) - timedelta(hours=OWNER_LEASE_HOURS)).isoformat(timespec="seconds")
    for table, status, timestamp, finished in (
        ("watch_runs", "error", "started_at", "finished_at"),
        ("console_runs", "failed", "updated_at", "updated_at"),
    ):
        rows = conn.execute(
            f"SELECT id,owner_pid,owner_process_started_at,"
            f"julianday(COALESCE(owner_started_at,{timestamp})) < julianday(?) AS expired "
            f"FROM {table} WHERE status='running'", (cutoff,)).fetchall()
        for row in rows:
            abandoned = (row["expired"] if row["owner_pid"] is None else
                         not owner_alive(row["owner_pid"], row["owner_process_started_at"]))
            if abandoned:
                conn.execute(f"UPDATE {table} SET status=?,{finished}=?,error=? WHERE id=? AND status='running'",
                             (status, now(), "interrupted: worker owner exited or legacy lease expired", row["id"]))
