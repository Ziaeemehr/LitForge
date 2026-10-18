"""Human-readable labels for legacy parser statuses; persisted values stay stable."""

from __future__ import annotations

from collections.abc import Iterable

from litforge.config import Config, Paths
from litforge.parsing import docling_parser, mineru_parser

INSTALL_HINT = "install with `litforge parse install-mineru`"


def parse_status_label(status: str | None) -> str:
    return (status or "?").replace("needs_docling", "needs accurate parser")


def parser_install_hint(statuses: Iterable[str | None], paths: Paths, config: Config) -> str:
    if ("needs_docling" in statuses and not mineru_parser.find_command(paths, config.parsing)
            and not docling_parser.docling_available()):
        return INSTALL_HINT
    return ""
