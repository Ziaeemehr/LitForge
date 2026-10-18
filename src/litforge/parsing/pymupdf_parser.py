"""Fast-path parser built on PyMuPDF.

Produces page-level provenance (page index, printed label, bbox) for every paragraph,
restores reading order on two-column pages, removes running headers/footers, detects
section headings, captions and display equations, and crops figures/tables/equations.
All heuristics are deliberately simple and recorded so they can be calibrated.
"""

from __future__ import annotations

import bisect
import contextlib
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf

from litforge.config import ParsingConfig
from litforge.parsing.base import (
    Asset,
    BBox,
    PageInfo,
    Paragraph,
    ParagraphKind,
    ParsedDocument,
    Section,
    SectionKind,
)

if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()  # pymupdf_layout is a benchmark candidate, not a default

# Bump when heuristics change so cached parses are invalidated.
ALGORITHM_VERSION = "6"

CAPTION_RE = re.compile(
    r"^\s*(?P<kind>fig(?:ure)?s?\.?|table|tab\.)\s*(?P<num>S?\d+[a-z]?)"
    r"(?P<delim>\s*[.:|\-—–](?!\d)|\s*$)?(?=\s|$)",
    re.IGNORECASE,
)
MIN_UNCAPTIONED_TABLE_FILL = 0.6  # caption-less grids must be mostly filled to count as tables


def is_caption(text: str, first_line_bold: bool = False) -> bool:
    """'Table 3. Errors' or a bold 'Figure 2 ...' is a caption; 'Table 3 shows that ...' is prose."""
    match = CAPTION_RE.match(text)
    return bool(match and (match.group("delim") is not None or first_line_bold))
_SECTION_WORDS = (
    r"abstract|introduction|background|related\s+work|preliminaries|"
    r"materials?\s+and\s+methods|methods?|methodology|experiments?|experimental\s+setup|"
    r"results?(?:\s+and\s+discussion)?|discussion|conclusions?(?:\s+and\s+future\s+work)?|"
    r"summary|limitations|acknowledg(?:e)?ments?|funding|references|bibliography|"
    r"appendix(?:\s+[a-z0-9]+)?|appendices|supplementary(?:\s+(?:material|information|methods))?|"
    r"supporting\s+information|data\s+availability|code\s+availability|"
    r"author\s+contributions|competing\s+interests"
)
HEADING_WORDS_RE = re.compile(
    rf"^(?:(?P<num>\d+(?:\.\d+)*|[IVX]+|[A-Z])\.?\s+)?(?P<word>{_SECTION_WORDS})\s*:?$",
    re.IGNORECASE,
)
NUMBERED_HEADING_RE = re.compile(r"^(?P<num>\d+(?:\.\d+){0,3}|[IVX]{1,5})\.?\s+[A-Z][^\n]{1,100}$")
APPENDIX_HEADING_RE = re.compile(r"^(?:(?i:appendix)\s+)?(?P<num>[A-H](?:\.\d+)*)\.?\s+[A-Z][^\n]{1,100}$")
# A sentence end inside the text ("Shortcuts. We have shown") marks a run-in paragraph, not a heading.
RUN_IN_RE = re.compile(r"(?:[A-Za-z]{2}|[)\]])\.\s+\S")
INLINE_ABSTRACT_RE = re.compile(r"^abstract\s*[.:—–\-]\s*", re.IGNORECASE)
EQ_LABEL_RE = re.compile(r"\(\s*\d+[a-z]?\s*\)\s*$")
MATH_CHARS = set("=+−×÷∑∏∫∂∇≈≤≥∝∞αβγδεζηθκλμνξπρστφχψωΓΔΘΛΞΠΣΦΨΩ^_|")
# TeX/Office math fonts; plain CMR is excluded because LaTeX body text uses it too.
MATH_FONT_RE = re.compile(r"CMMI|CMSY|CMEX|MSAM|MSBM|EUFM|EUSM|RSFS|STIX|Math|Symbol|MTMI|MTSY|MTEX", re.IGNORECASE)
PUA_RE = re.compile("[\ue000-\uf8ff]")  # private-use glyphs: big brackets, radicals
CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f]")
MATH_WORDS = {"det", "log", "exp", "max", "min", "sin", "cos", "tan", "lim", "sup", "inf", "arg", "tr", "diag"}

