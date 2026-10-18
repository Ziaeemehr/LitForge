"""Inspect configured models and make a guarded, non-library diagnostic call."""

from __future__ import annotations

import json
import struct
import sys
import time
import zlib
from contextlib import redirect_stdout
from typing import Annotated, Any, Literal, TextIO

import typer

from litforge.analysis.gateway import ModelGateway
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.llm.hardware import detect_hardware
from litforge.llm.llamacpp_server import LlamaCppServer, status_line
from litforge.llm.local_models import HuggingFace, ollama_change, ollama_pull
from litforge.llm.model_setup import (
    CLOUD_NOTICE,
    ConfigChange,
    Tier,
    api_model_change,
    api_model_patch,
    cloud_change,
    remove_model_change,
    setup_proposal,
)
from litforge.llm.pricing import OpenRouterPricing
from litforge.llm.router import CloudApprovalRequired, RoutingError
from litforge.llm.secrets import KeyStore


def _image() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress((b"\0" + b"\xff" * 64 * 3) * 64)) + chunk(b"IEND", b""))


def key_status(variable: str | None) -> str:
    return KeyStore().source(variable) if variable else "not required"


def diagnostic_call(gateway: ModelGateway, profile: str, vision: bool = False,
                    approved: bool = False) -> dict[str, Any]:
    _, cfg = gateway.resolve("asset_analyze" if vision else "ask", profile, vision)
    if cfg.remote and not gateway.config.privacy.allow_cloud:
        raise RoutingError("cloud disabled")
    if cfg.remote and not approved:
        raise CloudApprovalRequired("remote model tests require separate approval")
    key = key_status(cfg.api_key_env)
    if key == "keychain error":
        raise RoutingError("keychain error")

    # Diagnostics require approval even under hybrid policy, and never include library content.
    packet = EvidencePacket(items=[EvidenceItem(id="diagnostic", paper_id="diagnostic", page=1,
                                                text="Fixed model test")], sensitive=cfg.remote)
    messages = [{"role": "user", "content": 'Return {"ok": true} as JSON.'}]
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    started = time.monotonic()
    if vision:
        reply = gateway.call_vision("asset_analyze", packet, messages, schema, [_image()], profile,
                                    approved=approved, max_output_tokens=64, prompt_version="model-test-v1")
    else:
        reply = gateway.call("ask", packet, messages, schema, profile, approved=approved,
                             max_output_tokens=64, prompt_version="model-test-v1")
    return {"reachable": True, "valid": reply.content == {"ok": True}, "key": key,
            "latency_ms": int((time.monotonic() - started) * 1000), "usage": reply.usage.model_dump()}


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return f"{round(seconds)} s" if seconds < 60 else f"{round(seconds / 60)} min"


class DownloadProgress:
    def __init__(self, filename: str, *, stream: TextIO | None = None) -> None:
        self.filename, self.stream = filename, stream if stream is not None else sys.stderr
        self.tty = self.stream.isatty()
        self.last_bucket = -1
        self.width = 0

    def __call__(self, info: dict[str, Any]) -> None:
        bucket = min(20, int(info['bytes'] * 20 / max(1, info['total'])))
        if not self.tty and bucket <= self.last_bucket:
            return
        self.last_bucket = bucket
        line = (f"{self.filename}: {info['bytes']}/{info['total']} bytes · "
                f"{info['speed'] / 1e6:.1f} MB/s · ETA {format_eta(info['eta_s'])}")
        self.stream.write(('\r' + line.ljust(self.width)) if self.tty else line + '\n')
        self.width = len(line)
        self.stream.flush()

    def finish(self) -> None:
        if self.tty and self.width:
            self.stream.write('\n')
            self.stream.flush()
            self.width = 0


