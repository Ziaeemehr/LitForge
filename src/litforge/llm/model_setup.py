"""Reviewable model proposals and partial, atomic user configuration updates."""

from __future__ import annotations

import difflib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from litforge.config import DEFAULT_CONFIG_PATH, TASK_NAMES, Config, LlamaCppServerConfig, _deep_merge
from litforge.llm.files import atomic_write, file_lock
from litforge.llm.hardware import Hardware

CONFIG_NOTE = ("# Managed in part by LitForge (models setup / models add). Comments elsewhere in this file "
               "are not preserved; the previous version is in config.yaml.bak.")

Tier = Literal["large", "medium", "small", "cloud"]
# Thresholds in GiB: large Apple RAM, large NVIDIA VRAM/RAM, medium accelerator/RAM.
THRESHOLDS = {"large_apple": 24, "large_vram": 16, "large_ram": 32, "medium": 12}
INSTALL_LLAMA = "brew install llama.cpp; or https://github.com/ggml-org/llama.cpp/releases"
PROVIDERS = {
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "openrouter", "json_schema"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "reasoning_effort", "json_schema"),
    "anthropic": ("https://api.anthropic.com/v1/", "ANTHROPIC_API_KEY", "none", "json_object"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY", "none", "json_object"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "none", "json_object"),
    "qwen": ("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY", "none", "json_object"),
    "kimi": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY", "none", "json_object"),
}
PRESET = """[gemma4]
hf-repo = ggml-org/gemma-4-26B-A4B-it-GGUF:Q4_0
ctx-size = 16384
batch-size = 2048
ubatch-size = 1152
n-gpu-layers = 99
reasoning = auto

[qwen3-4b]
hf-repo = ggml-org/Qwen3-4B-GGUF:Q4_K_M
ctx-size = 16384
n-gpu-layers = 99
reasoning = auto
"""


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


@dataclass
class ConfigChange:
    root: Path
    before: dict[Path, str | None]
    after: dict[Path, str]

    stop_server: LlamaCppServerConfig | None = None
    delete_paths: list[Path] = field(default_factory=list)

    @property
    def diff(self) -> str:
        diff = "".join("".join(difflib.unified_diff((self.before[path] or "").splitlines(True),
                       content.splitlines(True), fromfile=str(path), tofile=str(path)))
                       for path, content in self.after.items())
        diff += "".join(f"Delete downloaded model file: {path}\n" for path in self.delete_paths)
        return diff if CONFIG_NOTE in diff else CONFIG_NOTE + "\n" + diff

    def apply(self) -> None:
        with file_lock(self.root / "state" / "config.lock"):
            if any(_read(path) != content for path, content in self.before.items()):
                raise ValueError("configuration changed since preview; review the new diff")
            if self.stop_server is not None:
                from litforge.llm.llamacpp_server import LlamaCppServer
                LlamaCppServer(self.root).stop(self.stop_server)
            config_path = self.root / "config.yaml"
            if config_path.exists():
                shutil.copyfile(config_path, self.root / "config.yaml.bak")
            # Preset first: a published config must never reference a missing preset.
            for path, content in reversed(list(self.after.items())):
                atomic_write(path, content)
            for path in self.delete_paths:
                if path.resolve().is_relative_to((self.root / "models").resolve()) and not path.is_symlink():
                    path.unlink(missing_ok=True)


def config_change(root: Path, patch: dict[str, Any], *,
                  remove: list[tuple[str, ...]] | None = None, preset: str | None = None) -> ConfigChange:
    path = root / "config.yaml"
    original = _read(path)
    data = yaml.safe_load(original or "") or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml must contain a mapping")
    for keys in remove or []:
        node = data
        for key in keys[:-1]:
            node = node.get(key, {})
        node.pop(keys[-1], None)
    data = _deep_merge(data, patch)
    # Validate the effective defaults + user config without writing anything.
    defaults = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text()) if DEFAULT_CONFIG_PATH.exists() else {}
    Config.model_validate(_deep_merge(defaults, data))
    content = CONFIG_NOTE + "\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    before, after = {path: original}, {path: content}
    if preset is not None:
        preset_path = root / "llamacpp-models.ini"
        before[preset_path], after[preset_path] = _read(preset_path), preset
    return ConfigChange(root, before, after)


