"""Shared deterministic checks against exactly the transmitted evidence."""

from __future__ import annotations

import re
import unicodedata
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


_EDGE = re.compile(r"[^\w\s]*")
_DASHES = "-‐‑‒–—―−"  # a dash before a digit is a minus sign or a range
_MEANING = "+<>≤≥=±×%"  # symbols that change what a number means


def _glyph_lines(text: str) -> set[int]:
    """Indices of characters on stand-alone lines of at most two characters (stray equation glyphs in PDF text)."""
    skip: set[int] = set()
    for match in re.finditer(r"(?<=\n)[ \t]*(\S{1,2})[ \t]*(?=\r?\n)", text):
        skip.update(range(match.start(1), match.end(1)))
    return skip


def _quote_text(text: str, keep_line_break_hyphen: bool = False,
                skip: set[int] | None = None) -> tuple[str, list[int]]:
    """Letters and digits only, NFKC-normalised and casefolded, with each kept character's index in `text`.

    Spaces, punctuation, dashes, soft hyphens and line breaks are ignored, so PDF ligatures (ﬁ), hyphenation
    across lines or blank lines, and punctuation the model changed do not hide a real quote; words still have to
    match in order. Number meaning is kept: separators between digits, a sign or range dash before a digit, and
    comparison/percent symbols.
    """
    chars: list[str] = []
    offsets: list[int] = []
    for i, original in enumerate(text):
        if skip and i in skip:
            continue
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if original in _DASHES and i and text[i - 1].isalpha():
            if nxt.isalpha():
                chars.append("-")  # a hyphen inside a word carries meaning: re-sign is not resign
                offsets.append(i)
                continue
            after = re.match(r"[ \t]*(?:\r?\n[ \t]*)+(?=[^\W\d_])", text[i + 1:])
            if after and keep_line_break_hyphen:  # "simulation-\nbased" may be a compound split by the layout
                chars.append("-")
                offsets.append(i)
                continue
        if original in ".," and i and text[i - 1].isdigit() and nxt.isdigit():
            chars.append(original)  # decimal or thousands separator
            offsets.append(i)
            continue
        if (original in _DASHES and nxt.isdigit()) or original in _MEANING:
            chars.append("-" if original in _DASHES else original)  # signs, ranges, comparisons, %
            offsets.append(i)
            continue
        for char in unicodedata.normalize("NFKC", original).casefold():
            if char.isalnum():
                chars.append(char)
                offsets.append(i)
    return "".join(chars), offsets


def locate_quote(quote: str, text: str) -> tuple[int, int] | None:
    """Find a quote in `text`, returning character offsets into the original text (end exclusive)."""
    needle = _quote_text(quote)[0]
    if not needle:
        return None
    # A hyphen at a line break is ambiguous ("treat-\nment" vs "simulation-\nbased"): try joining, then keeping it.
    glyphs = _glyph_lines(text)
    for keep in (False, True):
        haystack, offsets = _quote_text(text, keep, glyphs)
        found = haystack.find(needle)
        # The match must start and end on word boundaries: "gain" is not a quote of "again".
        while found >= 0 and not _on_boundaries(text, offsets[found], offsets[found + len(needle) - 1] + 1):
            found = haystack.find(needle, found + 1)
        if found >= 0:
            break
    else:
        return None
    start, end = offsets[found], offsets[found + len(needle) - 1] + 1
    # Include the symbols the quote itself starts or ends with (closing "%.", quotation marks).
    lead = len(_EDGE.match(quote.strip())[0])  # type: ignore[index]
    trail = len(_EDGE.match(quote.strip()[::-1])[0])  # type: ignore[index]
    while lead and start > 0 and _symbol(text[start - 1]):
        start, lead = start - 1, lead - 1
    while trail and end < len(text) and _symbol(text[end]):
        end, trail = end + 1, trail - 1
    return start, end


def _on_boundaries(text: str, start: int, end: int) -> bool:
    return (start == 0 or not text[start - 1].isalnum()) and (end >= len(text) or not text[end].isalnum())


def _symbol(char: str) -> bool:
    return not char.isalnum() and not char.isspace()
