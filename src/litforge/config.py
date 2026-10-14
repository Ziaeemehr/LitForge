"""Configuration: YAML defaults, environment overrides and data-directory layout."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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


class PrivacyConfig(BaseModel):
    allow_cloud: bool = False


class Config(BaseModel):
    parsing: ParsingConfig = Field(default_factory=ParsingConfig)
    indexing: IndexingConfig = Field(default_factory=IndexingConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    similar: SimilarConfig = Field(default_factory=SimilarConfig)
    screening: ScreeningConfig = Field(default_factory=ScreeningConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)


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
