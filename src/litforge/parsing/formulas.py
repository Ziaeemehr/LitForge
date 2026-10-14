"""Better math, medium level: formulas found and read by Pix2Text, text from the PDF's own text layer.

Pix2Text (optional extra `formulas`) finds display ("isolated") and inline ("embedding") formulas on a page
image and recognizes each as LaTeX, on the CPU. Its own text OCR is poor, so only the formulas are taken
from it: the page's paragraphs keep the text layer's words, the words a formula covers are replaced by its
LaTeX ($...$ inline, $$...$$ for a display with its equation number as \\tag), and paragraphs no formula
touches stay exactly as the parser wrote them. Results live in `formulas.json` next to the parse outputs.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import pymupdf

from litforge.parsing.base import ParsedDocument
from litforge.parsing.canonical import read_canonical
from litforge.parsing.pymupdf_parser import join_lines

FORMULAS = "formulas.json"
POST_VERSION = "6"  # how detections are assembled into a page; cached detections are re-assembled on change
_EQ_NUMBER = re.compile(r"\(\s*(?:[A-Z]?\d+(?:\.\d+)*[a-z]?)\s*\)")
_FUNCS = "exp|log|ln|sin|cos|tan|sinh|cosh|tanh|max|min|lim|sup|inf|det|arg"


@dataclass
class Formula:
    box: tuple[float, float, float, float]  # pixels of the page image
    kind: str  # isolated (display) | embedding (inline)
    latex: str


class FormulaEngine(Protocol):
    name: str

    def detect(self, png: bytes) -> list[Formula]: ...


def formulas_available() -> bool:
    return importlib.util.find_spec("pix2text") is not None


def _cuda() -> bool:
    """Is an NVIDIA GPU usable (the `formulas-gpu` extra)? Its CUDA libraries come as Python packages."""
    try:
        import onnxruntime
        import torch

        if not torch.cuda.is_available() or "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
            return False
        if hasattr(onnxruntime, "preload_dlls"):
            onnxruntime.preload_dlls()  # load the pip-installed CUDA/cuDNN libraries
        return True
    except Exception:  # noqa: BLE001 - any trouble means: use the CPU
        return False


class Pix2TextEngine:
    """Pix2Text's formula detector and recognizer, on the GPU when the `formulas-gpu` extra is installed and a
    card is usable, else on the CPU (`formulas` extra)."""

    def __init__(self) -> None:
        if not formulas_available():
            raise RuntimeError("Pix2Text is not installed; install it with `uv sync --extra formulas` "
                               "(or `--extra formulas-gpu` on an NVIDIA GPU)")
        from importlib import metadata

        from pix2text import Pix2Text

        self.device = "cuda" if _cuda() else "cpu"
        with contextlib.redirect_stdout(io.StringIO()):
            self._ocr = Pix2Text.from_config(device=self.device).text_formula_ocr
        # The device is not in the name: both give the same LaTeX, so cached results stay valid.
        self.name = f"pix2text-{metadata.version('pix2text')}"

    def detect(self, png: bytes) -> list[Formula]:
        from PIL import Image

        image = Image.open(io.BytesIO(png)).convert("RGB")
        with contextlib.redirect_stdout(io.StringIO()):  # the detector prints a line per image
            found = self._ocr.mfd.detect(image, resized_shape=768)
            boxes = []
            for d in found:
                xs, ys = [p[0] for p in d["box"]], [p[1] for p in d["box"]]
                boxes.append(((max(0.0, min(xs) - 2), max(0.0, min(ys) - 2), max(xs) + 2, max(ys) + 2), d["type"]))
            texts = self._ocr.latex_ocr.recognize([image.crop(b) for b, _ in boxes]) if boxes else []
        found = [Formula(box=tuple(map(float, b)), kind=k, latex=tidy_latex(t["text"]))  # type: ignore[arg-type]
                 for (b, k), t in zip(boxes, texts, strict=True)]
        return [f for f in found if plausible_latex(f.latex)]  # a rejected formula leaves its words as text


class FakeFormulas:
    """For tests: answers with a function of the page image."""

    def __init__(self, answer: Callable[[bytes], list[Formula]], name: str = "fake-formulas") -> None:
        self.answer, self.name, self.calls = answer, name, 0

    def detect(self, png: bytes) -> list[Formula]:
        self.calls += 1
        return self.answer(png)


def tidy_latex(latex: str) -> str:
    """Undo Pix2Text's habit of spacing out names (\\operatorname{e x p}) and a trailing line break."""
    latex = re.sub(r"\\(operatorname|mathrm|text|mathsf)\{((?:[A-Za-z] )+[A-Za-z])\}",
                   lambda m: f"\\{m.group(1)}{{{m.group(2).replace(' ', '')}}}", latex)
    latex = re.sub(rf"\\operatorname\{{({_FUNCS})\}}", r"\\\1", latex)
    latex = re.sub(r"\\\\\s*(\\end\{)", r"\1", latex)
    return latex.strip()