@dataclass
class SetupProposal:
    hardware: Hardware
    recommended_tier: Tier
    tier: Tier
    available: bool
    notes: str
    download_gb: float
    memory_gb: float
    preset: str
    change: ConfigChange

    def as_dict(self) -> dict[str, Any]:
        return {"hardware": self.hardware.model_dump(), "recommended_tier": self.recommended_tier,
                "tier": self.tier, "available": self.available, "notes": self.notes,
                "download_gb": self.download_gb, "memory_gb": self.memory_gb,
                "diff": self.change.diff, "files": [str(p) for p in self.change.after]}


def choose_tier(hw: Hardware) -> Tier:
    t = THRESHOLDS
    if (hw.unified_memory and hw.ram_gb >= t["large_apple"] or
            hw.gpu_vram_gb >= t["large_vram"] and hw.ram_gb >= t["large_ram"]):
        return "large"
    if (hw.unified_memory and hw.ram_gb >= t["medium"] or
            hw.gpu_vram_gb > 0 and
            hw.gpu_vram_gb + hw.ram_gb >= t["medium"]):
        return "medium"
    return "small"


Mode = Literal["local", "cloud", "hybrid", "manual"]
# Many calls on many papers: on this computer in Hybrid. Every other task (questions, analysis, writing) is the
# quality work a strong cloud model does better.
BULK_TASKS = frozenset({"screen", "screen_fulltext", "matrix_extract", "claim_extract", "asset_analyze",
                        "equation_compare", "page_rewrite"})


