"""Immutable project state and deterministic evidence-change signals."""

from __future__ import annotations

import math
import sqlite3
from typing import Literal

from pydantic import BaseModel, Field

from litforge.analysis.ask import get_answer
from litforge.analysis.claim_graph import claim_landscape, list_relations
from litforge.analysis.claims import list_claims
from litforge.analysis.matrix import CellValue, list_matrices
from litforge.analysis.runs import canonical_json, input_hash
from litforge.storage.db import new_id, now, transaction


class PaperState(BaseModel):
    id: str
    title: str | None = None
    year: int | None = None
    doi: str | None = None
    s1: str | None = None
    s2: str | None = None
    mark: str | None = None
    priority: float | None = None

    @property
    def included(self) -> bool:
        if self.mark is not None:
            return self.mark == "relevant"
        return (self.s2 or self.s1) == "include"


class CellState(BaseModel):
    matrix_id: str
    column_key: str
    paper_id: str
    status: str
    value: CellValue = None
    unit: str | None = None
    evidence_ids: list[str]
    cache_key: str


class ClaimState(BaseModel):
    id: str
    paper_id: str
    text: str
    canonical_hash: str
    cluster_id: str | None
    status: str
    evidence_ids: list[str]


class RelationState(BaseModel):
    pair: tuple[str, str]
    relation: str


class ClusterState(BaseModel):
    id: str
    label: str
    claim_ids: list[str]
    supporting: int
    contradicting: int
    single_source: bool

    @property
    def strength(self) -> int:
        return self.supporting - self.contradicting


class SentenceState(BaseModel):
    text: str
    kind: str
    evidence_ids: list[str]


class AnswerState(BaseModel):
    question: str
    sentences: list[SentenceState]


class SnapshotData(BaseModel):
    papers: list[PaperState] = Field(default_factory=list)
    matrix: list[CellState] = Field(default_factory=list)
    claims: list[ClaimState] = Field(default_factory=list)
    relations: list[RelationState] = Field(default_factory=list)
    answers: list[AnswerState] = Field(default_factory=list)
    clusters: list[ClusterState] = Field(default_factory=list)


class Snapshot(BaseModel):
    id: str
    project_id: str
    label: str
    created_at: str
    content_hash: str
    data: SnapshotData


