"""Route, guard and audit model calls before touching a provider."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from litforge.analysis.runs import input_hash
from litforge.config import Config, LlamaCppServerConfig, ModelProfileConfig, ReasoningMode, validate_options
from litforge.jobs import check_cancelled
from litforge.llm.audit import ModelCallRecord, record_model_call
from litforge.llm.base import InvalidModelJson, LlmError, LlmUnavailable, ModelReply, ModelTextReply, ModelUsage
from litforge.llm.budget import BudgetExceeded, BudgetGuard
from litforge.llm.evidence import EvidencePacket
from litforge.llm.files import file_lock
from litforge.llm.openai_compatible import OpenAICompatibleClient, schema_messages
from litforge.llm.pricing import OpenRouterPricing, PriceInfo
from litforge.llm.router import CloudApprovalRequired, ModelProfile, ModelRequest, ModelRouter, RoutingError
from litforge.screening.llm import OllamaClient

LOCAL_VISION = "local_vision"  # default route for image calls: the configured local vision model


class GatewayError(RuntimeError):
    pass


class VisionUnsupported(GatewayError):
    """The selected configured provider does not expose image input."""


class ProviderLike(Protocol):
    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any] | ModelReply: ...


class VisionProviderLike(Protocol):
    def vision_json(self, messages: list[dict[str, Any]], schema: dict[str, Any],
                    images: list[bytes]) -> dict[str, Any] | ModelReply: ...


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
    reasoning: ReasoningMode = "off"
    from_task_map: bool = False
    image_description: str = ""
    model_notice: str = ""
    dropped_evidence_ids: list[str] = Field(default_factory=list)


class ModelGateway:
    def __init__(self, conn: sqlite3.Connection, config: Config, providers: Mapping[str, ProviderLike] | None = None,
                 provider_factory: Callable[[str, ModelProfileConfig], ProviderLike] | None = None,
                 pricing: OpenRouterPricing | None = None,
                 ensure_server: Callable[[LlamaCppServerConfig, str], bool] | None = None,
                 progress: Callable[[str], None] | None = None) -> None:
        self.conn, self.config = conn, config
        self.providers = dict(providers or {})
        self.provider_factory = provider_factory
        database = conn.execute("PRAGMA database_list").fetchone()[2]
        root = Path(database).parent.parent if database else Path("data")
        root = config.data_root or root
        self.data_root = root
        self.pricing = pricing or OpenRouterPricing(root / "cache" / "http")
        from litforge.llm.llamacpp_server import LlamaCppServer
        self.ensure_server = ensure_server or LlamaCppServer(root).ensure_running
        self.progress = progress

    def _profiles(self) -> dict[str, ModelProfileConfig]:
        vision = ModelProfileConfig(provider="ollama", model=self.config.vision.model or "disabled",
                                    vision=self.config.vision.model is not None, timeout_s=self.config.vision.timeout_s)
        return {LOCAL_VISION: vision, **self.config.models.profiles, "local": self.config.models.local}

    def resolve(self, task: str, profile: str | None = None, vision: bool = False) -> tuple[str, ModelProfileConfig]:
        mapping = self.config.models.tasks.get(task)
        default = (self.config.models.vision_profile or LOCAL_VISION) if vision else "local"
        name = profile or (mapping.profile if mapping else None) or default
        if name not in self._profiles():
            raise RoutingError(f"unknown profile: {name}")
        return name, self._profiles()[name]

    def _reasoning(self, task: str, cfg: ModelProfileConfig) -> ReasoningMode:
        mapping = self.config.models.tasks.get(task)
        return (mapping.reasoning if mapping else None) or cfg.reasoning

    def _output(self, task: str, cfg: ModelProfileConfig, cap: int | None,
                schema: dict[str, Any] | None = None, estimated: int | None = None) -> int:
        if cap is not None:
            return cap
        mapping = self.config.models.tasks.get(task)
        if mapping is not None and mapping.max_output_tokens is not None:
            return mapping.max_output_tokens
        if cfg.max_output_tokens is not None:
            return cfg.max_output_tokens
        defaults = {"asset_analyze": 1024 if schema and "latex" in schema.get("properties", {}) else 4096,
                    "page_rewrite": 8192, "claim_extract": 2048}
        output = defaults.get(task, 1024)
        if self._reasoning(task, cfg) == "on":
            output = max(output, cfg.reasoning_output_tokens)
        if task == "page_rewrite" and cfg.context_tokens is not None and estimated is not None:
            output = min(output, max(1, cfg.context_tokens - estimated))
        return output

    def price_info(self, cfg: ModelProfileConfig) -> PriceInfo:
        if cfg.pricing_source == "openrouter":
            return self.pricing.lookup(cfg.model)
        return PriceInfo(pricing=cfg.pricing, source="config")

    def _guard(self) -> BudgetGuard:
        return BudgetGuard(**self.config.budget.model_dump())

    def _route(self, task: str, evidence: EvidencePacket, profile: str | None, approved: bool,
               vision: bool = False) -> ModelProfile:
        selected, cfg = self.resolve(task, profile, vision)
        # Task assignments are deliberate: never silently replace a mapped cloud model.
        if profile is None and task in self.config.models.tasks and cfg.remote and not self.config.privacy.allow_cloud:
            raise RoutingError("cloud disabled")
        router = ModelRouter({name: ModelProfile(name=name, provider=p.provider, model=p.model, remote=p.remote)
                              for name, p in self._profiles().items()}, "local")
        chosen = router.route(ModelRequest(task=task, evidence=evidence, preferred_profile=selected,
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
    def _estimate(messages: list[dict[str, Any]], max_output_tokens: int,
                  schema: dict[str, Any] | None = None) -> int:
        if max_output_tokens < 0:
            raise GatewayError("max_output_tokens must be non-negative")
        chars = sum(len(str(m.get("content", ""))) for m in messages)
        return math.ceil((chars + (len(json.dumps(schema)) if schema is not None else 0)) / 4)

    @staticmethod
    def _context(cfg: ModelProfileConfig, estimated: int, cap: int) -> None:
        if cfg.context_tokens is not None and estimated + cap > cfg.context_tokens:
            raise GatewayError(f"request does not fit the model's context of {cfg.context_tokens} tokens")

    def fit_evidence(self, task: str, evidence: EvidencePacket,
                     build: Callable[[EvidencePacket], list[dict[str, str]]], profile: str | None = None,
                     max_output_tokens: int | None = None, image_count: int = 0, schema: dict[str, Any] | None = None
                     ) -> tuple[EvidencePacket, list[dict[str, str]]]:
        _, cfg = self.resolve(task, profile, bool(image_count))
        cap = self._output(task, cfg, max_output_tokens, schema)
        items, dropped = list(evidence.items), list(evidence.dropped_ids)
        original_ids = {e.id for e in evidence.items}
        while True:
            packet = EvidencePacket(items=items, sensitive=evidence.sensitive,
                                    dropped_ids=[i.id for i in evidence.items if i.id in dropped] +
                                                [i for i in evidence.dropped_ids if i not in original_ids])
            messages = schema_messages(build(packet), schema, cfg.structured_output)
            estimated = self._estimate(messages, cap, schema if cfg.structured_output == "json_schema" else None)
            estimated += 1024 * image_count
            cap = self._output(task, cfg, max_output_tokens, schema, estimated)
            if cfg.context_tokens is None or estimated + cap <= cfg.context_tokens:
                return packet, messages
            if len(items) <= 1:
                self._context(cfg, estimated, cap)
            dropped.append(items.pop().id)

    def preview(self, task: str, evidence: EvidencePacket, messages: list[dict[str, Any]],
                profile: str | None = None, max_output_tokens: int | None = None,
                schema: dict[str, Any] | None = None) -> CallPreview:
        return self._preview(task, evidence, messages, profile, max_output_tokens, 0, schema)

    def _preview(self, task: str, evidence: EvidencePacket, messages: list[dict[str, Any]],
                 profile: str | None, max_output_tokens: int | None, image_count: int,
                 schema: dict[str, Any] | None = None) -> CallPreview:
        selected = profile or (self.config.models.tasks[task].profile if task in self.config.models.tasks else
                               (self.config.models.vision_profile or LOCAL_VISION) if image_count else "local")
        estimated = self._estimate(messages, max_output_tokens or 1024) + 1024 * image_count
        if selected not in self._profiles():
            return CallPreview(profile=selected, provider="", model="", remote=False,
                               evidence_ids=[e.id for e in evidence.items], estimated_input_tokens=estimated,
                               max_output_tokens=max_output_tokens or 1024, max_cost_usd=None, requires_approval=False,
                               blocked_reason="unknown profile")
        blocked = None
        try:
            selected = self._route(task, evidence, profile, approved=True, vision=bool(image_count)).name
        except RoutingError as exc:
            blocked = "local_only policy" if "local_only" in str(exc) else str(exc)
        cfg = self._profiles()[selected]
        cap = self._output(task, cfg, max_output_tokens, schema)
        messages = schema_messages(messages, schema, cfg.structured_output)
        estimated = self._estimate(messages, cap, schema if cfg.structured_output == "json_schema" else None)
        estimated += 1024 * image_count
        cap = self._output(task, cfg, max_output_tokens, schema, estimated)
        approval = cfg.remote and (self.config.models.policy == "ask_before_cloud" or evidence.sensitive or
                                   task == "page_rewrite")
        cost: float | None = 0
        if blocked is None:
            try:
                self._context(cfg, estimated, cap)
                if image_count and not cfg.vision:
                    raise VisionUnsupported("vision is not supported by this profile")
            except GatewayError as exc:
                blocked = str(exc)
        if cfg.remote:
            price = (self.price_info(cfg).pricing if blocked is None or cfg.pricing_source == "config" else None)
            cost = None if price is None else BudgetGuard._cost(price, estimated, cap)
            if blocked is None:
                try:
                    self._guard().check(price, estimated, cap, *self._spend())
                except BudgetExceeded:
                    blocked = "missing pricing" if price is None else "over budget"
        return CallPreview(profile=selected, provider=cfg.provider, model=cfg.model, remote=cfg.remote,
                           evidence_ids=[e.id for e in evidence.items], estimated_input_tokens=estimated,
                           max_output_tokens=cap, max_cost_usd=cost, requires_approval=approval, blocked_reason=blocked,
                           reasoning=self._reasoning(task, cfg), from_task_map=profile is None and
                           task in self.config.models.tasks, dropped_evidence_ids=evidence.dropped_ids,
                           model_notice=self._model_notice(cfg),
                           image_description=f"{image_count} page images will be sent"
                           if task == "page_rewrite" and cfg.remote else
                           f"{image_count} page images" if task == "page_rewrite" else
                           f"{image_count} images" if image_count else "")

    def preview_vision(self, task: str, evidence: EvidencePacket, messages: list[dict[str, Any]],
                       profile: str | None = None, max_output_tokens: int | None = None,
                       image_count: int = 1, schema: dict[str, Any] | None = None) -> CallPreview:
        if image_count < 1:
            raise GatewayError("vision preview requires at least one image")
        return self._preview(task, evidence, messages, profile, max_output_tokens, image_count, schema)

    def _setup_ollama(self, cfg: ModelProfileConfig) -> bool:
        return (not cfg.remote and cfg.provider == "ollama" and self.config.models.setup_tier is not None
                and cfg.model in {"qwen3:4b", "qwen2.5vl:7b"})

    def _model_notice(self, cfg: ModelProfileConfig) -> str:
        if cfg.server and self.config.models.servers[cfg.server].managed:
            return "starts llama-server; the model loads on first use (may download weights)"
        if self._setup_ollama(cfg):
            return "first use downloads model weights if needed; loading may take several minutes"
        return ""

    def _default_provider(self, name: str, profile: ModelProfileConfig) -> ProviderLike:
        if profile.provider == "ollama":
            return OllamaClient(os.environ.get("LITFORGE_OLLAMA_URL") or self.config.screening.ollama_url,
                                profile.model, profile.timeout_s)
        if profile.provider == "openai_compatible":
            if not profile.base_url or (profile.remote and not profile.api_key_env):
                raise GatewayError("OpenAI-compatible profile requires base_url; remote profiles require api_key_env")
            return OpenAICompatibleClient(profile.base_url, profile.model, profile.api_key_env,
                                          timeout_s=profile.timeout_s)
        raise GatewayError("unknown model provider")

    @staticmethod
    def _safe_error(exc: Exception, remote: bool = True) -> LlmError | GatewayError:
        if isinstance(exc, VisionUnsupported):
            return exc
        if isinstance(exc, InvalidModelJson):
            return InvalidModelJson("invalid JSON response from provider", exc.usage)
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
             max_output_tokens: int | None = None, prompt_version: str = "") -> ModelReply:
        reply = self._dispatch(task, evidence, messages, schema, profile, approved, run_id, max_output_tokens,
                               prompt_version, [])
        assert isinstance(reply, ModelReply)
        return reply

    def _dispatch(self, task: str, evidence: EvidencePacket, messages: list[dict[str, Any]],
                  schema: dict[str, Any] | None, profile: str | None, approved: bool, run_id: str | None,
                  max_output_tokens: int | None, prompt_version: str, images: list[bytes]
                  ) -> ModelReply | ModelTextReply:
        for attempt in range(2):
            check_cancelled()
            try:
                return self._once(task, evidence, messages, schema, profile, approved, run_id,
                                  max_output_tokens, prompt_version, images)
            except InvalidModelJson:
                if attempt or schema is None:
                    raise LlmError("invalid JSON response from provider") from None
                messages = [*messages, {"role": "system", "content": "Return only valid JSON for this schema: " +
                                       json.dumps(schema, sort_keys=True)}]
        raise AssertionError("unreachable")

    def _once(self, task: str, evidence: EvidencePacket, messages: list[dict[str, Any]],
              schema: dict[str, Any] | None, profile: str | None, approved: bool, run_id: str | None,
              max_output_tokens: int | None, prompt_version: str, images: list[bytes]
              ) -> ModelReply | ModelTextReply:
        _, selected_cfg = self.resolve(task, profile, bool(images))
        if task == "page_rewrite" and selected_cfg.remote and not approved:
            raise CloudApprovalRequired("Page rewrite sends whole-page images and requires per-run approval")
        chosen = self._route(task, evidence, profile, approved, bool(images))
        cfg = self._profiles()[chosen.name]
        cap = self._output(task, cfg, max_output_tokens, schema)
        messages = schema_messages(messages, schema, cfg.structured_output)
        estimated = self._estimate(messages, cap, schema if cfg.structured_output == "json_schema" else None)
        estimated += 1024 * len(images)
        cap = self._output(task, cfg, max_output_tokens, schema, estimated)
        if images and not cfg.vision:
            raise VisionUnsupported("vision is not supported by this profile")
        self._context(cfg, estimated, cap)
        guard = self._guard()
        spent_run, spent_month = self._spend(run_id)
        price = self.price_info(cfg).pricing if chosen.remote else None
        if chosen.remote:
            guard.check(price, estimated, cap, spent_run, spent_month)
        validate_options(cfg.extra_body, cfg.headers)
        provider = self._provider(chosen.name, cfg)
        if isinstance(provider, OpenAICompatibleClient) and chosen.remote:
            if not cfg.api_key_env:
                raise GatewayError("remote profiles require api_key_env")
            provider.api_key_env = cfg.api_key_env
        cold = False
        if cfg.server:
            cold = self.ensure_server(self.config.models.servers[cfg.server], cfg.model)
            if cold and self.progress:
                self.progress(f"Loading {cfg.model}…")
        if isinstance(provider, OllamaClient) and self._setup_ollama(cfg):
            with file_lock(self.data_root / "state" / "ollama-models.lock", cfg.load_timeout_s):
                names = {m.get("name") for m in provider._call("/api/tags").get("models", [])}
                if cfg.model not in names and f"{cfg.model}:latest" not in names:
                    if self.progress:
                        self.progress(f"Downloading {cfg.model}…")
                    previous_timeout = provider.timeout_s
                    try:
                        provider.timeout_s = cfg.load_timeout_s
                        provider._call("/api/pull", {"name": cfg.model, "stream": False})
                    finally:
                        provider.timeout_s = previous_timeout
        reasoning = self._reasoning(task, cfg)
        payload: dict[str, Any] = {"messages": messages, "schema": schema, "evidence_hash": evidence.content_hash,
                   "model": chosen.model, "provider": chosen.provider}
        if images:
            payload["image_hashes"] = [input_hash(image.hex()) for image in images]
        if cfg.extra_body:
            payload["extra_body"] = cfg.extra_body  # canonical JSON is hashed; raw options are never persisted
        if reasoning != "off":
            payload["reasoning"] = reasoning
        record = ModelCallRecord(provider=chosen.provider, model=chosen.model, task=task,
            input_hash=input_hash(payload), evidence_ids=[e.id for e in evidence.items], run_id=run_id,
            prompt_version=prompt_version, status="ok", reasoning=reasoning, dropped_evidence_ids=evidence.dropped_ids)
        if run_id and evidence.dropped_ids:
            row = self.conn.execute("SELECT dropped_evidence_ids_json FROM analysis_runs WHERE id=?",
                                    (run_id,)).fetchone()
            dropped = list(dict.fromkeys([*(json.loads(row[0]) if row else []), *evidence.dropped_ids]))
            self.conn.execute("UPDATE analysis_runs SET dropped_evidence_ids_json=? WHERE id=?",
                              (json.dumps(dropped), run_id))
        started = time.monotonic()
        options: dict[str, Any] = dict(max_output_tokens=cap, reasoning=reasoning, reasoning_param=cfg.reasoning_param,
                       reasoning_effort=cfg.reasoning_effort, structured_output=cfg.structured_output,
                       extra_body=cfg.extra_body, headers=cfg.headers, context_tokens=cfg.context_tokens)
        if cold and cfg.server and self.config.models.servers[cfg.server].managed:
            options["request_timeout_s"] = cfg.load_timeout_s
        try:
            if isinstance(provider, OllamaClient | OpenAICompatibleClient):
                if images:
                    result = (provider.vision_text(messages, images, **options) if schema is None else
                              provider.vision_json(messages, schema, images, **options))
                else:
                    assert schema is not None
                    result = provider.chat_json(messages, schema, **options)
            elif images:
                method = getattr(provider, "vision_text" if schema is None else "vision_json", None)
                if not callable(method):
                    raise VisionUnsupported("vision is not supported by this provider")
                result = method(messages, images) if schema is None else method(messages, schema, images)
            else:
                assert schema is not None
                result = provider.chat_json(messages, schema)
            if isinstance(result, ModelReply | ModelTextReply):
                reply = result
            elif schema is None and isinstance(result, str):
                reply = ModelTextReply(content=result, provider=chosen.provider, model=chosen.model)
            else:
                reply = ModelReply(content=result, provider=chosen.provider, model=chosen.model, usage=ModelUsage())
            if (schema is None) != isinstance(reply, ModelTextReply):
                raise GatewayError("provider returned an invalid response")
        except Exception as exc:
            error = self._safe_error(exc, chosen.remote)
            record.status, record.error = "error", str(error)
            record.latency_ms = int((time.monotonic() - started) * 1000)
            if isinstance(exc, InvalidModelJson) and exc.usage is not None:
                record.input_tokens, record.output_tokens = exc.usage.input_tokens, exc.usage.output_tokens
                if chosen.remote and price is not None:
                    record.cost_usd = BudgetGuard._cost(price, record.input_tokens, record.output_tokens)
                    try:
                        guard.check_actual(price, record.input_tokens, record.output_tokens, spent_run, spent_month)
                    except BudgetExceeded:
                        record.status, record.error = "over_budget", "actual cost over budget"
                        record_model_call(self.conn, record)
                        raise
            record_model_call(self.conn, record)
            raise error from None
        record.latency_ms = int((time.monotonic() - started) * 1000)
        record.input_tokens, record.output_tokens = reply.usage.input_tokens, reply.usage.output_tokens
        if chosen.remote and price is not None:
            record.cost_usd = BudgetGuard._cost(price, record.input_tokens, record.output_tokens)
            try:
                guard.check_actual(price, record.input_tokens, record.output_tokens, spent_run, spent_month)
            except BudgetExceeded:
                record.status, record.error = "over_budget", "actual cost over budget"
                record_model_call(self.conn, record)
                raise
        record_model_call(self.conn, record)
        return reply

    def call_vision(self, task: str, evidence: EvidencePacket, messages: list[dict[str, Any]],
                    schema: dict[str, Any], images: list[bytes], profile: str | None = None,
                    approved: bool = False, run_id: str | None = None, max_output_tokens: int | None = None,
                    prompt_version: str = "") -> ModelReply:
        if not images:
            raise ValueError("vision calls require at least one image")
        reply = self._dispatch(task, evidence, messages, schema, profile, approved, run_id, max_output_tokens,
                               prompt_version, images)
        assert isinstance(reply, ModelReply)
        return reply

    def call_vision_text(self, task: str, evidence: EvidencePacket, messages: list[dict[str, Any]],
                         images: list[bytes], profile: str | None = None, approved: bool = False,
                         run_id: str | None = None, max_output_tokens: int | None = None,
                         prompt_version: str = "") -> ModelTextReply:
        if not images:
            raise ValueError("vision calls require at least one image")
        reply = self._dispatch(task, evidence, messages, None, profile, approved, run_id, max_output_tokens,
                               prompt_version, images)
        assert isinstance(reply, ModelTextReply)
        return reply