def setup_proposal(root: Path, hw: Hardware, tier: Tier | None = None, *, cheap: str | None = None,
                   strong: str | None = None, catalogue: list[dict[str, Any]] | None = None,
                   mode: Mode | None = None) -> SetupProposal:
    """Without `mode`: the tier as chosen. With a mode: Local (the hardware's tier, local only), Cloud (every
    task on cloud models), Hybrid (bulk work local, quality work on the strong cloud model) or Manual (only the
    choice is recorded; the user's assignments stay)."""
    hardware_tier = choose_tier(hw)
    recommended: Tier = "cloud" if hardware_tier == "small" else hardware_tier
    if mode == "manual":
        change = config_change(root, {"models": {"mode": "manual"}})
        return SetupProposal(hw, recommended, tier or recommended, True,
                             "Manual: your own task assignments are kept as they are.", 0.0, 0.0, "", change)
    if mode in ("local", "hybrid"):
        tier = tier if tier in ("large", "medium", "small") else hardware_tier
    elif mode == "cloud":
        tier = "cloud"
    chosen = tier or recommended
    notes = "Downloads happen on first use, after confirmation; nothing is installed by setup."
    if chosen == "large" and not hw.llama_server:
        notes += " llama-server missing: " + INSTALL_LLAMA + "."
        if tier is None and hw.ollama:
            chosen = "medium"
            notes += " Offering installed Ollama instead."
    available = bool(hw.llama_server if chosen == "large" else True if chosen == "cloud" else hw.ollama)
    if chosen not in {"large", "cloud"} and not hw.ollama:
        notes += " Ollama missing: brew install ollama (macOS), or https://ollama.com/download."
    patch: dict[str, Any] = {"models": {"setup_tier": chosen}}
    models = patch["models"]
    if mode is not None:
        models["mode"] = mode
    preset = ""
    if chosen == "cloud":
        if mode is None:  # the tier path asks before every call; a mode keeps the consent the user gave
            patch["privacy"] = {"allow_cloud": False}
        models["policy"] = "ask_before_cloud" if mode is None else "hybrid"
        models["local"] = {"provider": "ollama", "model": "qwen3:4b", "remote": False,
                           "server": None, "base_url": None, "vision": False}
        models["profiles"], models["tasks"] = {}, {}
        models["vision_profile"] = "cloud_cheap" if cheap else None
        patch["vision"] = {"model": None}
        sizes = (3.0 if hw.ollama else 0.0, 4.0 if hw.ollama else 0.0)
        if not cheap:
            available = False
            notes = "Choose a cheap image-capable model from the catalogue, and optionally a strong analysis model."
        else:
            entries = {m["id"]: m for m in catalogue or []}
            if cheap not in entries or "image" not in entries[cheap]["input_modalities"]:
                raise ValueError("Choose a catalogued cheap model that accepts images")
            if strong and strong not in entries:
                raise ValueError("Choose a catalogued strong model")
            models["profiles"].update(_cloud_profiles(entries, cheap, strong))
            extraction = {"matrix_extract", "claim_extract", "asset_analyze", "equation_compare", "page_rewrite"}
            local_screening = hw.ollama and mode is None  # Cloud mode: light work goes to the cloud too
            models["tasks"] = {task: {"profile": "local" if local_screening and task in {"screen", "screen_fulltext"}
                else "cloud_cheap" if task in extraction or task in {"screen", "screen_fulltext"}
                else "cloud_strong" if strong else "cloud_cheap"} for task in TASK_NAMES}
            estimate = cloud_estimate(entries[cheap])
            notes = (f"Typical matrix (20 papers × 10 columns, 1000 input + 100 output tokens/cell): "
                     f"${estimate['matrix_usd']:.4f}; one page rewrite (1024 input + 4000 output tokens): "
                     f"${estimate['page_rewrite_usd']:.4f}. Estimates; actual usage varies. "
                     "Cloud stays off until you confirm Enable cloud.")
    elif chosen == "large":
        preset = PRESET
        models["servers"] = {"llamacpp": {"managed": True, "command": hw.llama_server or "llama-server",
                                        "preset": "llamacpp-models.ini", "host": "127.0.0.1", "port": 8093,
                                        "models_max": 1, "idle_unload_minutes": 15, "start_timeout_s": 60}}
        def profile(model: str, vision: bool = False) -> dict[str, Any]:
            return {"provider": "openai_compatible", "model": model, "server": "llamacpp", "remote": False,
                    "vision": vision, "context_tokens": 16384, "reasoning_param": "chat_template_kwargs",
                    "timeout_s": 600, "reasoning_output_tokens": 8192}
        models["profiles"] = {"gemma4": profile("gemma4", True), "qwen3-4b": profile("qwen3-4b")}
        models["local"] = profile("qwen3-4b")
        models["tasks"] = {task: {"profile": "qwen3-4b" if task in {"screen", "screen_fulltext"} else "gemma4",
                                 "reasoning": "off"}
                           for task in TASK_NAMES}
        models["vision_profile"] = "gemma4"
        patch["screening"] = {"model": "qwen3-4b"}
        sizes = (20.0, 24.0)
    else:
        models["vision_profile"] = None
        models["local"] = {"provider": "ollama", "model": "qwen3:4b", "remote": False,
                           "server": None, "base_url": None, "vision": False}
        models["tasks"] = {task: {"profile": "local"} for task in TASK_NAMES
                           if task not in {"asset_analyze", "page_rewrite", "equation_compare"}}
        if chosen == "medium":
            models["profiles"] = {"ollama_vision": {"provider": "ollama", "model": "qwen2.5vl:7b", "vision": True}}
            models["tasks"].update({task: {"profile": "ollama_vision"} for task in
                                   ("asset_analyze", "page_rewrite", "equation_compare")})
        else:
            models["tasks"].update({task: {"profile": "local_vision"} for task in
                                   ("asset_analyze", "page_rewrite", "equation_compare")})
        patch["vision"] = {"model": "qwen2.5vl:7b" if chosen == "medium" else None}
        patch["screening"] = {"model": "qwen3:4b"}
        sizes = (8.0, 12.0) if chosen == "medium" else (3.0, 4.0)
    if mode == "local":
        models["policy"] = "local_only"
        patch["privacy"] = {"allow_cloud": False}
    if mode == "hybrid":
        # The local tier above does the bulk work; questions, analysis and writing go to a cloud model.
        cloud_model = strong or cheap
        if not cloud_model:
            available = False
            notes = "Hybrid needs a cloud model for questions, analysis and writing: choose one from the catalogue."
        else:
            entries = {m["id"]: m for m in catalogue or []}
            if cloud_model not in entries:
                raise ValueError("Choose a catalogued cloud model")
            alias = "cloud_strong" if strong else "cloud_cheap"
            models.setdefault("profiles", {}).update(_cloud_profiles(entries, None if strong else cheap, strong))
            models["tasks"].update({task: {"profile": alias} for task in TASK_NAMES if task not in BULK_TASKS})
            models["policy"] = "hybrid"
    if hw.free_disk_gb and hw.free_disk_gb < sizes[0]:
        notes += " Free disk space is below the approximate download size."
    previous = yaml.safe_load(_read(root / "config.yaml") or "") or {}
    old_models = previous.get("models", {})
    owned = old_models.get("setup_owned", {})
    # A user mapping wins over setup. models add releases ownership when it assigns a task.
    user_tasks = set(old_models.get("tasks", {})) - set(owned.get("tasks", []))
    models["tasks"] = {key: value for key, value in models["tasks"].items() if key not in user_tasks}
    user_profiles = set(old_models.get("profiles", {})) - set(owned.get("profiles", []))
    if "profiles" in models:
        models["profiles"] = {key: value for key, value in models["profiles"].items() if key not in user_profiles}
    server_owned = chosen == "large"
    if chosen == "large" and "llamacpp" in old_models.get("servers", {}) and owned.get("server") != "llamacpp":
        # Do not claim or overwrite a server configured by the user, including its preset.
        models.pop("servers")
        preset, server_owned = "", False
        notes += " Existing user-configured llama.cpp server and preset retained."
    models["setup_owned"] = {"profiles": list(models.get("profiles", {})),
                             "tasks": list(models["tasks"]),
                             "server": "llamacpp" if server_owned else None}
    remove = [("models", "local"),
              *(("models", "profiles", name) for name in owned.get("profiles", [])),
              *(("models", "tasks", name) for name in owned.get("tasks", []))]
    stop_server = None
    if owned.get("server") and chosen != "large":
        key = owned["server"]
        stop_server = LlamaCppServerConfig.model_validate(old_models["servers"][key])
        remove.append(("models", "servers", key))
    change = config_change(root, patch, preset=preset or None, remove=remove)
    change.stop_server = stop_server
    return SetupProposal(hw, recommended, chosen, available, notes, *sizes, preset, change)


