from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from base64 import b64encode
from typing import Any, Protocol

from litforge.llm.base import LlmError, LlmUnavailable, ModelReply, ModelUsage


class HttpTransport(Protocol):
    def request(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]: ...


class UrllibTransport:
    def request(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - configured provider
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise LlmError(f"provider HTTP error {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LlmUnavailable("provider is unavailable") from exc
        if not isinstance(result, dict):
            raise LlmError("provider response is not an object")
        return result


class OpenAICompatibleClient:
    provider = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key_env: str, transport: HttpTransport | None = None,
                 timeout_s: float = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = self.model = model
        self.api_key_env = api_key_env
        self.transport = transport or UrllibTransport()
        self.timeout_s = timeout_s

    def check(self) -> None:
        if not os.environ.get(self.api_key_env):
            raise LlmError(f"API key environment variable {self.api_key_env} is not set")

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any],
                  max_output_tokens: int | None = None) -> ModelReply:
        self.check()
        key = os.environ[self.api_key_env]
        body = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {"name": "litforge", "schema": schema}},
            "temperature": 0,
        }
        if max_output_tokens is not None:
            body["max_tokens"] = max_output_tokens
        try:
            response = self.transport.request(
                f"{self.base_url}/chat/completions",
                {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                body,
                self.timeout_s,
            )
            content = response["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("content is not an object")
            usage = response.get("usage") or {}
            return ModelReply(
                content=parsed,
                provider=self.provider,
                model=self.model,
                usage=ModelUsage(input_tokens=int(usage.get("prompt_tokens", 0)),
                                 output_tokens=int(usage.get("completion_tokens", 0))),
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LlmError("invalid JSON response from provider") from exc
        except (LlmError, LlmUnavailable):
            raise
        except Exception as exc:
            raise LlmUnavailable(f"provider request failed: {type(exc).__name__}") from exc

    def vision_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], images: list[bytes],
                    max_output_tokens: int | None = None) -> ModelReply:
        if not images:
            raise LlmError("vision calls require an image")
        self.check()
        key = os.environ[self.api_key_env]
        visual = [dict(message) for message in messages]
        user = next((message for message in reversed(visual) if message.get("role") == "user"), None)
        if user is None:
            raise LlmError("vision calls require a user message")
        text = str(user.get("content", ""))
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for image in images:
            mime = "image/png" if image.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
            encoded = b64encode(image).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        user["content"] = content
        body: dict[str, Any] = {
            "model": self.model,
            "messages": visual,
            "response_format": {"type": "json_schema", "json_schema": {"name": "litforge", "schema": schema}},
            "temperature": 0,
        }
        if max_output_tokens is not None:
            body["max_tokens"] = max_output_tokens
        try:
            response = self.transport.request(
                f"{self.base_url}/chat/completions",
                {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                body,
                self.timeout_s,
            )
            content_text = response["choices"][0]["message"]["content"]
            parsed = json.loads(content_text)
            if not isinstance(parsed, dict):
                raise ValueError("content is not an object")
            usage = response.get("usage") or {}
            return ModelReply(
                content=parsed,
                provider=self.provider,
                model=self.model,
                usage=ModelUsage(input_tokens=int(usage.get("prompt_tokens", 0)),
                                 output_tokens=int(usage.get("completion_tokens", 0))),
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LlmError("invalid JSON response from provider") from exc
        except (LlmError, LlmUnavailable):
            raise
        except Exception as exc:
            raise LlmUnavailable(f"provider request failed: {type(exc).__name__}") from exc
