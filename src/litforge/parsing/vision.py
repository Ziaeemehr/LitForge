"""Rewrite hard pages with a local vision model: equations as LaTeX, tables as Markdown tables.

The PyMuPDF parser reads the text layer, which keeps prose but scatters display equations into single
characters, and in scanned papers often misses them altogether. For the pages that show it (an equation,
a table, many fragment lines, broken characters), a vision model (Ollama, e.g. qwen2.5vl) transcribes the
page image to Markdown. Each transcription is checked against the page's text layer and kept only when
it matches, so a model that skips or invents text cannot replace the parser's output. Results live in
`vision.json` next to the parse outputs, per page, and are reused until the model or prompt changes.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pymupdf

from litforge.config import Config, Paths, VisionConfig
from litforge.indexing.fts import current_runs
from litforge.observability import NULL_LOG, EventLog
from litforge.parsing import formulas
from litforge.parsing.base import ParsedDocument
from litforge.parsing.canonical import read_canonical
from litforge.projects import MARK_SCOPES
from litforge.screening.llm import OllamaClient

VISION = "vision.json"
PROMPT_VERSION = "1"
POST_VERSION = "6"  # the checks applied to a model's raw answer; cached answers are re-checked when it changes
PROMPT = (
    "Transcribe this page of a scientific paper into Markdown. Rules: keep the text exactly as printed, in "
    "reading order; write every mathematical expression in LaTeX: inline math as $...$, display equations as "
    "$$...$$ on their own lines, with the equation number as \\tag{...} inside the display; write section "
    "headings as Markdown headings (## for sections, ### for subsections) with their numbers; write tables as "
    "Markdown tables; for each figure write only [Figure] followed by its caption; omit running headers, "
    "footers and page numbers. Output only the Markdown."
)

_MATH_CHARS = set("∑∫√≈≠≤≥±∂∇∞∈∀∃αβγδεθλμνξπρστφχψωΓΔΘΛΣΦΨΩ")
_WORD = re.compile(r"[a-z]{4,}")
_EQ_NUMBER = re.compile(r"\(\s*(?:[A-Z]?\d+(?:\.\d+)*[a-z]?)\s*\)")  # (3), (2.5), (A1) alone on a line


@runtime_checkable
class Vision(Protocol):
    name: str

    def transcribe(self, png: bytes, page_text: str) -> str: ...


class OllamaVision:
    def __init__(self, url: str, cfg: VisionConfig) -> None:
        self.client = OllamaClient(url, cfg.model, cfg.timeout_s)
        self.name = cfg.model

    def check(self) -> None:
        self.client.check()

    def transcribe(self, png: bytes, page_text: str) -> str:
        reply = self.client._call("/api/generate", {
            "model": self.name, "prompt": PROMPT, "images": [base64.b64encode(png).decode()], "stream": False,
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 4096},
        })
        return str(reply.get("response", ""))


class FakeVision:
    """For tests: answers with a function of the page image and its text layer."""

    def __init__(self, answer: Callable[[bytes, str], str], name: str = "fake-vision") -> None:
        self.answer, self.name, self.calls = answer, name, 0

    def transcribe(self, png: bytes, page_text: str) -> str:
        self.calls += 1
        return self.answer(png, page_text)


def suspicious_pages(doc: ParsedDocument) -> list[int]:
    """Pages the text layer is likely to have garbled: equations, tables, fragment lines, broken characters."""
    pages: set[int] = set()
    for asset in doc.assets:
        if asset.kind in ("equation", "table"):
            pages.add(asset.page)
    fragments: dict[int, int] = {}
    math: dict[int, int] = {}
    for section in doc.sections:
        for para in section.paragraphs:
            if para.kind == "equation":
                pages.add(para.page)
            lines = [line.strip() for line in para.text.split("\n")]
            if any(_EQ_NUMBER.fullmatch(line) for line in lines):
                pages.add(para.page)
            fragments[para.page] = fragments.get(para.page, 0) + sum(1 for line in lines if 0 < len(line) <= 2)
            math[para.page] = math.get(para.page, 0) + sum(ch in _MATH_CHARS for ch in para.text)
    for page in doc.pages:
        if page.replacement_chars or (page.line_count >= 10 and page.short_line_count / page.line_count >= 0.4):
            pages.add(page.index)
        if fragments.get(page.index, 0) >= 5 or math.get(page.index, 0) >= 8:
            pages.add(page.index)
    return sorted(pages)


def clean_markdown(text: str) -> str:
    """Strip the code fence models like to add and turn LaTeX sectioning into Markdown headings."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n", "", text)
    text = re.sub(r"\n?```$", "", text)
    for command, hashes in (("subsubsection", "####"), ("subsection", "###"), ("section", "##")):
        text = re.sub(rf"^\\{command}\*?\{{(.+?)\}}\s*$", rf"{hashes} \1", text, flags=re.M)
    text = re.sub(r"^\\(begin|end)\{document\}\s*$\n?", "", text, flags=re.M)
    # The model has no image files: a table of picture cells or an image link stands for a figure.
    out: list[str] = []
    table: list[str] = []
    for line in [*text.split("\n"), ""]:
        if line.lstrip().startswith("|"):
            table.append(line)
            continue
        if table:
            out += ["[Figure]"] if any("![" in row for row in table) else table
            table = []
        if re.fullmatch(r"\s*!\[[^\]]*\]\([^)]*\)\s*", line):
            line = "[Figure]"
        elif m := re.fullmatch(r"\s*\$([^$]+)\$\s*(?:\((\d+[a-z]?)\))?\s*", line):
            tag = f" \\tag{{{m.group(2)}}}" if m.group(2) else ""
            line = f"$$\n{m.group(1).strip()}{tag}\n$$"  # a formula on a line of its own is a display
        elif not line.lstrip().startswith("[Figure"):
            line = line.replace("[Figure]", "Figure")  # a reference in the text, not a figure's place
        out.append(line)
    return "\n".join(out).strip()


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NUMBER = re.compile(r"^((?:\d+|[A-Z])(?:\.\d+)*\.?)\s+")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def fix_headings(markdown: str, page_text: str) -> str:
    """Keep only headings printed on the page (models like to invent a title or the section the page
    belongs to), with the section number the page shows; the note's title is its only level-1 heading."""
    raw_lines = [line.strip() for line in page_text.split("\n") if line.strip()]
    # A heading may wrap onto the next line of the text layer.
    candidates = [line + " " + nxt for line, nxt in zip(raw_lines, [*raw_lines[1:], ""], strict=True)]
    out: list[str] = []
    for line in markdown.split("\n"):
        m = _HEADING.match(line)
        if not m:
            out.append(line)
            continue
        hashes, text = m.group(1), m.group(2)
        number = _NUMBER.match(text)
        bare = text[number.end():] if number else text
        source = next((c for c in candidates if _norm(_NUMBER.sub("", c, count=1)).startswith(_norm(bare))), None)
        if source is None:
            if len(bare.split()) > 8 or "$" in bare:
                out.append(bare)  # real content (a caption, an equation) the model set as a heading
            continue  # a short heading not printed on this page: invented
        if not number and (found := _NUMBER.match(source)):
            text = f"{found.group(1).rstrip('.')} {bare}"
        out.append(f"{'#' * max(len(hashes), 2)} {text}")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def plausible(markdown: str, page_text: str) -> bool:
    """Does the transcription carry the page's words, without inventing or repeating a lot more?"""
    if not markdown.strip():
        return False
    words = set(_WORD.findall(page_text.lower()))
    if len(words) < 20:
        return True  # an image-only or nearly empty text layer: nothing to compare with
    found = set(_WORD.findall(markdown.lower()))
    if len(words & found) / len(words) < 0.5:
        return False
    return len(markdown.split()) <= 3 * len(page_text.split()) + 300  # runaway repetition


