"""Structured JSONL event logging."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EventLog:
    """Append-only JSONL log. One file per day under the logs directory."""

    def __init__(self, log_dir: Path | None) -> None:
        self.log_dir = log_dir

    def emit(self, event: str, **fields: Any) -> None:
        if self.log_dir is None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        record = {"ts": now.isoformat(timespec="milliseconds"), "event": event, **fields}
        path = self.log_dir / f"{now:%Y-%m-%d}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    @contextmanager
    def timed(self, event: str, **fields: Any) -> Iterator[dict[str, Any]]:
        """Emit `event` with elapsed milliseconds; callers may add fields to the yielded dict."""
        extra: dict[str, Any] = {}
        start = time.perf_counter()
        status = "ok"
        try:
            yield extra
        except Exception as exc:
            status = "error"
            extra["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            elapsed = round((time.perf_counter() - start) * 1000, 1)
            self.emit(event, status=status, elapsed_ms=elapsed, **fields, **extra)


NULL_LOG = EventLog(None)