def _cloud_profiles(entries: dict[str, dict[str, Any]], cheap: str | None,
                    strong: str | None) -> dict[str, dict[str, Any]]:
    """OpenRouter profiles for the chosen catalogue models: cloud_cheap and / or cloud_strong."""
    profiles = {}
    for alias, model_id in (("cloud_cheap", cheap), ("cloud_strong", strong)):
        if not model_id:
            continue
        entry = entries[model_id]
        profile = api_model_patch("openrouter", model_id, name=alias,
            price_in=entry["input_per_million_usd"], price_out=entry["output_per_million_usd"],
            vision="image" in entry["input_modalities"])["models"]["profiles"][alias]
        profile["context_tokens"] = entry["context_length"]
        profiles[alias] = profile
    return profiles


def api_model_patch(provider: str, model: str, *, name: str | None = None, price_in: float | None = None,
                    price_out: float | None = None, tasks: list[str] | None = None, vision: bool = False,
                    reasoning: str = "off") -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise ValueError("unknown provider; choose " + ", ".join(PROVIDERS))
    if not model.strip() or any(ord(c) < 32 for c in model):
        raise ValueError("model ID is required")
    name = name or provider + "_" + re.sub(r"[^a-zA-Z0-9_-]", "_", model)
    if name in {"local", "local_vision"} or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("choose a profile name with letters, numbers, hyphens or underscores (not local/local_vision)")
    url, key, param, mode = PROVIDERS[provider]
    profile: dict[str, Any] = {"provider": "openai_compatible", "model": model, "base_url": url,
                              "api_key_env": key, "remote": True, "vision": vision,
                              "reasoning": reasoning, "reasoning_param": param, "structured_output": mode}
    if (price_in is None) != (price_out is None):
        raise ValueError("provide both --price-in and --price-out, or neither (pricing missing)")
    if price_in is not None:
        profile["pricing"] = {"input_per_million_usd": price_in, "output_per_million_usd": price_out}
    if provider == "openrouter":
        profile.update(pricing_source="openrouter", headers={"X-Title": "LitForge"},
                       extra_body={"provider": {"data_collection": "deny", "require_parameters": True}})
    patch = {"models": {"profiles": {name: profile}, "tasks": {task: {"profile": name} for task in tasks or []}}}
    Config.model_validate(patch)
    return patch


