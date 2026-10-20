"""Documents other than PDF, read with Microsoft's MarkItDown (optional: `uv sync --extra documents`) into the
same canonical document as a PDF. Markdown and plain text need no package. A document has no pages: its text
is page 1, so quotes point to the file rather than to a page."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

from litforge.config import ParsingConfig
from litforge.parsing.base import PageInfo, Paragraph, ParsedDocument, Section, SectionKind, section_kind

DOCUMENT_SUFFIXES = {".docx", ".pptx", ".xlsx", ".html", ".htm", ".epub", ".md", ".markdown", ".txt"}
PLAIN_SUFFIXES = {".md", ".markdown", ".txt"}
# A folder brings PDFs and these; web pages, notes and Markdown only when chosen one by one (review: a Zotero
# storage or export folder turned every snapshot and note into a paper).
FOLDER_SUFFIXES = {".docx", ".pptx", ".xlsx", ".epub"}
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
_REMOTE_IMAGE = re.compile(r"!\[([^\]]*)\]\((?:https?:)?//[^)]*\)")
INSTALL_HINT = "install the documents extra: uv sync --extra documents --inexact"
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def is_document(path: Path) -> bool:
    return path.suffix.lower() in DOCUMENT_SUFFIXES


def markitdown_available() -> bool:
    try:
        import markitdown  # noqa: F401
    except ImportError:
        return False
    return True


def markdown_of(path: Path) -> str:
    return _convert(path)[0]


def _convert(path: Path) -> tuple[str, str | None]:
    """The document as Markdown, and the title it declares (an HTML page's <title>). Images from the web are
    reduced to their description: the page would otherwise load them when the text is shown."""
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise RuntimeError(f"document larger than {MAX_DOCUMENT_BYTES // 2**20} MB")
    if path.suffix.lower() in PLAIN_SUFFIXES:
        text, title = path.read_text(encoding="utf-8", errors="replace"), None
    else:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise RuntimeError(f"{path.suffix} files need MarkItDown: {INSTALL_HINT}") from exc
        result = MarkItDown(enable_plugins=False).convert(str(path))
        text, title = str(result.text_content or ""), (result.title or "").strip() or None
    return _REMOTE_IMAGE.sub(lambda m: m.group(1), text), title


def document_from_markdown(markdown: str, parser: str, version: str) -> tuple[ParsedDocument, str | None]:
    """Sections at the headings, paragraphs at blank lines (a table or a list stays one block); the title is
    the first line when short, else the first heading."""
    sections: list[Section] = []
    current = Section(heading=None, kind="front", level=1)
    title: str | None = None
    block: list[str] = []

    def flush() -> None:
        text = "\n".join(block).strip()
        if text:
            current.paragraphs.append(Paragraph(text=text, page=1))
        block.clear()

    for line in markdown.replace("\r\n", "\n").split("\n"):
        heading = _HEADING.match(line)
        if heading:
            flush()
            if current.heading is not None or current.paragraphs:
                sections.append(current)
            name = heading.group(2).strip()
            title = title or name
            kind: SectionKind = ("front" if not sections and title == name and len(heading.group(1)) == 1
                                 else section_kind(name))
            current = Section(heading=name, kind=kind, level=len(heading.group(1)))
        elif line.strip():
            block.append(line.rstrip())
        else:
            flush()
    flush()
    if current.heading is not None or current.paragraphs:
        sections.append(current)
    # The first line is the title when it is short, as a reader sees it (Word's "Title" style is not a heading).
    first = next((ln.strip().lstrip("#").strip() for ln in markdown.split("\n")
                  if ln.strip() and not ln.strip().startswith("<!--")), "")  # PowerPoint: <!-- Slide number -->
    if first and len(first) <= 200 and not first.startswith(("|", "-", "*", "!")):
        title = first
    text = "\n\n".join(p.text for s in sections for p in s.paragraphs)
    page = PageInfo(index=1, label=None, width=0, height=0, char_count=len(text), image_count=0,
                    line_count=text.count("\n") + 1 if text else 0)
    return ParsedDocument(parser=parser, parser_version=version, page_count=1, pages=[page], sections=sections), title


def document_title(path: Path) -> str | None:
    """The title the document declares (an HTML <title>), else its first line or heading; None for a workbook,
    whose first heading is only a sheet name, so its file name is used."""
    if path.suffix.lower() == ".xlsx":
        return None
    try:
        text, declared = _convert(path)
        _doc, title = document_from_markdown(text, "markitdown", "")
    except Exception:  # noqa: BLE001 - a title is optional; reading errors show up when the file is parsed
        return None
    return declared or title


class MarkItDownParser:
    name = "markitdown"

    @property
    def version(self) -> str:
        try:
            return importlib.metadata.version("markitdown")
        except importlib.metadata.PackageNotFoundError:
            return "plain"

    def parse(self, pdf_path: Path, out_dir: Path, config: ParsingConfig) -> ParsedDocument:
        out_dir.mkdir(parents=True, exist_ok=True)
        doc, _title = document_from_markdown(markdown_of(pdf_path), self.name, self.version)
        if not doc.sections:
            raise RuntimeError("no text found in the document")
        return doc
