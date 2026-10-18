"""Stable citation keys and BibTeX entries built from library metadata."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from litforge.storage.db import now, transaction

KEY_RE = re.compile(r"^[a-z0-9:_-]+$")
_ENTRY_KEY = re.compile(r"@\w+\s*\{\s*([^,\s]+)\s*,")
BEGIN_MARK = "% litforge:begin managed entries (rewritten on each cite; edit outside this block)"
END_MARK = "% litforge:end managed entries"
_STOPWORDS = {
    "a", "an", "the", "of", "on", "in", "for", "and", "to", "with", "from", "by", "at", "as", "is", "are",
    "towards", "toward", "via", "using", "into", "about", "its", "their",
}
_SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
            "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "{": r"\{", "}": r"\}"}


class CitationKeyError(ValueError):
    pass


def _ascii(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.lower())


def _authors(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    return [str(a).strip() for a in value if str(a).strip()] if isinstance(value, list) else []


def _last_name(author: str) -> str:
    if "," in author:
        return author.split(",", 1)[0].strip()
    parts = author.split()
    return parts[-1] if parts else ""


def _base_key(row: sqlite3.Row) -> str:
    authors = _authors(row["authors_json"])
    who = _ascii(_last_name(authors[0])) if authors else ""
    if not who:  # no Latin form of the name (e.g. Persian): fall back to the paper id
        who = "p" + _ascii(row["id"])[:8]
    year = str(row["year"]) if row["year"] else "nd"
    words = [_ascii(w) for w in re.split(r"[\s\-–:/]+", row["title"] or "")]
    word = next((w for w in words if w and w not in _STOPWORDS), "")
    return f"{who}{year}{word}"


def citation_key(conn: sqlite3.Connection, paper_id: str) -> str:
    """The paper's citation key, created once (firstauthorYEARword, a/b/... on collision) and then kept."""
    if found := conn.execute("SELECT key FROM citation_keys WHERE paper_id=?", (paper_id,)).fetchone():
        return str(found["key"])
    row = conn.execute("SELECT id, title, year, authors_json FROM papers WHERE id=?", (paper_id,)).fetchone()
    if row is None:
        raise LookupError(f"paper {paper_id} not found")
    base = _base_key(row)
    with transaction(conn, immediate=True):
        if found := conn.execute("SELECT key FROM citation_keys WHERE paper_id=?", (paper_id,)).fetchone():
            return str(found["key"])
        taken = {str(r[0]) for r in conn.execute(
            "SELECT key FROM citation_keys WHERE key=? OR key GLOB ?", (base, base + "[a-z]*"))}
        key = base
        suffix = 0
        while key in taken:
            key = base + _suffix(suffix)
            suffix += 1
        conn.execute("INSERT INTO citation_keys(paper_id, key, user_set, created_at) VALUES (?,?,0,?)",
                     (paper_id, key, now()))
    return key


def _suffix(n: int) -> str:
    out = ""
    n += 1
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(97 + rem) + out
    return out


def set_citation_key(conn: sqlite3.Connection, paper_id: str, key: str) -> str:
    key = key.strip()
    if not KEY_RE.fullmatch(key) or len(key) > 80:
        raise CitationKeyError("a citation key uses only a-z, 0-9, ':', '_' and '-'")
    if conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone() is None:
        raise LookupError(f"paper {paper_id} not found")
    with transaction(conn, immediate=True):
        other = conn.execute("SELECT paper_id FROM citation_keys WHERE key=?", (key,)).fetchone()
        if other and other["paper_id"] != paper_id:
            raise CitationKeyError(f"citation key '{key}' is already used by another paper")
        conn.execute(
            """INSERT INTO citation_keys(paper_id, key, user_set, created_at) VALUES (?,?,1,?)
               ON CONFLICT(paper_id) DO UPDATE SET key=excluded.key, user_set=1""", (paper_id, key, now()))
    return key


