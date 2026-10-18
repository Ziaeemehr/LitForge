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
        self.processes: dict[int, Callable[[], None]] = {}

    def check(self) -> None:
        if self.cancelled.is_set():
            raise JobCancelled("cancelled: server shutdown")

    def terminate_processes(self) -> None:
        with self._lock:
            callbacks = list(self.processes.values())
        for terminate in callbacks:
            terminate()


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
def track_process(pid: int, terminate: Callable[[], None]) -> Iterator[None]:
    control = getattr(_context, "control", None)
    if control is None:
        yield
        return
    # Shutdown and the command's own exception cleanup may race. Terminate a tree once.
    lock = threading.Lock()
    terminated = False
    active = True

    def stop() -> None:
        nonlocal terminated
        with lock:
            if active and not terminated:
                terminate()
                terminated = True

    with control._lock:
        control.processes[pid] = stop
    try:
        if control.cancelled.is_set():
            stop()
        control.check()
        yield
        control.check()
    finally:
        with lock:
            active = False
        with control._lock:
            control.processes.pop(pid, None)
