"""Read reference lists exported from other tools (Zotero, Elicit, Google Scholar, ...).

Supported: BibTeX (.bib), RIS (.ris), CSV (.csv, column names matched case-insensitively).
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from litforge.discovery.records import WorkRecord, normalize_arxiv_id

_LATEX_CMDS = re.compile(r"\\[a-zA-Z]+\s*|[{}]")


def _clean_tex(value: str) -> str:
    literals = {r"\textbraceleft{}": "\ue000", r"\textbraceright{}": "\ue001",
                r"\textbackslash{}": "\ue002", r"\textasciitilde{}": "~", r"\textasciicircum{}": "^"}
    for command, literal in literals.items():
        value = value.replace(command, literal)
    value = re.sub(r"\\([&%$#_])", r"\1", value)
    value = re.sub(r"\s+", " ", _LATEX_CMDS.sub("", value)).strip()
    return value.replace("\ue000", "{").replace("\ue001", "}").replace("\ue002", "\\")


def _year(value: str | None) -> int | None:
    match = re.search(r"(1[6-9]|20|21)\d{2}", value or "")
    return int(match.group(0)) if match else None


def _bib_entries(text: str) -> list[tuple[str, str, dict[str, str]]]:
    """Minimal BibTeX reader: @type{key, field = {..} | ".." | bare, ...} with nested braces."""
    entries = []
    pos = 0
    while (start := text.find("@", pos)) != -1:
        head = re.match(r"@(\w+)\s*[{(]", text[start:])
        if not head:
            pos = start + 1
            continue
        kind = head.group(1).lower()
        i = start + head.end()
        depth, j = 1, i
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        body = text[i : j - 1]
        pos = j
        if kind in ("comment", "string", "preamble"):
            continue
        key, _, rest = body.partition(",")
        fields: dict[str, str] = {}
        k = 0
        while k < len(rest):
            m = re.match(r"\s*([\w\-]+)\s*=\s*", rest[k:])
            if not m:
                break
            name = m.group(1).lower()
            k += m.end()
            if k < len(rest) and rest[k] in "{\"":
                close = "}" if rest[k] == "{" else '"'
                depth, v = 1, k + 1
                while v < len(rest) and depth:
                    if rest[v] == "{" and close == "}":
                        depth += 1
                    elif rest[v] == close:
                        depth -= 1
                    v += 1
                value = rest[k + 1 : v - 1]
                k = v
            else:
                m2 = re.match(r"([^,]*)", rest[k:])
                value = m2.group(1) if m2 else ""
                k += len(value)
            fields[name] = _clean_tex(value)
            k += len(re.match(r"\s*,?", rest[k:]).group(0))  # type: ignore[union-attr]
        entries.append((kind, key.strip(), fields))
    return entries


def parse_bibtex(text: str, source: str) -> list[WorkRecord]:
    out = []
    for _kind, key, f in _bib_entries(text):
        arxiv = f.get("eprint") if f.get("archiveprefix", "").lower() == "arxiv" else None
        arxiv = arxiv or normalize_arxiv_id(f.get("url", "")) or normalize_arxiv_id(f.get("journal", ""))
        out.append(WorkRecord(
            source=source, source_id=key, title=f.get("title"),
            authors=[a.strip() for a in f.get("author", "").split(" and ") if a.strip()],
            year=_year(f.get("year")), venue=f.get("journal") or f.get("booktitle"),
            doi=f.get("doi"), arxiv_id=arxiv, abstract=f.get("abstract"),
        ).normalized())
    return out


def parse_ris(text: str, source: str) -> list[WorkRecord]:
    out: list[WorkRecord] = []
    current: dict[str, list[str]] = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])  - ?(.*)$", line)
        if not m:
            continue
        tag, value = m.group(1), m.group(2).strip()
        if tag == "ER":
            def first(*tags: str, fields: dict[str, list[str]] = current) -> str | None:
                return next((fields[t][0] for t in tags if fields.get(t)), None)

            out.append(WorkRecord(
                source=source, source_id=first("ID", "DO") or str(len(out) + 1),
                title=first("TI", "T1"), authors=current.get("AU", []) + current.get("A1", []),
                year=_year(first("PY", "Y1", "DA")), venue=first("JO", "T2", "JF"),
                doi=first("DO"), abstract=first("AB", "N2"),
                arxiv_id=normalize_arxiv_id(first("UR") or ""),
            ).normalized())
            current = {}
        else:
            current.setdefault(tag, []).append(value)
    return out


_CSV_COLUMNS = {
    "title": ("title", "paper title", "article title"),
    "doi": ("doi",),
    "year": ("year", "publication year", "publication_year", "published date", "date"),
    "authors": ("authors", "author", "author names"),
    "abstract": ("abstract", "summary"),
    "venue": ("venue", "journal", "source", "source title", "publication"),
    "url": ("url", "link", "pdf url"),
    "arxiv": ("arxiv", "arxiv id", "arxiv_id"),
    "note": ("relevance", "notes", "note", "reason"),
}


def _split_authors(text: str) -> list[str]:
    """'A; B', 'A and B', or Elicit's 'Jessica Phillips-Silver, L. Trainor' (comma only when every
    part looks like a full name, so 'Cranmer, Kyle' stays one author)."""
    if ";" in text or " and " in text:
        parts = re.split(r";|\band\b", text)
    else:
        commas = [p.strip() for p in text.split(",")]
        parts = commas if len(commas) > 1 and all(" " in p for p in commas) else [text]
    return [p.strip() for p in parts if p.strip()]


def parse_csv(text: str, source: str) -> list[WorkRecord]:
    reader = csv.DictReader(io.StringIO(text))
    header = {h.strip().lower(): h for h in reader.fieldnames or []}
    cols = {key: next((header[a] for a in aliases if a in header), None) for key, aliases in _CSV_COLUMNS.items()}
    if cols["title"] is None and cols["doi"] is None:
        raise ValueError(f"CSV needs a 'title' or 'doi' column; found: {', '.join(header) or 'none'}")
    out = []
    for n, row in enumerate(reader, 1):
        def get(key: str, row: dict[str, str] = row) -> str | None:
            col = cols[key]
            value = (row.get(col) or "").strip() if col else ""
            return value or None

        authors = get("authors")
        out.append(WorkRecord(
            source=source, source_id=str(n), title=get("title"), doi=get("doi"), year=_year(get("year")),
            authors=_split_authors(authors) if authors else [],
            abstract=get("abstract"), venue=get("venue"), note=get("note"),
            arxiv_id=normalize_arxiv_id(get("arxiv") or get("url") or ""),
        ).normalized())
    return out


def read_references(path: Path) -> list[WorkRecord]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    source = f"import:{path.name}"
    suffix = path.suffix.lower()
    if suffix == ".bib":
        return parse_bibtex(text, source)
    if suffix == ".ris":
        return parse_ris(text, source)
    if suffix == ".csv":
        return parse_csv(text, source)
    raise ValueError(f"unsupported reference file '{path.suffix}' (use .bib, .ris or .csv)")
