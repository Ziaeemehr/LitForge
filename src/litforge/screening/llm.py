"""Local LLM access for screening: Ollama over HTTP (stdlib only) and a fake for tests."""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from base64 import b64encode
from collections.abc import Callable
from typing import Any, Protocol

from litforge.config import ScreeningConfig, validate_options
from litforge.llm import LlmError as LlmError
from litforge.llm import LlmUnavailable as LlmUnavailable
from litforge.llm import ModelReply
from litforge.llm.base import InvalidModelJson, ModelTextReply, ModelUsage
from litforge.llm.http import open_url
from litforge.llm.json_schema import check_json_structure
from litforge.llm.openai_compatible import schema_messages


class Llm(Protocol):
    name: str

    def check(self) -> None: ...

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any] | ModelReply: ...


def reply_content(reply: dict[str, Any] | ModelReply) -> dict[str, Any]:
    return reply.content if isinstance(reply, ModelReply) else reply


class OllamaClient:
    def __init__(self, url: str, model: str, timeout_s: float = 120) -> None:
        self.url, self.name, self.timeout_s = url.rstrip("/"), model, timeout_s

    def _call(self, path: str, body: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json", **(headers or {})},
                                     method="POST" if data is not None else "GET")
        try:
            with open_url(req, timeout=self.timeout_s) as resp:  # noqa: S310 - local URL
                return json.load(resp)  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            raise LlmError(f"Ollama error {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LlmUnavailable(f"Ollama not reachable at {self.url} ({exc}); start it with `ollama serve`") from exc

    def check(self) -> None:
        names = {m.get("name") for m in self._call("/api/tags").get("models", [])}
        if self.name not in names and f"{self.name}:latest" not in names:
            raise LlmError(f"model {self.name} is not pulled; run `ollama pull {self.name}`")

    def _request(self, messages: list[dict[str, Any]], schema: dict[str, Any] | None,
                 images: list[bytes] | None = None, max_output_tokens: int | None = None,
                 reasoning: str = "off", structured_output: str = "json_schema",
                 extra_body: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                 context_tokens: int | None = None, **unused: Any
                 ) -> dict[str, Any] | ModelReply | ModelTextReply:
        validate_options(extra_body or {}, headers or {})
        visual = [dict(m) for m in schema_messages(messages, schema, structured_output)]
        if context_tokens is None:
            chars = sum(len(str(m.get("content", ""))) for m in visual)
            if schema is not None and structured_output == "json_schema":
                chars += len(json.dumps(schema))
            needed = math.ceil(chars / 4) + (max_output_tokens or 0) + 1024 * len(images or [])
            context_tokens = next((size for size in (8192, 16384, 32768) if needed <= size), None)
            if context_tokens is None:
                raise LlmError("request exceeds automatic Ollama context; set profile.context_tokens explicitly")
        if images:
            user = next((m for m in reversed(visual) if m.get("role") == "user"), None)
            if user is None:
                raise LlmError("vision calls require a user message")
            user["images"] = [b64encode(image).decode() for image in images]
        body: dict[str, Any] = {**(extra_body or {}), "model": self.name, "messages": visual, "stream": False,
                                "options": {"temperature": 0, **(extra_body or {}).get("options", {}),
                                            "num_ctx": context_tokens}}
        if max_output_tokens is not None:
            body["options"]["num_predict"] = max_output_tokens
        if reasoning != "auto":
            body["think"] = reasoning == "on"
        if schema is not None and structured_output != "none":
            body["format"] = schema if structured_output == "json_schema" else "json"
        reply = self._call("/api/chat", body, headers=headers) if headers else self._call("/api/chat", body)
        usage = ModelUsage(input_tokens=reply.get("prompt_eval_count", 0), output_tokens=reply.get("eval_count", 0))
        content = (reply.get("message") or {}).get("content", "")
        if schema is None:
            return ModelTextReply(content=content, provider="ollama", model=self.name,
                                  usage=usage)
        try:
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("model reply is not a JSON object")
            check_json_structure(result, schema)
        except (ValueError, TypeError) as exc:
            raise InvalidModelJson("invalid JSON response from provider", usage) from exc
        if "prompt_eval_count" in reply or "eval_count" in reply:
            return ModelReply(content=result, provider="ollama", model=self.name, usage=usage)
        return result

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any],
                  max_output_tokens: int | None = None, **options: Any) -> dict[str, Any] | ModelReply:
        result = self._request(messages, schema, max_output_tokens=max_output_tokens, **options)
        assert isinstance(result, dict | ModelReply)
        return result

    def vision_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], images: list[bytes],
                    max_output_tokens: int | None = None, **options: Any) -> dict[str, Any] | ModelReply:
        if not images:
            raise LlmError("vision calls require an image")
        result = self._request(messages, schema, images, max_output_tokens, **options)
        assert isinstance(result, dict | ModelReply)
        return result

    def vision_text(self, messages: list[dict[str, Any]], images: list[bytes],
                    max_output_tokens: int | None = None, **options: Any) -> ModelTextReply:
        result = self._request(messages, None, images, max_output_tokens, **options)
        assert isinstance(result, ModelTextReply)
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
