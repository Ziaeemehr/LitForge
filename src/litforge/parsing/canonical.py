"""Write and read the canonical parse outputs: canonical.json, paper.md, assets.json."""

from __future__ import annotations

import json
from pathlib import Path

from litforge.parsing.base import ParsedDocument

CANONICAL = "canonical.json"
MARKDOWN = "paper.md"
ASSETS = "assets.json"


def to_markdown(doc: ParsedDocument, title: str | None = None, asset_prefix: str = "") -> str:
    """Readable Markdown with page markers; asset links are relative (`asset_prefix` + path)."""
    out: list[str] = []
    if title:
        out.append(f"# {title}\n")
    figures_by_caption = {a.caption: a for a in doc.assets if a.caption and a.path}
    current_page = 0
    for section in doc.sections:
        if section.heading:
            depth = min(section.level + 1, 6)
            out.append(f"{'#' * depth} {section.heading}\n")
        for para in section.paragraphs:
            if para.page != current_page:
                current_page = para.page
                out.append(f"<!-- page {current_page} -->\n")
            if para.kind == "caption":
                asset = figures_by_caption.get(para.text.split("\n\n", 1)[0])
                if asset is not None and asset.path:
                    alt = asset.label or asset.kind
                    out.append(f"![{alt}]({asset_prefix}{asset.path})\n")
                out.append(f"> {para.text.replace(chr(10), chr(10) + '> ')}\n")
            elif para.kind == "equation":
                # The crop is the faithful record; extracted equation text is best-effort only.
                asset = figures_by_caption.get(para.text)
                if asset is not None and asset.path:
                    out.append(f"![{asset.label or 'equation'}]({asset_prefix}{asset.path})\n")
                out.append(f"```text\n{para.text}\n```\n")
            else:
                out.append(f"{para.text}\n")
    return "\n".join(out)


def write_outputs(doc: ParsedDocument, out_dir: Path, title: str | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / CANONICAL).write_text(doc.model_dump_json(indent=1), encoding="utf-8")
    (out_dir / MARKDOWN).write_text(to_markdown(doc, title), encoding="utf-8")
    manifest = [a.model_dump(exclude={"data"}) for a in doc.assets]
    (out_dir / ASSETS).write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")


def read_canonical(out_dir: Path) -> ParsedDocument:
    return ParsedDocument.model_validate_json((out_dir / CANONICAL).read_text(encoding="utf-8"))


def outputs_exist(out_dir: Path) -> bool:
    return (out_dir / CANONICAL).is_file() and (out_dir / MARKDOWN).is_file()
