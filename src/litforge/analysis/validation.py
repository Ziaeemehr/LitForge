"""Shared deterministic checks against exactly the transmitted evidence."""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import AfterValidator

from litforge.llm.evidence import EvidencePacket

_NUMBER = re.compile(r"(?<![\d.])[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?(?!\d|\.\d)")
_EVIDENCE_ID = r"\bev_[0-9a-f]{8,}\b"
_EVIDENCE_LIST = rf"{_EVIDENCE_ID}(?:\s*(?:,\s*(?:and\s+)?|and\s+){_EVIDENCE_ID})*"
_EVIDENCE_MENTION = re.compile(
    rf"(?:,\s*|\band\s+)?(?:\(\s*{_EVIDENCE_LIST}\s*\)|\[\s*{_EVIDENCE_LIST}\s*\]|"
    rf"\{{\s*{_EVIDENCE_LIST}\s*\}}|{_EVIDENCE_LIST})(?:\s*,)?", re.I,
)


def strip_evidence_mentions(text: str) -> str:
    """Keep citations in structured IDs, so their hex digits cannot become reported numbers."""
    if not _EVIDENCE_MENTION.search(text):
        return text.strip()
    text = _EVIDENCE_MENTION.sub(" ", text)
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


EvidenceText = Annotated[str, AfterValidator(strip_evidence_mentions)]


def number(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value).strip().replace("−", "-").replace(",", ""))
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def numbers(text: str) -> set[Decimal]:
    return {n for m in _NUMBER.finditer(text.replace("−", "-")) if (n := number(m.group())) is not None}


def missing_numbers(text: str, ids: list[str], packet: EvidencePacket | None) -> bool:
    cited = set() if packet is None else set().union(
        *(numbers(e.text) for e in packet.items if e.id in ids and not e.from_image))
    return bool(numbers(strip_evidence_mentions(text)) - cited)


def image_only_numbers(text: str, ids: list[str], packet: EvidencePacket | None) -> bool:
    if packet is None:
        return False
    cited = set().union(*(numbers(e.text) for e in packet.items if e.id in ids and e.from_image))
    textual = set().union(*(numbers(e.text) for e in packet.items if e.id in ids and not e.from_image))
    return bool((numbers(strip_evidence_mentions(text)) - textual) & cited)


def citations(ids: list[str], packet: EvidencePacket | None) -> tuple[list[str], list[str]]:
    allowed = {e.id for e in packet.items} if packet else set()
    return (list(dict.fromkeys(i for i in ids if i in allowed)), [i for i in ids if i not in allowed])


_DASHES = "-‐‑‒–—―−"
_LINE_BREAK_HYPHEN = re.compile(r"(?<=[^\W\d_])-[ \t]*(?:\r?\n[ \t]*)+(?=[^\W\d_])")
_NUMERIC_ATOM = r"(?:\d+(?:[.,]\s*\d+)*|[.,]\s*\d+)(?:e\s*[+-]?\s*\d+)?(?:\s*%)?"
_QUOTE_TOKEN = re.compile(
    rf"(?:[+\-<>≤≥=±×]\s*)*{_NUMERIC_ATOM}(?:\s*[-+±×/⁄<>≤≥=]+\s*{_NUMERIC_ATOM})*"
    r"(?![^\W_])|[^\W_]+(?:-[^\W_]+)*|[+<>≤≥=±×%]"
)
_SPACE = re.compile(r"\s+")


@dataclass
class _QuoteText:
    # NUL delimiters make substring searches match complete token sequences.
    text: str
    positions: list[int]
    starts: list[int]
    ends: list[int]

    def find(self, needle: _QuoteText) -> int | None:
        found = self.text.find(needle.text)
        while found >= 0:
            first = bisect_left(self.positions, found)
            last = first + len(needle.starts) - 1
            if self.starts[first] >= 0 and self.ends[last] >= 0:
                return first
            found = self.text.find(needle.text, found + 1)
        return None


def _glyph_lines(text: str) -> set[int]:
    """Indices on stand-alone lines of at most two characters (stray PDF equation glyphs)."""
    skip: set[int] = set()
    for match in re.finditer(r"(?<=\n)[ \t]*(\S{1,2})[ \t]*(?=\r?\n)", text):
        skip.update(range(match.start(1), match.end(1)))
    return skip


def _nfkc_text(text: str, skip: set[int] | None = None) -> tuple[str, list[int]]:
    """Normalize before classifying punctuation; expansions retain their source character index."""
    chars: list[str] = []
    offsets: list[int] = []
    for i, original in enumerate(text):
        if skip and i in skip:
            continue
        for char in unicodedata.normalize("NFKC", original).casefold():
            if char == "\u00ad":
                continue
            chars.append("-" if char in _DASHES else char)
            offsets.append(i)
    return "".join(chars), offsets


