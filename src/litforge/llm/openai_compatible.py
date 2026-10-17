from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
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

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> ModelReply:
        self.check()
        key = os.environ[self.api_key_env]
        body = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {"name": "litforge", "schema": schema}},
            "temperature": 0,
        }
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