def plausible_latex(latex: str) -> bool:
    """Reject recognitions that render badly: a token repeated in a loop (the recognizer got stuck),
    commands that do not exist in math mode, unbalanced braces or \\left/\\right."""
    if not latex.strip() or re.search(r"\\(verb|d)(?![A-Za-z])", latex):
        return False
    bare = latex.replace("\\{", "").replace("\\}", "")  # escaped braces are printed, not grouping
    if bare.count("{") != bare.count("}") or len(re.findall(r"\\left(?![a-z])", latex)) != len(
            re.findall(r"\\right(?![a-z])", latex)):
        return False
    # Spacing (\\, \\quad ...) may repeat harmlessly; a stuck recognizer repeats real tokens.
    latex = re.sub(r"\\(quad|qquad)(?![A-Za-z])|\\[,;:! ]|~", " ", latex)
    tokens = re.findall(r"\\[A-Za-z]+|\\.|[^\s]", latex)
    run = 1
    for a, b in zip(tokens, tokens[1:], strict=False):
        run = run + 1 if a == b and not a.isalnum() else 1
        if run >= 6:
            return False
    return not re.search(r"(.{6,}?)\1{3,}", latex.replace(" ", ""))  # a phrase repeated four times


def _center_in(rect: pymupdf.Rect, box: pymupdf.Rect, pad: float = 1.0) -> bool:
    x, y = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
    return box.x0 - pad <= x <= box.x1 + pad and box.y0 - pad <= y <= box.y1 + pad


def _display(latex: str, tag: str | None) -> str:
    return f"$$\n{latex}{f' \\tag{{{tag}}}' if tag else ''}\n$$"


