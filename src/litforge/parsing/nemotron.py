"""NVIDIA Nemotron 3.5 Parse: a small (0.6B) document parser that reads a page image into classed blocks.

On an NVIDIA GPU it reads a page in about 5 s with about 2 GB of memory, and reads tables better than the
other engines here. It is weak on Persian (right-to-left text mixed with Latin), so Persian pages go to the
Ollama vision model instead (`is_persian`). On the processor it takes about two minutes per page, so it is
only chosen automatically when a GPU with enough free memory is present (`gpu_ready`).

The model, its code and its output helpers come from one pinned revision of the Hugging Face repository,
downloaded under `<data>/models/nemotron-parse`. The Python packages come from the `nemotron` extra.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from litforge.config import Paths
from litforge.parsing.pymupdf_parser import join_lines

REPO = "nvidia/Nemotron-3.5-Parse"
REVISION = "62cb1e4ba153d113ff64aaf2be6e068c377cf53c"
NAME = f"nemotron-3.5-parse@{REVISION[:8]}"
PROMPT = "</s><s><predict_bbox><predict_classes><output_markdown><predict_text_in_pic>"
MAX_SIZE = (1664, 2048)  # the model card's largest recommended page image (width, height)
MAX_NEW_TOKENS = 8744
MIN_FREE_GPU_GB = 3.0
MODEL_GB = 2.5
PACKAGES = ("torch", "torchvision", "transformers", "timm", "bs4")
# Everything the model and its output helpers need; the repository's docs, tests and benchmarks are left out.
FILES = ["*.json", "*.py", "*.jinja", "tokenizer*", "model.safetensors", "nemotron_parse/**", "LICENSE",
         "THIRD_PARTY_NOTICES.md"]
SKIP_FILES = ["tests/**", "reference/**", "logitsprocs/**", "vllm_tied_patch/**", "vllm_example.py"]
_SKIP = {"Page-header", "Page-footer"}
_FIGURE = {"Picture", "Chart"}
_PROSE = {"Text", "List-item", "Caption", "Footnote"}


def model_dir(paths: Paths) -> Path:
    return paths.root / "models" / "nemotron-parse"


def packages_available() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in PACKAGES)


def model_ready(paths: Paths) -> bool:
    folder = model_dir(paths)
    try:
        marker = json.loads((folder / "litforge.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return marker.get("revision") == REVISION and (folder / "model.safetensors").is_file()


def gpu_free_gb() -> float | None:
    """Free memory of the first CUDA device, or None when torch or a CUDA device is missing."""
    if importlib.util.find_spec("torch") is None:
        return None
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        free, _ = torch.cuda.mem_get_info()
        return float(free) / 1024**3
    except Exception:  # noqa: BLE001 - any trouble means: no usable GPU
        return None


def gpu_ready() -> bool:
    free = gpu_free_gb()
    return free is not None and free >= MIN_FREE_GPU_GB


def available(paths: Paths) -> bool:
    """Installed: the packages and the model are there (a GPU is checked separately)."""
    return packages_available() and model_ready(paths)


def status(paths: Paths) -> dict[str, Any]:
    free = gpu_free_gb() if packages_available() else None
    return {"packages": packages_available(), "model": model_ready(paths), "installed": available(paths),
            "gpu_free_gb": None if free is None else round(free, 1), "gpu_ready": free is not None and
            free >= MIN_FREE_GPU_GB, "revision": REVISION, "size": f"~{MODEL_GB:g} GB",
            "path": str(model_dir(paths))}


def download_model(paths: Paths, progress: Callable[[str], None] = lambda _: None) -> Path:
    """Download the pinned revision (about 2.5 GB); a finished download is marked, so a broken one is redone."""
    from huggingface_hub import snapshot_download

    folder = model_dir(paths)
    if model_ready(paths):
        progress(f"{REPO} is already downloaded")
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    progress(f"downloading {REPO} ({MODEL_GB:g} GB) to {folder}")
    snapshot_download(REPO, revision=REVISION, local_dir=folder, allow_patterns=FILES, ignore_patterns=SKIP_FILES)
    (folder / "litforge.json").write_text(json.dumps({"repo": REPO, "revision": REVISION}), encoding="utf-8")
    progress(f"{REPO} is ready")
    return folder


# ---- Persian pages --------------------------------------------------------------------------------------------

_ARABIC_SCRIPT = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_LATIN = re.compile(r"[A-Za-z]")


def is_persian(text: str) -> bool | None:
    """Is the text mostly Persian (Arabic script, including the presentation forms broken PDFs carry)?
    None when there are too few letters to tell, as on a scanned page."""
    arabic, latin = len(_ARABIC_SCRIPT.findall(text)), len(_LATIN.findall(text))
    if arabic + latin < 20:
        return None
    return arabic >= 0.3 * (arabic + latin)


# ---- from model output to LitForge Markdown -------------------------------------------------------------------

_INLINE = re.compile(r"\\\((.+?)\\\)", re.S)
_DISPLAY = re.compile(r"\\\[(.+?)\\\]", re.S)
_SPAN_MATH = re.compile(r"\\\((.+?)\\\)|\\\[(.+?)\\\]|\$\$(.+?)\$\$", re.S)
_TAG = re.compile(r"\((?P<tag>[A-Z]?\d+(?:\.\d+)*[a-z]?)\)\s*$")
_SPAN = re.compile(r"\b(?:col|row)span=\"(\d+)\"")


def _bracket(m: re.Match[str]) -> str:
    # The model escapes a literal bracket the Markdown way, so a citation [16] arrives as \[16\]: only
    # LaTeX-looking content is display math.
    inner = m.group(1).strip()
    return f"$${inner}$$" if _LATEX_HINT.search(inner) else f"[{inner}]"


_LATEX_HINT = re.compile(r"[\\^_={}]")


def inline_math(text: str) -> str:
    """\\( \\) and \\[ \\] become the $ and $$ LitForge writes everywhere else."""
    text = _DISPLAY.sub(_bracket, text)
    return _INLINE.sub(lambda m: f"${m.group(1).strip()}$", text)


def prose(content: str) -> str:
    """A text block as one paragraph: the model keeps the page's line breaks and end-of-line hyphens."""
    return inline_math(join_lines(content.split("\n")))


