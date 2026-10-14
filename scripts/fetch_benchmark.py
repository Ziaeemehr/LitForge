"""Download a small open-access arXiv benchmark set into data/benchmark/pdfs (gitignored).

Usage: uv run python scripts/fetch_benchmark.py [--ids-file ids.txt] [--out data/benchmark]
The default list mixes single/two-column layouts, equation-heavy and table/figure-heavy papers,
leaning toward simulation-based inference and computational neuroscience.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import time
import urllib.request
from pathlib import Path

DEFAULT_IDS = {
    "1911.01429": "Cranmer et al., The frontier of simulation-based inference",
    "2007.09114": "Tejero-Cantero et al., sbi toolkit",
    "1905.07488": "Greenberg et al., Automatic posterior transformation",
    "1805.07226": "Papamakarios et al., Sequential neural likelihood",
    "1605.06376": "Papamakarios & Murray, epsilon-free inference",
    "1705.07057": "Papamakarios et al., Masked autoregressive flow",
    "1806.07366": "Chen et al., Neural ODEs",
    "1512.03385": "He et al., Deep residual learning (two-column, tables)",
    "1412.6980": "Kingma & Ba, Adam (equation-heavy)",
    "1706.03762": "Vaswani et al., Attention is all you need (tables, figures)",
}
USER_AGENT = "LitForge-benchmark/0.1 (personal research; mailto via git config)"


def fetch(arxiv_id: str, dest: Path) -> str:
    req = urllib.request.Request(f"https://arxiv.org/pdf/{arxiv_id}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https host
        data = resp.read()
    if not data.startswith(b"%PDF-"):
        raise ValueError("response is not a PDF")
    dest.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids-file", type=Path, help="one arXiv id per line (optional '# note')")
    parser.add_argument("--out", type=Path, default=Path("data/benchmark"))
    parser.add_argument("--delay", type=float, default=3.0, help="seconds between requests (arXiv policy)")
    args = parser.parse_args()

    ids = DEFAULT_IDS
    if args.ids_file:
        ids = {}
        for line in args.ids_file.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                aid, _, note = line.partition("#")
                ids[aid.strip()] = note.strip()
    pdf_dir = args.out / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, (aid, note) in enumerate(ids.items()):
        dest = pdf_dir / f"arxiv_{aid.replace('/', '_')}.pdf"
        if dest.exists():
            rows.append({"arxiv_id": aid, "note": note, "file": dest.name, "status": "exists",
                         "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()})
            continue
        if i:
            time.sleep(args.delay)
        try:
            sha = fetch(aid, dest)
            rows.append({"arxiv_id": aid, "note": note, "file": dest.name, "status": "downloaded", "sha256": sha})
            print(f"downloaded {aid}  {note}")
        except Exception as exc:  # noqa: BLE001
            rows.append({"arxiv_id": aid, "note": note, "file": "", "status": f"error: {exc}", "sha256": ""})
            print(f"FAILED {aid}: {exc}")
    with (args.out / "manifest.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["arxiv_id", "note", "file", "status", "sha256"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"{sum(r['status'] != 'exists' and not r['status'].startswith('error') for r in rows)} new, "
          f"manifest: {args.out / 'manifest.csv'}")


if __name__ == "__main__":
    main()
