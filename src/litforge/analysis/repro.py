"""Reproducibility checklist and inert, evidence-backed links; no remote lookups."""

from __future__ import annotations

import re
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from litforge.analysis.claims import paper_runs
from litforge.analysis.ledger import EvidenceSpan, get_spans, record_spans
from litforge.analysis.markdown_safe import excerpt, inert
from litforge.analysis.matrix import Matrix, MatrixColumn, create_matrix, list_matrices
from litforge.indexing.fts import SearchHit, current_runs

# These instructions go through the same extraction, validation, cache and review path as other matrices.
REPRO_COLUMNS = [MatrixColumn(key=key, label=label, instruction=instruction, type="text")
                 for key, label, instruction in [
    ("data_availability", "Data availability", "Report how data are available and any access restrictions."),
    ("dataset_names", "Dataset names", "Report the exact dataset names and identifiers needed to reproduce the study."),
    ("code_availability", "Code availability", "Report whether code is available and any access restrictions."),
    ("repository_urls", "Repository URLs", "Report code and data repository URLs verbatim; never visit them."),
    ("software_versions", "Software and versions", "Report software, packages and exact versions."),
    ("random_seeds", "Random seeds", "Report random seeds and how randomness is controlled."),
    ("preprocessing", "Preprocessing", "Report preprocessing steps, order and settings."),
    ("hyperparameters", "Hyperparameters", "Report hyperparameters and tuning choices."),
    ("hardware", "Hardware", "Report hardware, accelerator models and counts."),
    ("data_split", "Data split", "Report train, validation and test split sizes and method."),
    ("metrics", "Metrics", "Report evaluation metrics and definitions."),
    ("compute_budget", "Compute budget", "Report runtime or compute budget with units; do not estimate."),
]]


def ensure_repro_matrix(conn: sqlite3.Connection, project_id: str) -> Matrix:
    for matrix in list_matrices(conn, project_id):
        if matrix.name == "reproducibility":
            if matrix.columns != REPRO_COLUMNS:
                raise ValueError("the reproducibility matrix name is reserved for its built-in template")
            return matrix
    return create_matrix(conn, project_id, "reproducibility", "What is needed to reproduce this study?", REPRO_COLUMNS)


class ReproLink(BaseModel):
    paper_id: str
    url: str
    kind: Literal["code", "data", "other"]
    evidence_id: str


_URL = re.compile(r"https?://[^\s<>\"\x00-\x1f]+", re.I)
# Registrants that hold only data. Zenodo, Figshare and OSF also host papers and software, so a link there
# is recorded as "other": the link alone does not establish that it is a dataset.
_DATA_DOI = re.compile(r"^10\.(?:5061/dryad\.|18112/openneuro\.|13026/|7910/dvn/|5255/|6073/pasta/|15468/)", re.I)
_MIXED_DOI = re.compile(r"^10\.(?:5281/zenodo\.|6084/m9\.figshare\.|17605/osf\.io/)", re.I)
_CODE_HOSTS = {"github.com", "gitlab.com", "bitbucket.org"}
_DATA_HOSTS = {"kaggle.com", "datadryad.org", "physionet.org", "openneuro.org"}
_MIXED_HOSTS = {"zenodo.org", "figshare.com", "osf.io"}


def _link_kind(url: str) -> Literal["code", "data", "other"] | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
        if parts.username or parts.password or parts.port is not None or not parts.path.strip("/"):
            return None
    except ValueError:
        return None
    if host in _CODE_HOSTS:
        return "code"
    if any(host == h or host.endswith("." + h) for h in _DATA_HOSTS):
        return "data"
    if any(host == h or host.endswith("." + h) for h in _MIXED_HOSTS):
        return "other"
    if host == "huggingface.co":
        return "data" if parts.path.startswith("/datasets/") else "other"
    if host in ("doi.org", "dx.doi.org"):
        doi = parts.path.lstrip("/")
        return "data" if _DATA_DOI.match(doi) else "other" if _MIXED_DOI.match(doi) else None
    return None


def extract_links(conn: sqlite3.Connection, paper_id: str) -> list[ReproLink]:
    if not conn.execute("SELECT 1 FROM papers WHERE id=?", (paper_id,)).fetchone():
        raise KeyError(paper_id)
    runs = {r["id"] for r in current_runs(conn, None) if r["paper_id"] == paper_id}
    links: dict[tuple[str, str], ReproLink] = {}
    for row in conn.execute("SELECT c.*,p.title FROM chunks c JOIN papers p ON p.id=c.paper_id "
                            "WHERE c.paper_id=? ORDER BY c.parse_run_id,c.order_idx,c.id", (paper_id,)):
        if row["parse_run_id"] not in runs:
            continue
        found = []
        for match in _URL.finditer(row["text"]):
            url = match.group().rstrip(".,;:!?'\"]}")
            while url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1]
            kind = _link_kind(url)
            if kind:
                found.append((url, kind))
        if not found:
            continue
        hit = SearchHit(chunk_id=row["id"], paper_id=paper_id, title=row["title"], section=row["section_path"],
            kind=row["kind"], page=row["page_start"], page_start=row["page_start"], page_end=row["page_end"],
            score=0, snippet="")
        span = record_spans(conn, [hit])[0]
        for url, kind in found:
            link = ReproLink(paper_id=paper_id, url=url, kind=kind, evidence_id=span.id)
            conn.execute("INSERT OR IGNORE INTO repro_links VALUES (?,?,?,?)", (paper_id, url, kind, span.id))
            links[url, span.id] = link
    return [links[k] for k in sorted(links)]