def formula_latex(content: str) -> tuple[str, str | None] | None:
    """A Formula block as (LaTeX, equation number) when it is nothing but math: one span, or several
    (one per line, joined by <br>) gathered into one display. None when prose is mixed in."""
    tag = _TAG.search(content)
    body = content[:tag.start()] if tag else content
    spans = [next(g for g in m.groups() if g is not None).strip() for m in _SPAN_MATH.finditer(body)]
    if not spans or re.sub(r"<br\s*/?>", "", _SPAN_MATH.sub("", body)).strip():
        return None
    latex = tidy_formula(spans[0]) if len(spans) == 1 else (
        "\\begin{gathered}\n" + " \\\\\n".join(tidy_formula(s) for s in spans) + "\n\\end{gathered}")
    return latex, tag.group("tag") if tag else None


def tidy_formula(latex: str) -> str:
    """Repair the model's two habits in display math: an environment it never closes, and the same row
    written twice (an array of identical rows becomes that one row)."""
    latex = latex.strip()
    for env in re.findall(r"\\begin\{(\w+\*?)\}", latex):
        if latex.count(f"\\begin{{{env}}}") > latex.count(f"\\end{{{env}}}"):
            latex += f"\n\\end{{{env}}}"
    m = re.fullmatch(r"\\begin\{array\}\{[^}]*\}\s*(.*?)\s*\\end\{array\}", latex, re.S)
    if m:
        rows = [r.strip() for r in re.split(r"\\\\", m.group(1)) if r.strip()]
        if rows and all(r == rows[0] for r in rows):
            return rows[0]
    return latex


def _cells(row: str) -> list[str]:
    return [re.sub(r"\s+", " ", re.sub(r"<br\s*/?>", " ", c)).strip()
            for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)]


def table_markdown(html: str) -> str:
    """A plain table as a Markdown table; one with merged cells stays HTML, which Markdown carries as is."""
    html = inline_math(html)
    if any(int(n) > 1 for n in _SPAN.findall(html)) or "<table" not in html:
        return re.sub(r"\s*\n\s*", " ", html).strip()
    rows = [_cells(r) for r in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)]
    rows = [r for r in rows if r]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    lines = ["| " + " | ".join(c.replace("|", "\\|") for c in r + [""] * (width - len(r))) + " |" for r in rows]
    lines.insert(1, "|" + " --- |" * width)
    return "\n".join(lines)


def blocks_markdown(blocks: list[dict[str, Any]]) -> str:
    """Write one page's blocks the way LitForge's page rewrites look: headings, $-math, display equations
    with \\tag, tables, and [Figure] where a figure is (its caption follows as its own block)."""
    out: list[str] = []
    for block in blocks:
        cls, content = str(block.get("class")), str(block.get("content") or "").strip()
        if cls in _SKIP:
            continue
        if cls in _FIGURE:
            out.append("[Figure]")
        elif cls == "Table":
            if table := table_markdown(content):
                out.append(table)
        elif cls == "Formula" and (found := formula_latex(content)):
            latex, number = found
            out.append(f"$$\n{latex}{f' \\tag{{{number}}}' if number else ''}\n$$")
        elif cls == "Title" and content:
            out.append("# " + inline_math(content.lstrip("# ")))
        elif cls in _PROSE and content:
            out.append(prose(content))
        elif content:
            out.append(inline_math(content))
    return "\n\n".join(out)


