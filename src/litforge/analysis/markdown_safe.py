"""Render untrusted text as inert, single-line Markdown."""

from __future__ import annotations

import re


def inert(text: object) -> str:
    value = " ".join(str(text).splitlines())
    return re.sub(r"([\\`*_{}\[\]()<>|!#+~&])", r"\\\1", value)


def excerpt(text: str, limit: int = 400) -> str:
    """Cap a quote, including its ellipsis, without splitting a word."""
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    prefix = value[:limit - 1]
    if not value[limit - 1].isspace():
        prefix = prefix.rsplit(" ", 1)[0] if " " in prefix else ""
    return prefix.rstrip() + "…"
