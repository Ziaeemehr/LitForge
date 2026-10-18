"""Shared deterministic checks against exactly the transmitted evidence."""

from __future__ import annotations

import re
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
