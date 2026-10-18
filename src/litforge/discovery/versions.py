"""Detect, link and select distinct versions of the same scholarly work."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from litforge.discovery.http import HttpClient
from litforge.metadata.extract import DOI_RE, normalize_doi, normalize_title
from litforge.storage.db import now, transaction

VERSION_ORDER = {"published": 0, "accepted": 1, "submitted": 2, "unknown": 3}
ROLE_ORDER = {"published": 0, "accepted": 1, "preprint": 2, "revision": 3, "unknown": 4}
_HOSTS = (
    "arxiv.org", "biorxiv.org", "medrxiv.org", "ssrn.com", "researchsquare.com", "osf.io",
    "psyarxiv.com", "chemrxiv.org", "preprints.org",
)
# Statements a copy makes about itself. A citation on page 1 ("arXiv preprint arXiv:2101.00001") must not
# match, so arXiv counts only as its margin stamp and "preprint" only in self-describing phrases.
_TEXT_MARKERS = re.compile(
    r"arXiv:\d{4}\.\d{4,5}(?:v\d+)?\s+\[[\w.-]+\]"            # arXiv margin stamp: arXiv:2007.09114v2 [cs.LG]
    r"|\b(?:bio|med|chem|psy)Rxiv\s+preprint\b"
    r"|\bpreprint\s+(?:submitted|version|server|\(not)\b|\bthis\s+(?:is\s+a|article\s+is\s+a)\s+preprint\b"
    r"|not\s+(?:yet\s+)?peer[- ]reviewed|not\s+certified\s+by\s+peer\s+review", re.I,
)
_DOI_REVISION = re.compile(r"^(10\.\d{4,9}/.+)\.(\d+)$", re.I)


@dataclass(frozen=True)
class VersionCandidate:
    url: str
    version: str
    version_source: str
    direct: bool
    order: int


def infer_version(
    source_url: str | None,
    declared_version: str | None = None,
    declared_source: str | None = None,
    pdf_text: str | None = None,
) -> tuple[str, str]:
    """Resolve known registry, host and first-page signals in descending confidence."""
    versions = {
        "publishedversion": "published", "published": "published",
        "acceptedversion": "accepted", "accepted": "accepted",
        "submittedversion": "submitted", "submitted": "submitted",
    }
    if declared_version and declared_version.lower() in versions and declared_source == "user":
        return versions[declared_version.lower()], declared_source
    host = (source_url or "").lower().split("/", 3)[2] if "://" in (source_url or "") else ""
    if any(name in host for name in _HOSTS):
        return "submitted", "host"
    if (declared_version and declared_version.lower() in versions and
            declared_source in {"openalex", "unpaywall"}):
        return versions[declared_version.lower()], declared_source
    if pdf_text and _TEXT_MARKERS.search(pdf_text[:15000]):
        return "submitted", "pdf_text"
    return "unknown", "unknown"


def _version_name(raw: str | None) -> str:
    return {
        "publishedversion": "published", "published": "published",
        "acceptedversion": "accepted", "accepted": "accepted",
        "submittedversion": "submitted", "submitted": "submitted",
    }.get((raw or "").lower(), "unknown")


def _host_is_preprint(url: str) -> bool:
    host = url.lower().split("/", 3)[2] if "://" in url else ""
    return any(name in host for name in _HOSTS)


def _direct(url: str) -> bool:
    path = url.split("?", 1)[0].lower()
    return path.endswith(".pdf") or "arxiv.org/pdf/" in path


def rank_candidates(
    arxiv_id: str | None,
    oa_url: str | None,
    openalex_locations: Sequence[Mapping[str, Any]] = (),
    unpaywall_locations: Sequence[Mapping[str, Any]] = (),
) -> list[VersionCandidate]:
    """Rank OA candidates by version, retaining source order and preferring direct PDFs."""
    candidates: list[VersionCandidate] = []
    seen: set[str] = set()
    order = 0

    def append(url: str | None, raw_version: str | None, source: str) -> None:
        nonlocal order
        if not url or not url.startswith(("http://", "https://")) or url in seen:
            return
        if arxiv_id and "arxiv.org" in url and url != f"https://arxiv.org/pdf/{arxiv_id}":
            return
        seen.add(url)
        version = _version_name(raw_version)
        version_source = source if version != "unknown" else "unknown"
        if version == "unknown" and _host_is_preprint(url):
            version, version_source = "submitted", "host"
        candidates.append(VersionCandidate(url, version, version_source, _direct(url), order))
        order += 1

    # Retain the historic source preference inside a version: arXiv, repository copies,
    # the record's OA URL, then publisher copies.
    if arxiv_id:
        append(f"https://arxiv.org/pdf/{arxiv_id}", None, "host")
    for loc in openalex_locations:
        if loc.get("is_oa"):
            append(loc.get("pdf_url") or loc.get("landing_page_url"), loc.get("version"), "openalex")
    for loc in unpaywall_locations:
        append(loc.get("url_for_pdf") or loc.get("url"), loc.get("version"), "unpaywall")
    append(oa_url, None, "unknown")
    return sorted(candidates, key=lambda c: (VERSION_ORDER[c.version], not c.direct, c.order))


def _role_for_paper(conn: sqlite3.Connection, paper_id: str) -> str:
    row = conn.execute("SELECT doi, arxiv_id FROM papers WHERE id=?", (paper_id,)).fetchone()
    artifact = conn.execute(
        "SELECT version FROM artifacts WHERE paper_id=? AND status='ok' ORDER BY created_at DESC LIMIT 1",
        (paper_id,),
    ).fetchone()
    version = artifact["version"] if artifact else "unknown"
    if version == "published":
        return "published"
    if version == "accepted":
        return "accepted"
    if version == "submitted" or (row and row["arxiv_id"]):
        return "preprint"
    if (row and row["doi"] and (match := _DOI_REVISION.match(row["doi"])) and
            conn.execute("SELECT 1 FROM papers WHERE lower(doi)=lower(?)", (match.group(1),)).fetchone()):
        return "revision"
    if row and row["doi"] and row["doi"].lower().startswith(
        ("10.1101/", "10.48550/arxiv.", "10.2139/ssrn.", "10.21203/", "10.31219/")
    ):
        return "preprint"
    if row and row["doi"]:
        return "published"
    return "unknown"


def _group_for(conn: sqlite3.Connection, paper_id: str) -> str | None:
    row = conn.execute("SELECT group_id FROM paper_versions WHERE paper_id=? AND confirmed=1 LIMIT 1",
                       (paper_id,)).fetchone()
    return str(row["group_id"]) if row else None


def _join_pair(conn: sqlite3.Connection, a: str, b: str, evidence: str, *, confirmed: bool = True) -> bool:
    if a == b:
        return False
    ts = now()
    ga, gb = _group_for(conn, a), _group_for(conn, b)
    if ga and ga == gb:
        return False
    group_id = ga or gb or str(uuid.uuid4())
    if ga and gb and ga != gb:
        # Explicit identifiers can reconcile independently discovered groups.
        conn.execute("UPDATE paper_versions SET is_primary=0 WHERE group_id IN (?,?)", (ga, gb))
        conn.execute("UPDATE paper_versions SET group_id=? WHERE group_id=?", (group_id, gb))
    roles = {a: _role_for_paper(conn, a), b: _role_for_paper(conn, b)}
    for paper_id in (a, b):
        conn.execute(
            """INSERT INTO paper_versions(group_id,paper_id,role,evidence,confirmed,created_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT(group_id,paper_id) DO UPDATE SET
               role=excluded.role,evidence=excluded.evidence,confirmed=excluded.confirmed,rejected=0""",
            (group_id, paper_id, roles[paper_id], evidence, int(confirmed), ts),
        )
    return True


def link_explicit_versions(
    conn: sqlite3.Connection,
    crossref_relations: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> int:
    """Link only explicit Crossref DOI relations, DOI revisions and shared OpenAlex works."""
    linked = 0
    doi_ids = {normalize_doi(str(r["doi"])).lower(): str(r["id"]) for r in conn.execute(
        "SELECT id,doi FROM papers WHERE doi IS NOT NULL")}
    with transaction(conn):
        for doi, relations in (crossref_relations or {}).items():
            source_id = doi_ids.get(doi.lower())
            if not source_id:
                continue
            for rel in relations:
                if str(rel.get("id-type", "")).lower() != "doi" or rel.get("relation") not in {
                    "is-preprint-of", "has-preprint", "is-version-of", "has-version",
                }:
                    continue
                target_id = doi_ids.get(normalize_doi(str(rel.get("id", ""))).lower())
                if target_id:
                    linked += _join_pair(conn, source_id, target_id,
                                         f"crossref:{rel['relation']}", confirmed=True)
        # eLife-style version DOI suffixes have a shared, explicit base DOI.
        papers = conn.execute("SELECT id,doi FROM papers WHERE doi IS NOT NULL").fetchall()
        by_doi = {normalize_doi(str(r["doi"])).lower(): str(r["id"]) for r in papers}
        for row in papers:
            match = _DOI_REVISION.match(str(row["doi"]))
            if match and match.group(1).lower() in by_doi and match.group(1).lower() != normalize_doi(
                str(row["doi"])
            ).lower():
                linked += _join_pair(conn, str(row["id"]), by_doi[match.group(1).lower()],
                                     f"doi-revision:{match.group(1)}", confirmed=True)
        for rows in conn.execute(
            "SELECT openalex_id,GROUP_CONCAT(id) ids FROM papers WHERE openalex_id IS NOT NULL "
            "GROUP BY openalex_id HAVING COUNT(*)>1"
        ):
            ids = str(rows["ids"]).split(",")
            for other in ids[1:]:
                linked += _join_pair(conn, ids[0], other, f"openalex:{rows['openalex_id']}", confirmed=True)
        for rows in conn.execute(
            "SELECT arxiv_id,GROUP_CONCAT(id) ids FROM papers WHERE arxiv_id IS NOT NULL "
            "GROUP BY arxiv_id HAVING COUNT(*)>1"
        ):
            ids = str(rows["ids"]).split(",")
            for other in ids[1:]:
                linked += _join_pair(conn, ids[0], other, f"arxiv:{rows['arxiv_id']}", confirmed=True)
    return linked


def link_versions(conn: sqlite3.Connection, http: HttpClient, mailto: str | None = None) -> int:
    """Use cached Crossref relation lookups; unavailable APIs simply leave records unlinked."""
    relations: dict[str, list[Mapping[str, Any]]] = {}
    for row in conn.execute("SELECT doi FROM papers WHERE doi IS NOT NULL"):
        doi = str(row["doi"])
        try:
            item = http.get_json(http.url(f"https://api.crossref.org/works/{doi}", {"mailto": mailto}))
        except Exception:  # noqa: BLE001 - linking is explicitly offline-safe
            continue
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        raw_relations = message.get("relation")
        if not isinstance(raw_relations, dict):
            continue
        for relation, refs in raw_relations.items():
            if isinstance(refs, list):
                relations.setdefault(doi, []).extend(
                    {**ref, "relation": relation} for ref in refs if isinstance(ref, dict)
                )
    for row in conn.execute("SELECT doi FROM papers WHERE lower(doi) LIKE '10.1101/%'"):
        doi = str(row["doi"])
        for server in ("biorxiv", "medrxiv"):
            try:
                data = http.get_json(f"https://api.biorxiv.org/details/{server}/{doi}/na/json")
            except Exception:  # noqa: BLE001 - online links are optional
                continue
            if not isinstance(data, dict):
                continue
            collection = data.get("collection")
            item = next(iter(collection), {}) if isinstance(collection, list) else {}
            if not isinstance(item, dict):
                continue
            published = item.get("published")
            if published:
                relations.setdefault(doi, []).append(
                    {"id-type": "doi", "id": published, "relation": "is-preprint-of"})
                break
    return link_explicit_versions(conn, relations)


def suggest_similar_versions(conn: sqlite3.Connection) -> int:
    """Store title/author/year matches as unconfirmed suggestions, never as analysis links."""
    rows = conn.execute("SELECT id,title,authors_json,year FROM papers WHERE title IS NOT NULL").fetchall()
    count = 0
    with transaction(conn):
        for idx, a in enumerate(rows):
            title_a = normalize_title(a["title"])
            if len(title_a) < 20:
                continue
            authors_a = {t.lower() for t in re.findall(r"[a-z]{3,}", " ".join(json.loads(a["authors_json"] or "[]")))}
            for b in rows[idx + 1:]:
                title_b = normalize_title(b["title"])
                authors_b = {
                    t.lower() for t in re.findall(
                        r"[a-z]{3,}", " ".join(json.loads(b["authors_json"] or "[]")))
                }
                if SequenceMatcher(None, title_a, title_b).ratio() < 0.92:
                    continue
                if a["year"] and b["year"] and abs(a["year"] - b["year"]) > 1:
                    continue
                if authors_a and authors_b and len(authors_a & authors_b) / len(authors_a | authors_b) < 0.25:
                    continue
                evidence = "similarity:title-authors-year"
                existing = conn.execute(
                    """SELECT 1 FROM paper_versions x JOIN paper_versions y ON x.group_id=y.group_id
                       WHERE x.paper_id=? AND y.paper_id=? AND x.evidence=? AND x.rejected=1""",
                    (a["id"], b["id"], evidence),
                ).fetchone()
                if existing:
                    continue
                existing = conn.execute(
                    """SELECT 1 FROM paper_versions x JOIN paper_versions y ON x.group_id=y.group_id
                       WHERE x.paper_id=? AND y.paper_id=? AND x.evidence=?""",
                    (a["id"], b["id"], evidence),
                ).fetchone()
                group_a, group_b = _group_for(conn, str(a["id"])), _group_for(conn, str(b["id"]))
                if existing or group_a and group_a == group_b:
                    continue
                group_id = str(uuid.uuid4())
                for row in (a, b):
                    conn.execute(
                        "INSERT OR IGNORE INTO paper_versions(group_id,paper_id,role,evidence,confirmed,created_at)"
                        " VALUES (?,?,?,?,0,?)",
                        (group_id, row["id"], _role_for_paper(conn, row["id"]), evidence, now()),
                    )
                count += 1
    return count


def confirm_version_link(conn: sqlite3.Connection, a: str, b: str) -> bool:
    with transaction(conn):
        return _join_pair(conn, a, b, "user:confirmed", confirmed=True)


def reject_version_link(conn: sqlite3.Connection, a: str, b: str) -> bool:
    with transaction(conn):
        row = conn.execute(
            """SELECT x.group_id FROM paper_versions x JOIN paper_versions y ON x.group_id=y.group_id
               WHERE x.paper_id=? AND y.paper_id=? AND x.confirmed=0 AND y.confirmed=0
               AND x.rejected=0 AND y.rejected=0 LIMIT 1""", (a, b),
        ).fetchone()
        if not row:
            return False
        conn.execute("UPDATE paper_versions SET rejected=1 WHERE group_id=? AND confirmed=0",
                     (row["group_id"],))
        return True


def set_primary(conn: sqlite3.Connection, paper_id: str) -> None:
    row = conn.execute("SELECT group_id FROM paper_versions WHERE paper_id=? AND confirmed=1 LIMIT 1",
                       (paper_id,)).fetchone()
    with transaction(conn):
        group_id = str(row["group_id"]) if row else str(uuid.uuid4())
        if not row:
            conn.execute(
                "INSERT INTO paper_versions(group_id,paper_id,role,evidence,confirmed,created_at) "
                "VALUES (?,?,?,'user:standalone',1,?)",
                (group_id, paper_id, _role_for_paper(conn, paper_id), now()),
            )
        conn.execute("UPDATE paper_versions SET is_primary=0 WHERE group_id=?", (group_id,))
        conn.execute("UPDATE paper_versions SET is_primary=1 WHERE group_id=? AND paper_id=?",
                     (group_id, paper_id))


def primary_paper_ids(conn: sqlite3.Connection, project_id: str | None = None) -> set[str]:
    """Return project/library paper IDs with confirmed groups reduced to their primary member."""
    base_sql = (
        "SELECT paper_id FROM project_papers WHERE project_id=?" if project_id
        else "SELECT id AS paper_id FROM papers"
    )
    base = {str(r["paper_id"]) for r in conn.execute(base_sql, (project_id,) if project_id else ())}
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_versions'"
    ).fetchone():
        return base
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(
        """SELECT pv.group_id,pv.paper_id,pv.role,pv.is_primary,
                  EXISTS(SELECT 1 FROM artifacts a WHERE a.paper_id=pv.paper_id AND a.status='ok'
                         AND a.id=(SELECT current.id FROM artifacts current WHERE current.paper_id=pv.paper_id
                                   AND current.status='ok' ORDER BY current.created_at DESC LIMIT 1)
                         AND COALESCE(a.version_label,'') NOT LIKE 'this PDF is %') has_pdf
           FROM paper_versions pv WHERE pv.confirmed=1 AND pv.rejected=0"""
    ):
        if row["paper_id"] in base:
            grouped.setdefault(str(row["group_id"]), []).append(row)
    selected: set[str] = set()
    for members in grouped.values():
        explicit = next((r for r in members if r["is_primary"]), None)
        winner = explicit or min(members, key=lambda r: (
            not bool(r["has_pdf"]), ROLE_ORDER.get(r["role"], 4), str(r["paper_id"])))
        selected.add(str(winner["paper_id"]))
    grouped_ids = {str(r["paper_id"]) for members in grouped.values() for r in members}
    return (base - grouped_ids) | selected


def versions_for_paper(conn: sqlite3.Connection, paper_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT pv.paper_id,pv.role,pv.evidence,pv.confirmed,pv.is_primary,pv.rejected,
                  p.title,p.doi,a.version,a.version_source,a.version_label AS pdf_warning
           FROM paper_versions pv JOIN papers p ON p.id=pv.paper_id
           LEFT JOIN artifacts a ON a.id=(SELECT id FROM artifacts WHERE paper_id=p.id AND status='ok'
                                          ORDER BY created_at DESC LIMIT 1)
           WHERE pv.group_id IN (SELECT group_id FROM paper_versions WHERE paper_id=? AND confirmed=1)
                 AND pv.confirmed=1
           UNION ALL
           SELECT pv.paper_id,pv.role,pv.evidence,pv.confirmed,pv.is_primary,pv.rejected,
                  p.title,p.doi,NULL,NULL,NULL
           FROM paper_versions pv JOIN papers p ON p.id=pv.paper_id
           WHERE pv.group_id IN (SELECT group_id FROM paper_versions WHERE paper_id=?
                                 AND confirmed=0 AND rejected=0)
           ORDER BY confirmed DESC, role,paper_id""",
        (paper_id, paper_id),
    ).fetchall()
    return [dict(r) for r in rows]