MARGIN_FRACTION = 0.08  # top/bottom band examined for running headers/footers


@dataclass
class Line:
    text: str
    bbox: BBox
    size: float
    bold: bool
    math: float = 0.0  # fraction of characters set in math fonts or math symbols


@dataclass(eq=False)  # identity semantics: membership tests must not equate twin blocks
class Block:
    page: int
    bbox: BBox
    lines: list[Line]
    kind: str = "text"  # text | furniture | caption | heading | equation | figure_text | table_text
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return join_lines([ln.text for ln in self.lines])

    @property
    def size(self) -> float:
        total = sum(len(ln.text) for ln in self.lines) or 1
        return sum(ln.size * len(ln.text) for ln in self.lines) / total

    @property
    def bold(self) -> bool:
        chars = sum(len(ln.text) for ln in self.lines) or 1
        return sum(len(ln.text) for ln in self.lines if ln.bold) / chars > 0.6

    @property
    def math(self) -> float:
        chars = sum(len(ln.text) for ln in self.lines) or 1
        return sum(ln.math * len(ln.text) for ln in self.lines) / chars


def join_lines(lines: list[str]) -> str:
    """Join PDF lines into a paragraph, repairing end-of-line hyphenation."""
    out = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
        elif out.endswith("-") and not out.endswith(" -"):
            # "simu-/lation" -> "simulation"; "Jansen-/Rit" keeps the hyphen.
            out = out[:-1] + line if line[:1].islower() else out + line
        else:
            out = f"{out} {line}"
    return out


