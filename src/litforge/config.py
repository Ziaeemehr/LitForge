"""Configuration: YAML defaults, environment overrides and data-directory layout."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from litforge.llm.budget import Pricing

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "default.yaml"


class RouterThresholds(BaseModel):
    min_chars_per_page: int = 200
    max_sparse_page_ratio: float = 0.5
    max_replacement_char_ratio: float = 0.01
    max_fragmented_line_ratio: float = 0.35
    max_table_count: int = 6


class MinerUConfig(BaseModel):
    command: str | None = None
    seconds_per_page_timeout: float = Field(default=120, gt=0, allow_inf_nan=False)
    install_timeout_s: float = Field(default=2700, gt=0, allow_inf_nan=False)


class ParsingConfig(BaseModel):
    fast_path: str = "pymupdf"
    complex_fallback: Literal["auto", "mineru", "docling", "none"] = "auto"
    mineru: MinerUConfig = Field(default_factory=MinerUConfig)
    ocr: str = "selective"
    keep_originals: bool = True
    figure_dpi: int = 150
    router: RouterThresholds = Field(default_factory=RouterThresholds)


class IndexingConfig(BaseModel):
    chunk_target_tokens: int = 600
    chunk_overlap_tokens: int = 80
    index_references: bool = False
    index_captions: bool = True


class DiscoveryConfig(BaseModel):
    sources: list[str] = Field(default_factory=lambda: ["openalex", "crossref", "arxiv"])
    limit: int = 25  # results per source and query
    cache_ttl_days: float = 7
    mailto: str | None = None  # optional contact for API "polite pools"; never filled in automatically


class SimilarConfig(BaseModel):
    # Small CPU-friendly default (runs on a laptop). On a GPU machine try e.g. BAAI/bge-large-en-v1.5
    # with embedding_device: cuda (requires the fastembed-gpu package). null disables embeddings.
    embedding_model: str | None = "BAAI/bge-small-en-v1.5"
    embedding_device: str = "cpu"  # cpu | cuda
    per_channel_limit: int = 50  # max papers fetched per channel (references, citing, keywords ...)
    keyword_queries: int = 3


class ScreeningConfig(BaseModel):
    max_false_negative_rate: float = Field(default=0.05, ge=0, le=1)
    model: str = "qwen3:4b"  # any Ollama model; qwen3:8b is slower but more careful
    ollama_url: str = "http://localhost:11434"  # LITFORGE_OLLAMA_URL overrides
    default_top: int = 100  # papers of a run screened when no count is given
    timeout_s: float = 120


class VisionConfig(BaseModel):
    """Local vision model (Ollama) that rewrites pages with equations, tables or broken text as Markdown with
    LaTeX. qwen2.5vl:7b needs about 6 GB of GPU memory; pull with `ollama pull qwen2.5vl:7b`."""

    model: str | None = "qwen2.5vl:7b"
    dpi: int = 150  # page image resolution sent to the model
    formula_dpi: int = 150  # page image resolution for Pix2Text (medium level)
    timeout_s: float = 300


class PrivacyConfig(BaseModel):
    allow_cloud: bool = False
    send_full_pdf: bool = False
    require_confirmation_for_sensitive_content: bool = True


ReasoningMode = Literal["off", "on", "auto"]
TASK_NAMES = ("ask", "audit", "matrix_extract", "claim_extract", "claim_relation", "screen", "screen_fulltext",
              "asset_analyze", "equation_compare", "gap_design", "console_plan", "page_rewrite",
              "write_fix", "write_rewrite", "write_cite")


def reasoning_mode(value: Any) -> Any:
    # YAML 1.1 reads unquoted on/off as booleans.
    return ("on" if value else "off") if isinstance(value, bool) else value


def validate_options(extra_body: dict[str, Any], headers: dict[str, str]) -> None:
    reserved = {"model", "messages", "response_format", "max_tokens"}
    credentials = ("auth", "token", "apikey", "secret", "password", "cookie", "credential", "session")
    def check(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                name = str(key).lower()
                normalized = name.replace("-", "").replace("_", "")
                if name not in {"x-title", "http-referer"} and any(part in normalized for part in credentials):
                    raise ValueError("credentials belong in api_key_env")
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    if reserved & extra_body.keys():
        raise ValueError("extra_body cannot override model, messages, response_format or max_tokens")
    check(extra_body)
    check(headers)
    if any("\r" in k + v or "\n" in k + v for k, v in headers.items()):
        raise ValueError("invalid header")


class ModelProfileConfig(BaseModel):
    provider: str
    model: str
    remote: bool = False
    base_url: str | None = None
    api_key_env: str | None = None
    pricing: Pricing | None = None
    vision: bool = False
    reasoning: ReasoningMode = "off"
    reasoning_param: Literal["none", "chat_template_kwargs", "openrouter", "reasoning_effort"] = "none"
    reasoning_effort: Literal["low", "medium", "high"] = "medium"
    reasoning_output_tokens: int = Field(default=4096, gt=0)
    context_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    structured_output: Literal["json_schema", "json_object", "none"] = "json_schema"
    extra_body: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    pricing_source: Literal["config", "openrouter"] = "config"
    server: Literal["llamacpp"] | None = None
    timeout_s: float = Field(default=120, gt=0, allow_inf_nan=False)
    load_timeout_s: float = Field(default=900, gt=0, allow_inf_nan=False)

    _reasoning = field_validator("reasoning", mode="before")(reasoning_mode)

    @model_validator(mode="after")
    def safe_options(self) -> ModelProfileConfig:
        validate_options(self.extra_body, self.headers)
        if ("reasoning_output_tokens" not in self.model_fields_set and not self.remote and
                self.provider == "openai_compatible" and
                (self.server == "llamacpp" or self.reasoning_param == "chat_template_kwargs")):
            self.reasoning_output_tokens = 8192
        return self


class TaskModelConfig(BaseModel):
    profile: str
    reasoning: ReasoningMode | None = None
    max_output_tokens: int | None = Field(default=None, ge=0)

    _reasoning = field_validator("reasoning", mode="before")(reasoning_mode)


class LlamaCppServerConfig(BaseModel):
    managed: bool = False
    command: str = "llama-server"
    preset: str = "llamacpp-models.ini"
    host: str = "127.0.0.1"
    port: int = Field(default=8093, ge=1, le=65535)
    models_max: int = Field(default=1, ge=1)
    idle_unload_minutes: float = Field(default=15, ge=0, allow_inf_nan=False)
    start_timeout_s: float = Field(default=60, gt=0, allow_inf_nan=False)

    @field_validator("host")
    @classmethod
    def loopback(cls, host: str) -> str:
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError("managed llama.cpp host must be loopback")
        return host

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}/v1"


class SetupOwnedConfig(BaseModel):
    profiles: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    server: Literal["llamacpp"] | None = None


class ModelsConfig(BaseModel):
    setup_owned: SetupOwnedConfig = Field(default_factory=SetupOwnedConfig)
    vision_profile: str | None = None
    policy: Literal["local_only", "ask_before_cloud", "hybrid"] = "local_only"
    local: ModelProfileConfig = Field(
        default_factory=lambda: ModelProfileConfig(provider="ollama", model="qwen3:4b")
    )
    profiles: dict[str, ModelProfileConfig] = Field(default_factory=dict)
    tasks: dict[str, TaskModelConfig] = Field(default_factory=dict)
    servers: dict[Literal["llamacpp"], LlamaCppServerConfig] = Field(default_factory=dict)
    setup_tier: Literal["large", "medium", "small", "cloud"] | None = None

    @model_validator(mode="after")
    def linked_servers(self) -> ModelsConfig:
        for profile in [self.local, *self.profiles.values()]:
            if profile.server:
                if profile.server not in self.servers:
                    raise ValueError("profile server is not configured")
                if profile.remote or profile.provider != "openai_compatible":
                    raise ValueError("server profiles must be local openai_compatible profiles")
                endpoint = self.servers[profile.server].base_url
                if profile.base_url is not None and profile.base_url.rstrip("/") != endpoint:
                    raise ValueError("server profile base_url must match the loopback server")
                profile.base_url = endpoint
        return self

    @field_validator("tasks")
    @classmethod
    def known_tasks(cls, tasks: dict[str, TaskModelConfig]) -> dict[str, TaskModelConfig]:
        if set(tasks) - set(TASK_NAMES):
            raise ValueError("unknown task; valid tasks: " + ", ".join(TASK_NAMES))
        return tasks


class BudgetConfig(BaseModel):
    max_per_call_usd: float = Field(default=1, ge=0, allow_inf_nan=False)
    max_per_run_usd: float = Field(default=2, ge=0, allow_inf_nan=False)
    max_per_month_usd: float = Field(default=20, ge=0, allow_inf_nan=False)


class ConsoleConfig(BaseModel):
    seconds_per_model_call: float = Field(default=2.0, gt=0)


class Config(BaseModel):
    data_root: Path | None = Field(default=None, exclude=True)
    config_path: Path | None = Field(default=None, exclude=True)
    parsing: ParsingConfig = Field(default_factory=ParsingConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    similar: SimilarConfig = Field(default_factory=SimilarConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    console: ConsoleConfig = Field(default_factory=ConsoleConfig)


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LITFORGE_", env_file=".env", extra="ignore")

    home: Path = Path("data")
    config: Path | None = None
    openalex_api_key: str | None = None
    mailto: str | None = None


class Paths:
    """Filesystem layout under the data root. Nothing here deletes files."""

    def __init__(self, root: Path, exports_root: Path | None = None) -> None:
        self.root = root.expanduser().resolve()
        # Exports are meant to be opened by the user, so by default they live outside the data root.
        default = (os.environ.get("LITFORGE_EXPORT_ROOT") or load_settings(self).get("exports_root")
                   or "~/Downloads/LitForge")
        self.exports_root = (exports_root or Path(default)).expanduser().resolve()

    def default_exports(self, project: str) -> Path:
        """Where a project's exports go unless a folder was chosen for it."""
        return self.exports_root / project

    @property
    def library(self) -> Path:
        return self.root / "library"

    @property
    def db(self) -> Path:
        return self.library / "papers.sqlite"

    @property
    def pdfs(self) -> Path:
        return self.library / "pdfs"

    @property
    def parsed(self) -> Path:
        return self.library / "parsed"

    @property
    def projects(self) -> Path:
        return self.root / "projects"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def model_cache(self) -> Path:
        return self.root / "cache" / "models"

    @property
    def http_cache(self) -> Path:
        return self.root / "cache" / "http"

    @property
    def benchmark(self) -> Path:
        return self.root / "benchmark"

    def project_dir(self, name: str) -> Path:
        return self.projects / name

    @property
    def settings(self) -> Path:
        return self.root / "settings.json"

    def ensure(self) -> None:
        for d in (self.library, self.pdfs, self.parsed, self.projects, self.logs):
            d.mkdir(parents=True, exist_ok=True)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | None = None, data_root: Path | None = None) -> Config:
    """Merge defaults, data-directory settings, then explicit/environment overrides."""
    data: dict[str, Any] = {}
    if DEFAULT_CONFIG_PATH.exists():
        data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}
    user = data_root / "config.yaml" if data_root is not None else None
    if user is not None and user.exists():
        data = _deep_merge(data, yaml.safe_load(user.read_text()) or {})
    override = path or EnvSettings().config
    if override is not None:
        data = _deep_merge(data, yaml.safe_load(Path(override).read_text()) or {})
    cfg = Config.model_validate(data)
    cfg.data_root, cfg.config_path = data_root, override
    return cfg