class ChecklistItem(BaseModel):
    key: str
    label: str
    state: Literal["present", "missing", "unclear"]
    value: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class BriefItem(BaseModel):
    label: str
    value: str
    status: Literal["reported_fact", "interpretation", "unsupported", "proposal"]
    evidence_ids: list[str] = Field(default_factory=list)


class BriefSection(BaseModel):
    title: str
    items: list[BriefItem]


class ReproCard(BaseModel):
    paper_id: str
    title: str
    checklist: list[ChecklistItem]
    links: list[ReproLink]
    brief: str
    sections: list[BriefSection]
    evidence: list[EvidenceSpan]


def repro_card(conn: sqlite3.Connection, paper_id: str, project_id: str | None = None) -> ReproCard:
    row = conn.execute("SELECT title FROM papers WHERE id=?", (paper_id,)).fetchone()
    if row is None:
        raise KeyError(paper_id)
    if project_id is not None and not conn.execute(
            "SELECT 1 FROM project_papers WHERE project_id=? AND paper_id=?", (project_id, paper_id)).fetchone():
        raise ValueError("paper is outside project")
    # The paper-only API can use a unique matrix; conflicting project reviews require an explicit project.
    if project_id is None:
        candidates = [r[0] for r in conn.execute(
            "SELECT m.project_id FROM matrices m JOIN project_papers pp ON pp.project_id=m.project_id "
            "WHERE m.name='reproducibility' AND pp.paper_id=? ORDER BY m.project_id", (paper_id,))]
        if len(candidates) == 1:
            project_id = candidates[0]
    matrices = list_matrices(conn, project_id) if project_id else []
    matrix = next((m for m in matrices if m.name == "reproducibility"), None)
    runs = paper_runs(conn, project_id, [paper_id]) if project_id else {}
    parse_id = runs[paper_id]["id"] if paper_id in runs else None
    cells = {c.column_key: c for c in matrix.cells if c.paper_id == paper_id} if matrix else {}
    items = []
    for column in REPRO_COLUMNS:
        cell = cells.get(column.key)
        state: Literal["present", "missing", "unclear"] = "unclear"
        if cell and cell.parse_run_id == parse_id and cell.review_status != "rejected" and not cell.needs_review:
            if cell.status == "reported" and cell.value is not None and cell.evidence_ids:
                state = "present"
            elif cell.status in ("not_reported", "not_applicable"):
                state = "missing"
        items.append(ChecklistItem(key=column.key, label=column.label, state=state,
            value=str(cell.value) if state == "present" and cell else None,
            evidence_ids=sorted(cell.evidence_ids) if cell and state == "present" else []))
    links = extract_links(conn, paper_id)
    by_key = {i.key: i for i in items}
    data_keys = ("data_availability", "dataset_names", "data_split")

    def reported(item: ChecklistItem) -> BriefItem:
        return BriefItem(label=item.label, value=item.value or "", status="reported_fact",
                         evidence_ids=item.evidence_ids)

    data = [reported(by_key[key]) if by_key[key].state == "present" else BriefItem(
        label=by_key[key].label, value="not established from the current extraction.", status="unsupported")
        for key in data_keys]
    steps = [BriefItem(label="Ask the authors", value=f"clarify {item.label.lower()}.", status="proposal")
             if item.state != "present" else reported(item)
             for item in items if item.state != "present" or item.key not in data_keys]
    hardware, budget = by_key["hardware"], by_key["compute_budget"]
    estimate = BriefItem(label="Compute estimate", value="not reported", status="unsupported")
    if hardware.state == budget.state == "present" and re.search(
            r"\d\s*(?:hours?|hrs?|minutes?|mins?|seconds?|secs?|days?|[hms])\b", budget.value or "", re.I):
        estimate = BriefItem(label="Compute estimate",
            value=f"use the reported budget {budget.value or ''} on the reported hardware {hardware.value or ''}; "
                  "transfer to other hardware is unknown",
            status="interpretation", evidence_ids=sorted(set(hardware.evidence_ids + budget.evidence_ids)))
    sections = [BriefSection(title="Data needed", items=data),
                BriefSection(title="Steps and parameters to clarify", items=steps),
                BriefSection(title="Compute estimate", items=[estimate]),
                BriefSection(title="Links", items=[BriefItem(label=link.kind, value=link.url,
                    status="reported_fact", evidence_ids=[link.evidence_id]) for link in links])]
    # Markdown is an export representation of the same structured content shown in the UI.
    lines = [f"# Reproduction brief — {inert(row['title'] or paper_id)}", ""]
    for section in sections:
        lines.extend([f"## {section.title}", ""])
        for entry in section.items:
            label = "" if entry.status == "reported_fact" else f" [{entry.status}]"
            markers = "".join(f"[^{e}]" for e in entry.evidence_ids)
            lines.append(f"{inert(entry.label)}: {inert(entry.value)}{label}{markers}")
        lines.append("")
    ids = sorted({e for item in items for e in item.evidence_ids} | {link.evidence_id for link in links})
    evidence = get_spans(conn, ids)
    if evidence:
        lines.extend(["## Evidence appendix", ""])
        for span in evidence:
            lines.append(f"[^{span.id}]: {inert(span.citation)}; parse={span.parse_run_id}; "
                         f"{inert(excerpt(span.text))}")
    return ReproCard(paper_id=paper_id, title=row["title"] or paper_id, checklist=items, links=links,
                     brief="\n".join(lines) + "\n", sections=sections, evidence=evidence)