def _union(boxes: list[BBox]) -> BBox:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _h_overlap(a: BBox, b: BBox) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def _inside_fraction(inner: BBox, outer: BBox) -> float:
    ix = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    iy = max(0.0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    area = max((inner[2] - inner[0]) * (inner[3] - inner[1]), 1e-6)
    return ix * iy / area


def _segment_spans(spans: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a line's spans where the horizontal gap exceeds ~3 character heights."""
    segments: list[list[dict[str, Any]]] = []
    last_x1: float | None = None
    for span in spans:
        if not span["text"].strip():
            continue
        gap_limit = max(3 * span["size"], 15.0)
        is_eq_label = bool(EQ_LABEL_RE.fullmatch(span["text"].strip()))  # keep "(3)" with its equation
        if last_x1 is None or (span["bbox"][0] - last_x1 > gap_limit and not is_eq_label):
            segments.append([])
        segments[-1].append(span)
        last_x1 = span["bbox"][2]
    return segments


def _make_line(spans: list[dict[str, Any]]) -> Line:
    text = ""
    for span in spans:
        # Re-insert the space PyMuPDF dropped between spans that are visibly apart.
        if text and not text.endswith(" ") and not span["text"].startswith(" "):
            prev = spans[spans.index(span) - 1]
            if span["bbox"][0] - prev["bbox"][2] > 0.15 * span["size"]:
                text += " "
        text += span["text"]
    # Math-extension fonts map big delimiters to control codes; they carry no text.
    text = CONTROL_RE.sub("", text)
    chars = sum(len(s["text"]) for s in spans) or 1
    bold_chars = sum(len(s["text"]) for s in spans if s["flags"] & 16 or "bold" in s["font"].lower())
    return Line(
        text=text,
        bbox=_union([tuple(s["bbox"]) for s in spans]),
        size=sum(s["size"] * len(s["text"]) for s in spans) / chars,
        bold=bold_chars / chars > 0.6,
        math=sum(
            len(s["text"]) if MATH_FONT_RE.search(s["font"])
            else sum(ch in MATH_CHARS or bool(PUA_RE.match(ch)) for ch in s["text"])
            for s in spans
        ) / chars,
    )


def _column_groups(lines: list[Line]) -> list[list[Line]]:
    """Group line segments whose x-ranges overlap (same column), preserving vertical order."""
    groups: list[tuple[float, float, list[Line]]] = []
    for line in sorted(lines, key=lambda ln: ln.bbox[0]):
        if groups and line.bbox[0] <= groups[-1][1]:
            x0, x1, members = groups[-1]
            groups[-1] = (x0, max(x1, line.bbox[2]), members + [line])
        else:
            groups.append((line.bbox[0], line.bbox[2], [line]))
    return [sorted(members, key=lambda ln: (ln.bbox[1], ln.bbox[0])) for _, _, members in groups]


def _is_math_fragment(block: Block) -> bool:
    """Could this block be one piece of a display equation (numerator, sub/superscript, label...)?"""
    if block.kind not in ("text", "equation"):
        return False
    text = PUA_RE.sub("", block.text).strip()
    if not text or EQ_LABEL_RE.fullmatch(text):
        return True
    if block.math >= 0.4 and len(text) < 300:
        return True
    is_prose_word = text.isalpha() and text.islower() and len(text) >= 3 and text not in MATH_WORDS
    return len(text) <= 6 and not is_prose_word


def _group_equations(blocks: list[Block], body_size: float, page_width: float) -> list[Block]:
    """Merge the scattered pieces of each display equation into one `equation` block.

    PyMuPDF returns fractions, sums, scripts, big delimiters and the "(n)" label as separate
    blocks. Pieces that are vertically close, horizontally related and in the same column are
    joined when at least one of them is math-heavy. The text stays best-effort; the page crop
    rendered from the merged bbox is the faithful record.
    """
    frags = [b for b in blocks if _is_math_fragment(b)]
    if not frags:
        return blocks
    mid, tol = page_width / 2, page_width * 0.04

    def side(b: Block) -> str:
        if b.bbox[2] <= mid + tol:
            return "L"
        return "R" if b.bbox[0] >= mid - tol else "F"

    # Equation fragments alone cannot reveal the layout: a centred single-column
    # equation can have pieces entirely in either half of the page.
    prose = [b for b in blocks if b.kind == "text" and len(b.text) >= 60 and b.math < 0.2]
    prose_sides = {side(b) for b in prose}
    two_columns = (
        {"L", "R"} <= prose_sides
        and sum(len(b.text) for b in prose if side(b) != "F") >= 0.6 * sum(len(b.text) for b in prose)
    )

    def near(a: Block, b: Block) -> bool:
        if two_columns and {side(a), side(b)} == {"L", "R"}:
            return False
        dy = max(a.bbox[1], b.bbox[1]) - min(a.bbox[3], b.bbox[3])  # negative when rows overlap
        if dy > 0.9 * body_size:
            return False
        if _h_overlap(a.bbox, b.bbox) > 0:
            return True
        dx = max(a.bbox[0], b.bbox[0]) - min(a.bbox[2], b.bbox[2])
        return dy < 0 and dx < 250

    parent = list(range(len(frags)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(frags)):
        for j in range(i + 1, len(frags)):
            if near(frags[i], frags[j]):
                parent[find(i)] = find(j)
    clusters: dict[int, list[Block]] = {}
    for i, frag in enumerate(frags):
        clusters.setdefault(find(i), []).append(frag)

    replaced: dict[int, Block | None] = {}  # id(original) -> merged block (first member) or None
    for members in clusters.values():
        anchors = [m for m in members if m.math >= 0.4 and len(PUA_RE.sub("", m.text).strip()) >= 2]
        if not anchors:
            continue
        labels = [m for m in members if EQ_LABEL_RE.fullmatch(PUA_RE.sub("", m.text).strip())]
        if len(members) == 1:
            if labels or anchors[0].math >= 0.5:
                anchors[0].kind = "equation"
            continue
        body = [m for m in members if m not in labels]
        row = 0.6 * body_size
        lines = sorted(
            (Line(PUA_RE.sub("", ln.text).strip(), ln.bbox, ln.size, ln.bold, ln.math)
             for m in body for ln in m.lines),
            key=lambda ln: (round((ln.bbox[1] + ln.bbox[3]) / 2 / row), ln.bbox[0]),
        )
        lines = [ln for ln in lines if ln.text]
        label_text = labels[0].text.strip() if labels else None
        if label_text:
            lines.append(Line(label_text, labels[0].bbox, labels[0].size, False))
        merged = Block(members[0].page, _union([m.bbox for m in members]), lines or members[0].lines,
                       kind="equation", meta={"label": label_text, "fragments": len(members)})
        for k, m in enumerate(sorted(members, key=lambda m: (m.bbox[1], m.bbox[0]))):
            replaced[id(m)] = merged if k == 0 else None
    if not replaced:
        return blocks
    out: list[Block] = []
    for b in blocks:
        if id(b) not in replaced:
            out.append(b)
        elif (merged_block := replaced[id(b)]) is not None:
            out.append(merged_block)
    return out


SECTION_NUMBER_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVX]{1,5}\.|[A-H](?:\.\d+)*\.?)$")


def _merge_section_numbers(blocks: list[Block]) -> list[Block]:
    """Join a bare section number ("1", "3.2.") with the heading text on the same baseline."""
    consumed: set[int] = set()
    out: list[Block] = []
    for i, block in enumerate(blocks):
        if i in consumed:
            continue
        if len(block.lines) == 1 and SECTION_NUMBER_RE.match(block.text.strip()):
            for j, other in enumerate(blocks):
                if j == i or j in consumed:
                    continue
                first = other.lines[0]
                same_baseline = abs(first.bbox[3] - block.bbox[3]) < 3
                just_right = 0 <= first.bbox[0] - block.bbox[2] < 40
                if same_baseline and just_right:
                    merged = Line(f"{block.text.strip()} {first.text.strip()}",
                                  _union([block.bbox, first.bbox]), first.size, first.bold)
                    lines = [merged, *other.lines[1:]]
                    out.append(Block(block.page, _union([ln.bbox for ln in lines]), lines))
                    consumed.add(j)
                    break
            else:
                out.append(block)
            continue
        out.append(block)
    return out


def _normalize_furniture(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower()).strip()


def _count_bad_chars(text: str) -> int:
    return sum(
        1 for ch in text if ch == "�" or (unicodedata.category(ch) == "Cc" and ch not in "\n\t")
    )


class PyMuPDFParser:
    name = "pymupdf"

    @property
    def version(self) -> str:
        return f"{pymupdf.__version__}+a{ALGORITHM_VERSION}"

    def parse(self, pdf_path: Path, out_dir: Path, config: ParsingConfig) -> ParsedDocument:
        asset_dir = out_dir / "assets"
        with pymupdf.open(pdf_path) as doc:
            pages, blocks_by_page, images_by_page = self._collect(doc)
            body_size = self._body_font_size(blocks_by_page)
            furniture = self._mark_furniture(pages, blocks_by_page)
            for blocks in blocks_by_page:
                for block in blocks:
                    self._classify(block, body_size)
            for page_info, blocks in zip(pages, blocks_by_page, strict=True):
                blocks[:] = _group_equations(blocks, body_size, page_info.width)
            assets: list[Asset] = []
            for page_idx, blocks in enumerate(blocks_by_page):
                page = doc[page_idx]
                assets += self._figures(page, page_idx + 1, blocks, images_by_page[page_idx])
                assets += self._tables(page, page_idx + 1, blocks)
                assets += self._equations(page_idx + 1, blocks)
            ordered: list[Block] = []
            for page_info, blocks in zip(pages, blocks_by_page, strict=True):
                page_order, columns = self._reading_order(blocks, page_info.width)
                page_info.column_count = columns
                ordered += page_order
            self._render_assets(doc, assets, asset_dir, config.figure_dpi)
        self._line_stats(pages, ordered)
        sections = self._build_sections(ordered, body_size, assets)
        return ParsedDocument(
            parser=self.name,
            parser_version=self.version,
            page_count=len(pages),
            pages=pages,
            sections=sections,
            assets=assets,
            furniture=sorted(furniture),
        )

    # ---- collection -------------------------------------------------------

    def _collect(
        self, doc: pymupdf.Document
    ) -> tuple[list[PageInfo], list[list[Block]], list[list[BBox]]]:
        pages: list[PageInfo] = []
        all_blocks: list[list[Block]] = []
        all_images: list[list[BBox]] = []
        flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
        for idx in range(doc.page_count):
            page = doc[idx]
            data = page.get_text("dict", flags=flags)
            blocks: list[Block] = []
            raw_text: list[str] = []
            for b in data["blocks"]:
                if b.get("type") != 0:
                    continue
                lines: list[Line] = []
                for ln in b["lines"]:
                    if not any(s["text"].strip() for s in ln["spans"]):
                        continue
                    raw_text.append("".join(s["text"] for s in ln["spans"]))
                    segments = [_make_line(seg) for seg in _segment_spans(ln["spans"])]
                    if abs(ln["dir"][1]) > 0.1:  # vertical margin stamps (e.g. arXiv id)
                        blocks += [Block(idx + 1, seg.bbox, [seg], kind="furniture") for seg in segments]
                        continue
                    lines += segments
                # PyMuPDF sometimes merges a heading with figure labels or the other column; regroup by x.
                for group in _column_groups(lines):
                    blocks.extend(self._split_leading_heading(idx + 1, group))
            blocks = _merge_section_numbers(blocks)
            images = [tuple(info["bbox"]) for info in page.get_image_info()]
            joined = "".join(raw_text)
            pages.append(
                PageInfo(
                    index=idx + 1,
                    label=page.get_label() or None,
                    width=page.rect.width,
                    height=page.rect.height,
                    char_count=len(joined.strip()),
                    image_count=len(images),
                    replacement_chars=_count_bad_chars(joined),
                )
            )
            all_blocks.append(blocks)
            all_images.append(images)
        return pages, all_blocks, all_images

    @staticmethod
    def _split_leading_heading(page: int, lines: list[Line]) -> list[Block]:
        """Split a block whose first line is styled differently (heading glued to its paragraph)."""
        if len(lines) >= 2:
            first, rest = lines[0], lines[1:]
            rest_size = sum(ln.size for ln in rest) / len(rest)
            rest_bold = sum(ln.bold for ln in rest) / len(rest) > 0.5
            styled = first.size > rest_size * 1.1 or (first.bold and not rest_bold)
            if styled and len(first.text.strip()) <= 100 and not RUN_IN_RE.search(first.text):
                return [
                    Block(page, first.bbox, [first]),
                    Block(page, _union([ln.bbox for ln in rest]), rest),
                ]
        return [Block(page, _union([ln.bbox for ln in lines]), lines)]

    @staticmethod
    def _body_font_size(blocks_by_page: list[list[Block]]) -> float:
        sizes: Counter[float] = Counter()
        for blocks in blocks_by_page:
            for block in blocks:
                for ln in block.lines:
                    sizes[round(ln.size * 2) / 2] += len(ln.text)
        return sizes.most_common(1)[0][0] if sizes else 10.0

    @staticmethod
    def _mark_furniture(pages: list[PageInfo], blocks_by_page: list[list[Block]]) -> set[str]:
        """Mark running headers/footers and bare page numbers in the top/bottom margins."""
        def in_margin(block: Block, page: PageInfo) -> bool:
            band = page.height * MARGIN_FRACTION
            return block.bbox[3] <= band or block.bbox[1] >= page.height - band

        seen: Counter[str] = Counter()
        for page, blocks in zip(pages, blocks_by_page, strict=True):
            keys = {_normalize_furniture(b.text) for b in blocks if in_margin(b, page)}
            seen.update(k for k in keys if k)
        # 30%: journals alternate running headers on odd/even pages (title vs. authors).
        min_pages = max(2, int(0.3 * len(pages)))
        repeated = {k for k, n in seen.items() if n >= min_pages}
        removed: set[str] = set()
        for page, blocks in zip(pages, blocks_by_page, strict=True):
            for block in blocks:
                if block.kind == "furniture" or not in_margin(block, page):
                    continue
                key = _normalize_furniture(block.text)
                if key in repeated or re.fullmatch(r"#|page #( of #)?|- # -", key):
                    block.kind = "furniture"
                    removed.add(block.text.strip())
        return removed

    # ---- classification ---------------------------------------------------

    @staticmethod
    def _classify(block: Block, body_size: float) -> None:
        if block.kind != "text":
            return
        text = block.text.strip()
        if is_caption(text, block.lines[0].bold):
            block.kind = "caption"
            return
        if (
            EQ_LABEL_RE.search(text)
            and len(text) < 250
            and sum(ch in MATH_CHARS for ch in text) >= 1
        ):
            block.kind = "equation"
            return
        if len(text) > 120 or len(block.lines) > 2:
            return
        if HEADING_WORDS_RE.match(text):
            block.kind = "heading"
            return
        letters = [ch for ch in text if ch.isalpha()]
        numbered_text = NUMBERED_HEADING_RE.match(text) or APPENDIX_HEADING_RE.match(text)
        # Small-caps headings ("2 ALGORITHM") only when numbered; bare caps are often table labels.
        upper_ratio = sum(ch.isupper() for ch in letters) / len(letters) if letters else 0.0
        all_caps = bool(numbered_text) and len(letters) >= 4 and upper_ratio > 0.8
        styled = block.size >= body_size * 1.12 or (block.bold and len(text) < 100) or all_caps
        if "=" in text or sum(ch in MATH_CHARS for ch in text) > 2:
            return
        ends_like_prose = text.endswith((".", ",", ";", "-")) and not NUMBERED_HEADING_RE.match(text)
        if not styled or ends_like_prose or RUN_IN_RE.search(text):
            return
        if NUMBERED_HEADING_RE.match(text) or APPENDIX_HEADING_RE.match(text) or (
            len(text.split()) <= 12 and text[:1].isupper()
        ):
            block.kind = "heading"
            block.meta["styled_only"] = not NUMBERED_HEADING_RE.match(text)

    # ---- reading order ----------------------------------------------------

    @staticmethod
    def _reading_order(blocks: list[Block], width: float) -> tuple[list[Block], int]:
        """Order blocks; on two-column pages read left column, then right, between full-width rows."""
        content = [b for b in blocks if b.kind != "furniture"]
        mid = width / 2
        tol = width * 0.04
        left = [b for b in content if b.bbox[2] <= mid + tol]
        right = [b for b in content if b.bbox[0] >= mid - tol]
        right = [b for b in right if b not in left]
        full = [b for b in content if b not in left and b not in right]
        left_chars = sum(len(b.text) for b in left)
        right_chars = sum(len(b.text) for b in right)
        two_col = len(left) >= 2 and len(right) >= 2 and min(left_chars, right_chars) >= 200
        if not two_col:
            return sorted(content, key=lambda b: (round(b.bbox[1], 1), b.bbox[0])), 1
        full.sort(key=lambda b: b.bbox[1])
        starts = [b.bbox[1] for b in full]
        regions: list[tuple[list[Block], list[Block]]] = [([], []) for _ in range(len(full) + 1)]
        for b in left:
            regions[bisect.bisect_right(starts, b.bbox[1])][0].append(b)
        for b in right:
            regions[bisect.bisect_right(starts, b.bbox[1])][1].append(b)
        ordered: list[Block] = []
        for i, (lcol, rcol) in enumerate(regions):
            ordered += sorted(lcol, key=lambda b: b.bbox[1])
            ordered += sorted(rcol, key=lambda b: b.bbox[1])
            if i < len(full):
                ordered.append(full[i])
        return ordered, 2

    @staticmethod
    def _line_stats(pages: list[PageInfo], ordered: list[Block]) -> None:
        for block in ordered:
            if block.kind not in ("text", "heading"):
                continue
            info = pages[block.page - 1]
            for ln in block.lines:
                info.line_count += 1
                stripped = ln.text.strip()
                if len(stripped) <= 3 and not stripped.isdigit():
                    info.short_line_count += 1

    # ---- assets -----------------------------------------------------------

    @staticmethod
    def _graphics(page: pymupdf.Page, images: list[BBox]) -> list[BBox]:
        area = page.rect.width * page.rect.height
        rects: list[BBox] = list(images)
        with contextlib.suppress(Exception):  # drawing clustering is best effort
            rects += [tuple(r) for r in page.cluster_drawings()]
        out = []
        for r in rects:
            w, h = r[2] - r[0], r[3] - r[1]
            if w < 20 or h < 20 or w * h > 0.9 * area:
                continue
            out.append(r)
        return out

    def _figures(
        self, page: pymupdf.Page, page_no: int, blocks: list[Block], images: list[BBox]
    ) -> list[Asset]:
        captions = [
            b for b in blocks
            if b.kind == "caption" and CAPTION_RE.match(b.text).group("kind").lower().startswith("fig")  # type: ignore[union-attr]
        ]
        if not captions:
            return []
        graphics = self._graphics(page, images)
        body = [b for b in blocks if b.kind == "text" and len(b.text) >= 80]
        assets = []
        for cap in captions:
            match = CAPTION_RE.match(cap.text)
            assert match is not None
            label = f"Figure {match.group('num')}"
            # Region above the caption, bounded by the nearest long body paragraph above it.
            above_limit = max(
                (b.bbox[3] for b in body if b.bbox[3] <= cap.bbox[1] + 2 and _h_overlap(b.bbox, cap.bbox) > 0),
                default=0.0,
            )
            chosen = [
                g for g in graphics
                if g[1] >= above_limit - 2 and g[3] <= cap.bbox[1] + 6 and _h_overlap(g, cap.bbox) > 0
            ]
            if not chosen:  # caption placed above the figure
                below_limit = min(
                    (b.bbox[1] for b in body if b.bbox[1] >= cap.bbox[3] - 2 and _h_overlap(b.bbox, cap.bbox) > 0),
                    default=page.rect.height,
                )
                chosen = [
                    g for g in graphics
                    if g[1] >= cap.bbox[3] - 6 and g[3] <= below_limit + 2 and _h_overlap(g, cap.bbox) > 0
                ]
            region = _union(chosen) if chosen else None
            if region is not None:
                for b in blocks:
                    if b.kind in ("text", "heading") and len(b.text) < 200 and _inside_fraction(b.bbox, region) > 0.8:
                        b.kind = "figure_text"
            assets.append(
                Asset(
                    kind="figure",
                    label=label,
                    caption=cap.text,
                    page=page_no,
                    bbox=region,
                    quality="uncertain" if region else "failed",
                )
            )
        return assets

    @staticmethod
    def _tables(page: pymupdf.Page, page_no: int, blocks: list[Block]) -> list[Asset]:
        captions = [
            b for b in blocks
            if b.kind == "caption" and CAPTION_RE.match(b.text).group("kind").lower().startswith("tab")  # type: ignore[union-attr]
        ]
        # Grid detection is slow and noisy on diagrams; only run it where a table caption exists.
        if not captions:
            return []
        try:
            found = page.find_tables().tables
        except Exception:  # noqa: BLE001 - table detection is best effort
            found = []
        assets = []
        used: set[int] = set()
        for table in found:
            bbox: BBox = tuple(table.bbox)
            try:
                cells = table.extract()
            except Exception:  # noqa: BLE001
                cells = []
            if len(cells) < 2:
                continue
            values = [c for row in cells for c in row]
            fill = sum(1 for c in values if c and str(c).strip()) / max(len(values), 1)
            best, best_dist = None, float("inf")
            for i, cap in enumerate(captions):
                if i in used or _h_overlap(cap.bbox, bbox) <= 0:
                    continue
                dist = min(abs(cap.bbox[3] - bbox[1]), abs(bbox[3] - cap.bbox[1]))
                if dist < best_dist:
                    best, best_dist = i, dist
            caption = captions[best] if best is not None and best_dist < 120 else None
            if caption is None and fill < MIN_UNCAPTIONED_TABLE_FILL:
                continue
            if caption is not None and best is not None:
                used.add(best)
            try:
                markdown = table.to_markdown()
            except Exception:  # noqa: BLE001
                markdown = ""
            label = None
            if caption is not None:
                m = CAPTION_RE.match(caption.text)
                label = f"Table {m.group('num')}" if m else None
            for b in blocks:
                if b.kind == "text" and _inside_fraction(b.bbox, bbox) > 0.8:
                    b.kind = "table_text"
            assets.append(
                Asset(
                    kind="table",
                    label=label,
                    caption=caption.text if caption else None,
                    page=page_no,
                    bbox=bbox,
                    data={"cells": cells, "markdown": markdown, "fill_ratio": round(fill, 2)},
                    quality="uncertain",
                )
            )
        # Table captions with no detected grid still get a caption-only asset.
        for i, cap in enumerate(captions):
            if i not in used:
                m = CAPTION_RE.match(cap.text)
                assets.append(
                    Asset(
                        kind="table",
                        label=f"Table {m.group('num')}" if m else None,
                        caption=cap.text,
                        page=page_no,
                        bbox=None,
                        quality="failed",
                    )
                )
        return assets

    @staticmethod
    def _equations(page_no: int, blocks: list[Block]) -> list[Asset]:
        out = []
        for b in blocks:
            if b.kind != "equation":
                continue
            m = EQ_LABEL_RE.search(b.text)
            label = f"Eq. {m.group(0).strip()}" if m else None
            data: dict[str, object] = {"raw_text": b.text, "fragments": b.meta.get("fragments", 1)}
            out.append(
                Asset(kind="equation", label=label, caption=b.text, page=page_no, bbox=b.bbox,
                      data=data, quality="uncertain")
            )
        return out

    @staticmethod
    def _render_assets(doc: pymupdf.Document, assets: list[Asset], asset_dir: Path, dpi: int) -> None:
        counters: Counter[tuple[str, int]] = Counter()
        for asset in assets:
            if asset.bbox is None:
                continue
            counters[(asset.kind, asset.page)] += 1
            n = counters[(asset.kind, asset.page)]
            page = doc[asset.page - 1]
            clip = pymupdf.Rect(asset.bbox) + (-4, -4, 4, 4)
            clip &= page.rect
            if clip.is_empty:
                continue
            asset_dir.mkdir(parents=True, exist_ok=True)
            name = f"{asset.kind}_p{asset.page:03d}_{n}.png"
            page.get_pixmap(clip=clip, dpi=dpi).save(asset_dir / name)
            asset.path = f"assets/{name}"

    # ---- sections ---------------------------------------------------------

    @staticmethod
    def _build_sections(ordered: list[Block], body_size: float, assets: list[Asset]) -> list[Section]:
        table_text = {
            a.caption: a.data.get("markdown") for a in assets
            if a.kind == "table" and a.caption and a.data
        }
        sections: list[Section] = [Section(heading=None, kind="front", level=1)]
        seen_heading = False
        # Styled lines on page 1 above the first structural heading are title/authors/affiliations.
        first_heading_y = min(
            (b.bbox[1] for b in ordered if b.page == 1 and b.kind == "heading"
             and (HEADING_WORDS_RE.match(b.text.strip()) or NUMBERED_HEADING_RE.match(b.text.strip()))),
            default=float("inf"),
        )
        for block in ordered:
            text = block.text.strip()
            if not text or block.kind in ("furniture", "figure_text", "table_text"):
                continue
            current = sections[-1]
            if block.kind == "heading":
                words = HEADING_WORDS_RE.match(text)
                numbered = NUMBERED_HEADING_RE.match(text)
                # Big-font lines on page 1 before any real heading are title/authors.
                above_first = block.page == 1 and block.bbox[1] < first_heading_y
                if block.page == 1 and (not seen_heading or above_first) and not words and not numbered:
                    current.paragraphs.append(Paragraph(text=text, page=block.page, bbox=block.bbox))
                    continue
                kind: SectionKind = "body"
                if words:
                    word = words.group("word").lower()
                    if word == "abstract":
                        kind = "abstract"
                    elif word in ("references", "bibliography"):
                        kind = "references"
                    elif word.startswith(("appendix", "appendices", "supplementary", "supporting")):
                        kind = "appendix"
                elif current.kind == "references":
                    # Inside references only a clearly styled numbered heading ends the list.
                    appendix_like = numbered or APPENDIX_HEADING_RE.match(text)
                    if not (appendix_like and (block.size >= body_size * 1.12 or block.bold)):
                        current.paragraphs.append(Paragraph(text=text, page=block.page, bbox=block.bbox))
                        continue
                    kind = "appendix"
                elif current.kind == "appendix":
                    kind = "appendix"
                letter = APPENDIX_HEADING_RE.match(text) if not (words or numbered) else None
                # Letter-numbered headings ("A Detailed ...", "B.2 ...") after digit-numbered sections
                # are appendices, even when they precede the reference list.
                uses_digit_sections = any(s.heading and NUMBERED_HEADING_RE.match(s.heading) for s in sections)
                if letter and uses_digit_sections:
                    kind = "appendix" if kind == "body" else kind
                num = (
                    (words.group("num") if words else None)
                    or (numbered.group("num") if numbered else None)
                    or (letter.group("num") if letter and kind == "appendix" else None)
                )
                level = num.count(".") + 1 if num else 1
                sections.append(Section(heading=text, kind=kind, level=level))
                seen_heading = True
                continue
            if block.kind == "text" and current.kind == "front" and INLINE_ABSTRACT_RE.match(text):
                body = INLINE_ABSTRACT_RE.sub("", text)
                sections.append(
                    Section(heading="Abstract", kind="abstract", level=1,
                            paragraphs=[Paragraph(text=body, page=block.page, bbox=block.bbox)])
                )
                seen_heading = True
                continue
            if block.kind == "caption":
                extra = table_text.get(text)
                content = f"{text}\n\n{extra}" if extra else text
                current.paragraphs.append(
                    Paragraph(text=content, kind="caption", page=block.page, bbox=block.bbox)
                )
                continue
            kind_p: ParagraphKind = "equation" if block.kind == "equation" else "text"
            current.paragraphs.append(
                Paragraph(text=text, kind=kind_p, page=block.page, bbox=block.bbox)
            )
        return [s for s in sections if s.paragraphs or s.heading]