def get_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> Snapshot:
    row = conn.execute("SELECT * FROM project_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise KeyError(snapshot_id)
    return Snapshot(**dict(row), data=SnapshotData.model_validate_json(row["data_json"]))


def list_snapshots(conn: sqlite3.Connection, project_id: str) -> list[Snapshot]:
    return [get_snapshot(conn, r[0]) for r in conn.execute(
        "SELECT id FROM project_snapshots WHERE project_id=? ORDER BY rowid DESC", (project_id,))]


def take_snapshot(conn: sqlite3.Connection, project_id: str, label: str = "") -> str:
    with transaction(conn):
        if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise KeyError(project_id)
        papers = [PaperState(**dict(r)) for r in conn.execute(
            "SELECT p.id,p.title,p.year,p.doi,s.decision AS s1,f.decision AS s2,m.mark,"
            "(SELECT MAX(d.priority) FROM discovery_results d JOIN discovery_runs r ON r.id=d.run_id "
            "WHERE r.project_id=pp.project_id AND d.paper_id=p.id) AS priority "
            "FROM project_papers pp JOIN papers p ON p.id=pp.paper_id "
            "LEFT JOIN screening_results s ON s.project_id=pp.project_id AND s.paper_id=p.id "
            "LEFT JOIN fulltext_screening f ON f.project_id=pp.project_id AND f.paper_id=p.id "
            "LEFT JOIN paper_marks m ON m.project_id=pp.project_id AND m.paper_id=p.id "
            "WHERE pp.project_id=? ORDER BY p.id", (project_id,))]
        members = {p.id for p in papers}
        claims = [ClaimState.model_validate(c.model_dump()) for c in list_claims(conn, project_id)
                  if c.paper_id in members]
        ids = {c.id for c in claims if c.status != "rejected"}
        clusters = [ClusterState(id=c.cluster_id, label=c.label,
                                claim_ids=sorted(i.id for i in claims if i.cluster_id == c.cluster_id and i.id in ids),
                                supporting=c.supporting_papers, contradicting=c.contradicting_papers,
                                single_source=c.single_source) for c in claim_landscape(conn, project_id)
                    if any(i.id in ids and i.cluster_id == c.cluster_id for i in claims)]
        answers: dict[str, AnswerState] = {}
        for r in conn.execute("SELECT id FROM analysis_runs WHERE project_id=? AND task='ask' AND status='ok' "
                              "ORDER BY rowid DESC", (project_id,)):
            answer = get_answer(conn, r[0])
            answers.setdefault(answer.question, AnswerState(question=answer.question, sentences=[
                SentenceState(text=s.text, kind=s.kind, evidence_ids=sorted(s.evidence_ids))
                for s in answer.sentences]))
        data = SnapshotData(papers=papers, claims=claims, clusters=clusters,
            matrix=sorted([CellState.model_validate(c.model_dump()) for m in list_matrices(conn, project_id)
                           for c in m.cells], key=lambda c: (c.matrix_id, c.paper_id, c.column_key)),
            relations=[RelationState(pair=(min(r.source_claim_id, r.target_claim_id),
                                                 max(r.source_claim_id, r.target_claim_id)), relation=r.relation)
                       for r in list_relations(conn, project_id) if r.status not in ("stale", "rejected")
                       and r.source_claim_id in ids and r.target_claim_id in ids],
            answers=[answers[q] for q in sorted(answers)])
        snapshot_id = new_id()
        payload = data.model_dump()
        conn.execute("INSERT INTO project_snapshots VALUES (?,?,?,?,?,?)",
                     (snapshot_id, project_id, label, now(), input_hash(payload), canonical_json(payload)))
    return snapshot_id


class ScreeningChange(BaseModel):
    paper_id: str
    before: tuple[str | None, str | None, str | None]
    after: tuple[str | None, str | None, str | None]


class CellChange(BaseModel):
    matrix_id: str
    paper_id: str
    column_key: str
    before: CellState | None
    after: CellState | None

    @property
    def reported_value_changed(self) -> bool:
        a, b = self.before, self.after
        if not (a and a.status == "reported" or b and b.status == "reported"):
            return False
        before = (a.value, a.unit, a.status) if a else None
        after = (b.value, b.unit, b.status) if b else None
        return before != after


class ClusterChange(BaseModel):
    cluster_ids: list[str]
    label: str
    support_before: int
    support_after: int
    contradiction_before: int
    contradiction_after: int
    strength_before: int
    strength_after: int
    direction: Literal["strengthened", "weakened", "changed", "regrouped"]


class AnswerChange(BaseModel):
    question: str
    text: str
    before: list[str]
    after: list[str]


class SnapshotDelta(BaseModel):
    old_id: str
    new_id: str
    added_papers: list[PaperState] = Field(default_factory=list)
    removed_papers: list[PaperState] = Field(default_factory=list)
    screening_changes: list[ScreeningChange] = Field(default_factory=list)
    matrix_changes: list[CellChange] = Field(default_factory=list)
    added_claims: list[ClaimState] = Field(default_factory=list)
    removed_claims: list[ClaimState] = Field(default_factory=list)
    cluster_changes: list[ClusterChange] = Field(default_factory=list)
    new_contradictions: list[RelationState] = Field(default_factory=list)
    single_source_claims: list[ClaimState] = Field(default_factory=list)
    answer_changes: list[AnswerChange] = Field(default_factory=list)
    priority_p75: float | None = None


def _cluster_changes(old: SnapshotData, new: SnapshotData) -> list[ClusterChange]:
    # Connected overlap components handle membership changes, merges and splits without ID churn signals.
    pending = [(False, c) for c in old.clusters] + [(True, c) for c in new.clusters]
    changes = []
    while pending:
        group = [pending.pop(0)]
        while True:
            matches = [(side, c) for side, c in pending if any(
                side != other and set(c.claim_ids) & set(member.claim_ids) for other, member in group)]
            if not matches:
                break
            group.extend(matches)
            pending = [p for p in pending if p not in matches]
        before = [c for side, c in group if not side]
        after = [c for side, c in group if side]
        if not before or not after:
            continue  # A new isolated claim is not replication or a strength change of an existing result.
        sb, sa = sum(c.supporting for c in before), sum(c.supporting for c in after)
        cb, ca = sum(c.contradicting for c in before), sum(c.contradicting for c in after)
        if (sb, cb) == (sa, ca):
            continue
        a, b = sb - cb, sa - ca
        # Every isolated cluster counts its origin paper, so sums over a merge or split are not comparable.
        regrouped = len(before) != 1 or len(after) != 1
        changes.append(ClusterChange(cluster_ids=sorted(c.id for _, c in group), label=after[0].label,
            support_before=sb, support_after=sa, contradiction_before=cb, contradiction_after=ca,
            strength_before=a, strength_after=b,
            direction="regrouped" if regrouped else "strengthened" if b > a else "weakened" if b < a else "changed"))
    return changes


def diff_snapshots(old: Snapshot, new: Snapshot) -> SnapshotDelta:
    if old.project_id != new.project_id:
        raise ValueError("snapshots must belong to the same project")
    a, b = old.data, new.data
    pa, pb = {p.id: p for p in a.papers}, {p.id: p for p in b.papers}
    ca, cb = {c.id: c for c in a.claims}, {c.id: c for c in b.claims}
    ma = {(c.matrix_id, c.paper_id, c.column_key): c for c in a.matrix}
    mb = {(c.matrix_id, c.paper_id, c.column_key): c for c in b.matrix}
    matrix_changes = []
    for key in sorted(ma.keys() | mb.keys()):
        x, y = ma.get(key), mb.get(key)
        if x is None or y is None or (x.status, x.value, x.unit, set(x.evidence_ids)) != (
                y.status, y.value, y.unit, set(y.evidence_ids)):
            matrix_changes.append(CellChange(matrix_id=key[0], paper_id=key[1], column_key=key[2], before=x, after=y))
    answers = {q.question: q for q in a.answers}
    answer_changes = []
    for answer in b.answers:
        previous = answers.get(answer.question)
        if previous is None:
            continue
        # Match exact texts first so reordering or deleting a sentence cannot change another's identity.
        unmatched = dict(enumerate(previous.sentences))
        matched: dict[int, SentenceState] = {}
        for idx, sentence in enumerate(answer.sentences):
            match = next((i for i, prior in unmatched.items() if prior.text == sentence.text), None)
            if match is not None:
                matched[idx] = unmatched.pop(match)
        pairs: list[tuple[SentenceState | None, SentenceState | None]] = []
        for idx, sentence in enumerate(answer.sentences):
            prior = matched.get(idx)
            if prior is None:
                prior = unmatched.pop(idx, None)
            pairs.append((prior, sentence))
        pairs.extend((prior, None) for prior in unmatched.values())
        for prior, current_sentence in pairs:
            evidence_before = sorted(set(prior.evidence_ids)) if prior else []
            evidence_after = sorted(set(current_sentence.evidence_ids)) if current_sentence else []
            if evidence_before != evidence_after:
                answer_changes.append(AnswerChange(question=answer.question,
                    text=current_sentence.text if current_sentence else prior.text if prior else "",
                    before=evidence_before, after=evidence_after))
    priorities = sorted(p.priority for p in b.papers if p.priority is not None)
    # Nearest-rank percentile; unknown priorities never count as high priority.
    threshold = priorities[math.ceil(.75 * len(priorities)) - 1] if priorities else None
    old_contradictions = {r.pair for r in a.relations if r.relation == "contradicts"}
    single = {c.id for c in b.clusters if c.single_source}
    return SnapshotDelta(old_id=old.id, new_id=new.id,
        added_papers=[pb[p] for p in sorted(pb.keys() - pa.keys())],
        removed_papers=[pa[p] for p in sorted(pa.keys() - pb.keys())],
        screening_changes=[ScreeningChange(paper_id=p, before=(pa[p].s1, pa[p].s2, pa[p].mark),
            after=(pb[p].s1, pb[p].s2, pb[p].mark)) for p in sorted(pa.keys() & pb.keys())
            if (pa[p].s1, pa[p].s2, pa[p].mark) != (pb[p].s1, pb[p].s2, pb[p].mark)],
        matrix_changes=matrix_changes, added_claims=[cb[c] for c in sorted(cb.keys() - ca.keys())],
        removed_claims=[ca[c] for c in sorted(ca.keys() - cb.keys())], cluster_changes=_cluster_changes(a, b),
        new_contradictions=[r for r in b.relations if r.relation == "contradicts" and r.pair not in old_contradictions],
        single_source_claims=[c for c in b.claims if c.cluster_id in single and c.status != "rejected"],
        answer_changes=answer_changes, priority_p75=threshold)


class Signal(BaseModel):
    kind: Literal["important_paper", "cluster_strength", "contradiction", "matrix_value", "answer_evidence"]
    message: str


def significance(delta: SnapshotDelta) -> list[Signal]:
    signals = [Signal(kind="important_paper", message=f"New included priority paper: {p.title or p.id}")
               for p in delta.added_papers if p.included and p.priority is not None
               and delta.priority_p75 is not None and p.priority >= delta.priority_p75]
    signals += [Signal(kind="cluster_strength", message=f"Result {c.direction}: {c.label} "
                       f"(strength {c.strength_before} → {c.strength_after})")
                for c in delta.cluster_changes
                if c.direction in ("strengthened", "weakened") and abs(c.strength_after - c.strength_before) >= 1]
    signals += [Signal(kind="contradiction", message=f"New contradiction: {' / '.join(r.pair)}")
                for r in delta.new_contradictions]
    signals += [Signal(kind="matrix_value", message=f"Reported value changed: {c.paper_id} / {c.column_key}")
                for c in delta.matrix_changes if c.reported_value_changed]
    signals += [Signal(kind="answer_evidence", message=f"Answer may need rewriting: {c.question}")
                for c in delta.answer_changes]
    return signals
