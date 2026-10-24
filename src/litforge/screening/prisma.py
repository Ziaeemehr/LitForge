"""Deterministic PRISMA 2020 accounting and a self-contained SVG report.

Records are distinct (method/source key, paper ID) among current project members.
Non-primary versions are duplicates. Sought includes all screened except S1 final
exclusions; current parse runs distinguish missing reports from awaiting S2.
"""
from __future__ import annotations

import sqlite3
import textwrap
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter

from pydantic import BaseModel

from litforge.analysis.claims import paper_runs
from litforge.screening.status import final_status


class Prisma(BaseModel):
    identified_by_source: dict[str, int]
    identified_other: dict[str, int]
    duplicates_removed: int
    awaiting_fulltext: int
    screened: int
    excluded_s1: int
    awaiting_screening: int
    sought: int
    not_retrieved: int
    assessed: int
    included_at_s1: int
    excluded_s2_by_reason: dict[str, int]
    included: int
    uncertain: int


def prisma_counts(conn: sqlite3.Connection, project_id: str) -> Prisma:
    statuses = final_status(conn, project_id)
    records: set[tuple[str, str]] = set()
    databases: Counter[str] = Counter()
    other: Counter[str] = Counter()
    hits = conn.execute("""SELECT h.*, r.kind FROM discovery_hits h
        JOIN discovery_runs r ON r.id=h.run_id
        JOIN project_papers pp ON pp.paper_id=h.paper_id AND pp.project_id=r.project_id
        WHERE r.project_id=?""", (project_id,))
    seen = set()
    for h in hits:
        relation, source, kind = h["relation"], h["source"], h["kind"]
        database = False
        if relation == "import" or source.startswith("import"):
            key, database = "import", True
        elif kind == "similar":
            key = "similar"
        elif kind in {"link", "links"} or relation == "link":
            key = "links"
        elif relation == "query":
            key, database = source, True
        elif kind == "citations" or relation in {"references", "cited_by", "seed"}:
            key = "citations"
        else:
            key = "similar" if relation in {"related", "keywords", "neighbours"} else kind
            key = {"pdfs": "own files"}.get(key, key.replace("_", " ").replace("-", " "))
        record = (key, h["paper_id"])
        seen.add(h["paper_id"])
        if record not in records:
            records.add(record)
            (databases if database else other)[key] += 1
    for row in conn.execute("SELECT paper_id,source FROM project_papers WHERE project_id=?", (project_id,)):
        if row["paper_id"] not in seen:
            records.add((row["source"], row["paper_id"]))
            other[row["source"]] += 1
    sought = {pid for pid, s in statuses.items() if s.stage != "none" and
              not (s.status == "excluded" and s.stage == "S1")}
    assessed = {pid for pid in sought if statuses[pid].stage == "S2"}
    included_at_s1 = {pid for pid in sought - assessed
                      if statuses[pid].status == "included" and statuses[pid].stage == "S1"}
    pending = sought - assessed - included_at_s1
    parsed = set(paper_runs(conn, project_id))
    return Prisma(
        identified_by_source=dict(sorted(databases.items())), identified_other=dict(sorted(other.items())),
        duplicates_removed=len(records) - len(statuses),
        screened=sum(s.stage != "none" for s in statuses.values()),
        awaiting_screening=sum(s.stage == "none" for s in statuses.values()),
        excluded_s1=sum(s.status == "excluded" and s.stage == "S1" for s in statuses.values()),
        sought=len(sought), assessed=len(assessed), not_retrieved=len(pending - parsed),
        included_at_s1=len(included_at_s1),
        awaiting_fulltext=len(pending & parsed),
        excluded_s2_by_reason=dict(sorted(Counter(s.reason for s in statuses.values()
                                                  if s.status == "excluded" and s.stage == "S2").items())),
        included=sum(s.status == "included" for s in statuses.values()),
        uncertain=sum(s.status == "uncertain" for s in statuses.values()))


def _svg_text(parent: ET.Element, text: str, left: int, right: int, y: int) -> None:
    # SVG has no direction="auto": use the first strong character, as HTML dir=auto does.
    first = next((unicodedata.bidirectional(c) for c in text
                  if unicodedata.bidirectional(c) in {"L", "R", "AL"}), "L")
    rtl = first in {"R", "AL"}
    ET.SubElement(parent, "text", x=str(right if rtl else left), y=str(y),
                  direction="rtl" if rtl else "ltr").text = text


def prisma_svg(p: Prisma, title: str) -> str:
    """Four rows, left-hand review flow and right-hand removals; all text is XML escaped."""
    groups = [
        ("Identification", ["Records identified"] +
         [f"{k}: {v}" for k, v in p.identified_by_source.items()] +
         ["Other methods"] + [f"{k}: {v}" for k, v in p.identified_other.items()],
         [f"Duplicates removed: {p.duplicates_removed}"]),
        ("Screening", [f"Records screened: {p.screened}", f"Awaiting screening: {p.awaiting_screening}"],
         [f"Excluded at S1: {p.excluded_s1}"]),
        ("Eligibility / Retrieval", [f"Reports sought: {p.sought}", f"Reports assessed: {p.assessed}",
                                     f"Awaiting full text: {p.awaiting_fulltext}"],
         [f"Included at S1 without full-text review: {p.included_at_s1}",
          f"Not retrieved: {p.not_retrieved}", "Excluded at S2: " + str(sum(p.excluded_s2_by_reason.values()))] +
         [f"{k}: {v}" for k, v in p.excluded_s2_by_reason.items()]),
        ("Included", [f"Included: {p.included}"], [f"Uncertain (all stages): {p.uncertain}"]),
    ]
    svg = ET.Element("svg", {"xmlns": "http://www.w3.org/2000/svg",
                             "font-family": "Arial, sans-serif", "font-size": "14"})
    ET.SubElement(svg, "title").text = title
    _svg_text(svg, title, 20, 1100, 26)
    y = 55
    for index, (label, left, right) in enumerate(groups):
        lines = [[part for text in side for part in (textwrap.wrap(text, 40) or [""])] for side in (left, right)]
        height = max(len(side) for side in lines) * 20 + 24
        ET.SubElement(svg, "text", x="20", y=str(y + 18)).text = label
        for x, side in zip((205, 675), lines, strict=True):
            ET.SubElement(svg, "rect", x=str(x), y=str(y), width="425", height=str(height),
                          fill="#f6f8fa", stroke="#455568", rx="6")
            for n, line in enumerate(side):
                _svg_text(svg, line, x + 12, x + 413, y + 24 + 20 * n)
        ET.SubElement(svg, "path", d=f"M630 {y + 20} h45 m-7 -4 l7 4 -7 4", fill="none", stroke="#455568")
        if index < 3:
            bottom = y + height
            ET.SubElement(svg, "path", d=f"M415 {bottom} v30 m-4 -7 l4 7 4 -7", fill="none", stroke="#455568")
        y += height + 32
    svg.set("viewBox", f"0 0 1120 {y}")
    svg.set("width", "1120")
    svg.set("height", str(y))
    return ET.tostring(svg, encoding="unicode")
