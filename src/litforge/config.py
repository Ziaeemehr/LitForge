"""Configuration: YAML defaults, environment overrides and data-directory layout."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field
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


class ParsingConfig(BaseModel):
    fast_path: str = "pymupdf"
    complex_fallback: str = "docling"
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
    model: str = "qwen3:4b"  # any Ollama model; qwen3:8b is slower but more careful
    ollama_url: str = "http://localhost:11434"  # LITFORGE_OLLAMA_URL overrides
    default_top: int = 100  # papers of a run screened when no count is given
    timeout_s: float = 120


class VisionConfig(BaseModel):
    """Local vision model (Ollama) that rewrites pages with equations, tables or broken text as Markdown with
    LaTeX. qwen2.5vl:7b needs about 6 GB of GPU memory; pull with `ollama pull qwen2.5vl:7b`."""

    model: str = "qwen2.5vl:7b"
    dpi: int = 150  # page image resolution sent to the model
    formula_dpi: int = 150  # page image resolution for Pix2Text (medium level)
    timeout_s: float = 300


class PrivacyConfig(BaseModel):
    allow_cloud: bool = False
    send_full_pdf: bool = False
    require_confirmation_for_sensitive_content: bool = True


class ModelProfileConfig(BaseModel):
    provider: str
    model: str
    remote: bool = False
    base_url: str | None = None
    api_key_env: str | None = None
    pricing: Pricing | None = None


class ModelsConfig(BaseModel):
    policy: Literal["local_only", "ask_before_cloud", "hybrid"] = "local_only"
    local: ModelProfileConfig = Field(
        default_factory=lambda: ModelProfileConfig(provider="ollama", model="qwen3:4b")
    )
    profiles: dict[str, ModelProfileConfig] = Field(default_factory=dict)


class BudgetConfig(BaseModel):
    max_per_call_usd: float = 1
    max_per_run_usd: float = 2
    max_per_month_usd: float = 20


class Config(BaseModel):
    parsing: ParsingConfig = Field(default_factory=ParsingConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    similar: SimilarConfig = Field(default_factory=SimilarConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)


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
        default = os.environ.get("LITFORGE_EXPORT_ROOT") or "~/Downloads/LitForge"
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


def load_config(path: Path | None = None) -> Config:
    """Load defaults from configs/default.yaml, then merge an optional override file."""
    data: dict[str, Any] = {}
    if DEFAULT_CONFIG_PATH.exists():
        data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) or {}
    override = path or EnvSettings().config
    if override is not None:
        data = _deep_merge(data, yaml.safe_load(Path(override).read_text()) or {})
    return Config.model_validate(data)


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


def contact_email(paths: Paths, config: Config) -> str | None:
    """The user's contact e-mail for OpenAlex/Crossref polite pools and Unpaywall, which requires one.
    Only ever one the user gave: LITFORGE_MAILTO, the web UI setting, or discovery.mailto in the config."""
    return EnvSettings().mailto or load_settings(paths).get("mailto") or config.discovery.mailto