def resolve_paths(data_dir: Path | None = None) -> Paths:
    return Paths(data_dir if data_dir is not None else EnvSettings().home)


def load_settings(paths: Paths) -> dict[str, Any]:
    """Settings chosen in the web UI (data/settings.json); empty when none were saved."""
    try:
        data = json.loads(paths.settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_mailto(paths: Paths, mailto: str | None) -> str | None:
    """Save the contact e-mail the user typed in ("" or None clears it); returns the saved value."""
    mailto = (mailto or "").strip() or None
    if mailto and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", mailto):
        raise ValueError("not an e-mail address")
    settings = {**load_settings(paths), "mailto": mailto}
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    paths.settings.write_text(json.dumps(settings, indent=1) + "\n", encoding="utf-8")
    return mailto


def save_update_check(paths: Paths, enabled: bool) -> None:
    """Save automatic update consent alongside the other UI settings."""
    if not isinstance(enabled, bool):
        raise ValueError('update_check must be a boolean')
    settings = {**load_settings(paths), 'update_check': enabled}
    paths.settings.parent.mkdir(parents=True, exist_ok=True)
    paths.settings.write_text(json.dumps(settings, indent=1) + '\n', encoding='utf-8')


def contact_email(paths: Paths, config: Config) -> str | None:
    """The user's contact e-mail for OpenAlex/Crossref polite pools and Unpaywall, which requires one.
    Only ever one the user gave: LITFORGE_MAILTO, the web UI setting, or discovery.mailto in the config."""
    return EnvSettings().mailto or load_settings(paths).get("mailto") or config.discovery.mailto