def assemble_page(page: pymupdf.Page, doc: ParsedDocument, n: int, formulas: list[Formula], dpi: int) -> str:
    """Markdown for page `n`: the parser's paragraphs in its reading order, with the formulas in place."""
    scale = dpi / 72
    words = [(pymupdf.Rect(w[:4]), w[4], (w[5], w[6])) for w in page.get_text("words")]
    placed = [(f, pymupdf.Rect(*(v / scale for v in f.box))) for f in formulas]
    displays = [(f, r) for f, r in placed if f.kind == "isolated"]
    inlines = [(f, r) for f, r in placed if f.kind != "isolated"]

    # Text-layer lines: a line lying in a display's band belongs to the display (in scans the OCR'd
    # fragments sit beside or around the detected box), except its equation number, which becomes the tag.
    lines: dict[Any, list[int]] = {}
    for wi, (_, _, key) in enumerate(words):
        lines.setdefault(key, []).append(wi)
    covered: set[int] = set()
    tags: dict[int, str] = {}
    for _, box in displays:
        band = pymupdf.Rect(box.x0 - 40, box.y0 - 1, box.x1 + 15, box.y1 + 1)
        for wis in lines.values():
            extent = pymupdf.Rect(words[wis[0]][0])
            for wi in wis:
                extent |= words[wi][0]
            mid, centre = (extent.y0 + extent.y1) / 2, (extent.x0 + extent.x1) / 2
            if band.y0 <= mid <= band.y1 and band.x0 <= centre <= band.x1:  # never a line of the next column
                covered.update(wi for wi in wis if not _EQ_NUMBER.fullmatch(words[wi][1]))
    for wi, (rect, _, _) in enumerate(words):
        if any(_center_in(rect, box, 1.5) for _, box in placed):
            covered.add(wi)
    for wi, (rect, text, _) in enumerate(words):  # equation numbers: right of a display, at most a line off
        if not _EQ_NUMBER.fullmatch(text):
            continue
        mid = (rect.y0 + rect.y1) / 2
        near = [(abs(mid - (b.y0 + b.y1) / 2), di) for di, (_, b) in enumerate(displays)
                if di not in tags and rect.x0 >= b.x1 - 5 and b.y0 - 12 <= mid <= b.y1 + 12]
        if near:
            tags[min(near)[1]] = text.strip("() ")
            covered.add(wi)

    paras = [(s, p) for s in doc.sections for p in s.paragraphs if p.page == n]
    boxes = [pymupdf.Rect(p.bbox) if p.bbox else None for _, p in paras]

    def owner(rect: pymupdf.Rect, pad: float) -> int | None:
        """The first paragraph (reading order) containing the centre, else the nearest one."""
        for pi, b in enumerate(boxes):
            if b is not None and _center_in(rect, b, pad):
                return pi
        x, y = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
        dists = [(abs(y - min(max(y, b.y0), b.y1)) + abs(x - min(max(x, b.x0), b.x1)), pi)
                 for pi, b in enumerate(boxes) if b is not None]
        return min(dists)[1] if dists and min(dists)[0] < 20 else None

    para_words: dict[int, list[int]] = {}
    dirty: set[int] = set()  # paragraphs whose text a formula changes
    for wi, (rect, _, _) in enumerate(words):
        if wi in covered:  # every paragraph showing these words, also an overlapping fragment the parser made
            dirty.update(pi for pi, b in enumerate(boxes) if b is not None and _center_in(rect, b, 2))
            continue
        pi = owner(rect, 2)
        if pi is not None:
            para_words.setdefault(pi, []).append(wi)
    para_inlines: dict[int, list[int]] = {}
    for fi, (_, r) in enumerate(inlines):
        pi = owner(r, 3)
        if pi is not None:
            para_inlines.setdefault(pi, []).append(fi)
            dirty.add(pi)
    para_displays: dict[int, list[int]] = {}
    loose: list[int] = []
    for di, (_, r) in enumerate(displays):
        pi = next((pi for pi, b in enumerate(boxes) if b is not None and _center_in(r, b, 2)), None)
        if pi is None:
            loose.append(di)
        else:
            para_displays.setdefault(pi, []).append(di)
            dirty.add(pi)
    captions = {a.caption for a in doc.assets if a.caption and a.path and a.kind in ("figure", "table")}

    out: list[str] = []
    headed: set[int] = set()
    for pi, (section, para) in enumerate(paras):
        bbox = boxes[pi]
        if bbox is not None:  # displays outside every paragraph, above this one in its column
            for di in sorted(loose, key=lambda i: displays[i][1].y0):
                b = displays[di][1]
                if b.y1 <= bbox.y0 + 2 and b.x1 >= bbox.x0 and b.x0 <= bbox.x1:
                    out.append(_display(displays[di][0].latex, tags.get(di)))
                    loose.remove(di)
        if section.heading and id(section) not in headed and section.page_start == n:
            headed.add(id(section))
            out.append(f"{'#' * min(section.level + 1, 6)} {section.heading}")
        if pi not in dirty:
            if para.kind == "caption" and para.text.split("\n\n", 1)[0] in captions:
                out.append("[Figure]\n" + para.text)
            elif para.kind == "equation":
                out.append(f"```text\n{para.text}\n```")
            else:
                out.append(para.text)
            continue
        # Rebuild the paragraph from its words, formulas in place of the words they cover. Rows are visual
        # lines: the text layer may split one printed line into several text lines.
        tokens = sorted(((words[wi][0].y0 + words[wi][0].y1) / 2, words[wi][0].x0, words[wi][1])
                        for wi in para_words.get(pi, []))
        rows: list[tuple[float, list[tuple[float, str]]]] = []
        for y, x, text in tokens:
            if rows and abs(rows[-1][0] - y) <= 3:
                rows[-1][1].append((x, text))
            else:
                rows.append((y, [(x, text)]))
        for fi in para_inlines.get(pi, []):
            f, r = inlines[fi]
            mid = (r.y0 + r.y1) / 2
            closest = min(rows, key=lambda row: abs(row[0] - mid), default=None)
            if closest is not None and abs(closest[0] - mid) <= max(5.0, r.height * 0.6):
                closest[1].append((r.x0, f"${f.latex}$"))
            else:
                rows.append((mid, [(r.x0, f"${f.latex}$")]))
        cuts = sorted(para_displays.get(pi, []), key=lambda i: displays[i][1].y0)
        chunk: list[str] = []
        for y, row in sorted(rows, key=lambda r: r[0]):
            while cuts and y > displays[cuts[0]][1].y1:
                if chunk:
                    out.append(join_lines(chunk))
                    chunk = []
                di = cuts.pop(0)
                out.append(_display(displays[di][0].latex, tags.get(di)))
            chunk.append(" ".join(t for _, t in sorted(row)))
        if chunk:
            out.append(join_lines(chunk))
        out += [_display(displays[di][0].latex, tags.get(di)) for di in cuts]
    out += [_display(displays[di][0].latex, tags.get(di)) for di in sorted(loose, key=lambda i: displays[i][1].y0)]
    return "\n\n".join(part for part in out if part.strip())


