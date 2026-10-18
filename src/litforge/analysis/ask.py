"""Cited answers with deterministic sentence validation."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from litforge.analysis.errors import run_error
from litforge.analysis.figures import relevant_asset_spans
from litforge.analysis.gateway import CallPreview, ModelGateway
from litforge.analysis.ledger import EvidenceSpan, record_spans, to_packet
from litforge.analysis.prompts import ASK_PROMPT_VERSION, ask_messages
from litforge.analysis.retrieve import retrieve_for_question
from litforge.analysis.runs import canonical_json, finish_run, get_run, link_evidence, start_run
from litforge.analysis.validation import (
    EvidenceText,
    citations,
    image_only_numbers,
    missing_numbers,
    strip_evidence_mentions,
)
from litforge.indexing.fts import SearchHit, current_runs
from litforge.llm.evidence import EvidencePacket
from litforge.storage.db import new_id, now, transaction

SentenceKind = Literal["reported_fact", "interpretation", "unsupported"]
AuditVerdict = Literal["supported", "partial", "unsupported", "contradicted"]
ReviewStatus = Literal["open", "accepted", "rejected", "edited"]


class SentenceDraft(BaseModel):
    text: EvidenceText
    kind: SentenceKind
    evidence_ids: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def recover_kind(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("kind") not in ("reported_fact", "interpretation", "unsupported"):
            return {**value, "kind": "unsupported", "flags": [*value.get("flags", []), "invalid_kind"]}
        return value


class AnswerSentence(SentenceDraft):
    id: str = Field(default_factory=new_id)
    idx: int = 0
    flags: list[str] = Field(default_factory=list)
    audit_verdict: AuditVerdict | None = None
    audit_note: EvidenceText = ""
    audit_model: str | None = None
    review_status: ReviewStatus = "open"

    @property
    def needs_review(self) -> bool:
        return bool(self.flags or self.kind != "reported_fact"
                    or (self.audit_verdict is not None and self.audit_verdict != "supported"))


class AnswerDraft(BaseModel):
    sentences: list[SentenceDraft] = Field(max_length=8)
    insufficient: bool
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def cap_sentences(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("sentences"), list) and len(value["sentences"]) > 8:
            return {**value, "sentences": value["sentences"][:8], "flags": ["truncated"]}
        return value


class Answer(BaseModel):
    run_id: str
    question: str
    sentences: list[AnswerSentence]
    evidence: list[EvidenceSpan]
    invalid_citations: list[str] = Field(default_factory=list)
    insufficient: bool
    flags: list[str] = Field(default_factory=list)
    model: str = ""
    provider: str = ""


def _question_spans(conn: sqlite3.Connection, hits: list[SearchHit], question: str,
                    project_id: str | None, paper_ids: list[str] | None = None) -> list[EvidenceSpan]:
    spans = [span.model_copy(update={"page": hit.page}) for span, hit in
             zip(record_spans(conn, hits), hits, strict=True)]
    seen = {span.id for span in spans}
    selected_runs: set[tuple[str, str]] = set()
    for hit in hits:
        row = conn.execute("SELECT parse_run_id FROM chunks WHERE id=?", (hit.chunk_id,)).fetchone()
        if row is not None:
            selected_runs.add((hit.paper_id, row["parse_run_id"]))
    if not hits:
        selected_runs.update((row["paper_id"], row["id"]) for row in current_runs(conn, project_id)
                             if paper_ids is None or row["paper_id"] in paper_ids)
    for paper_id, parse_run_id in sorted(selected_runs):
        for span in relevant_asset_spans(conn, paper_id, parse_run_id, question):
            if span.id not in seen:
                spans.append(span)
                seen.add(span.id)
    return spans


def validate_sentences(drafts: list[SentenceDraft], packet: EvidencePacket | None
                       ) -> tuple[list[AnswerSentence], list[str]]:
    result: list[AnswerSentence] = []
    invalid: list[str] = []
    for draft in drafts:
        text = strip_evidence_mentions(draft.text)
        if not text:
            continue
        ids, bad = citations(draft.evidence_ids, packet)
        invalid.extend(bad)
        flags = list(draft.flags)
        kind = draft.kind
        if kind == "reported_fact" and not ids:
            kind = "unsupported"
            flags.append("uncited_fact")
        elif kind == "interpretation" and not ids:
            flags.append("uncited_interpretation")
        if missing_numbers(text, ids, packet):
            flags.append("from_image" if image_only_numbers(text, ids, packet) else "number_not_in_evidence")
        result.append(AnswerSentence(idx=len(result), text=text, kind=kind, evidence_ids=ids, flags=flags))
    return result, invalid


def get_sentences(conn: sqlite3.Connection, run_id: str) -> list[AnswerSentence]:
    return [AnswerSentence(**{**dict(row), "evidence_ids": json.loads(row["evidence_ids_json"]),
                              "flags": json.loads(row["flags_json"])})
            for row in conn.execute("SELECT * FROM answer_sentences WHERE run_id=? ORDER BY idx", (run_id,))]


def get_answer(conn: sqlite3.Connection, run_id: str) -> Answer:
    run = get_run(conn, run_id)
    if run.task != "ask" or run.status != "ok":
        raise ValueError("not a completed answer run")
    answer = Answer.model_validate_json(run.result_json)
    answer.sentences = get_sentences(conn, run_id)
    return answer


def plan_calls(conn: sqlite3.Connection, gateway: ModelGateway, question: str, project_id: str | None = None,
               k: int = 8, profile: str | None = None, sensitive: bool = False,
               paper_ids: list[str] | None = None) -> list[CallPreview]:
    hits = retrieve_for_question(conn, question, project_id, k, paper_ids=paper_ids)
    spans = _question_spans(conn, hits, question, project_id, paper_ids)
    if not spans:
        return []
    packet, messages = gateway.fit_evidence("ask", to_packet(spans, sensitive=sensitive),
        lambda p: ask_messages(question, p, spans), profile, schema=AnswerDraft.model_json_schema())
    return [gateway.preview("ask", packet, messages, profile=profile, schema=AnswerDraft.model_json_schema())]


def ask(conn: sqlite3.Connection, gateway: ModelGateway, question: str, project_id: str | None = None,
        k: int = 8, profile: str | None = None, approved: bool = False, sensitive: bool = False,
        paper_ids: list[str] | None = None) -> Answer:
    run_id = start_run(conn, "ask", project_id, {"question": question},
                       {"question": question, "retrieval": "lexical", "k": k, "profile": profile,
                        "sensitive": sensitive, "paper_ids": paper_ids}, ASK_PROMPT_VERSION)
    try:
        hits = retrieve_for_question(conn, question, project_id, k, paper_ids=paper_ids)
        spans = _question_spans(conn, hits, question, project_id, paper_ids)
        link_evidence(conn, run_id, spans, [*(h.score for h in hits), *([None] * (len(spans) - len(hits)))])
        model, provider = "", ""
        if spans:
            packet, messages = gateway.fit_evidence("ask", to_packet(spans, sensitive=sensitive),
                lambda p: ask_messages(question, p, spans), profile, schema=AnswerDraft.model_json_schema())
            reply = gateway.call("ask", packet, messages, AnswerDraft.model_json_schema(),
                                 profile=profile, approved=approved, run_id=run_id, prompt_version=ASK_PROMPT_VERSION)
            draft = AnswerDraft.model_validate(reply.content)
            model, provider = reply.model, reply.provider
        else:
            packet = None
            draft = AnswerDraft(sentences=[SentenceDraft(
                text="No evidence found in the library for this question.", kind="unsupported")], insufficient=True)
        sentences, invalid = validate_sentences(draft.sentences, packet)
        answer = Answer(run_id=run_id, question=question, sentences=sentences, evidence=spans,
                        invalid_citations=invalid, insufficient=draft.insufficient, model=model, provider=provider,
                        flags=draft.flags)
        with transaction(conn):
            for s in sentences:
                conn.execute("""INSERT INTO answer_sentences(id,run_id,idx,text,kind,
                                evidence_ids_json,flags_json,created_at)
                                VALUES (?,?,?,?,?,?,?,?)""",
                             (s.id, run_id, s.idx, s.text, s.kind, canonical_json(s.evidence_ids),
                              canonical_json(s.flags), now()))
            finish_run(conn, run_id, "ok", answer.model_dump())
        return answer
    except Exception as exc:
        finish_run(conn, run_id, "error", {}, run_error(exc))
        raise
