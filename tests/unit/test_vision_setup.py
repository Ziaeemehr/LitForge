"""Setting up better math (high): status and model download against a fake Ollama server."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from litforge.parsing import vision_setup
from litforge.parsing.vision_setup import OllamaSetup

MODEL = "qwen2.5vl:7b"


class FakeOllama:
    def __init__(self) -> None:
        self.models: list[str] = []
        self.pull_events: list[dict[str, Any]] = []

    def serve(self) -> tuple[ThreadingHTTPServer, str]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:  # noqa: N802
                body = json.dumps({"models": [{"name": m} for m in fake.models]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                assert json.loads(self.rfile.read(int(self.headers["Content-Length"])))["model"] == MODEL
                body = b"".join(json.dumps(e).encode() + b"\n" for e in fake.pull_events)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                if fake.pull_events and fake.pull_events[-1].get("status") == "success":
                    fake.models.append(MODEL)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture()
def ollama() -> Iterator[tuple[FakeOllama, str]]:
    fake = FakeOllama()
    httpd, url = fake.serve()
    yield fake, url
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def computer(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """This computer, as the checks see it; tests change the values."""
    hw: dict[str, Any] = {"gpu": None, "memory": 16.0, "disk": 100.0, "ollama": None, "install": "brew"}
    monkeypatch.setattr(vision_setup, "gpu_name", lambda: hw["gpu"])
    monkeypatch.setattr(vision_setup, "memory_gb", lambda: hw["memory"])
    monkeypatch.setattr(vision_setup, "disk_free_gb", lambda: hw["disk"])
    monkeypatch.setattr(vision_setup, "find_ollama", lambda: hw["ollama"])
    monkeypatch.setattr(vision_setup, "install_method", lambda: hw["install"])
    return hw


def dead_url() -> str:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = httpd.server_address[1]
    httpd.server_close()
    return f"http://127.0.0.1:{port}"


def test_nothing_installed_without_gpu_can_be_installed_with_a_warning(computer: dict[str, Any]) -> None:
    st = OllamaSetup(dead_url(), MODEL).status()
    assert st.local and not st.installed and not st.running and not st.model_ready and not st.ready
    assert st.install == "brew" and not st.blockers
    assert st.warnings == [vision_setup.CPU_WARNING]  # a GPU is optional: CPU works, slowly
    d = st.as_dict()
    assert d["ready"] is False and d["requirements"]["memory_gb"] == vision_setup.MIN_MEMORY_GB


def test_too_little_memory_or_disk_blocks_and_run_refuses(computer: dict[str, Any]) -> None:
    computer.update(gpu="NVIDIA RTX 4060, 8188 MiB", memory=4.0, disk=3.0)
    st = OllamaSetup(dead_url(), MODEL).status()
    assert not st.warnings and len(st.blockers) == 2 and "4 GB of memory" in st.blockers[0]
    with pytest.raises(RuntimeError, match="memory"):
        OllamaSetup(dead_url(), MODEL).run(lambda m: None)


def test_no_install_method_gives_the_commands(computer: dict[str, Any]) -> None:
    computer["install"] = None
    st = OllamaSetup(dead_url(), MODEL).status()
    assert st.install is None and st.manual[-1] == f"ollama pull {MODEL}"
    with pytest.raises(RuntimeError, match="cannot install"):
        OllamaSetup(dead_url(), MODEL).run(lambda m: None)


def test_remote_ollama_that_does_not_answer_is_a_blocker(computer: dict[str, Any]) -> None:
    st = OllamaSetup("http://192.0.2.1:1", MODEL).status()
    assert not st.local and st.blockers and "LITFORGE_OLLAMA_URL" in st.blockers[0]


def test_running_without_model_pulls_it_with_progress(ollama: tuple[FakeOllama, str],
                                                      computer: dict[str, Any]) -> None:
    fake, url = ollama
    fake.models = ["qwen3:4b"]
    st = OllamaSetup(url, MODEL).status()
    assert st.running and st.installed and not st.model_ready and st.install is None
    fake.pull_events = [{"status": "pulling manifest"},
                        *({"status": "pulling a", "digest": "a", "total": 4_000_000_000, "completed": c}
                          for c in (0, 1_000_000_000, 1_010_000_000, 4_000_000_000)),
                        {"status": "pulling b", "digest": "b", "total": 1_000_000_000, "completed": 1_000_000_000},
                        {"status": "success"}]
    messages: list[str] = []
    OllamaSetup(url, MODEL).run(messages.append)
    downloads = [m for m in messages if "%" in m]
    assert downloads[0] == f"downloading {MODEL}: 0% of 4.0 GB" and downloads[-1].endswith("100% of 5.0 GB")
    assert len(downloads) == len(set(downloads))  # nearby updates are not repeated
    assert messages[-1].startswith("done:") and OllamaSetup(url, MODEL).status().ready


def test_pull_error_is_reported(ollama: tuple[FakeOllama, str], computer: dict[str, Any]) -> None:
    fake, url = ollama
    fake.pull_events = [{"status": "pulling manifest"}, {"error": "pull model manifest: file does not exist"}]
    with pytest.raises(RuntimeError, match="file does not exist"):
        OllamaSetup(url, MODEL).run(lambda m: None)
    fake.pull_events = [{"status": "pulling manifest"}]  # the stream stops without success
    with pytest.raises(RuntimeError, match="ended early"):
        OllamaSetup(url, MODEL).pull(lambda m: None)


def test_ready_skips_the_hardware_checks(ollama: tuple[FakeOllama, str], monkeypatch: pytest.MonkeyPatch) -> None:
    fake, url = ollama
    fake.models = [f"{MODEL}"]
    monkeypatch.setattr(vision_setup, "gpu_name", lambda: pytest.fail("not needed"))
    st = OllamaSetup(url, MODEL).status()
    assert st.ready and st.gpu is None


def test_installed_but_stopped_is_started(ollama: tuple[FakeOllama, str], computer: dict[str, Any],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    fake, url = ollama
    computer["ollama"] = "/usr/local/bin/ollama"
    calls: list[str] = []
    setup = OllamaSetup(url, MODEL, sleep=lambda s: None)
    real_models, checks = setup.models, []

    def models() -> set[str] | None:  # down for status, the first check and five waits, then up
        checks.append(1)
        return None if len(checks) <= 7 else real_models()

    monkeypatch.setattr(setup, "models", models)
    monkeypatch.setattr(vision_setup, "start_ollama", lambda u: calls.append(u))
    monkeypatch.setattr(vision_setup, "install_ollama", lambda m, p: pytest.fail("already installed"))
    fake.pull_events = [{"status": "success"}]
    setup.run(lambda m: None)
    assert calls == [url]