def register(app: typer.Typer) -> None:
    models = typer.Typer(help="Choose and test model backends.", no_args_is_help=True)
    app.add_typer(models, name="models")
    keys_app = typer.Typer(help="Manage system keychain entries; environment variables take precedence.")
    models.add_typer(keys_app, name="key")

    @keys_app.command("set")
    def key_set(ctx: typer.Context, name: str) -> None:
        keys = KeyStore(ctx.obj.config)
        try:
            keys.validate(name)
            value = typer.prompt("API key", hide_input=True, confirmation_prompt=True)
            keys.set(name, value)
            typer.echo("Saved in the system keychain.")
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @keys_app.command("remove")
    def key_remove(ctx: typer.Context, name: str) -> None:
        try:
            removed = KeyStore(ctx.obj.config).remove(name)
            typer.echo("Keychain entry removed; environment variables are unchanged." if removed else
                       f"No keychain entry for {name}.")
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @keys_app.command("status")
    def keys_status(ctx: typer.Context) -> None:
        keys = KeyStore(ctx.obj.config)
        for entry in keys.status()["keys"]:
            typer.echo(f"{entry['name']}: {entry['source']}")
        if not keys.available:
            typer.echo("System keychain unavailable; set keys as environment variables.")

    def write(change: ConfigChange, yes: bool = False, *, show_diff: bool = True, json_output: bool = False) -> None:
        if show_diff:
            typer.echo(change.diff or "No changes.")
        if json_output:
            with redirect_stdout(sys.stderr):
                confirmed = yes or typer.confirm("Apply these files?", default=False, err=True)
        else:
            confirmed = yes or typer.confirm("Apply these files?", default=False)
        if confirmed:
            change.apply()
            typer.echo("Saved. Defaults and explicit --config overrides still apply.", err=json_output)

    @models.command("setup")
    def setup(ctx: typer.Context, tier: Annotated[Tier | None, typer.Option("--tier")] = None,
              cheap: Annotated[str | None, typer.Option("--cheap")] = None,
              strong: Annotated[str | None, typer.Option("--strong")] = None,
              yes: Annotated[bool, typer.Option("--yes")] = False,
              as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        state = ctx.obj
        try:
            proposal = setup_proposal(state.paths.root, detect_hardware(state.paths.root), tier, cheap=cheap,
                strong=strong, catalogue=OpenRouterPricing(state.paths.model_cache).catalogue() if cheap else None)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None
        if as_json:
            typer.echo(json.dumps(proposal.as_dict(), indent=2))
        else:
            hw = proposal.hardware
            typer.echo(f"Hardware: {hw.os} {hw.arch}, {hw.ram_gb:g} GB "
                       f"{'unified ' if hw.unified_memory else ''}RAM, {hw.gpu_vram_gb:g} GB GPU VRAM")
            why = ("Apple unified memory >=24 GB or NVIDIA >=16 GB VRAM and >=32 GB RAM" if
                   proposal.recommended_tier == "large" else "accelerator memory + RAM >=12 GB" if
                   proposal.recommended_tier == "medium" else "limited accelerator memory")
            typer.echo(f"Tier: {proposal.tier} (hardware recommendation: {proposal.recommended_tier}; {why})")
            model_sizes = {"large": "gemma4 (~18 GB), qwen3-4b (~2.5 GB)",
                           "medium": "qwen3:4b (~2.5 GB), qwen2.5vl:7b (~6 GB)",
                           "small": "qwen3:4b (~2.5 GB)",
                           "cloud": "user-selected OpenRouter models; local qwen3:4b when installed"}
            typer.echo(f"Models: {model_sizes[proposal.tier]}; approximate memory: {proposal.memory_gb:g} GB")
            typer.echo(proposal.notes)
            typer.echo("Files to write:\n" + "\n".join(str(p) for p in proposal.change.after))
            typer.echo(proposal.change.diff or "No changes.")
        if not proposal.available:
            typer.echo("Select catalogue models for cloud, or install the proposed local backend. No files written.",
                       err=as_json)
            raise typer.Exit(1)
        write(proposal.change, yes, show_diff=False, json_output=as_json)

    @models.command("catalogue")
    def catalogue(ctx: typer.Context, vision: Annotated[bool, typer.Option("--vision")] = False,
                  max_price: Annotated[float | None, typer.Option("--max-price")] = None,
                  search: Annotated[str, typer.Option("--search")] = "") -> None:
        try:
            entries = OpenRouterPricing(ctx.obj.paths.model_cache).catalogue(vision=vision,
                max_input_price=max_price, q=search)
            for item in entries:
                typer.echo(f"{item['id']} · {item['name']} · ${item['input_per_million_usd']:g} in / "
                           f"${item['output_per_million_usd']:g} out per million · {item['context_length']} context" +
                           (" · accepts images" if "image" in item['input_modalities'] else "") +
                           (" · " + item["label"] if item.get("label") else ""))
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @models.command("search")
    def search_local(text: str, show_all: Annotated[bool, typer.Option("--show-all")] = False) -> None:
        try:
            for repo in HuggingFace().search(text, show_all=show_all):
                typer.echo(f"{repo['id']} · {repo['downloads']} downloads · {repo['badge']} · {repo['card_url']}" +
                           (" · " + repo['label'] if repo.get('label') else ""))
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @models.command("files")
    def local_files(ctx: typer.Context, repo: str) -> None:
        from litforge.llm.local_models import fit_check
        try:
            tree = HuggingFace().files(repo)
            hw = detect_hardware(ctx.obj.paths.root)
            for item in tree["models"]:
                fit = fit_check(hw, item["size"])
                typer.echo(f"{item['path']} · {item['size'] / 1e9:.2f} GB · {fit['badge']} {fit['note']} · "
                           f"sha256 {item['sha256']}")
            for item in tree["projectors"]:
                typer.echo(f"Image projector: {item['path']} · {item['size'] / 1e9:.2f} GB")
            typer.echo("Fit is approximate: 0.25 MB KV cache per 1000 context tokens + 1 GB overhead.")
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @models.command("download")
    def download(ctx: typer.Context, repo: str, file: str,
                 mmproj: Annotated[str | None, typer.Option("--mmproj")] = None,
                 name: Annotated[str | None, typer.Option("--name")] = None,
                 task: Annotated[list[str] | None, typer.Option("--task")] = None) -> None:
        from litforge.ui.api import ModelsPanel
        panel = ModelsPanel(ctx.obj.paths, ctx.obj.config, hardware=detect_hardware)
        try:
            preview = panel.prepare_download({"repo": repo, "file": file, "mmproj": mmproj,
                                               "name": name, "tasks": task or []})
            typer.echo(json.dumps(preview["fit"], indent=2))
            typer.echo(preview["diff"])
            if preview["fit"]["badge"] == "too large" and not typer.confirm(
                    "This model is too large for this machine. Download anyway?", default=False):
                return
            if not typer.confirm("Download weights and apply these exact files?", default=False):
                return
            plan = panel.download_pending[preview["token"]]
            for item in [plan["item"], *([plan["mmproj"]] if plan["mmproj"] else [])]:
                progress = DownloadProgress(item['path'])
                try:
                    panel.downloader.run(repo, item, progress=progress)
                finally:
                    progress.finish()
            plan["change"].apply()
            typer.echo("Verified and registered. Use Settings task checkboxes to assign more tasks.")
        except KeyboardInterrupt:
            typer.echo("Paused. Repeat the download command to resume.", err=True)
            raise typer.Exit(130) from None
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @models.command("pull-ollama")
    def pull_ollama(ctx: typer.Context, name: str) -> None:
        try:
            change = ollama_change(ctx.obj.paths.root, name)
            typer.echo(change.diff)
            if not typer.confirm("Pull this model through local Ollama and register it?", default=False):
                return
            ollama_pull(name, base_url=ctx.obj.config.screening.ollama_url,
                        progress=lambda item: typer.echo(json.dumps(item)))
            change.apply()
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @models.command("add")
    def add(ctx: typer.Context, provider: str, model: Annotated[str, typer.Option("--model")],
            name: Annotated[str | None, typer.Option("--name")] = None,
            price_in: Annotated[float | None, typer.Option("--price-in")] = None,
            price_out: Annotated[float | None, typer.Option("--price-out")] = None,
            task: Annotated[list[str] | None, typer.Option("--task")] = None,
            vision: Annotated[bool, typer.Option("--vision")] = False,
            reasoning: Annotated[Literal["on", "off"], typer.Option("--reasoning")] = "off") -> None:
        try:
            patch = api_model_patch(provider, model, name=name, price_in=price_in, price_out=price_out,
                                    tasks=task, vision=vision, reasoning=reasoning)
            profile = next(iter(patch["models"]["profiles"].values()))
            if not profile.get("pricing") and provider != "openrouter":
                typer.echo("pricing missing: this profile cannot be used until both prices are supplied.")
            typer.echo("Set " + profile["api_key_env"] + " via models key set or your environment. "
                       "Never paste a key into config.")
            typer.echo("Cloud permission is separate: litforge models cloud on")
            write(api_model_change(ctx.obj.paths.root, patch))
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @models.command("remove")
    def remove(ctx: typer.Context, name: str,
               delete_files: Annotated[bool, typer.Option("--delete-files")] = False) -> None:
        try:
            write(remove_model_change(ctx.obj.paths.root, name, ctx.obj.config, delete_files=delete_files))
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @models.command("cloud")
    def cloud(ctx: typer.Context, mode: Literal["on", "off"]) -> None:
        if mode == "on":
            typer.echo(CLOUD_NOTICE)
        write(cloud_change(ctx.obj.paths.root, mode == "on"))

    @models.command("start")
    def start(ctx: typer.Context) -> None:
        state = ctx.obj
        cfg = state.config.models.servers.get("llamacpp")
        if cfg is None:
            typer.echo("No llama.cpp server configured; run litforge models setup.")
            raise typer.Exit(1)
        profile = next((p for p in [state.config.models.local, *state.config.models.profiles.values()]
                        if p.server == "llamacpp"), None)
        if profile is None:
            typer.echo("No profile links to llama.cpp.")
            raise typer.Exit(1)
        try:
            LlamaCppServer(state.paths.root).ensure_running(cfg, profile.model)
            typer.echo("llama-server running; models load on first use.")
        except RuntimeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from None

    @models.command("stop")
    def stop(ctx: typer.Context) -> None:
        cfg = ctx.obj.config.models.servers.get("llamacpp")
        stopped = cfg is not None and LlamaCppServer(ctx.obj.paths.root).stop(cfg)
        typer.echo("Stopped LitForge's llama-server." if stopped else "No owned llama-server to stop.")

    @models.command("status")
    def status(ctx: typer.Context,
               as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        cfg = ctx.obj.config.models.servers.get("llamacpp")
        report: dict[str, Any] = (LlamaCppServer(ctx.obj.paths.root).status(cfg) if cfg else
                  {"running": False, "models": [], "detail": "No managed server configured"})
        if as_json:
            typer.echo(json.dumps(report, indent=2))
        else:
            typer.echo(status_line(report))

    @models.command("list")
    def list_models(ctx: typer.Context,
                    as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        state = ctx.obj
        conn = state.db()
        try:
            gateway = ModelGateway(conn, state.config)
            if as_json:
                typer.echo(json.dumps({"profiles": {name: cfg.model_dump()
                                                    for name, cfg in gateway._profiles().items()},
                                       "tasks": {name: task.model_dump(exclude_none=True)
                                                 for name, task in state.config.models.tasks.items()}}, indent=2))
                return
            for name, cfg in gateway._profiles().items():
                if name == "local_vision" and state.config.models.vision_profile:
                    continue
                price = gateway.price_info(cfg)
                typer.echo(f"{name}: {cfg.provider} {cfg.model} remote={cfg.remote} vision={cfg.vision} "
                           f"reasoning={cfg.reasoning} context={cfg.context_tokens or 'server default'} "
                           f"pricing={'available' if price.pricing else 'missing' if cfg.remote else 'local/free'} "
                           f"source={price.source}")
            typer.echo(f"{'Task':<20} {'Profile':<24} Reasoning")
            for name, task in state.config.models.tasks.items():
                typer.echo(f"{name:<20} {task.profile:<24} {task.reasoning or 'profile default'}")
        finally:
            conn.close()

    @models.command("pricing")
    def pricing(ctx: typer.Context) -> None:
        state = ctx.obj
        conn = state.db()
        try:
            gateway = ModelGateway(conn, state.config)
            for name, cfg in gateway._profiles().items():
                if name == "local_vision" and state.config.models.vision_profile:
                    continue
                price = gateway.price_info(cfg)
                values = price.pricing.model_dump() if price.pricing else "missing" if cfg.remote else "local/free"
                typer.echo(f"{name}: {values} source={price.source} age_s={price.age_s}")
        finally:
            conn.close()

    @models.command("test")
    def test_model(ctx: typer.Context, profile: str,
                   vision: Annotated[bool, typer.Option("--vision")] = False,
                   approve: Annotated[bool, typer.Option("--approve")] = False) -> None:
        state = ctx.obj
        conn = state.db()
        try:
            gateway = ModelGateway(conn, state.config)
            _, cfg = gateway.resolve("asset_analyze" if vision else "ask", profile, vision)
            typer.echo(f"key: {key_status(cfg.api_key_env)}")
            report = diagnostic_call(gateway, profile, vision, approve)
            typer.echo(f"reachable: yes; JSON: {'valid' if report['valid'] else 'invalid'}; "
                       f"latency: {report['latency_ms']} ms; "
                       f"tokens: input={report['usage']['input_tokens']} output={report['usage']['output_tokens']}")
            if not report["valid"]:
                raise typer.Exit(1)
        except (ValueError, RuntimeError) as exc:
            typer.secho(str(exc), fg="red", err=True)
            raise typer.Exit(1) from None
        finally:
            conn.close()
