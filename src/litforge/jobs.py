"""Cooperative UI worker cancellation and ownership of external process trees."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class JobCancelled(BaseException):
    pass


class JobControl:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self.processes: dict[int, Callable[[bool], None]] = {}

    def check(self) -> None:
        if self.cancelled.is_set():
            raise JobCancelled("cancelled: server shutdown")

    def process_stoppers(self) -> list[Callable[[bool], None]]:
        with self._lock:
            return list(self.processes.values())


_context = threading.local()


@contextmanager
def job_scope(control: JobControl) -> Iterator[None]:
    _context.control = control
    try:
        control.check()
        yield
    finally:
        del _context.control


def check_cancelled() -> None:
    control = getattr(_context, "control", None)
    if control is not None:
        control.check()


@contextmanager
def track_process(pid: int, terminate: Callable[[bool], None]) -> Iterator[None]:
    control = getattr(_context, "control", None)
    if control is None:
        yield
        return
    # Retain a signalled tree through escalation even if its group leader exits first.
    lock = threading.Lock()
    signalled = False
    active = True

    def stop(force: bool) -> None:
        nonlocal signalled
        with lock:
            if active or signalled:
                terminate(force)
                signalled = True

    with control._lock:
        control.processes[pid] = stop
    try:
        if control.cancelled.is_set():
            stop(True)
        control.check()
        yield
        control.check()
    finally:
        with lock:
            active = False
        with control._lock:
            control.processes.pop(pid, None)
