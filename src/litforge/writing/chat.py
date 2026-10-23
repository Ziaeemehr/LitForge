"""The Write view's assistant: answers about the open document and the project's papers, with quotes checked
against the evidence, and edits of the selection that become ordinary suggestions (same guards, accepted or
rejected by the user). Nothing changes in the document here."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from litforge.analysis.gateway import CallPreview, ModelGateway
from litforge.analysis.ledger import record_spans, to_packet
from litforge.analysis.prompts import WRITE_PROMPT_VERSION, write_chat_messages
from litforge.analysis.query_translate import search_query
from litforge.analysis.retrieve import retrieve_for_question
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.writing import assist
from litforge.writing.documents import Document

MAX_QUESTION = 2000
MAX_SELECTION = assist.MAX_SELECTION
MAX_CONTEXT = 6000
MAX_HISTORY = 8  # earlier messages sent along (user and assistant turns)
MAX_TURN = 2000
# Commands an inserted block commonly uses, so a model's unescaped "\\neq" or "\\hline" is repaired.
_INSERT_COMMANDS = (assist._MATH_COMMANDS + r" \begin \end \hline \toprule \midrule \bottomrule \centering"
                    r" \caption \label \textbf \textit \emph \noindent \newline \item \multicolumn \ref")
_SCHEMA = {"type": "object", "properties": {
    "answer": {"type": "string"}, "replacement": {"type": "string"}, "insertion": {"type": "string"},
    "citations": {"type": "array", "items": {"type": "object", "properties": {
        "evidence_id": {"type": "string"}, "quote": {"type": "string"}}, "required": ["evidence_id", "quote"]}}},
    "required": ["answer", "citations"]}


class ChatError(ValueError):
    pass


@dataclass
class ChatRequest:
    question: str
    selection: str
    wrap: tuple[str, str] | None = None  # the selection's own \\lr{ ... }, kept out of the model's text
    citation_request: assist.Request | None = None
    task: str = "write_chat"
    messages: list[dict[str, str]] = field(default_factory=list)
    packet: EvidencePacket | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    pad: tuple[bool, bool] = (False, False)  # a newline before / after text inserted at the cursor
    context: str = ""  # the lines around the cursor (no selection): their numbers and commands are the author's


def prepare(conn: sqlite3.Connection, doc: Document, question: str, selection: str = "", context: str = "",
            history: list[dict[str, str]] | None = None, pad_before: bool = False,
            pad_after: bool = False, gateway: ModelGateway | None = None) -> ChatRequest:
    """Build what would be sent: the question, the selection (or the lines around the cursor), the last few
    messages and the project's evidence for the question."""
    question = question.strip()
    if not question:
        raise ChatError("write a question")
    if len(question) > MAX_QUESTION:
        raise ChatError(f"the question is longer than {MAX_QUESTION} characters")
    if len(selection) > MAX_SELECTION:
        raise ChatError(f"select at most {MAX_SELECTION} characters")
    context = "" if selection else context[:MAX_CONTEXT]
    turns = [(str(t.get("role")), str(t.get("text") or "")[:MAX_TURN]) for t in (history or [])
             if isinstance(t, dict) and str(t.get("text") or "").strip()][-MAX_HISTORY:]
    while turns and turns[0][0] != "user":  # start with the author's turn
        turns.pop(0)
    query = search_query(conn, gateway, assist._plain(question + " " + selection), sensitive=True)
    hits = retrieve_for_question(conn, query, doc.project_id, k=8, max_per_paper=2)
    spans = record_spans(conn, hits) if hits else []
    papers = to_packet(spans, sensitive=True, max_chars_per_item=1500) if spans else None
    req = ChatRequest(question=question, selection=selection, evidence={s.id: s for s in spans},
                      wrap=assist.lr_wrapper(selection) if selection else None,
                      pad=(False, False) if selection else (pad_before, pad_after), context=context)
    shown = selection[len(req.wrap[0]):len(selection) - len(req.wrap[1])] if req.wrap else selection
    req.messages = write_chat_messages(question, shown, context, turns, papers, spans)
    req.citation_request = assist.Request(action="chat", text=selection, task=req.task, evidence=req.evidence)
    # The draft is unpublished: marked sensitive, so a cloud route always needs the author's approval.
    manuscript = EvidenceItem(id="selection", paper_id="manuscript", text=(selection or context or question), page=1)
    req.packet = EvidencePacket(items=[manuscript, *(papers.items if papers else [])], sensitive=True)
    return req