def artifact_warning(conn: sqlite3.Connection, paper_id: str) -> str | None:
    """Return the warning label for a current PDF whose printed DOI identifies another record."""
    row = conn.execute(
        "SELECT version_label FROM artifacts WHERE paper_id=? AND status='ok' "
        "ORDER BY created_at DESC LIMIT 1", (paper_id,),
    ).fetchone()
    label = str(row["version_label"]) if row and row["version_label"] else ""
    return label if label.startswith("this PDF is ") else None


def backfill_unknown_versions(conn: sqlite3.Connection, paths: Any) -> int:
    """Inspect each paper's current unclassified PDF; never move its artifact between records."""
    import pymupdf

    rows = conn.execute(
        """SELECT a.id,a.paper_id,a.pdf_path,a.source_url
           FROM artifacts a
           WHERE a.status='ok' AND a.version='unknown'
             AND a.id=(SELECT current.id FROM artifacts current WHERE current.paper_id=a.paper_id
                       AND current.status='ok' ORDER BY current.created_at DESC LIMIT 1)"""
    ).fetchall()
    changed = 0
    for row in rows:
        path = paths.root / str(row["pdf_path"])
        with pymupdf.open(path) as pdf:
            first_page = pdf[0].get_text() if pdf.page_count else ""
        version, source = infer_version(None, pdf_text=first_page)
        warning = None
        match = DOI_RE.search(first_page)
        if match:
            doi = normalize_doi(match.group(1))
            other = conn.execute(
                "SELECT id FROM papers WHERE lower(doi)=lower(?) AND id<>? ORDER BY id LIMIT 1",
                (doi, row["paper_id"]),
            ).fetchone()
            if other:
                warning = f"this PDF is {role_for_paper(conn, str(other['id']))} {doi}"
        if version == "unknown" and warning is None:
            continue
        label = warning or format_version_label(version, row["source_url"])
        with transaction(conn):
            changed += conn.execute(
                "UPDATE artifacts SET version=?,version_source=?,version_label=? WHERE id=? AND version='unknown'",
                (version, source, label, row["id"]),
            ).rowcount
    return changed


