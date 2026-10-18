"""Detect, link and select distinct versions of the same scholarly work."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from litforge.discovery.http import HttpClient
from litforge.indexing.stopwords import STOPWORDS
from litforge.metadata.extract import DOI_RE, extract_metadata, normalize_doi, normalize_title
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
    r"|\bpreprint\s+(?:submitted|server|\(not)\b|\bthis\s+(?:is\s+a|article\s+is\s+a)\s+preprint\b"
    # Only the copy speaking of itself: a review may say "studies that were not peer-reviewed were excluded".
    r"|\bthis\s+(?:article|manuscript|paper|preprint|work)\s+(?:is|has)\s+not\s+(?:yet\s+)?(?:been\s+)?"
    r"(?:peer[- ]reviewed|certified)"
    r"|not\s+certified\s+by\s+peer\s+review", re.I,
)
# Version suffixes only where the registrant numbers versions of one work (eLife reviewed preprints,
# F1000-style platforms); elsewhere `<doi>.<n>` is usually a chapter (10.1017/CBO9780511810817.003).
_DOI_REVISION = re.compile(r"^(10\.7554/elife\.\d+|10\.12688/[a-z0-9-]+\.\d+)\.(\d+)$", re.I)
# Preprint servers. bioRxiv/medRxiv share 10.1101 with Cold Spring Harbor journals (gr., gad., lm., ...).
_PREPRINT_DOI = re.compile(
    r"^(?:10\.1101/(?:\d{4}\.\d{2}\.\d{2}\.)?\d{6,}|10\.48550/arxiv\.|10\.2139/ssrn\.|10\.21203/|10\.31219/"
    r"|10\.31234/|10\.31235/|10\.26434/|10\.20944/)", re.I,
)


@dataclass(frozen=True)
class VersionCandidate:
    url: str
    version: str
    version_source: str
    direct: bool
    order: int
    declared_version: str | None = None


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
        if _host_is_preprint(url):
            version, version_source = "submitted", "host"
        candidates.append(VersionCandidate(url, version, version_source, _direct(url), order,
                                           _version_name(raw_version)))
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


def is_preprint_doi(doi: str) -> bool:
    return bool(_PREPRINT_DOI.match(normalize_doi(doi)))


def _record_role(doi: str | None, arxiv_id: str | None, pdf_version: str | None,
                 doi_in_library: Callable[[str], bool]) -> str:
    """The record's role from its own identifiers; its PDF's version decides only when it has none.
    A published record holding a preprint PDF stays published (the PDF carries its own version)."""
    if doi:
        doi = normalize_doi(doi)
        if (match := _DOI_REVISION.match(doi)) and doi_in_library(match.group(1)):
            return "revision"
        return "preprint" if _PREPRINT_DOI.match(doi) else "published"
    if arxiv_id:
        return "preprint"
    return {"published": "published", "accepted": "accepted", "submitted": "preprint"}.get(pdf_version or "",
                                                                                         "unknown")


def _role_for_paper(conn: sqlite3.Connection, paper_id: str) -> str:
    row = conn.execute("SELECT doi, arxiv_id FROM papers WHERE id=?", (paper_id,)).fetchone()
    if row is None:
        return "unknown"
    artifact = None if row["doi"] or row["arxiv_id"] else conn.execute(
        "SELECT version FROM artifacts WHERE paper_id=? AND status='ok' ORDER BY created_at DESC LIMIT 1",
        (paper_id,),
    ).fetchone()
    return _record_role(row["doi"], row["arxiv_id"], artifact["version"] if artifact else None,
                        lambda doi: conn.execute("SELECT 1 FROM papers WHERE lower(doi)=lower(?)",
                                                 (doi,)).fetchone() is not None)


def _group_for(conn: sqlite3.Connection, paper_id: str) -> str | None:
    row = conn.execute("SELECT group_id FROM paper_versions WHERE paper_id=? AND confirmed=1 LIMIT 1",
                       (paper_id,)).fetchone()
    return str(row["group_id"]) if row else None


def _rejected(conn: sqlite3.Connection, a: str, b: str) -> bool:
    """Whether the user rejected a suggestion linking a and b, or unlinked one from the other."""
    return conn.execute(
        """SELECT 1 FROM paper_versions x JOIN paper_versions y ON x.group_id=y.group_id
           WHERE x.paper_id=? AND y.paper_id=? AND x.rejected=1 AND y.rejected=1 LIMIT 1""", (a, b),
    ).fetchone() is not None


def _join_pair(conn: sqlite3.Connection, a: str, b: str, evidence: str, *, confirmed: bool = True,
               by_user: bool = False) -> bool:
    if a == b or (not by_user and _rejected(conn, a, b)):
        return False
    ga, gb = _group_for(conn, a), _group_for(conn, b)
    if ga != gb:
        left = [str(r[0]) for r in conn.execute(
            "SELECT paper_id FROM paper_versions WHERE group_id=? AND confirmed=1", (ga,))] if ga else [a]
        right = [str(r[0]) for r in conn.execute(
            "SELECT paper_id FROM paper_versions WHERE group_id=? AND confirmed=1", (gb,))] if gb else [b]
        if any(_rejected(conn, x, y) for x in left for y in right
               if not by_user or {x, y} != {a, b}):
            return False
    ts = now()
    # A decided pair is no longer a suggestion (the user's confirmation also lifts an earlier rejection).
    conn.execute(
        """DELETE FROM paper_versions WHERE confirmed=0 AND group_id IN (
             SELECT x.group_id FROM paper_versions x JOIN paper_versions y ON x.group_id=y.group_id
             WHERE x.paper_id=? AND y.paper_id=? AND x.confirmed=0 AND y.confirmed=0)""", (a, b),
    )
    roles = {a: _role_for_paper(conn, a), b: _role_for_paper(conn, b)}
    if ga and ga == gb:
        for paper_id in (a, b):
            conn.execute("UPDATE paper_versions SET role=? WHERE group_id=? AND paper_id=?",
                         (roles[paper_id], ga, paper_id))
        return False
    group_id = ga or gb or str(uuid.uuid4())
    if ga and gb and ga != gb:
        # Explicit identifiers can reconcile independently discovered groups. An explicit primary survives
        # unless both groups had one.
        sides = conn.execute("SELECT COUNT(DISTINCT group_id) FROM paper_versions WHERE group_id IN (?,?) "
                             "AND is_primary=1", (ga, gb)).fetchone()[0]
        if sides > 1:
            conn.execute("UPDATE paper_versions SET is_primary=0 WHERE group_id IN (?,?)", (ga, gb))
        conn.execute("UPDATE paper_versions SET group_id=? WHERE group_id=?", (group_id, gb))
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
    with transaction(conn, immediate=True):
        doi_ids = {normalize_doi(str(r["doi"])).lower(): str(r["id"]) for r in conn.execute(
            "SELECT id,doi FROM papers WHERE doi IS NOT NULL")}
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
        # Roles follow the records' current identifiers and PDFs.
        for row in conn.execute("SELECT DISTINCT paper_id FROM paper_versions").fetchall():
            conn.execute("UPDATE paper_versions SET role=? WHERE paper_id=?",
                         (_role_for_paper(conn, str(row["paper_id"])), row["paper_id"]))
    return linked


def link_versions(conn: sqlite3.Connection, http: HttpClient, mailto: str | None = None,
                  paper_ids: Collection[str] | None = None) -> int:
    """Use cached Crossref relation lookups; unavailable APIs simply leave records unlinked.
    `paper_ids` limits the online lookups to those papers (automatic runs pass the project's papers)."""
    relations: dict[str, list[Mapping[str, Any]]] = {}
    scope = None if paper_ids is None else set(paper_ids)
    dois = [str(row["doi"]) for row in conn.execute("SELECT id,doi FROM papers WHERE doi IS NOT NULL")
            if scope is None or row["id"] in scope]
    for doi in dois:
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
    for doi in dois:
        if not is_preprint_doi(doi) or not normalize_doi(doi).startswith("10.1101/"):
            continue
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


@dataclass(frozen=True)
class _TitleRow:
    id: str
    title: str
    words: tuple[str, ...]
    authors: frozenset[str]
    year: int | None


def suggest_similar_versions(conn: sqlite3.Connection, paper_ids: Collection[str] | None = None) -> int:
    """Store title/author/year matches as unconfirmed suggestions, never as analysis links.

    Only pairs sharing a title's first significant word or one of its two rarest words are compared, so a
    large library stays fast; `paper_ids` limits the run to pairs with one of these papers (automatic runs
    pass the project's papers)."""
    papers: list[_TitleRow] = []
    for r in conn.execute("SELECT id,title,authors_json,year FROM papers WHERE title IS NOT NULL"):
        title = normalize_title(r["title"])
        if len(title) < 20:
            continue
        names = " ".join(str(a) for a in json.loads(r["authors_json"] or "[]")).lower()
        papers.append(_TitleRow(str(r["id"]), title,
                                tuple(w for w in title.split() if len(w) >= 3 and w not in STOPWORDS),
                                frozenset(re.findall(r"[a-z]{3,}", names)), r["year"]))
    frequency = Counter(w for p in papers for w in set(p.words))
    blocks: dict[str, list[int]] = defaultdict(list)
    for i, p in enumerate(papers):
        keys = {"^" + p.words[0]} if p.words else set()
        keys.update(sorted(set(p.words), key=lambda w: (frequency[w], w))[:2])
        for key in keys:
            blocks[key].append(i)
    scope = None if paper_ids is None else set(paper_ids)
    pairs: set[tuple[int, int]] = set()
    for members in blocks.values():
        if len(members) > 1000:  # a word this common says nothing; the rare words still pair copies
            continue
        for x, i in enumerate(members):
            for j in members[x + 1:]:
                if scope is None or papers[i].id in scope or papers[j].id in scope:
                    pairs.add((i, j) if i < j else (j, i))
    # Existing decisions, read once: confirmed groups and every suggestion or rejection already stored.
    confirmed = {str(r["paper_id"]): str(r["group_id"]) for r in conn.execute(
        "SELECT paper_id,group_id FROM paper_versions WHERE confirmed=1")}
    pending: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute("SELECT group_id,paper_id FROM paper_versions WHERE confirmed=0"):
        pending[str(r["group_id"])].add(str(r["paper_id"]))
    decided = {frozenset((x, y)) for members in pending.values() for x in members for y in members if x != y}
    found: list[tuple[str, str]] = []
    for i, j in sorted(pairs):
        a, b = papers[i], papers[j]
        if abs(len(a.title) - len(b.title)) > 0.08 * max(len(a.title), len(b.title)):
            continue
        if a.year and b.year and abs(a.year - b.year) > 1:
            continue
        if a.authors and b.authors and len(a.authors & b.authors) / len(a.authors | b.authors) < 0.25:
            continue
        if frozenset((a.id, b.id)) in decided or (a.id in confirmed and confirmed[a.id] == confirmed.get(b.id)):
            continue
        matcher = SequenceMatcher(None, a.title, b.title)
        if matcher.real_quick_ratio() < 0.92 or matcher.quick_ratio() < 0.92 or matcher.ratio() < 0.92:
            continue
        found.append((a.id, b.id))
    roles = {paper_id: _role_for_paper(conn, paper_id) for pair in found for paper_id in pair}
    ts = now()
    inserted = 0
    with transaction(conn, immediate=True):
        # Similarity is computed outside the write lock, but decisions must be read again after
        # reserving the writer: another UI job may have suggested, rejected or confirmed this pair.
        confirmed = {str(r["paper_id"]): str(r["group_id"]) for r in conn.execute(
            "SELECT paper_id,group_id FROM paper_versions WHERE confirmed=1")}
        decided = {frozenset((str(r[0]), str(r[1]))) for r in conn.execute(
            "SELECT paper_id_a,paper_id_b FROM paper_version_pairs")}
        for pair in found:
            left_id, right_id = pair
            if frozenset(pair) in decided or (left_id in confirmed and
                                              confirmed[left_id] == confirmed.get(right_id)):
                continue
            group_id = str(uuid.uuid4())
            for paper_id in pair:
                conn.execute(
                    "INSERT INTO paper_versions(group_id,paper_id,role,evidence,confirmed,created_at)"
                    " VALUES (?,?,?,'similarity:title-authors-year',0,?)", (group_id, paper_id, roles[paper_id], ts),
                )
            inserted += 1
    return inserted


def link_project_versions(conn: sqlite3.Connection, http: HttpClient, mailto: str | None,
                          project_id: str | None) -> tuple[int, int]:
    """Link and suggest versions for one project's papers, or the whole library without a project.
    Returns (explicit links, suggestions)."""
    members = None if project_id is None else {str(r[0]) for r in conn.execute(
        "SELECT paper_id FROM project_papers WHERE project_id=?", (project_id,))}
    return link_versions(conn, http, mailto, members), suggest_similar_versions(conn, members)


def confirm_version_link(conn: sqlite3.Connection, a: str, b: str) -> bool:
    with transaction(conn, immediate=True):
        _join_pair(conn, a, b, "user:confirmed", confirmed=True, by_user=True)
        group = _group_for(conn, a)
        return group is not None and group == _group_for(conn, b)


def unlink_version(conn: sqlite3.Connection, paper_id: str) -> bool:
    """Remove a paper from its confirmed group and remember that it is not a version of the others."""
    with transaction(conn, immediate=True):
        group = _group_for(conn, paper_id)
        if group is None:
            return False
        others = [str(r["paper_id"]) for r in conn.execute(
            "SELECT paper_id FROM paper_versions WHERE group_id=? AND paper_id<>?", (group, paper_id))]
        conn.execute("DELETE FROM paper_versions WHERE group_id=? AND paper_id=?", (group, paper_id))
        if len(others) == 1:  # a group of one is no group
            conn.execute("DELETE FROM paper_versions WHERE group_id=?", (group,))
        ts = now()
        for other in others:
            # A similarity suggestion may predate this group's transitive confirmation. Replace
            # that pair's old decision before recording the unlink, preserving pair uniqueness.
            left_id, right_id = sorted((paper_id, other))
            conn.execute(
                "DELETE FROM paper_versions WHERE confirmed=0 AND group_id IN ("
                "SELECT group_id FROM paper_version_pairs WHERE paper_id_a=? AND paper_id_b=?)",
                (left_id, right_id),
            )
            rejection = str(uuid.uuid4())  # one per pair, so the remaining members stay versions of each other
            for member in (paper_id, other):
                conn.execute(
                    "INSERT INTO paper_versions(group_id,paper_id,role,evidence,confirmed,rejected,created_at)"
                    " VALUES (?,?,?,'user:unlinked',0,1,?)", (rejection, member, _role_for_paper(conn, member), ts),
                )
        return True


def reject_version_link(conn: sqlite3.Connection, a: str, b: str) -> bool:
    with transaction(conn, immediate=True):
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
    with transaction(conn, immediate=True):
        row = conn.execute("SELECT group_id FROM paper_versions WHERE paper_id=? AND confirmed=1 LIMIT 1",
                           (paper_id,)).fetchone()
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


def _version_selection(conn: sqlite3.Connection, project_id: str | None) -> tuple[set[str], set[str]]:
    """Confirmed group members (within the project, if given) and the primary chosen for each group."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_versions'"
    ).fetchone():
        return set(), set()
    sql = """SELECT pv.group_id,pv.paper_id,pv.role,pv.is_primary,
                  (a.id IS NOT NULL AND COALESCE(a.version_label,'') NOT LIKE 'this PDF is %') has_pdf,
                  (COALESCE(a.version_label,'') NOT LIKE 'this PDF is %' AND EXISTS(
                      SELECT 1 FROM parse_runs r JOIN chunks c ON c.parse_run_id=r.id
                      WHERE r.artifact_id=a.id AND r.is_current=1 AND r.output_dir IS NOT NULL
                        AND trim(c.text)<>'')) has_text
           FROM paper_versions pv
           LEFT JOIN artifacts a ON a.id=(SELECT current.id FROM artifacts current
               WHERE current.paper_id=pv.paper_id AND current.status='ok'
               ORDER BY current.created_at DESC LIMIT 1)
           WHERE pv.confirmed=1 AND pv.rejected=0"""
    params: tuple[str, ...] = ()
    if project_id:
        sql += " AND pv.paper_id IN (SELECT paper_id FROM project_papers WHERE project_id=?)"
        params = (project_id,)
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute(sql, params):
        grouped.setdefault(str(row["group_id"]), []).append(row)
    selected: set[str] = set()
    for members in grouped.values():
        explicit = next((r for r in members if r["is_primary"]), None)
        winner = explicit or min(members, key=lambda r: (
            not bool(r["has_text"]), not bool(r["has_pdf"]), ROLE_ORDER.get(r["role"], 4), str(r["paper_id"])))
        selected.add(str(winner["paper_id"]))
    return {str(r["paper_id"]) for members in grouped.values() for r in members}, selected


def primary_paper_ids(conn: sqlite3.Connection, project_id: str | None = None) -> set[str]:
    """Return project/library paper IDs with confirmed groups reduced to their primary member."""
    base_sql = (
        "SELECT paper_id FROM project_papers WHERE project_id=?" if project_id
        else "SELECT id AS paper_id FROM papers"
    )
    base = {str(r["paper_id"]) for r in conn.execute(base_sql, (project_id,) if project_id else ())}
    grouped, selected = _version_selection(conn, project_id)
    return (base - grouped) | selected


def hidden_version_ids(conn: sqlite3.Connection, project_id: str | None = None) -> set[str]:
    """Confirmed group members that are not their group's primary; empty while no versions are linked."""
    grouped, selected = _version_selection(conn, project_id)
    return grouped - selected


def version_summaries(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Version badge fields of every paper from three queries (the library and run lists show many)."""
    artifacts = {str(r["paper_id"]): r for r in conn.execute(
        """SELECT paper_id,version,version_label,source_url FROM (
             SELECT paper_id,version,version_label,source_url,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY created_at DESC) AS rank
             FROM artifacts WHERE status='ok' AND paper_id IS NOT NULL) WHERE rank=1""")}
    roles: dict[str, str] = {}
    for r in conn.execute("SELECT paper_id,role FROM paper_versions WHERE confirmed=1"):
        roles.setdefault(str(r["paper_id"]), str(r["role"]))
    papers = conn.execute("SELECT id,doi,arxiv_id FROM papers").fetchall()
    dois = {normalize_doi(str(r["doi"])) for r in papers if r["doi"]}
    out: dict[str, dict[str, Any]] = {}
    for r in papers:
        artifact = artifacts.get(str(r["id"]))
        pdf_version = format_version_label(artifact["version"], artifact["source_url"],
                                           artifact["version_label"]) if artifact else None
        role = roles.get(str(r["id"])) or _record_role(
            r["doi"], r["arxiv_id"], artifact["version"] if artifact else None, dois.__contains__)
        label = str(artifact["version_label"]) if artifact and artifact["version_label"] else ""
        out[str(r["id"])] = {"version": pdf_version or role, "pdf_version": pdf_version, "version_role": role,
                             "pdf_warning": label if label.startswith("this PDF is ") else None}
    return out


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


def printed_dois(pdf: Any, pages: int = 2) -> list[str]:
    """DOIs printed on a PDF's first pages, in order: its own and any it cites there."""
    found = [normalize_doi(m) for i in range(min(pages, pdf.page_count)) for m in DOI_RE.findall(pdf[i].get_text())]
    return list(dict.fromkeys(found))


def other_record_doi(conn: sqlite3.Connection, paper_id: str, own_doi: str | None,
                     dois: Sequence[str]) -> tuple[str, str] | None:
    """(paper id, DOI) of the library record a PDF identifies as, when that is not the record holding it.

    A PDF naming the record's own DOI is that record's copy even if it cites other library papers; one that
    names neither is not evidence of a mismatch."""
    if own_doi and normalize_doi(own_doi) in dois:
        return None
    for doi in dois:
        other = conn.execute("SELECT id FROM papers WHERE lower(doi)=lower(?) AND id<>? ORDER BY id LIMIT 1",
                             (doi, paper_id)).fetchone()
        if other:
            return str(other["id"]), doi
    return None


def backfill_unknown_versions(conn: sqlite3.Connection, paths: Any) -> tuple[int, int]:
    """Inspect each paper's current unclassified PDF; never move its artifact between records.
    Returns (labelled, skipped): a missing or unreadable PDF file is skipped."""
    import pymupdf

    rows = conn.execute(
        """SELECT a.id,a.paper_id,a.pdf_path,a.source_url,p.doi
           FROM artifacts a JOIN papers p ON p.id=a.paper_id
           WHERE a.status='ok' AND a.version='unknown'
             AND a.id=(SELECT current.id FROM artifacts current WHERE current.paper_id=a.paper_id
                       AND current.status='ok' ORDER BY current.created_at DESC LIMIT 1)"""
    ).fetchall()
    changed = skipped = 0
    for row in rows:
        path = paths.root / str(row["pdf_path"])
        try:
            with pymupdf.open(path) as pdf:
                first_page = pdf[0].get_text() if pdf.page_count else ""
                meta_doi = extract_metadata(pdf).doi.value
                dois = list(dict.fromkeys(([normalize_doi(str(meta_doi))] if meta_doi else []) + printed_dois(pdf)))
        except Exception:  # noqa: BLE001 - a missing or damaged file must not stop the backfill
            skipped += 1
            continue
        version, source = infer_version(None, pdf_text=first_page)
        warning = None
        if other := other_record_doi(conn, str(row["paper_id"]), row["doi"], dois):
            warning = f"this PDF is {role_for_paper(conn, other[0])} {other[1]}"
        if version == "unknown" and warning is None:
            continue
        label = warning or format_version_label(version, row["source_url"])
        with transaction(conn, immediate=True):
            changed += conn.execute(
                "UPDATE artifacts SET version=?,version_source=?,version_label=? WHERE id=? AND version='unknown'",
                (version, source, label, row["id"]),
            ).rowcount
    return changed, skipped


def role_for_paper(conn: sqlite3.Connection, paper_id: str) -> str:
    row = conn.execute("SELECT role FROM paper_versions WHERE paper_id=? AND confirmed=1 LIMIT 1",
                       (paper_id,)).fetchone()
    return str(row["role"]) if row else _role_for_paper(conn, paper_id)


def set_artifact_version(
    conn: sqlite3.Connection, paper_id: str, version: str, *, source: str = "user",
) -> None:
    if version not in VERSION_ORDER or source not in {"openalex", "unpaywall", "host", "pdf_text", "user", "unknown"}:
        raise ValueError("invalid PDF version or source")
    with transaction(conn, immediate=True):
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