def enhance_document(pdf: Path, out_dir: Path, engine: FormulaEngine, pages: list[int], dpi: int,
                     progress: Callable[[str], None] = lambda _: None) -> tuple[int, int]:
    """Detect and read the formulas of `pages` (cached per page); returns (pages, pages rewritten)."""
    doc = read_canonical(out_dir)
    try:
        saved = json.loads((out_dir / FORMULAS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if saved.get("engine") != engine.name:
        saved = {"engine": engine.name, "pages": {}}
    done: dict[str, Any] = saved["pages"]
    with pymupdf.open(pdf) as pdf_doc:
        for i, n in enumerate(pages, 1):
            entry = done.get(str(n))
            if entry and entry.get("post_version") == POST_VERSION:
                continue
            page = pdf_doc[n - 1]
            if not page.get_text("words"):
                done[str(n)] = {"markdown": None, "status": "no text layer", "post_version": POST_VERSION}
                continue
            if entry and "formulas" in entry:
                formulas = [Formula(box=tuple(f["box"]), kind=f["kind"], latex=f["latex"]) for f in entry["formulas"]
                            if plausible_latex(f["latex"])]
            else:
                progress(f"page {n} ({i}/{len(pages)})")
                formulas = engine.detect(page.get_pixmap(dpi=dpi).tobytes("png"))
            markdown = assemble_page(page, doc, n, formulas, dpi) if formulas else None
            done[str(n)] = {"markdown": markdown, "formulas": [asdict(f) for f in formulas],
                            "status": "ok" if formulas else "no formulas found", "post_version": POST_VERSION}
            (out_dir / FORMULAS).write_text(json.dumps(saved, indent=1, ensure_ascii=False), encoding="utf-8")
    return len(pages), sum(1 for n in pages if (done.get(str(n)) or {}).get("markdown"))
