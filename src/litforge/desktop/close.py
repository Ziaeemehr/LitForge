"""What the window's close button does. The page closes LitForge the way Quit does: it saves the open editor, asks
before cancelling running tasks, then stops the backend, and the window closes when the backend has exited.
A page that cannot answer (loading, an error page, a hung renderer) never keeps the window open.

Standard library only, Python 3.9+ (the host may run under the system Python).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

PAGE_CHECK = "window.LitForgeDesktop ? 'app' : 'other'"
REQUEST_CLOSE = "window.LitForgeDesktop.requestClose()"


class CloseCoordinator:
    def __init__(self, window: Any, backend_exited: Callable[[], bool], *, patience: float = 10.0,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = lambda text: None) -> None:
        self._window = window
        self._backend_exited = backend_exited
        self._patience = patience
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._asked_at: float | None = None  # when the page was asked; None when no request is open
        self._allowed = False

    def on_closing(self) -> bool:
        """The window's closing handler (GUI thread): True lets it close, False keeps it open for now."""
        with self._lock:
            if self._allowed or self._backend_exited():
                return True
            if self._asked_at is not None:
                if self._clock() - self._asked_at <= self._patience:
                    return False  # the page is still saving or asking: one request at a time
                self._log("the page did not finish closing in time; closing the window anyway")
                self._allowed = True
                return True
            self._asked_at = self._clock()
        threading.Thread(target=self._ask_page, daemon=True, name="litforge-close").start()
        return False

    def _close_now(self) -> None:
        with self._lock:
            self._allowed = True
        self._window.destroy()

    def _ask_page(self) -> None:  # off the GUI thread: evaluate_js waits for the GUI thread
        try:
            page = self._window.evaluate_js(PAGE_CHECK)
        except Exception as exc:  # noqa: BLE001 - a broken renderer must not keep the window open
            self._log(f"the page could not be asked to close: {exc!r}")
            page = "other"
        if page != "app":
            self._close_now()
            return
        try:
            self._window.evaluate_js(REQUEST_CLOSE, self._answered)
        except Exception as exc:  # noqa: BLE001
            self._log(f"the page could not be asked to close: {exc!r}")
            self._close_now()

    def _answered(self, quitting: Any) -> None:
        """The page's answer: True once it asked the backend to stop, False when the user kept working."""
        if quitting is not True:
            with self._lock:
                self._asked_at = None
            return
        deadline = time.monotonic() + self._patience
        while time.monotonic() < deadline:
            if self._backend_exited():
                return  # the session sees it too and closes the window
            time.sleep(0.05)
        self._log("the backend did not stop after the page quit; closing the window")
        self._close_now()
