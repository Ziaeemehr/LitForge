"""What the window's close button does: the page saves and confirms first; a page that cannot answer never traps."""

from __future__ import annotations

import ast
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from litforge.desktop import close

CLOSE_FILE = Path(close.__file__)


class FakeWindow:
    def __init__(self, page: str = "app", fail: bool = False) -> None:
        self.page = page
        self.fail = fail
        self.scripts: list[str] = []
        self.callbacks: list[Callable[[Any], None]] = []
        self.destroyed = threading.Event()

    def evaluate_js(self, script: str, callback: Callable[[Any], None] | None = None) -> Any:
        if self.fail:
            raise RuntimeError("renderer gone")
        self.scripts.append(script)
        if callback is not None:
            self.callbacks.append(callback)
            return "true"
        return self.page

    def destroy(self) -> None:
        self.destroyed.set()


def _wait(cond: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + 3
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.01)
    return cond()


def test_a_stopped_backend_lets_the_window_close_at_once() -> None:
    window = FakeWindow()
    coordinator = close.CloseCoordinator(window, backend_exited=lambda: True)
    assert coordinator.on_closing() is True
    assert window.scripts == []


def test_the_first_close_asks_the_page_and_keeps_the_window() -> None:
    window = FakeWindow()
    coordinator = close.CloseCoordinator(window, backend_exited=lambda: False)
    assert coordinator.on_closing() is False
    assert _wait(lambda: len(window.callbacks) == 1)
    assert "requestClose" in window.scripts[-1]
    assert not window.destroyed.is_set()  # the page quits the backend; the session then closes the window


def test_a_page_without_litforge_closes_directly() -> None:
    window = FakeWindow(page="other")  # loading page or error page: nothing to save
    coordinator = close.CloseCoordinator(window, backend_exited=lambda: False)
    assert coordinator.on_closing() is False
    assert window.destroyed.wait(3)
    assert coordinator.on_closing() is True  # destroy() closes the window through the same handler


def test_a_renderer_that_fails_closes_directly() -> None:
    window = FakeWindow(fail=True)
    coordinator = close.CloseCoordinator(window, backend_exited=lambda: False)
    assert coordinator.on_closing() is False
    assert window.destroyed.wait(3)


def test_repeated_clicks_while_the_page_works_are_one_request() -> None:
    window = FakeWindow()
    coordinator = close.CloseCoordinator(window, backend_exited=lambda: False)
    for _ in range(5):
        assert coordinator.on_closing() is False
    assert _wait(lambda: len(window.callbacks) == 1)
    time.sleep(0.1)
    assert len(window.callbacks) == 1


def test_keep_working_lets_a_later_close_ask_again() -> None:
    window = FakeWindow()
    coordinator = close.CloseCoordinator(window, backend_exited=lambda: False)
    coordinator.on_closing()
    assert _wait(lambda: len(window.callbacks) == 1)
    window.callbacks[0](False)  # the user kept working, or a save failed
    coordinator.on_closing()
    assert _wait(lambda: len(window.callbacks) == 2)
    assert not window.destroyed.is_set()


def test_a_page_that_never_answers_does_not_trap_the_user() -> None:
    window = FakeWindow()
    now = [100.0]
    coordinator = close.CloseCoordinator(window, backend_exited=lambda: False, clock=lambda: now[0], patience=10)
    coordinator.on_closing()
    assert _wait(lambda: len(window.callbacks) == 1)
    now[0] += 5
    assert coordinator.on_closing() is False  # still waiting for the page
    now[0] += 6
    assert coordinator.on_closing() is True  # more than 10 s: close anyway


def test_a_page_that_agreed_but_whose_backend_hangs_still_closes() -> None:
    window = FakeWindow()
    coordinator = close.CloseCoordinator(window, backend_exited=lambda: False, patience=0.2)
    coordinator.on_closing()
    assert _wait(lambda: len(window.callbacks) == 1)
    window.callbacks[0](True)  # quit was requested
    assert window.destroyed.wait(3)


def test_close_source_is_python_39_compatible_and_stdlib_only() -> None:
    import sys

    tree = ast.parse(CLOSE_FILE.read_text(encoding="utf-8"), feature_version=(3, 9))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported <= set(sys.stdlib_module_names)