def role_for_paper(conn: sqlite3.Connection, paper_id: str) -> str:
    row = conn.execute("SELECT role FROM paper_versions WHERE paper_id=? AND confirmed=1 LIMIT 1",
                       (paper_id,)).fetchone()
    return str(row["role"]) if row else _role_for_paper(conn, paper_id)


def set_artifact_version(
    conn: sqlite3.Connection, paper_id: str, version: str, *, source: str = "user",
) -> None:
    if version not in VERSION_ORDER or source not in {"openalex", "unpaywall", "host", "pdf_text", "user", "unknown"}:
        raise ValueError("invalid PDF version or source")
    with transaction(conn):
        changed = conn.execute(
            "UPDATE artifacts SET version=?,version_source=?,version_label=NULL WHERE id=(SELECT id FROM artifacts "
            "WHERE paper_id=? AND status='ok' ORDER BY created_at DESC LIMIT 1)",
            (version, source, paper_id),
        ).rowcount
        if not changed:
            raise LookupError(f"paper {paper_id} has no PDF")


def version_display(conn: sqlite3.Connection, paper_id: str) -> str | None:
    row = conn.execute(
        "SELECT version,version_label,source_url FROM artifacts WHERE paper_id=? AND status='ok' "
        "ORDER BY created_at DESC LIMIT 1", (paper_id,),
    ).fetchone()
    if not row:
        return None
    return format_version_label(row["version"], row["source_url"], row["version_label"])


