"""Structural PDF adapter for the local Nemotron Parse engine."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

import pymupdf

from litforge.config import ParsingConfig, Paths
from litforge.parsing import nemotron
from litforge.parsing.base import Asset, BBox, PageInfo, Paragraph, ParsedDocument, Section, section_kind


class PersianDocument(RuntimeError):
    """Nemotron's output is Persian and needs another parser."""


class _Engine(Protocol):
    def read(self, png: bytes) -> tuple[str, tuple[int, int]]: ...

    def blocks(self, raw: str, size: tuple[int, int]) -> list[dict[str, Any]]: ...


class _Rows(HTMLParser):
    """Extract readable table rows without losing the original HTML asset."""

    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cell: list[str] | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'tr':
            self.rows.append([])
        elif tag in ('td', 'th'):
            self.cell = []
        elif tag == 'br' and self.cell is not None:
            self.cell.append(' ')

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ('td', 'th') and self.cell is not None:
            if self.rows:
                self.rows[-1].append(' '.join(''.join(self.cell).split()))
            self.cell = None


def _distance(a: BBox, b: BBox) -> float:
    return min(abs(a[3] - b[1]), abs(b[3] - a[1]))


class NemotronParser:
    name = 'nemotron'

    def __init__(self, paths: Paths, engine: _Engine | None = None,
                 progress: Callable[[str], None] = lambda _: None) -> None:
        self.paths = paths
        self._engine = engine
        self.progress = progress

    @property
    def version(self) -> str:
        return nemotron.NAME

    @property
    def engine(self) -> _Engine:
        if self._engine is None:
            self._engine = nemotron.NemotronEngine(self.paths)
        return self._engine

    def parse(self, pdf_path: Path, out_dir: Path, config: ParsingConfig) -> ParsedDocument:
        out_dir.mkdir(parents=True, exist_ok=True)
        cache_path = out_dir / 'nemotron.json'
        try:
            cache = json.loads(cache_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            cache = {}
        if not isinstance(cache, dict) or cache.get('revision') != nemotron.REVISION:
            cache = {'revision': nemotron.REVISION, 'pages': {}}
        sections = [Section(heading=None, kind='front')]
        assets: list[Asset] = []
        furniture: list[str] = []
        pages: list[PageInfo] = []
        with pymupdf.open(pdf_path) as pdf:
            for idx, page in enumerate(pdf):
                number = idx + 1
                self.progress(f'page {number}/{len(pdf)}')
                entry = cache['pages'].get(str(number))
                if entry is None:
                    scale = min(nemotron.MAX_SIZE[0] / page.rect.width,
                                nemotron.MAX_SIZE[1] / page.rect.height)
                    png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes('png')
                    raw, size = self.engine.read(png)
                    entry = {'raw': raw, 'size': list(size)}
                    cache['pages'][str(number)] = entry
                    temporary = cache_path.with_suffix('.json.tmp')
                    temporary.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
                    temporary.replace(cache_path)
                size = (int(entry['size'][0]), int(entry['size'][1]))
                blocks = self.engine.blocks(entry['raw'], size)
                page_assets: list[Asset] = []
                captions: list[Paragraph] = []
                tables: list[tuple[Asset, Paragraph, str]] = []
                for block in blocks:
                    cls = block.get('class')
                    content = str(block.get('content') or '').strip()
                    box = block.get('bbox')
                    bbox: BBox | None = (float(box[0]) * page.rect.width, float(box[1]) * page.rect.height,
                                         float(box[2]) * page.rect.width, float(box[3]) * page.rect.height
                                         ) if box is not None else None
                    text = nemotron.inline_math(content)
                    if cls in ('Page-header', 'Page-footer'):
                        if text:
                            furniture.append(text)
                        continue
                    if cls == 'Section-header':
                        hashes = re.match(r'^\s*(#+)', content)
                        level = max(1, len(hashes[1]) - 1) if hashes else 1
                        heading = re.sub(r'^\s*#+\s*', '', content).replace('**', '')
                        heading = re.sub(r'(?<!\w)_(\S(?:.*?\S)?)_(?!\w)', r'\1', heading).strip()
                        sections.append(Section(heading=heading, kind=section_kind(heading), level=level))
                        continue
                    if cls in ('Picture', 'Chart', 'Table'):
                        asset = Asset(kind='table' if cls == 'Table' else 'figure', label=None,
                                      caption=None, page=number, bbox=bbox)
                        page_assets.append(asset)
                        assets.append(asset)
                        if cls == 'Table':
                            asset.data = {'html': content, 'markdown': nemotron.table_markdown(content)}
                            plain = '\n'.join(' | '.join(row) for row in _Rows(text).rows)
                            paragraph = Paragraph(text=plain, kind='caption', page=number, bbox=bbox)
                            sections[-1].paragraphs.append(paragraph)
                            tables.append((asset, paragraph, plain))
                        continue
                    if not text:
                        continue
                    paragraph = Paragraph(text=text, page=number, bbox=bbox)
                    if cls == 'Formula' and (match := nemotron.FORMULA.match(content)):
                        latex = (match['a'] or match['b'] or match['c']).strip()
                        tag = f" \\tag{{{match['tag']}}}" if match['tag'] else ''
                        paragraph.text = f'$${latex}{tag}$$'
                        paragraph.kind = 'equation'
                        assets.append(Asset(kind='equation', label=None, caption=None, page=number,
                                            bbox=bbox, data={'latex': latex}))
                    elif cls == 'Caption' and re.match(r'^(?:Figure\b|Fig\.|Table\b)', text, re.IGNORECASE):
                        paragraph.kind = 'caption'
                        captions.append(paragraph)
                    target = sections[0] if cls == 'Title' else sections[-1]
                    target.paragraphs.append(paragraph)
                for caption in captions:
                    candidates = [a for a in page_assets if a.bbox is not None]
                    if candidates and caption.bbox is not None:
                        cap_box = caption.bbox
                        nearest = min(candidates, key=lambda a: _distance(a.bbox, cap_box)
                                      if a.bbox is not None else float('inf'))
                        nearest.caption = caption.text
                for asset, paragraph, plain in tables:
                    paragraph.text = '\n\n'.join(t for t in (asset.caption, plain) if t)
                    # The table paragraph includes its caption, as in the MinerU mapping.
                    for section in sections:
                        section.paragraphs[:] = [p for p in section.paragraphs
                                                 if not (p in captions and p.text == asset.caption)]
                figures = [a for a in page_assets if a.kind == 'figure']
                for count, asset in enumerate(figures, 1):
                    if asset.bbox is None:
                        asset.quality = 'failed'
                        continue
                    clip = (pymupdf.Rect(asset.bbox) + (-4, -4, 4, 4)) & page.rect
                    if clip.is_empty:
                        asset.quality = 'failed'
                        continue
                    folder = out_dir / 'assets'
                    folder.mkdir(exist_ok=True)
                    name = f'figure_p{number:03d}_{count}.png'
                    page.get_pixmap(clip=clip, dpi=config.figure_dpi).save(folder / name)
                    asset.path = f'assets/{name}'
                if number == 2 and nemotron.is_persian(
                        '\n'.join(p.text for s in sections for p in s.paragraphs)) is True:
                    raise PersianDocument('Nemotron returned Persian text')  # stop before reading the rest
                pages.append(PageInfo(index=number, label=page.get_label() or None,
                                      width=page.rect.width, height=page.rect.height,
                                      char_count=0, image_count=len(figures), column_count=1))
        for info in pages:
            info.char_count = sum(len(p.text) for s in sections for p in s.paragraphs if p.page == info.index)
        result = ParsedDocument(parser=self.name, parser_version=self.version, page_count=len(pages), pages=pages,
                                sections=[s for s in sections if s.heading or s.paragraphs],
                                assets=assets, furniture=furniture)
        text = result.full_text()
        if not text.strip():
            raise ValueError('Nemotron returned no body text')
        if nemotron.is_persian(text) is True:
            raise PersianDocument('Nemotron returned Persian text')
        return result