def remove_model_change(root: Path, name: str, config: Config, *, delete_files: bool = False) -> ConfigChange:
    if name in {"local", "local_vision"} or name not in config.models.profiles:
        raise ValueError("unknown or built-in profile")
    remove: list[tuple[str, ...]] = [("models", "profiles", name), *(("models", "tasks", task)
              for task, mapping in config.models.tasks.items() if mapping.profile == name)]
    if config.models.vision_profile == name:
        remove.append(("models", "vision_profile"))
    import configparser
    profile = config.models.profiles[name]
    preset = None
    delete_paths = []
    if profile.server == "llamacpp":
        parser = configparser.ConfigParser(interpolation=None)
        path = root / "llamacpp-models.ini"
        parser.read(path)
        # A downloaded section uses model=; HF first-use presets and external files are retained.
        if parser.has_section(profile.model) and parser.has_option(profile.model, "model"):
            paths = [Path(parser[profile.model][k]) for k in ("model", "mmproj") if k in parser[profile.model]]
            shared = any(p.server == "llamacpp" and p.model == profile.model for key, p in
                         {"local": config.models.local, **config.models.profiles}.items() if key != name)
            if not shared:
                parser.remove_section(profile.model)
                import io
                stream = io.StringIO()
                parser.write(stream)
                preset = stream.getvalue()
                retained = {Path(section[key]).resolve() for section in parser.values() for key in ("model", "mmproj")
                            if key in section}
                if delete_files:
                    delete_paths = [p for p in paths if p.resolve().is_relative_to((root / "models").resolve())
                                    and p.resolve() not in retained]
    owned = config.models.setup_owned.model_dump()
    owned["profiles"] = [p for p in owned["profiles"] if p != name]
    owned["tasks"] = [t for t in owned["tasks"] if config.models.tasks.get(t) is not None
                      and config.models.tasks[t].profile != name]
    change = config_change(root, {"models": {"setup_owned": owned}}, remove=remove, preset=preset)
    change.delete_paths = delete_paths
    if preset is not None and (server := config.models.servers.get("llamacpp")) and server.managed:
        change.stop_server = server
    return change


def cloud_change(root: Path, enabled: bool) -> ConfigChange:
    return config_change(root, {"privacy": {"allow_cloud": enabled},
                               "models": {"policy": "ask_before_cloud" if enabled else "local_only"}})


CLOUD_NOTICE = ("Cloud models send selected evidence text and, for image actions, page/asset images to the provider. "
                "Sensitive content still requires separate approval. "
                "API keys remain in environment variables or the system keychain.")


def api_model_change(root: Path, patch: dict[str, Any]) -> ConfigChange:
    patch = _deep_merge({}, patch)
    previous = yaml.safe_load(_read(root / "config.yaml") or "") or {}
    owned = previous.get("models", {}).get("setup_owned", {})
    if owned:
        patch["models"]["setup_owned"] = {
            **owned,
            "profiles": [name for name in owned.get("profiles", []) if name not in patch["models"]["profiles"]],
            "tasks": [task for task in owned.get("tasks", []) if task not in patch["models"].get("tasks", {})]}
    return config_change(root, patch, remove=[("models", "profiles", name)
                                             for name in patch["models"]["profiles"]])


def math_high_cloud(config: Config) -> dict[str, Any] | None:
    """When Better math (high) runs on a cloud model: its name and the estimated cost of one page (a page image
    in, the page's text out), so the choice can say so before anything is sent. None when it runs locally."""
    task = config.models.tasks.get("page_rewrite")
    name = task.profile if task else config.models.vision_profile
    profile = config.models.profiles.get(name or "") if name not in (None, "local") else None
    if profile is None or not profile.remote:
        return None
    price = profile.pricing
    usd = (1024 * price.input_per_million_usd + 4000 * price.output_per_million_usd) / 1_000_000 if price else None
    return {"model": profile.model, "usd_per_page": usd}


def cloud_estimate(model: dict[str, Any], *, papers: int = 20, columns: int = 10,
                   average_input_tokens: int = 1000, average_output_tokens: int = 100) -> dict[str, float]:
    price_in, price_out = model["input_per_million_usd"], model["output_per_million_usd"]
    return {"matrix_usd": papers * columns * (average_input_tokens * price_in + average_output_tokens * price_out)
            / 1_000_000, "page_rewrite_usd": (1024 * price_in + 4000 * price_out) / 1_000_000}
