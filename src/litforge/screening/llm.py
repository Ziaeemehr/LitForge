"""Local LLM access for screening: Ollama over HTTP (stdlib only) and a fake for tests."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from base64 import b64encode
from collections.abc import Callable
from typing import Any, Protocol

from litforge.config import ScreeningConfig
from litforge.llm import LlmError as LlmError
from litforge.llm import LlmUnavailable as LlmUnavailable
from litforge.llm import ModelReply


class Llm(Protocol):
    name: str

    def check(self) -> None: ...

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any] | ModelReply: ...


def reply_content(reply: dict[str, Any] | ModelReply) -> dict[str, Any]:
    return reply.content if isinstance(reply, ModelReply) else reply


class OllamaClient:
    def __init__(self, url: str, model: str, timeout_s: float = 120) -> None:
        self.url, self.name, self.timeout_s = url.rstrip("/"), model, timeout_s

    def _call(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"},
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 - local URL
                return json.load(resp)  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            raise LlmError(f"Ollama error {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LlmUnavailable(f"Ollama not reachable at {self.url} ({exc}); start it with `ollama serve`") from exc

    def check(self) -> None:
        names = {m.get("name") for m in self._call("/api/tags").get("models", [])}
        if self.name not in names and f"{self.name}:latest" not in names:
            raise LlmError(f"model {self.name} is not pulled; run `ollama pull {self.name}`")

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any],
                  max_output_tokens: int | None = None) -> dict[str, Any]:
        options = {"temperature": 0}
        if max_output_tokens is not None:
            options["num_predict"] = max_output_tokens
        reply = self._call("/api/chat", {"model": self.name, "messages": messages, "stream": False,
                                         "think": False, "format": schema, "options": options})
        content = (reply.get("message") or {}).get("content", "")
        try:
            out = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmError(f"invalid JSON from model: {content[:120]!r}") from exc
        if not isinstance(out, dict):
            raise LlmError("model reply is not a JSON object")
        return out

    def vision_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], images: list[bytes],
                    max_output_tokens: int | None = None) -> dict[str, Any]:
        if not images:
            raise LlmError("vision calls require an image")
        vision_messages = [dict(message) for message in messages]
        user = next((message for message in reversed(vision_messages) if message.get("role") == "user"), None)
        if user is None:
            raise LlmError("vision calls require a user message")
        user["images"] = [b64encode(image).decode("ascii") for image in images]
        options: dict[str, Any] = {"temperature": 0}
        if max_output_tokens is not None:
            options["num_predict"] = max_output_tokens
        response = self._call("/api/chat", {"model": self.name, "messages": vision_messages, "stream": False,
                                            "think": False, "format": schema, "options": options})
        content = (response.get("message") or {}).get("content", "")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmError("invalid JSON from vision model") from exc
        if not isinstance(result, dict):
            raise LlmError("vision model reply is not a JSON object")
        return result


class FakeLlm:
    """Answers with a function of the messages; a str answer is parsed like a model reply."""

    def __init__(self, answer: Callable[[list[dict[str, str]]], dict[str, Any] | str], name: str = "fake") -> None:
        self.answer, self.name, self.calls = answer, name, 0

    def check(self) -> None:
        pass

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        out = self.answer(messages)
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except json.JSONDecodeError as exc:
                raise LlmError("invalid JSON from model") from exc
        if not isinstance(out, dict):
            raise LlmError("model reply is not a JSON object")
        return out


def ollama_from_config(cfg: ScreeningConfig, model: str | None = None) -> OllamaClient:
    url = os.environ.get("LITFORGE_OLLAMA_URL") or cfg.ollama_url
    return OllamaClient(url, model or cfg.model, cfg.timeout_s)
