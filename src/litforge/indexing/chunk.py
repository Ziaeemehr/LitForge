"""Section- and paragraph-aware chunking with page spans for every piece of text."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Protocol

from litforge.parsing.base import BBox, ParsedDocument

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[\"'])")


class TokenCounter(Protocol):
    def __call__(self, text: str) -> int: ...


def approx_tokens(text: str) -> int:
    """~4 characters per token for English scientific prose; swap for a real tokenizer later."""
    return max(1, round(len(text) / 4))


@dataclass
class Unit:
    text: str
    page: int
    bbox: BBox | None
    tokens: int


@dataclass
class Span:
    """Character span of a chunk's text that came from one page (for page-exact citations)."""

    start: int
    end: int
    page: int
    bbox: BBox | None


@dataclass
class ChunkDraft:
    id: str
    kind: str  # text | caption | references
    section_idx: int
    section_path: str
    text: str
    token_count: int
    page_start: int
    page_end: int
    spans: list[Span] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def _split_sentences(text: str, target: int, count: TokenCounter) -> list[str]:
    """Group sentences into pieces of at most ~target tokens."""
    pieces: list[str] = []
    current = ""
    start = 0
    # Keep source separators: caption/table boundaries carry structural meaning.
    for end in [m.end() for m in SENTENCE_SPLIT_RE.finditer(text)] + [len(text)]:
        sentence = text[start:end]
        start = end
        candidate = current + sentence
        if current and count(candidate) > target:
            pieces.append(current.strip())
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current.strip())
    return pieces


def _tail(unit: Unit, overlap: int, count: TokenCounter) -> Unit | None:
    if unit.tokens <= overlap:
        return unit
    tail = ""
    for sentence in reversed(SENTENCE_SPLIT_RE.split(unit.text)):
        candidate = f"{sentence} {tail}".strip()
        if count(candidate) > overlap:
            break
        tail = candidate
    return Unit(tail, unit.page, unit.bbox, count(tail)) if tail else None


def _assemble(units: list[Unit]) -> tuple[str, list[Span]]:
    text = ""
    spans: list[Span] = []
    for unit in units:
        if text:
            text += "\n\n"
        start = len(text)
        text += unit.text
        spans.append(Span(start, len(text), unit.page, unit.bbox))
    return text, spans


def section_paths(doc: ParsedDocument) -> list[str]:
    stack: list[tuple[int, str]] = []
    out = []
    for section in doc.sections:
        if section.heading is None:
            out.append("Front matter")
            continue
        while stack and stack[-1][0] >= section.level:
            stack.pop()
        stack.append((section.level, section.heading))
        out.append(" > ".join(h for _, h in stack))
    return out


def chunk_document(
    doc: ParsedDocument,
    key: str,
    target: int = 600,
    overlap: int = 80,
    index_references: bool = False,
    index_captions: bool = True,
    count: TokenCounter = approx_tokens,
) -> list[ChunkDraft]:
    """Chunk by section and paragraph. `key` (cache key of the parse) makes IDs stable."""
    drafts: list[ChunkDraft] = []
    paths = section_paths(doc)

    def emit(kind: str, s_idx: int, units: list[Unit]) -> None:
        text, spans = _assemble(units)
        if not text.strip():
            return
        order = len(drafts)
        digest = hashlib.sha256(f"{key}|{s_idx}|{order}|{text}".encode()).hexdigest()[:20]
        drafts.append(ChunkDraft(
            id=digest, kind=kind, section_idx=s_idx, section_path=paths[s_idx], text=text,
            token_count=count(text), page_start=min(u.page for u in units),
            page_end=max(u.page for u in units), spans=spans,
        ))

    for s_idx, section in enumerate(doc.sections):
        if section.kind == "references" and not index_references:
            continue
        kind = "references" if section.kind == "references" else "text"
        buffer: list[Unit] = []
        buffer_tokens = 0
        captions: list[Unit] = []  # emitted after the section's text so order stays readable
        for para in section.paragraphs:
            if para.kind == "caption":
                if index_captions:
                    captions += [Unit(p, para.page, para.bbox, count(p))
                                 for p in _split_sentences(para.text, target * 2, count)]
                continue
            tokens = count(para.text)
            units = (
                [Unit(para.text, para.page, para.bbox, tokens)]
                if tokens <= target
                else [Unit(p, para.page, para.bbox, count(p))
                      for p in _split_sentences(para.text, target, count)]
            )
            for unit in units:
                if buffer and buffer_tokens + unit.tokens > target:
                    emit(kind, s_idx, buffer)
                    carry = _tail(buffer[-1], overlap, count) if overlap else None
                    buffer = [carry] if carry else []
                    buffer_tokens = carry.tokens if carry else 0
                buffer.append(unit)
                buffer_tokens += unit.tokens
        if buffer:
            emit(kind, s_idx, buffer)
        for unit in captions:
            emit("caption", s_idx, [unit])
    return drafts