# ---- the model ------------------------------------------------------------------------------------------------

_lock = threading.RLock()  # _load releases an older model while read() holds it
_loaded: dict[str, Any] = {}


def _helpers(folder: Path) -> ModuleType:
    """The repository's own output helpers (`nemotron_parse`), imported from the pinned download."""
    name = "litforge_nemotron_parse"
    with _lock:  # another job must not see the module half-imported
        return sys.modules.get(name) or _import_helpers(name, folder)


def _import_helpers(name: str, folder: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, folder / "nemotron_parse" / "__init__.py",
                                                  submodule_search_locations=[str(folder / "nemotron_parse")])
    if spec is None or spec.loader is None:
        raise RuntimeError("the Nemotron output helpers are missing; download the model again")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[name]
        raise
    return module


def _load(folder: Path, device: str) -> dict[str, Any]:
    if _loaded.get("folder") == str(folder) and _loaded.get("device") == device:
        return _loaded
    release()
    import torch
    from transformers import AutoModel, AutoProcessor, GenerationConfig

    dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32
    # The model's own code (pinned revision, downloaded with the weights) defines its architecture.
    from transformers.utils import logging as hf_logging

    verbosity = hf_logging.get_verbosity()
    cast(Any, hf_logging.set_verbosity_error)()  # suppress the model's notes about processors and remote code
    try:
        model = AutoModel.from_pretrained(folder, trust_remote_code=True, dtype=dtype,
                                          local_files_only=True).to(device).eval()
        processor = cast(Any, AutoProcessor).from_pretrained(
            folder, trust_remote_code=True, local_files_only=True)
    finally:
        hf_logging.set_verbosity(verbosity)
    generation = GenerationConfig.from_pretrained(folder, local_files_only=True)
    generation.max_new_tokens, generation.do_sample, generation.num_beams = MAX_NEW_TOKENS, False, 1
    generation.repetition_penalty = 1.1
    _loaded.update(folder=str(folder), device=device, model=model, processor=processor, generation=generation)
    return _loaded


def release() -> None:
    """Free the model's memory (GPU included); the next page loads it again in a few seconds."""
    with _lock:
        if not _loaded:
            return
        device = str(_loaded.get("device"))
        _loaded.clear()
        if device.startswith("cuda"):
            import torch

            torch.cuda.empty_cache()


def page_image(png: bytes) -> Any:
    """The page as an RGB image no larger than the model's recommended size, keeping its proportions."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    scale = min(MAX_SIZE[0] / image.width, MAX_SIZE[1] / image.height, 1.0)
    if scale < 1.0:
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
    return image


class NemotronEngine:
    """Reads page images with Nemotron Parse. `transcribe` returns the model's raw output (cached as is);
    `render` turns it into LitForge Markdown, so a better conversion does not need the model again."""

    provider = "local"
    name = NAME
    page_size = MAX_SIZE  # pages are rendered at this size for it: at 150 dpi it writes formulas as plain text

    def __init__(self, paths: Paths, device: str | None = None) -> None:
        if not packages_available():
            raise RuntimeError("Nemotron Parse needs the `nemotron` extra: run `uv sync --extra nemotron --inexact`")
        if not model_ready(paths):
            raise RuntimeError("the Nemotron Parse model is not downloaded; set up Math: high again")
        self.folder = model_dir(paths)
        self.device = device or ("cuda:0" if gpu_free_gb() is not None else "cpu")

    def read(self, png: bytes) -> tuple[str, tuple[int, int]]:
        """The raw output for one page image, with the size of the image the model saw."""
        import torch

        image = page_image(png)
        with _lock:
            m = _load(self.folder, self.device)
            inputs = m["processor"](images=[image], text=PROMPT, return_tensors="pt",
                                    add_special_tokens=False).to(self.device)
            with torch.inference_mode():
                ids = m["model"].generate(**inputs, generation_config=m["generation"])
            raw = m["processor"].batch_decode(ids, skip_special_tokens=False)[0]
        return str(raw), (image.width, image.height)

    def blocks(self, raw: str, size: tuple[int, int] | None = None) -> list[dict[str, Any]]:
        doc = _helpers(self.folder).postprocess_document(raw, image_size=size)
        return list(doc["blocks"])

    def transcribe(self, png: bytes, page_text: str) -> str:
        return self.read(png)[0]

    def release(self) -> None:
        release()

    def render(self, raw: str) -> str:
        return blocks_markdown(self.blocks(raw))