def read_page_markdown(out_dir: Path) -> dict[int, str]:
    """The rewritten pages of one parse output, by page number: the vision model's transcription where
    there is one (high), else the page with Pix2Text's formulas (medium)."""
    pages: dict[int, str] = {}
    for name in (formulas.FORMULAS, VISION):  # later files win
        try:
            data = json.loads((out_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pages |= {int(n): p["markdown"] for n, p in (data.get("pages") or {}).items() if p.get("markdown")}
    return pages


@dataclass
class VisionReport:
    papers: int = 0
    pages_improved: int = 0
    pages_rejected: int = 0
    # One entry per paper: {paper_id, title, pages, improved, rejected}.
    items: list[dict[str, Any]] = field(default_factory=list)


def enhance_document(pdf: Path, out_dir: Path, vision: Vision, dpi: int,
                     progress: Callable[[str], None] = lambda _: None) -> tuple[int, int, int]:
    """Transcribe the suspicious pages of one parsed PDF; returns (pages, improved, rejected)."""
    doc = read_canonical(out_dir)
    pages = suspicious_pages(doc)
    try:
        saved = json.loads((out_dir / VISION).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if saved.get("model") != vision.name or saved.get("prompt_version") != PROMPT_VERSION:
        saved = {"model": vision.name, "prompt_version": PROMPT_VERSION, "pages": {}}
    done: dict[str, Any] = saved["pages"]
    with pymupdf.open(pdf) as pdf_doc:
        for i, n in enumerate(pages, 1):
            entry = done.get(str(n))
            if entry and entry.get("post_version") == POST_VERSION:
                continue
            page = pdf_doc[n - 1]
            page_text = page.get_text()
            raw = (entry or {}).get("raw") or (entry or {}).get("markdown")
            if raw is None:  # not transcribed yet (or rejected before the raw answer was kept)
                progress(f"page {n} ({i}/{len(pages)})")
                raw = vision.transcribe(page.get_pixmap(dpi=dpi).tobytes("png"), page_text)
            markdown = fix_headings(clean_markdown(raw), page_text)
            ok = plausible(markdown, page_text)
            done[str(n)] = {"markdown": markdown if ok else None, "raw": raw, "post_version": POST_VERSION,
                            "status": "ok" if ok else "rejected: does not match the page's text"}
            (out_dir / VISION).write_text(json.dumps(saved, indent=1, ensure_ascii=False), encoding="utf-8")
    improved = sum(1 for n in pages if (done.get(str(n)) or {}).get("markdown"))
    return len(pages), improved, len(pages) - improved


def enhance_project(
    conn: sqlite3.Connection,
    paths: Paths,
    config: Config,
    project_id: str,
    engine: Vision | formulas.FormulaEngine,
    only: str = "all",
    paper_ids: Collection[str] | None = None,
    log: EventLog = NULL_LOG,
    progress: Callable[[str], None] = lambda _: None,
) -> VisionReport:
    """Rewrite the suspicious pages of a project's parsed papers (cached per page): with a vision model
    (high: whole pages) or a formula engine (medium: formulas only, text from the PDF)."""
    if only not in MARK_SCOPES:
        raise ValueError(f"only must be one of {sorted(MARK_SCOPES)}")
    allowed = {r[0] for r in conn.execute(
        f"""SELECT pp.paper_id FROM project_papers pp
            LEFT JOIN paper_marks m ON m.paper_id = pp.paper_id AND m.project_id = pp.project_id
            WHERE pp.project_id = ?{MARK_SCOPES[only]}""", (project_id,))}
    if paper_ids is not None:
        allowed &= set(paper_ids)
    runs = [r for r in current_runs(conn, project_id) if r["paper_id"] in allowed and r["status"] != "failed"]
    report = VisionReport()
    for i, run in enumerate(runs, 1):
        row = conn.execute("SELECT p.title, a.pdf_path FROM papers p JOIN artifacts a ON a.paper_id = p.id"
                           " WHERE a.id=?", (run["artifact_id"],)).fetchone()
        title = row["title"] or run["paper_id"]
        label = f"[{i}/{len(runs)}] {title[:70]}"
        progress(label)

        def page_progress(message: str, label: str = label) -> None:
            progress(f"{label}: {message}")

        pdf, out_dir = paths.root / row["pdf_path"], paths.root / run["output_dir"]
        if isinstance(engine, Vision):
            pages, improved, rejected = enhance_document(pdf, out_dir, engine, config.vision.dpi, page_progress)
        else:
            pages, improved = formulas.enhance_document(
                pdf, out_dir, engine, suspicious_pages(read_canonical(out_dir)), config.vision.formula_dpi,
                page_progress)
            rejected = 0
        report.papers += 1
        report.pages_improved += improved
        report.pages_rejected += rejected
        report.items.append({"paper_id": run["paper_id"], "title": title, "pages": pages, "improved": improved,
                             "rejected": rejected})
        log.emit("vision.paper", paper_id=run["paper_id"], pages=pages, improved=improved, rejected=rejected)
    return report
