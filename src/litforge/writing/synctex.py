"""SyncTeX lookups: source line → place in the PDF (forward) and place in the PDF → source line (inverse).

Reads the `main.synctex.gz` Tectonic or latexmk writes next to the PDF. Positions come back in PDF points from the
page's top-left corner, which is what the viewer draws with. TeX tags whole line boxes with the line where the
paragraph ended, but the small records inside a line (kerns, glue, math) carry the line each word came from, so
lookups work from those and use the enclosing line box only to draw the highlight."""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

SP_PER_BP = 65781.76  # TeX scaled points per PDF point
MAX_BYTES = 50 * 1024 * 1024  # an uncompressed SyncTeX file larger than this is not read

_RECORD = re.compile(r"([\[(hvkgx$r])(\d+),(\d+)(?:,-?\d+)?:(-?\d+),(-?\d+)(?::(-?\d+),(-?\d+),(-?\d+))?")
_LEAF = set("hvkgx$r")


class SyncTexError(ValueError):
    """The SyncTeX file is missing, too large, or unreadable (for example still being written)."""


@dataclass(frozen=True)
class Record:
    kind: str
    tag: int
    line: int
    page: int
    x: float
    y: float  # baseline
    w: float = 0.0
    h: float = 0.0  # height above the baseline
    d: float = 0.0  # depth below it


@dataclass
class SyncTex:
    files: dict[int, str] = field(default_factory=dict)  # input tag -> file name inside the document folder
    records: list[Record] = field(default_factory=list)


def _read(path: Path, max_bytes: int) -> bytes:
    """The file's text, decompressed in a bounded read so a small archive cannot expand without limit."""
    try:
        with path.open("rb") as raw:
            gzipped = raw.read(2) == b"\x1f\x8b"
        opener: Any = gzip.open if gzipped else open
        with opener(path, "rb") as stream:
            data = stream.read(max_bytes + 1)
    except (OSError, EOFError) as exc:  # truncated (still being written) or not gzip after all
        raise SyncTexError(f"the position data cannot be read: {exc}") from None
    if len(data) > max_bytes:
        raise SyncTexError("the SyncTeX file is too large")
    return bytes(data)


def load(path: Path, folder: Path, max_bytes: int = MAX_BYTES) -> SyncTex:
    """Parse a (gzipped) SyncTeX file. Only inputs inside `folder` are kept; package files are ignored."""
    text = _read(path, max_bytes)
    sync = SyncTex()
    unit, magnification, x_offset, y_offset, page = 1.0, 1000.0, 0.0, 0.0, 0
    root = PurePosixPath(str(folder))
    for line in text.decode("utf-8", "replace").splitlines():
        if line.startswith("Input:"):
            tag, _, name = line[6:].partition(":")
            relative = _inside(root, name)
            if tag.isdigit() and relative:
                sync.files[int(tag)] = relative
        elif line.startswith("Unit:"):
            unit = _number(line[5:], 1.0)
        elif line.startswith("Magnification:"):
            magnification = _number(line[14:], 1000.0) or 1000.0
        elif line.startswith("X Offset:"):
            x_offset = _number(line[9:], 0.0)
        elif line.startswith("Y Offset:"):
            y_offset = _number(line[9:], 0.0)
        elif line.startswith("{") and line[1:].isdigit():
            page = int(line[1:])
        elif page and (match := _RECORD.match(line)):
            kind, input_tag, number = match.group(1), int(match.group(2)), int(match.group(3))
            if input_tag not in sync.files:
                continue
            scale = unit * magnification / 1000 / SP_PER_BP  # as synctex_parser.c converts to PDF points
            x, y, *size = [int(v) * scale if v is not None else 0.0 for v in match.groups()[3:]]
            sync.records.append(Record(kind, input_tag, number, page, x + x_offset * unit / SP_PER_BP,
                                       y + y_offset * unit / SP_PER_BP, *size))
    return sync


def _number(text: str, default: float) -> float:
    try:
        return float(text.strip())
    except ValueError:
        return default


