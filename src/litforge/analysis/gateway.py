"""Route, guard and audit model calls before touching a provider."""

from __future__ import annotations

import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel

from litforge.analysis.runs import input_hash
from litforge.config import Config, ModelProfileConfig
from litforge.llm.audit import ModelCallRecord, record_model_call
from litforge.llm.base import LlmError, LlmUnavailable, ModelReply, ModelUsage
from litforge.llm.budget import BudgetExceeded, BudgetGuard
from litforge.llm.evidence import EvidencePacket
from litforge.llm.openai_compatible import OpenAICompatibleClient
from litforge.llm.router import CloudApprovalRequired, ModelProfile, ModelRequest, ModelRouter, RoutingError
from litforge.screening.llm import OllamaClient


class GatewayError(RuntimeError):
    pass


class ProviderLike(Protocol):
    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any] | ModelReply: ...


class CallPreview(BaseModel):
    profile: str
    provider: str
    model: str
    remote: bool
    evidence_ids: list[str]
    estimated_input_tokens: int
    max_output_tokens: int
    max_cost_usd: float | None
    requires_approval: bool
    blocked_reason: str | None = None


class ModelGateway:
    def __init__(self, conn: sqlite3.Connection, config: Config, providers: Mapping[str, ProviderLike] | None = None,
                 provider_factory: Callable[[str, ModelProfileConfig], ProviderLike] | None = None) -> None:
        self.conn, self.config = conn, config
        self.providers = dict(providers or {})
        self.provider_factory = provider_factory

    def _profiles(self) -> dict[str, ModelProfileConfig]:
        return {**self.config.models.profiles, "local": self.config.models.local}

    def _guard(self) -> BudgetGuard:
        return BudgetGuard(**self.config.budget.model_dump())

    def _route(self, task: str, evidence: EvidencePacket, profile: str | None, approved: bool) -> ModelProfile:
        router = ModelRouter({name: ModelProfile(name=name, provider=p.provider, model=p.model, remote=p.remote)
                              for name, p in self._profiles().items()}, "local")
        chosen = router.route(ModelRequest(task=task, evidence=evidence, preferred_profile=profile,
                                           allow_cloud=self.config.privacy.allow_cloud),
                              self.config.models.policy, approved).profile
        if chosen.remote and not self.config.privacy.allow_cloud:
            raise RoutingError("cloud disabled")
        if (chosen.remote and evidence.sensitive and self.config.privacy.require_confirmation_for_sensitive_content
                and not approved):
            raise CloudApprovalRequired("sensitive content requires approval")
        return chosen

    def _spend(self, run_id: str | None = None) -> tuple[float, float]:
        spent_run = 0.0
        if run_id is not None:
            spent_run = float(self.conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE run_id=?", (run_id,),
            ).fetchone()[0])
        month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        spent_month = float(self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM model_calls WHERE julianday(created_at)>=julianday(?)",
            (month,),
        ).fetchone()[0])
        return spent_run, spent_month

    @staticmethod
    def _estimate(messages: list[dict[str, str]], max_output_tokens: int) -> int:
        if max_output_tokens < 0:
            raise GatewayError("max_output_tokens must be non-negative")
        return math.ceil(sum(len(m.get("content", "")) for m in messages) / 4)

    def preview(self, task: str, evidence: EvidencePacket, messages: list[dict[str, str]],
                profile: str | None = None, max_output_tokens: int = 1024) -> CallPreview:
        estimated = self._estimate(messages, max_output_tokens)
        profiles = self._profiles()
        selected = profile or "local"
        if selected not in profiles:
            return CallPreview(profile=selected, provider="", model="", remote=False,
                               evidence_ids=[e.id for e in evidence.items], estimated_input_tokens=estimated,
                               max_output_tokens=max_output_tokens, max_cost_usd=None, requires_approval=False,
                               blocked_reason="unknown profile")
        blocked = None
        try:
            selected = self._route(task, evidence, profile, approved=True).name
        except RoutingError as exc:
            blocked = "local_only policy" if "local_only" in str(exc) else str(exc)
        chosen = profiles[selected]
        approval = chosen.remote and (self.config.models.policy == "ask_before_cloud" or evidence.sensitive)
        cost: float | None = 0
        if chosen.remote:
            cost = None if chosen.pricing is None else BudgetGuard._cost(chosen.pricing, estimated, max_output_tokens)
            if blocked is None:
                try:
                    self._guard().check(chosen.pricing, estimated, max_output_tokens, *self._spend())
                except BudgetExceeded:
                    blocked = "missing pricing" if chosen.pricing is None else "over budget"
        return CallPreview(profile=selected, provider=chosen.provider, model=chosen.model, remote=chosen.remote,
                           evidence_ids=[e.id for e in evidence.items], estimated_input_tokens=estimated,
                           max_output_tokens=max_output_tokens, max_cost_usd=cost, requires_approval=approval,
                           blocked_reason=blocked)

    def _default_provider(self, name: str, profile: ModelProfileConfig) -> ProviderLike:
        if profile.provider == "ollama":
            return OllamaClient(os.environ.get("LITFORGE_OLLAMA_URL") or self.config.screening.ollama_url,
                                profile.model, self.config.screening.timeout_s)
        if profile.provider == "openai_compatible":
            if not profile.base_url or not profile.api_key_env:
                raise GatewayError("OpenAI-compatible profile requires base_url and api_key_env")
            return OpenAICompatibleClient(profile.base_url, profile.model, profile.api_key_env)
        raise GatewayError("unknown model provider")

    @staticmethod
    def _safe_error(exc: Exception, remote: bool = True) -> LlmError | GatewayError:
        if not remote and (
            isinstance(exc, LlmUnavailable)
            or (isinstance(exc, LlmError)
                and re.fullmatch(r"model \S+ is not pulled; run `ollama pull \S+`", str(exc)))
        ):
            return exc
        if isinstance(exc, LlmUnavailable):
            return LlmUnavailable("model provider unavailable")
        if isinstance(exc, LlmError):
            return LlmError("model provider failed")
        return GatewayError("model provider failed")

    def _provider(self, name: str, profile: ModelProfileConfig) -> ProviderLike:
        if name not in self.providers:
            if self.provider_factory is None:
                self.providers[name] = self._default_provider(name, profile)
            else:
                try:
                    self.providers[name] = self.provider_factory(name, profile)
                except Exception as exc:
                    raise self._safe_error(exc) from None
        return self.providers[name]

    def call(self, task: str, evidence: EvidencePacket, messages: list[dict[str, str]], schema: dict[str, Any],
             profile: str | None = None, approved: bool = False, run_id: str | None = None,
             max_output_tokens: int = 1024, prompt_version: str = "") -> ModelReply:
        chosen = self._route(task, evidence, profile, approved)
        cfg = self._profiles()[chosen.name]
        estimated = self._estimate(messages, max_output_tokens)
        guard = self._guard()
        spent_run, spent_month = self._spend(run_id)
        if chosen.remote:
            guard.check(cfg.pricing, estimated, max_output_tokens, spent_run, spent_month)
        provider = self._provider(chosen.name, cfg)
        record = ModelCallRecord(
            provider=chosen.provider, model=chosen.model, task=task,
            input_hash=input_hash({"messages": messages, "schema": schema, "evidence_hash": evidence.content_hash,
                                   "model": chosen.model, "provider": chosen.provider}),
            evidence_ids=[e.id for e in evidence.items], run_id=run_id, prompt_version=prompt_version, status="ok",
        )
        started = time.monotonic()
        try:
            if isinstance(provider, OllamaClient | OpenAICompatibleClient):
                result = provider.chat_json(messages, schema, max_output_tokens=max_output_tokens)
            else:
                result = provider.chat_json(messages, schema)
            reply = result if isinstance(result, ModelReply) else ModelReply(
                content=result, provider=chosen.provider, model=chosen.model, usage=ModelUsage(),
            )
        except Exception as exc:
            # Provider exception text can contain headers, URLs and credentials. Never persist or repeat it.
            error = self._safe_error(exc, chosen.remote)
            record.status = "error"
            record.error = str(error)
            record.latency_ms = int((time.monotonic() - started) * 1000)
            record_model_call(self.conn, record)
            raise error from None
        record.latency_ms = int((time.monotonic() - started) * 1000)
        record.input_tokens, record.output_tokens = reply.usage.input_tokens, reply.usage.output_tokens
        if chosen.remote and cfg.pricing is not None:
            record.cost_usd = BudgetGuard._cost(cfg.pricing, record.input_tokens, record.output_tokens)
            try:
                guard.check_actual(cfg.pricing, record.input_tokens, record.output_tokens, spent_run, spent_month)
            except BudgetExceeded:
                record.status = "over_budget"
                record.error = "actual cost over budget"
                record_model_call(self.conn, record)
                raise
        record_model_call(self.conn, record)
        return reply
