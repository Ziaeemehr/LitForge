from __future__ import annotations

import json
import urllib.error
import urllib.request
from base64 import b64encode
from typing import Any, Protocol

from litforge.llm.base import InvalidModelJson, LlmError, LlmUnavailable, ModelReply, ModelTextReply, ModelUsage
from litforge.llm.http import open_url
from litforge.llm.json_schema import check_json_structure, loads_model_json
from litforge.llm.secrets import KeyStore


class HttpTransport(Protocol):
    def request(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]: ...


class UrllibTransport:
    def request(self, url: str, headers: dict[str, str], body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with open_url(request, timeout=timeout_s) as response:  # noqa: S310 - configured provider
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise LlmError(f"provider HTTP error {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LlmUnavailable("provider is unavailable") from exc
        if not isinstance(result, dict):
            raise LlmError("provider response is not an object")
        return result


def schema_messages(messages: list[dict[str, Any]], schema: dict[str, Any] | None,
                    mode: str) -> list[dict[str, Any]]:
    if schema is None or mode == "json_schema":
        return messages
    instruction = "Return only valid JSON for this schema: " + json.dumps(schema, sort_keys=True)
    if any(instruction in str(m.get("content", "")) for m in messages):
        return messages
    return [{"role": "system", "content": instruction}, *messages]


class OpenAICompatibleClient:
    provider = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key_env: str | None = None,
                 transport: HttpTransport | None = None, timeout_s: float = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = self.model = model
        self.api_key_env = api_key_env
        self.transport = transport or UrllibTransport()
        self.timeout_s = timeout_s

    def check(self) -> None:
        if self.api_key_env and not KeyStore().resolve(self.api_key_env):
            raise LlmError(f"API key {self.api_key_env} is missing (environment or system keychain)")

    def _request(self, messages: list[dict[str, Any]], schema: dict[str, Any] | None,
                 max_output_tokens: int | None = None, reasoning: str = "off", reasoning_param: str = "none",
                 reasoning_effort: str = "medium", structured_output: str = "json_schema",
                 extra_body: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                 request_timeout_s: float | None = None, **unused: Any) -> ModelReply | ModelTextReply:
        from litforge.config import validate_options

        validate_options(extra_body or {}, headers or {})
        key = KeyStore().resolve(self.api_key_env)
        if self.api_key_env and not key:
            raise LlmError(f"API key {self.api_key_env} is missing (environment or system keychain)")
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if self.api_key_env:
            request_headers["Authorization"] = f"Bearer {key}"
        body: dict[str, Any] = {"temperature": 0, **(extra_body or {}), "model": self.model,
            "messages": schema_messages(messages, schema, structured_output)}
        if schema is not None and structured_output != "none":
            body["response_format"] = {"type": structured_output}
            if structured_output == "json_schema":
                body["response_format"]["json_schema"] = {"name": "litforge", "schema": schema}
        if max_output_tokens is not None:
            body["max_tokens"] = max_output_tokens
        if reasoning != "auto":
            enabled = reasoning == "on"
            if reasoning_param == "chat_template_kwargs":
                body["chat_template_kwargs"] = {**body.get("chat_template_kwargs", {}), "enable_thinking": enabled}
            elif reasoning_param == "openrouter":
                body["reasoning"] = {**body.get("reasoning", {}), "enabled": enabled}
            elif reasoning_param == "reasoning_effort" and enabled:
                body["reasoning_effort"] = reasoning_effort
        tokens = ModelUsage()
        try:
            response = self.transport.request(f"{self.base_url}/chat/completions", request_headers,
                                              body, self.timeout_s if request_timeout_s is None else request_timeout_s)
            usage = response.get("usage") or {}
            tokens = ModelUsage(input_tokens=int(usage.get("prompt_tokens", 0)),
                                output_tokens=int(usage.get("completion_tokens", 0)))
            content = response["choices"][0]["message"]["content"]
            if schema is None:
                if not isinstance(content, str):
                    raise ValueError("content is not text")
                return ModelTextReply(content=content, provider=self.provider, model=self.model, usage=tokens)
            parsed = loads_model_json(content)
            if not isinstance(parsed, dict):
                raise ValueError("content is not an object")
            check_json_structure(parsed, schema)
            return ModelReply(content=parsed, provider=self.provider, model=self.model, usage=tokens)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise InvalidModelJson("invalid JSON response from provider", tokens) from exc
        except (LlmError, LlmUnavailable):
            raise
        except Exception as exc:
            raise LlmUnavailable(f"provider request failed: {type(exc).__name__}") from exc

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any],
                  max_output_tokens: int | None = None, **options: Any) -> ModelReply:
        result = self._request(messages, schema, max_output_tokens, **options)
        assert isinstance(result, ModelReply)
        return result

    @staticmethod
    def _images(messages: list[dict[str, Any]], images: list[bytes]) -> list[dict[str, Any]]:
        if not images:
            raise LlmError("vision calls require an image")
        visual = [dict(message) for message in messages]
        user = next((message for message in reversed(visual) if message.get("role") == "user"), None)
        if user is None:
            raise LlmError("vision calls require a user message")
        content: list[dict[str, Any]] = [{"type": "text", "text": str(user.get("content", ""))}]
        for image in images:
            mime = "image/png" if image.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
            url = f"data:{mime};base64,{b64encode(image).decode()}"
            content.append({"type": "image_url", "image_url": {"url": url}})
        user["content"] = content
        return visual

    def vision_json(self, messages: list[dict[str, Any]], schema: dict[str, Any], images: list[bytes],
                    max_output_tokens: int | None = None, **options: Any) -> ModelReply:
        result = self._request(self._images(messages, images), schema, max_output_tokens, **options)
        assert isinstance(result, ModelReply)
        return result

    def vision_text(self, messages: list[dict[str, Any]], images: list[bytes],
                    max_output_tokens: int | None = None, **options: Any) -> ModelTextReply:
        result = self._request(self._images(messages, images), None, max_output_tokens, **options)
        assert isinstance(result, ModelTextReply)
        return result