def schema_for(req: ChatRequest) -> dict[str, Any]:
    """With a selection the reply must carry a replacement field (empty for a plain question), so a model that
    rewrites the text cannot leave it only in the answer."""
    if not req.selection:
        return _SCHEMA
    # Models write the fields in this order; with replacement first, qwen3:8b put the new text there instead of
    # in the answer (real run, 2026-10-19).
    props: dict[str, Any] = _SCHEMA["properties"]  # type: ignore[assignment]
    ordered = {"replacement": props["replacement"], "answer": props["answer"], "citations": props["citations"]}
    return {**_SCHEMA, "properties": ordered, "required": ["replacement", *_SCHEMA["required"]]}


def preview(gateway: ModelGateway, req: ChatRequest) -> CallPreview:
    assert req.packet is not None
    return gateway.preview(req.task, req.packet, req.messages, None, _cap(req), schema=schema_for(req))


def _cap(req: ChatRequest) -> int:
    # Without a selection the reply may carry a whole table or figure to insert (review: 1500 cut it off).
    return min(8192, 1500 + len(req.selection)) if req.selection else 2500


def run(conn: sqlite3.Connection, gateway: ModelGateway, doc: Document, req: ChatRequest, file: str,
        approved: bool = False) -> dict[str, Any]:
    assert req.packet is not None
    reply = gateway.call(req.task, req.packet, req.messages, schema_for(req), approved=approved,
                         max_output_tokens=_cap(req), prompt_version=WRITE_PROMPT_VERSION)
    content = reply.content
    verified, rejected = assist.verify_quotes(conn, req.evidence, content.get("citations") or [])
    flags = [f"{rejected} quote(s) removed: not found in the evidence"] if rejected else []
    suggestion = None
    replacement = str(content.get("replacement") or "")
    insertion = str(content.get("insertion") or "")
    if not req.selection and insertion.strip():
        # Nothing selected: text the author asked to add (a table, an equation) goes in at the cursor. The text
        # around the cursor and common LaTeX commands tell the escape repair what "\n" + "eq" was ("\neq").
        edit = assist.Request(action="insert", text="", task=req.task, evidence=req.evidence, pad=req.pad,
                              context=req.context + " " + _INSERT_COMMANDS)
        assert req.citation_request is not None
        insertion = assist.repair_escapes(insertion, edit.context)
        insertion, cited, dropped = assist._resolve_citations(conn, req.citation_request, insertion,
                                                              content.get("citations") or [])
        if dropped:
            flags.append(f"{dropped} citation(s) left out: their quote was not found in the evidence")
        try:
            suggestion = assist.suggestion_from(conn, doc, edit, {"replacement": insertion, "note": ""},
                                                f"{reply.provider}:{reply.model}", file, citations_in=cited)
        except assist.AssistError:  # nothing left to insert: the answer still stands
            flags.append("nothing to insert: the proposed text was empty once unverified citations were removed")
    if req.selection and replacement.strip() and replacement.strip() != req.selection.strip():
        # An English \\lr{...} passage translated into Persian loses the wrapper, as in the Translate action.
        persian = bool(assist._PERSIAN.search(replacement)) and not assist._PERSIAN.search(req.selection)
        edit = assist.Request(action="translate-fa" if persian else "chat", text=req.selection, task=req.task,
                              evidence=req.evidence, wrap=req.wrap)
        # \\cite{evidence id} in the edit becomes the paper's key when its quote was verified, so accepting it
        # also registers the paper for references.bib.
        assert req.citation_request is not None
        replacement, cited, dropped = assist._resolve_citations(conn, req.citation_request, replacement,
                                                                content.get("citations") or [])
        if dropped:
            flags.append(f"{dropped} citation(s) left out of the edit: their quote was not found in the evidence")
        suggestion = assist.suggestion_from(conn, doc, edit, {"replacement": replacement, "note": ""},
                                            f"{reply.provider}:{reply.model}", file, citations_in=cited)
    return {"answer": str(content.get("answer") or "")[:6000], "citations": list(verified.values()),
            "flags": flags, "suggestion": suggestion, "model": reply.model, "provider": reply.provider}