def _inside(root: PurePosixPath, name: str) -> str | None:
    if not name:
        return None
    path = PurePosixPath(name)
    if ".." in path.parts:
        return None
    if not path.is_absolute():  # ./main.tex or sub/part.tex: relative to the folder the engine ran in
        path = root / path
    try:
        relative = path.relative_to(root)
    except ValueError:
        return None
    # Files in hidden folders (.build/main.bbl) are TeX's own output, which the editor does not open.
    return None if any(part.startswith(".") for part in relative.parts) else str(relative)


def forward(sync: SyncTex, file: str, line: int) -> dict[str, Any] | None:
    """The page and the line boxes that show `line` of `file`; the nearest line with output when it has none."""
    tags = {tag for tag, name in sync.files.items() if name == file}
    candidates = [r for r in sync.records if r.tag in tags]
    if not candidates:
        return None
    lines = sorted({r.line for r in candidates})
    later = [n for n in lines if n >= line]
    target = later[0] if later else lines[-1]
    hits = [r for r in candidates if r.line == target and r.kind in _LEAF] or \
        [r for r in candidates if r.line == target]
    page = hits[0].page
    boxes: list[dict[str, float]] = []
    for baseline in sorted({round(r.y, 1) for r in hits if r.page == page}):
        on_line = [r for r in hits if r.page == page and round(r.y, 1) == baseline]
        box = _line_box(sync, page, on_line)
        if box not in boxes:
            boxes.append(box)
    # Parts of a line on their own baselines (sub- and superscripts in math) would draw small boxes on top of
    # the line's own highlight: keep only boxes not inside another.
    boxes = [b for b in boxes if not any(o is not b and _within(b, o) for o in boxes)]
    return {"page": page, "line": target, "boxes": boxes}


def _within(inner: dict[str, float], outer: dict[str, float], slack: float = 1.0) -> bool:
    return (outer["x"] - slack <= inner["x"] and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + slack
            and outer["y"] - slack <= inner["y"] and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + slack)


def _line_box(sync: SyncTex, page: int, on_line: list[Record]) -> dict[str, float]:
    """The widest line box with this baseline that contains the records, else a box around the records."""
    y = on_line[0].y
    left, right = min(r.x for r in on_line), max(r.x + r.w for r in on_line)
    boxes = [r for r in sync.records if r.kind == "(" and r.page == page and abs(r.y - y) < 0.5 and r.w > 0
             and r.x - 0.5 <= left and right <= r.x + r.w + 0.5]
    if boxes:
        best = max(boxes, key=lambda r: r.w)
        return _rect(best.x, best.y - best.h, best.w, best.h + best.d)
    return _rect(left, y - 8, max(right - left, 4), 10)


def _rect(x: float, y: float, w: float, h: float) -> dict[str, float]:
    return {"x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(max(h, 2), 2)}


def inverse(sync: SyncTex, page: int, x: float, y: float) -> dict[str, Any] | None:
    """The source file and line of the text at (x, y) on `page` (PDF points from the top-left)."""
    on_page = [r for r in sync.records if r.page == page]
    leaves = [r for r in on_page if r.kind in _LEAF]
    if not leaves:
        return None
    lines = [r for r in on_page if r.kind == "(" and r.w > 0 and r.y - r.h - 1 <= y <= r.y + r.d + 1]
    inside = [r for r in lines if r.x <= x <= r.x + r.w]
    if inside or lines:  # the line under the point (or, in a margin, the line at that height)
        box = min(inside, key=lambda r: r.w * (r.h + r.d)) if inside else \
            min(lines, key=lambda r: min(abs(x - r.x), abs(x - r.x - r.w)))
        same = [r for r in leaves if abs(r.y - box.y) < 0.5 and box.x - 0.5 <= r.x <= box.x + box.w + 0.5]
        if same:
            before = [r for r in same if r.x <= x]
            hit = max(before, key=lambda r: r.x) if before else min(same, key=lambda r: r.x)
            return {"file": sync.files[hit.tag], "line": hit.line}
    hit = min(leaves, key=lambda r: abs(r.y - y) * 4 + abs(r.x - x))
    return {"file": sync.files[hit.tag], "line": hit.line}