def _quote_text(text: str, keep_line_break_hyphen: bool = False, *,
                normalized: tuple[str, list[int]] | None = None) -> _QuoteText:
    """Words and numeric expressions, with complete-token offsets in the original text."""
    value, offsets = normalized if normalized is not None else _nfkc_text(text)
    # Disjoint slices copy each character at most once, even on very long hyphenated spans.
    parts: list[str] = []
    joined_offsets: list[int] = []
    previous = 0
    for match in _LINE_BREAK_HYPHEN.finditer(value):
        end = match.start() + int(keep_line_break_hyphen)
        parts.append(value[previous:end])
        joined_offsets.extend(offsets[previous:end])
        previous = match.end()
    parts.append(value[previous:])
    joined_offsets.extend(offsets[previous:])
    value, offsets = "".join(parts), joined_offsets

    tokens: list[str] = []
    positions: list[int] = []
    starts: list[int] = []
    ends: list[int] = []
    position = 0
    for match in _QUOTE_TOKEN.finditer(value):
        a, b = match.span()
        token = _SPACE.sub("", match.group())
        tokens.append(token)
        positions.append(position)
        position += len(token) + 1
        # An NFKC/casefold expansion must be consumed in full, even if it creates multiple tokens.
        starts.append(offsets[a] if a == 0 or offsets[a - 1] != offsets[a] else -1)
        ends.append(offsets[b - 1] + 1 if b == len(offsets) or offsets[b] != offsets[b - 1] else -1)
    return _QuoteText("\0" + "\0".join(tokens) + "\0" if tokens else "", positions, starts, ends)


def _quote_variants(text: str) -> tuple[_QuoteText, _QuoteText]:
    normalized = _nfkc_text(text, _glyph_lines(text))
    return (_quote_text(text, normalized=normalized),
            _quote_text(text, keep_line_break_hyphen=True, normalized=normalized))


def _quote_edges(quote: str, needle: _QuoteText, text: str, start: int, end: int) -> tuple[int, int]:
    """Extend only over corresponding, previously unmatched quote-edge punctuation."""
    # The needle's source offsets exclude symbols already consumed by numeric tokens (such as - and %).
    for char in reversed(quote[:needle.starts[0]].strip()):
        if start == 0 or _nfkc_text(char)[0] != _nfkc_text(text[start - 1])[0]:
            break
        start -= 1
    for char in quote[needle.ends[-1]:].strip():
        if end == len(text) or _nfkc_text(char)[0] != _nfkc_text(text[end])[0]:
            break
        end += 1
    return start, end


class QuoteLocator:
    """Prepare evidence once, then locate any number of quotes against its transmitted tokens.

    Normal packets are prefixes of their stored spans, so their offsets are already source offsets.
    For a normalized packet, align the *whole packet* once and map its tokens to that occurrence.
    """

    def __init__(self, text: str, source: str | None = None):
        self.text = text
        self.source = source if source is not None else text
        self.variants = _quote_variants(text)
        self._source_variants: tuple[_QuoteText, _QuoteText] | None = None
        self._mapped: dict[int, _QuoteText | None] = {}
        self._prefix_end: int | None = None
        if self.source.startswith(text):
            self._prefix_end = len(text)
        elif text.endswith("…") and self.source.startswith(text[:-1]):
            self._prefix_end = len(text) - 1

    def _map_variant(self, index: int) -> _QuoteText | None:
        if index in self._mapped:
            return self._mapped[index]
        if self._source_variants is None:
            self._source_variants = _quote_variants(self.source)
        packet = self.variants[index]
        candidates = [(variant.starts[first], first, variant) for variant in self._source_variants
                      if (first := variant.find(packet)) is not None]
        mapped = None
        if candidates:
            _, first, source = min(candidates, key=lambda candidate: candidate[0])
            end = first + len(packet.starts)
            mapped = _QuoteText(packet.text, packet.positions, source.starts[first:end], source.ends[first:end])
        self._mapped[index] = mapped
        return mapped

    def locate(self, quote: str) -> tuple[int, int] | None:
        needle = _quote_text(quote)
        if not needle.text:
            return None
        for index, variant in enumerate(self.variants):
            first = variant.find(needle)
            if first is None:
                continue
            last = first + len(needle.starts) - 1
            start, end = _quote_edges(quote, needle, self.text, variant.starts[first], variant.ends[last])
            if self._prefix_end is not None:
                return (start, end) if end <= self._prefix_end else None
            mapped = self._map_variant(index)
            if mapped is not None and mapped.starts[first] >= 0 and mapped.ends[last] >= 0:
                return _quote_edges(quote, needle, self.source, mapped.starts[first], mapped.ends[last])
        return None


def locate_quote(quote: str, text: str) -> tuple[int, int] | None:
    """Find complete quote tokens, returning original character offsets (end exclusive)."""
    return QuoteLocator(text).locate(quote)
