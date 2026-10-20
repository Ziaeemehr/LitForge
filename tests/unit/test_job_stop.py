"""Stopping one running UI job (the Stop button under the progress log)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from litforge.config import Paths, load_config
from litforge.ui.server import App, Job


def _wait(job: Job, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while job.status == "running" and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.fixture()
def app(tmp_path: Path) -> App:
    return App(Paths(tmp_path / "data", exports_root=tmp_path / "exports"), load_config())


def test_a_stopped_job_stops_at_its_next_progress_line(app: App) -> None:
    started, lines = threading.Event(), []

    def work(job: Job) -> None:
        started.set()
        for i in range(10_000):
            job.messages.append(f"paper {i}")
            lines.append(i)
            time.sleep(0.005)

    job = app.start_job("export", "demo", work)
    assert started.wait(5)
    time.sleep(0.05)
    assert app.stop_job(job.id) is job
    _wait(job)
    assert (job.status, job.error) == ("failed", "stopped")
    done = len(lines)
    time.sleep(0.05)
    assert len(lines) == done < 10_000  # nothing more ran after the stop
    assert job.messages[:2] == ["paper 0", "paper 1"]  # what it reported before stays


def test_stopping_a_finished_or_unknown_job(app: App) -> None:
    job = app.start_job("export", "demo", lambda job: None)
    _wait(job)
    assert app.stop_job(job.id).status == "done"
    with pytest.raises(LookupError):
        app.stop_job("nope")


def test_shutdown_still_says_why(app: App) -> None:
    started = threading.Event()

    def work(job: Job) -> None:
        started.set()
        while True:
            job.messages.append("tick")
            time.sleep(0.005)

    job = app.start_job("export", "demo", work)
    assert started.wait(5)
    app.shutdown(timeout=2)
    assert job.error == "cancelled: server shutdown"
