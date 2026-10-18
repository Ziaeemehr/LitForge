"""Independent, cited-span-only answer audit."""

from __future__ import annotations

import json
import sqlite3
from functools import partial

from pydantic import BaseModel

from litforge.analysis.ask import AnswerSentence, AuditVerdict, get_answer
from litforge.analysis.errors import run_error
from litforge.analysis.gateway import CallPreview, ModelGateway
from litforge.analysis.ledger import to_packet
from litforge.analysis.prompts import AUDIT_PROMPT_VERSION, audit_messages
from litforge.analysis.runs import finish_run, get_run, link_evidence, start_run
from litforge.analysis.validation import EvidenceText


class AuditDraft(BaseModel):
    verdict: AuditVerdict
    note: EvidenceText


def plan_calls(conn: sqlite3.Connection, gateway: ModelGateway, run_id: str,
               profile: str | None = None) -> list[CallPreview]:
    answer = get_answer(conn, run_id)
    sensitive = json.loads(get_run(conn, run_id).config_json).get("sensitive", False)
    previews = []
    for sentence in answer.sentences:
        spans = [s for s in answer.evidence if s.id in sentence.evidence_ids]
        if spans:
            packet, messages = gateway.fit_evidence("audit", to_packet(spans, sensitive=sensitive),
                partial(audit_messages, sentence.text, spans=spans), profile, schema=AuditDraft.model_json_schema())
            previews.append(gateway.preview("audit", packet, messages,
                                            profile=profile, schema=AuditDraft.model_json_schema()))
    return previews


def audit_answer(conn: sqlite3.Connection, gateway: ModelGateway, run_id: str, profile: str | None = None,
                 approved: bool = False) -> list[AnswerSentence]:
    answer = get_answer(conn, run_id)
    source_run = get_run(conn, run_id)
    sensitive = json.loads(source_run.config_json).get("sensitive", False)
    audit_id = start_run(conn, "audit", source_run.project_id, {"answer_run": run_id},
                         {"answer_run": run_id, "profile": profile, "sensitive": sensitive}, AUDIT_PROMPT_VERSION)
    try:
        link_evidence(conn, audit_id, answer.evidence, [None] * len(answer.evidence))
        for sentence in answer.sentences:
            spans = [s for s in answer.evidence if s.id in sentence.evidence_ids]
            model = None
            if spans:
                packet, messages = gateway.fit_evidence("audit", to_packet(spans, sensitive=sensitive),
                    partial(audit_messages, sentence.text, spans=spans), profile, schema=AuditDraft.model_json_schema())
                reply = gateway.call("audit", packet, messages,
                                     AuditDraft.model_json_schema(), profile=profile, approved=approved,
                                     run_id=audit_id, prompt_version=AUDIT_PROMPT_VERSION)
                draft = AuditDraft.model_validate(reply.content)
                model = reply.model
            else:
                draft = AuditDraft(verdict="unsupported", note="No cited evidence.")
            if draft.verdict != "supported" and draft.verdict != sentence.audit_verdict:
                sentence.review_status = "open"
            sentence.audit_verdict, sentence.audit_note, sentence.audit_model = draft.verdict, draft.note, model
            conn.execute("UPDATE answer_sentences SET audit_verdict=?,audit_note=?,audit_model=?,review_status=? "
                         "WHERE id=?",
                         (draft.verdict, draft.note, model, sentence.review_status, sentence.id))
        finish_run(conn, audit_id, "ok", {"sentences": [s.model_dump() for s in answer.sentences]})
        finish_run(conn, run_id, "ok", answer.model_dump())
        return answer.sentences
    except Exception as exc:
        finish_run(conn, audit_id, "error", {}, run_error(exc))
        raise
