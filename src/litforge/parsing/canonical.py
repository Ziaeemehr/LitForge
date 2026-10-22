"""Write and read the canonical parse outputs: canonical.json, paper.md, assets.json."""

from __future__ import annotations

import json
from pathlib import Path

from litforge.parsing.base import ParsedDocument

CANONICAL = "canonical.json"
MARKDOWN = "paper.md"
ASSETS = "assets.json"


def to_markdown(doc: ParsedDocument, title: str | None = None, asset_prefix: str = "",
                page_md: dict[int, str] | None = None) -> str:
    """Readable Markdown with page markers; asset links are relative (`asset_prefix` + path). A page in
    `page_md` (a vision model's transcription) replaces the parser's paragraphs of that page; its
    [Figure] markers become the figure crops."""
    page_md = page_md or {}
    out: list[str] = []
    if title:
        out.append(f"# {title}\n")
    figures_by_caption = {a.caption: a for a in doc.assets if a.caption and a.path}
    current_page = 0
    # Rewritten pages the parser found no text on (scans) go in by page order.
    with_text = {p.page for s in doc.sections for p in s.paragraphs}
    textless = sorted(n for n in page_md if n not in with_text)

    def textless_before(page: int) -> None:
        while textless and textless[0] < page:
            n = textless.pop(0)
            out.append(f"<!-- page {n} -->\n")
            out.append(_page_block(doc, n, page_md[n], asset_prefix))

    for section in doc.sections:
        heading = section.heading
        start = section.page_start
        if heading and start in page_md and heading.lower() in page_md[start].lower():
            heading = None  # the transcription of its first page has it
        if heading and not section.paragraphs:
            out.append(f"{'#' * min(section.level + 1, 6)} {heading}\n")
            heading = None
        for para in section.paragraphs:
            textless_before(para.page)
            if para.page in page_md:
                if para.page != current_page:
                    current_page = para.page
                    out.append(f"<!-- page {current_page} -->\n")
                    out.append(_page_block(doc, para.page, page_md[para.page], asset_prefix))
                continue
            if heading:
                out.append(f"{'#' * min(section.level + 1, 6)} {heading}\n")
                heading = None
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
    textless_before(doc.page_count + 1 + max(page_md, default=0))
    return "\n".join(out)


def _page_block(doc: ParsedDocument, page: int, markdown: str, asset_prefix: str) -> str:
    """A transcribed page with its figure crops in place of the [Figure] markers (leftovers at the end);
    table crops are added only when the transcription has no Markdown table."""
    crops = [a for a in doc.assets if a.page == page and a.path and
             (a.kind == "figure" or (a.kind == "table" and "|---" not in markdown.replace(" ", "")))]
    lines: list[str] = []
    for line in markdown.split("\n"):
        if line.strip().startswith("[Figure"):
            rest = line.strip().split("]", 1)[1].strip() if "]" in line else ""
            if crops:
                crop = crops.pop(0)
                lines.append(f"![{crop.label or crop.kind}]({asset_prefix}{crop.path})")
            if rest:
                lines.append(rest)
            continue
        lines.append(line)
    lines += [f"\n![{c.label or c.kind}]({asset_prefix}{c.path})" for c in crops]
    return "\n".join(lines) + "\n"


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
