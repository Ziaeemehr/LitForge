"""Screening against a fake Ollama HTTP server and a fake LLM (no GPU needed)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from litforge.screening.criteria import Criteria
from litforge.screening.llm import LlmError, OllamaClient
from litforge.screening.prompt import build_messages, response_schema


class _Ollama(BaseHTTPRequestHandler):
    models: list[str] = ["qwen3:4b"]
    reply: str = '{"relevance": 4, "inclusion": [], "exclusion": [], "reason": "ok"}'
    seen: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _json(self, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._json({"models": [{"name": m} for m in self.models]})

    def do_POST(self) -> None:  # noqa: N802
        _Ollama.seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self._json({"message": {"role": "assistant", "content": self.reply}})


@pytest.fixture()
def ollama() -> Iterator[str]:
    _Ollama.models, _Ollama.seen = ["qwen3:4b"], []
    _Ollama.reply = '{"relevance": 4, "inclusion": [], "exclusion": [], "reason": "ok"}'
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_ollama_client_sends_schema_and_parses_json(ollama: str) -> None:
    c = Criteria(question="q")
    llm = OllamaClient(ollama, "qwen3:4b")
    llm.check()
    out = llm.chat_json(build_messages(c, "t", "a", None, None), response_schema(c))
    assert out["relevance"] == 4
    sent = _Ollama.seen[0]
    assert sent["model"] == "qwen3:4b" and sent["stream"] is False and sent["think"] is False
    assert sent["format"]["type"] == "object" and sent["options"]["temperature"] == 0


def test_ollama_client_accepts_latest_tag(ollama: str) -> None:
    _Ollama.models = ["qwen3:latest"]
    OllamaClient(ollama, "qwen3").check()


def test_ollama_missing_model_names_pull_command(ollama: str) -> None:
    _Ollama.models = ["llama3:8b"]
    with pytest.raises(LlmError, match="ollama pull qwen3:4b"):
        OllamaClient(ollama, "qwen3:4b").check()


def test_ollama_unreachable_is_llm_error() -> None:
    with pytest.raises(LlmError, match="not reachable"):
        OllamaClient("http://127.0.0.1:9", "qwen3:4b", timeout_s=1).check()


def test_ollama_invalid_json_is_llm_error(ollama: str) -> None:
    _Ollama.reply = "not json"
    with pytest.raises(LlmError, match="invalid JSON"):
        OllamaClient(ollama, "qwen3:4b").chat_json([{"role": "user", "content": "x"}], {})