def pdf_warning(conn: sqlite3.Connection, paper_id: str) -> str | None:
    """The warning on a paper's current PDF when it is another record's version (see backfill)."""
    row = conn.execute(
        "SELECT version_label FROM artifacts WHERE paper_id=? AND status='ok' ORDER BY created_at DESC LIMIT 1",
        (paper_id,),
    ).fetchone()
    label = row["version_label"] if row else None
    return label if label and label.startswith("this PDF is ") else None


def format_version_label(version: str, source_url: str | None, label: str | None = None) -> str | None:
    if label:
        return label
    if version == "submitted" and source_url:
        host = source_url.lower().split("/", 3)[2].removeprefix("www.")
        provider = next((name for name in _HOSTS if name in host), None)
        if provider:
            labels = {
                "arxiv.org": "arXiv", "biorxiv.org": "bioRxiv", "medrxiv.org": "medRxiv",
                "ssrn.com": "SSRN", "researchsquare.com": "Research Square", "osf.io": "OSF",
                "psyarxiv.com": "PsyArXiv", "chemrxiv.org": "chemRxiv", "preprints.org": "Preprints.org",
            }
            match = re.search(r"(?:v|%76)(\d+)(?:\.|/|$)", source_url, re.I)
            return labels.get(provider, provider) + " preprint" + (f", v{match[1]}" if match else "")
    return {"published": "published", "accepted": "accepted manuscript",
            "submitted": "preprint"}.get(version)


def source_url_from_metadata(metadata: object) -> str | None:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return None
    return str(metadata.get("source_url")) if isinstance(metadata, dict) and metadata.get("source_url") else None
