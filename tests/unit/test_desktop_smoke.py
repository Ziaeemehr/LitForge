"""A native smoke run must not report success for missing or failed checks."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "desktop_smoke_under_test", Path(__file__).resolve().parents[2] / "scripts/desktop_smoke.py")
assert spec and spec.loader
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
check_report = smoke.check_report


@pytest.fixture
def good_report() -> dict[str, object]:
    return {
        "checks": {
            "desktop": True, "codemirror": True, "worker": True, "withResolvers": True,
            "structuredClone": True, "randomUUID": True, "segmenter": True, "cssHas": True,
            "clipboardApi": True, "secureContext": True, "rtl": "rtl", "pdf_text": "LitForge probe page",
        },
        "activation": "activated", "host_exit_code": 0, "window_closed": True,
        "backend_stopped": True, "lease_released": True,
    }


def test_complete_native_report_passes(good_report: dict[str, object]) -> None:
    assert check_report(good_report) == []


@pytest.mark.parametrize("name", ["desktop", "codemirror", "worker", "withResolvers", "structuredClone",
                                   "randomUUID", "segmenter", "cssHas", "clipboardApi", "secureContext"])
def test_missing_or_false_browser_check_fails(good_report: dict[str, object], name: str) -> None:
    checks = good_report["checks"]
    assert isinstance(checks, dict)
    checks[name] = False
    assert name in check_report(good_report)
    del checks[name]
    assert name in check_report(good_report)


@pytest.mark.parametrize(("name", "value"), [("rtl", "ltr"), ("pdf_text", ""), ("pdf_text", "error")])
def test_incorrect_rendering_fails(good_report: dict[str, object], name: str, value: str) -> None:
    checks = good_report["checks"]
    assert isinstance(checks, dict)
    checks[name] = value
    assert name in check_report(good_report)


@pytest.mark.parametrize(("name", "value"), [("activation", "none"), ("host_exit_code", 2),
                                            ("window_closed", False), ("backend_stopped", False),
                                            ("lease_released", False)])
def test_lifecycle_failure_fails(good_report: dict[str, object], name: str, value: object) -> None:
    good_report[name] = value
    assert name in check_report(good_report)


def test_empty_or_errored_report_fails(good_report: dict[str, object]) -> None:
    assert check_report({})
    good_report["error"] = "renderer failed"
    assert "error" in check_report(good_report)


def test_supervisor_timeout_stops_child_heartbeat(tmp_path: Path) -> None:
    heartbeat = tmp_path / "child-heartbeat"
    child = ("import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile True:\n"
             " p.write_text(str(time.time()))\n time.sleep(.02)")
    parent = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(60)"
    )
    error = smoke.run_supervised(
        [sys.executable, "-c", parent, child, str(heartbeat)], tmp_path / "host.log", timeout=1)
    assert "exceeded" in error
    assert heartbeat.exists(), "the descendant must have started before the timeout"
    last = heartbeat.read_text()
    time.sleep(.15)
    assert heartbeat.read_text() == last, "a descendant survived the supervisor's timeout"


def test_supervisor_reports_nonzero_exit_and_preserves_logs(tmp_path: Path) -> None:
    log = tmp_path / "host.log"
    error = smoke.run_supervised([sys.executable, "-c", "print('engine failed'); raise SystemExit(7)"], log, timeout=5)
    assert "7" in error
    assert "engine failed" in log.read_text()


def test_page_check_waits_for_the_renderer_loaded_event() -> None:
    loaded = threading.Event()

    class Window:
        events = type("Events", (), {"loaded": loaded})()

        def evaluate_js(self, script: str) -> bool:
            assert loaded.is_set(), "do not trigger pywebview's shorter internal startup timeout"
            return True

    timer = threading.Timer(.05, loaded.set)
    timer.start()
    try:
        smoke.wait_for_app(Window(), timeout=1)
    finally:
        timer.join()


def test_renderer_that_never_loads_fails_with_a_bounded_timeout() -> None:
    class Window:
        events = type("Events", (), {"loaded": threading.Event()})()

        def evaluate_js(self, script: str) -> bool:
            raise AssertionError("JavaScript cannot be evaluated before load")

    with pytest.raises(TimeoutError, match="initialize"):
        smoke.wait_for_app(Window(), timeout=.05)
