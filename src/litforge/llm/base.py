from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class LlmError(RuntimeError):
    pass


class InvalidModelJson(LlmError):
    def __init__(self, message: str, usage: ModelUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


class LlmUnavailable(LlmError):
    pass


class ModelUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class ModelReply(BaseModel):
    content: dict[str, Any]
    provider: str
    model: str
    usage: ModelUsage = Field(default_factory=ModelUsage)


class ModelProvider(Protocol):
    name: str

    def check(self) -> None: ...

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> ModelReply: ...


class ModelTextReply(BaseModel):
    content: str
    provider: str
    model: str
    usage: ModelUsage = Field(default_factory=ModelUsage)
