"""Shared deterministic checks against exactly the transmitted evidence."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from litforge.llm.evidence import EvidencePacket

_NUMBER = re.compile(r"(?<![\d.])[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?(?!\d|\.\d)")


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
    cited = set() if packet is None else set().union(*(numbers(e.text) for e in packet.items if e.id in ids))
    return bool(numbers(text) - cited)


def citations(ids: list[str], packet: EvidencePacket | None) -> tuple[list[str], list[str]]:
    allowed = {e.id for e in packet.items} if packet else set()
    return (list(dict.fromkeys(i for i in ids if i in allowed)), [i for i in ids if i not in allowed])