def paper_for_key(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT paper_id FROM citation_keys WHERE key=?", (key,)).fetchone()
    return str(row["paper_id"]) if row else None


def escape(text: str) -> str:
    """Escape BibTeX/LaTeX special characters so the value is always brace-balanced."""
    return "".join(_SPECIAL.get(ch, ch) for ch in re.sub(r"\s+", " ", text).strip())


def _title(text: str) -> str:
    # Protect words whose capitals must survive a lower-casing bibliography style ("DNA", "fMRI", "Bayes").
    out = []
    for i, word in enumerate(escape(text).split(" ")):
        letters = re.sub(r"\\[a-z]+\{\}|\\.", "", word)
        caps = any(c.isupper() for c in (letters[1:] if i == 0 else letters))
        out.append("{" + word + "}" if caps else word)
    return " ".join(out)


def _author_field(authors: list[str]) -> str:
    names = []
    for author in authors:
        if "," in author:
            names.append(escape(author))
            continue
        parts = author.split()
        names.append(escape(f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else author))
    return " and ".join(names)


def bibtex_entry(conn: sqlite3.Connection, paper_id: str) -> str:
    from litforge.discovery.versions import _role_for_paper

    row = conn.execute(
        "SELECT id, title, year, authors_json, venue, doi, arxiv_id, oa_url FROM papers WHERE id=?",
        (paper_id,)).fetchone()
    if row is None:
        raise LookupError(f"paper {paper_id} not found")
    key = citation_key(conn, paper_id)
    preprint = _role_for_paper(conn, paper_id) == "preprint"
    kind = "article" if row["venue"] and not preprint else "misc"
    fields: list[tuple[str, str]] = []
    if authors := _authors(row["authors_json"]):
        fields.append(("author", _author_field(authors)))
    fields.append(("title", _title(row["title"] or "Untitled")))
    if row["venue"]:
        fields.append(("journal" if kind == "article" else "howpublished", escape(row["venue"])))
    if row["year"]:
        fields.append(("year", str(row["year"])))
    if row["doi"]:
        fields.append(("doi", escape(row["doi"])))
    if row["arxiv_id"]:
        fields.append(("eprint", escape(row["arxiv_id"])))
        fields.append(("archiveprefix", "arXiv"))
    if row["oa_url"]:  # URLs stay verbatim (the url package reads them raw); braces would unbalance
        fields.append(("url", re.sub(r"[{}\\\s]", "", row["oa_url"])))
    if preprint:
        fields.append(("note", "Preprint"))
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields)
    return f"@{kind}{{{key},\n{body}\n}}\n"


def write_bib(conn: sqlite3.Connection, paper_ids: Iterable[str], path: Path) -> list[str]:
    """Rewrite the LitForge-managed block of `path` with these papers' entries; text outside the markers
    (the user's own entries) is kept as it is. Returns the keys written."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    before, after = existing, ""
    if BEGIN_MARK in existing and END_MARK in existing.split(BEGIN_MARK, 1)[1]:
        before, rest = existing.split(BEGIN_MARK, 1)
        after = rest.split(END_MARK, 1)[1]
    own = {m.group(1).strip() for m in _ENTRY_KEY.finditer(before + after)}  # the user's entries win
    ids = [pid for pid in dict.fromkeys(paper_ids) if citation_key(conn, pid) not in own]
    entries = [bibtex_entry(conn, pid) for pid in ids]
    keys = [citation_key(conn, pid) for pid in ids]
    block = BEGIN_MARK + "\n" + "\n".join(entries) + END_MARK
    text = before.rstrip("\n") + ("\n\n" if before.strip() else "") + block + "\n" + after.lstrip("\n")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return keys


def export_bibtex(conn: sqlite3.Connection, paper_ids: Iterable[str]) -> str:
    return "\n".join(bibtex_entry(conn, pid) for pid in paper_ids)
