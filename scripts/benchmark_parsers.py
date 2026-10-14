"""Time parsers and record quality metrics + routing per PDF into a CSV.

Usage: uv run python scripts/benchmark_parsers.py [--pdfs data/benchmark/pdfs] [--docling]
GPU columns (peak VRAM) stay empty on machines without nvidia-smi; fill them on the RTX 5070.
"""

from __future__ import annotations

import argparse
import csv
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from litforge.config import load_config
from litforge.parsing.base import Parser
from litforge.parsing.docling_parser import DoclingParser, docling_available
from litforge.parsing.pymupdf_parser import PyMuPDFParser
from litforge.parsing.quality import compute_quality
from litforge.parsing.router import decide


def gpu_memory_used_mb() -> str:
    if not shutil.which("nvidia-smi"):
        return ""
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=False).stdout.strip()
    return out.splitlines()[0] if out else ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdfs", type=Path, default=Path("data/benchmark/pdfs"))
    ap.add_argument("--out", type=Path, default=Path("data/benchmark/parser_benchmark.csv"))
    ap.add_argument("--docling", action="store_true", help="also benchmark Docling if installed")
    args = ap.parse_args()

    config = load_config()
    parsers: list[Parser] = [PyMuPDFParser()]
    if args.docling:
        if docling_available():
            parsers.append(DoclingParser())
        else:
            print("Docling not installed; skipping (uv sync --extra docling)")
    pdfs = sorted(args.pdfs.glob("*.pdf"))
    if not pdfs:
        raise SystemExit(f"no PDFs in {args.pdfs}; run scripts/fetch_benchmark.py first")

    rows = []
    for pdf in pdfs:
        for parser in parsers:
            with tempfile.TemporaryDirectory() as tmp:
                start = time.perf_counter()
                try:
                    doc = parser.parse(pdf, Path(tmp), config.parsing)
                except Exception as exc:  # noqa: BLE001
                    rows.append({"file": pdf.name, "parser": parser.name, "error": repr(exc)})
                    print(f"{pdf.name:<32} {parser.name:<8} ERROR {exc}")
                    continue
                ms = (time.perf_counter() - start) * 1000
            q = compute_quality(doc, config.parsing.router)
            d = decide(q, config.parsing.router)
            row = {
                "file": pdf.name, "parser": parser.name, "parser_version": parser.version,
                "machine": f"{platform.system()}-{platform.machine()}",
                "pages": q.page_count, "ms_total": round(ms), "ms_per_page": round(ms / max(q.page_count, 1), 1),
                "chars": q.total_chars, "sections": q.section_count, "has_abstract": q.has_abstract,
                "has_references": q.has_references, "two_column_pages": q.two_column_pages,
                "tables": q.table_count, "figures": q.figure_count,
                "figures_without_region": q.figures_without_region,
                "fragmented_line_ratio": q.fragmented_line_ratio,
                "replacement_char_ratio": q.replacement_char_ratio,
                "route": d.route, "reasons": "; ".join(d.reasons), "flags": "; ".join(d.flags),
                "gpu_mem_used_mb": gpu_memory_used_mb(), "error": "",
            }
            rows.append(row)
            print(f"{pdf.name:<32} {parser.name:<8} {row['pages']:>3}p {row['ms_per_page']:>7} ms/p "
                  f"sec={q.section_count:<3} tab={q.table_count:<2} fig={q.figure_count:<2} "
                  f"(no region {q.figures_without_region}) 2col={q.two_column_pages:<3} -> {d.route}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with args.out.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
