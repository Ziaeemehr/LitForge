"""Three model actions on a selection: fix a compile error, rewrite, suggest citations. Nothing is applied here:
each result is a list of hunks the user accepts or rejects, with guard flags computed before it is shown."""

from __future__ import annotations

import difflib
import html
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from litforge.analysis.gateway import CallPreview, ModelGateway
from litforge.analysis.ledger import record_spans, to_packet
from litforge.analysis.prompts import (
    WRITE_PROMPT_VERSION,
    write_cite_messages,
    write_fix_messages,
    write_rewrite_messages,
)
from litforge.analysis.retrieve import retrieve_for_question
from litforge.analysis.validation import QuoteLocator, numbers
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.storage.db import dumps, new_id, now
from litforge.writing.bib import citation_key, paper_for_key
from litforge.writing.cite import cited_keys
from litforge.writing.documents import Document, create_snapshot

ACTIONS = {"fix": "write_fix", "clearer": "write_rewrite", "shorter": "write_rewrite",
           "translate-en": "write_rewrite", "translate-fa": "write_rewrite", "cite": "write_cite"}
MAX_SELECTION = 12_000
_REPLACEMENT = {"type": "object", "properties": {"replacement": {"type": "string"}, "note": {"type": "string"}},
                "required": ["replacement"]}
_CITED = {"type": "object", "properties": {
    "text": {"type": "string"}, "note": {"type": "string"},
    "citations": {"type": "array", "items": {"type": "object", "properties": {
        "evidence_id": {"type": "string"}, "quote": {"type": "string"}}, "required": ["evidence_id", "quote"]}}},
    "required": ["text", "citations"]}
_COMMAND = re.compile(r"\\([a-zA-Z]+)")
_ARG_COMMANDS = re.compile(r"\\(ref|eqref|autoref|cref|Cref|label|pageref)\*?\{([^}]*)\}")
_KEYED = re.compile(r"\\[a-zA-Z]*(?:cite|ref|label)[a-zA-Z]*\*?(?:\[[^\]]*\])*\{[^}]*\}")
_MATH = re.compile(r"\$\$.+?\$\$|\$[^$]+\$|\\\(.+?\\\)|\\\[.+?\\\]", re.S)


class AssistError(ValueError):
    pass


@dataclass
class Request:
    action: str
    text: str
    error: str = ""
    first_line: int = 1
    task: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    packet: EvidencePacket | None = None
    schema: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)  # evidence id -> EvidenceSpan for citations


def prepare(conn: sqlite3.Connection, doc: Document, action: str, text: str, error: str = "",
            first_line: int = 1) -> Request:
    """Build what would be sent; only the selection (and, for citations, the retrieved evidence) is included."""
    if action not in ACTIONS:
        raise AssistError(f"unknown action; use one of {', '.join(ACTIONS)}")
    if not text.strip():
        raise AssistError("select some text first")
    if len(text) > MAX_SELECTION:
        raise AssistError(f"select at most {MAX_SELECTION} characters")
    req = Request(action=action, text=text, error=error, first_line=first_line, task=ACTIONS[action])
    # The user's draft is unpublished work: mark it sensitive so a cloud route always needs approval.
    selection = EvidenceItem(id="selection", paper_id="manuscript", text=text, page=1)
    if action == "fix":
        if not error.strip():
            raise AssistError("choose a compile error to fix")
        req.messages, req.schema = write_fix_messages(error[:2000], text, first_line), _REPLACEMENT
        req.packet = EvidencePacket(items=[selection], sensitive=True)
    elif action == "cite":
        hits = retrieve_for_question(conn, _plain(text), doc.project_id, k=8, max_per_paper=2)
        if not hits:
            raise AssistError("no evidence in this project's papers matches the selection")
        spans = record_spans(conn, hits)
        packet = to_packet(spans, sensitive=True, max_chars_per_item=1500)
        req.messages, req.schema = write_cite_messages(text, packet, spans), _CITED
        req.packet = EvidencePacket(items=[selection, *packet.items], sensitive=True)
        req.evidence = {s.id: s for s in spans}
    else:
        req.messages, req.schema = write_rewrite_messages(text, action), _REPLACEMENT
        req.packet = EvidencePacket(items=[selection], sensitive=True)
    return req


def _plain(text: str) -> str:
    return re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", " ", re.sub(r"(?<!\\)%.*", "", text))


def preview(gateway: ModelGateway, req: Request, profile: str | None = None) -> CallPreview:
    assert req.packet is not None
    return gateway.preview(req.task, req.packet, req.messages, profile, schema=req.schema)


def run(conn: sqlite3.Connection, gateway: ModelGateway, doc: Document, req: Request, file: str,
        approved: bool = False, profile: str | None = None) -> dict[str, Any]:
    assert req.packet is not None
    reply = gateway.call(req.task, req.packet, req.messages, req.schema, profile=profile, approved=approved,
                         prompt_version=WRITE_PROMPT_VERSION)
    content = reply.content
    citations: list[dict[str, Any]] = []
    flags: list[str] = []
    if req.action == "cite":
        new_text, citations, dropped = _resolve_citations(conn, req, str(content.get("text") or ""),
                                                          content.get("citations") or [])
        if dropped:
            flags.append(f"{dropped} citation(s) removed: their quote was not found in the evidence")
    else:
        new_text = str(content.get("replacement") or "")
    if not new_text.strip():
        raise AssistError("the model returned no text")
    allowed = _numbers(req.text).union(*(numbers(span.text) for span in req.evidence.values()))
    known = set(cited_keys(req.text)) | {c["key"] for c in citations}
    flags += latex_flags(req.text, new_text)
    hunks = [_flag_hunk(h, allowed, known, conn) for h in diff_hunks(req.text, new_text)]
    suggestion_id = new_id()
    conn.execute(
        """INSERT INTO writing_suggestions(id, document_id, task, model, prompt_version, file, original, hunks_json,
           flags_json, citations_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
        (suggestion_id, doc.id, req.task, f"{reply.provider}:{reply.model}", WRITE_PROMPT_VERSION, file, req.text,
         dumps(hunks), dumps(flags), dumps(citations), now()))
    return {"id": suggestion_id, "task": req.task, "action": req.action, "model": reply.model,
            "provider": reply.provider, "note": str(content.get("note") or "")[:500], "original": req.text,
            "hunks": hunks, "flags": flags, "citations": citations}


def _resolve_citations(conn: sqlite3.Connection, req: Request, text: str,
                       raw: list[Any]) -> tuple[str, list[dict[str, Any]], int]:
    """Keep only citations whose quote is really in the evidence; turn evidence ids into citation keys."""
    verified: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        evidence_id, quote = str(item.get("evidence_id") or ""), html.unescape(str(item.get("quote") or ""))
        span = req.evidence.get(evidence_id)
        if span is None or evidence_id in verified or len(quote.split()) < 2:
            continue
        located = QuoteLocator(span.text).locate(quote)
        if located is None:
            continue
        verified[evidence_id] = {"evidence_id": evidence_id, "paper_id": span.paper_id,
                                 "key": citation_key(conn, span.paper_id), "page": span.page,
                                 "title": span.title, "quote": span.text[located[0]:located[1]]}
    dropped = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal dropped
        keys = []
        for part in match.group(2).split(","):
            part = part.strip()
            if part in verified:
                keys.append(verified[part]["key"])
            elif part in req.evidence or part.startswith("ev_"):
                dropped += 1
            elif part:
                keys.append(part)  # a key the user already had
        keys = list(dict.fromkeys(keys))
        return f"{match.group(1)}{{{','.join(keys)}}}" if keys else ""

    out = re.sub(r"(\\[a-zA-Z]*cite[a-zA-Z]*\*?(?:\[[^\]]*\])*)\{([^}]*)\}", replace, text)
    used = {c for c in cited_keys(out)}
    return out, [c for c in verified.values() if c["key"] in used], dropped


def diff_hunks(old: str, new: str) -> list[dict[str, Any]]:
    """Line hunks (word hunks for a one-line text), with character offsets into `old`."""
    split = (lambda t: re.findall(r"\S+|\s+", t)) if "\n" not in old.strip() else (
        lambda t: t.splitlines(keepends=True))
    a, b = split(old), split(new)
    offsets = [0]
    for piece in a:
        offsets.append(offsets[-1] + len(piece))
    hunks: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        hunks.append({"index": len(hunks), "start": offsets[i1], "end": offsets[i2], "old": "".join(a[i1:i2]),
                      "new": "".join(b[j1:j2]), "flags": []})
    return _merge_close(hunks, old)


def _merge_close(hunks: list[dict[str, Any]], old: str) -> list[dict[str, Any]]:
    # Word diffs of a rewritten sentence would give dozens of tiny hunks; join those separated by one space.
    merged: list[dict[str, Any]] = []
    for hunk in hunks:
        if merged and hunk["start"] - merged[-1]["end"] <= 1:
            gap = old[merged[-1]["end"]:hunk["start"]]
            prev = merged[-1]
            prev["old"] += gap + hunk["old"]
            prev["new"] += gap + hunk["new"]
            prev["end"] = hunk["end"]
        else:
            merged.append(dict(hunk))
    for i, hunk in enumerate(merged):
        hunk["index"] = i
    return merged


def _flag_hunk(hunk: dict[str, Any], allowed: set[Any], known: set[str], conn: sqlite3.Connection) -> dict[str, Any]:
    flags = []
    invented = sorted(str(n) for n in _numbers(hunk["new"]) - allowed)
    if invented:
        flags.append("number not in your text or the evidence: " + ", ".join(invented[:5]))
    unknown = [k for k in cited_keys(hunk["new"]) if k not in known and paper_for_key(conn, k) is None]
    unbacked = [k for k in cited_keys(hunk["new"]) if k not in known and k not in unknown]
    if unknown:
        flags.append("unknown citation key: " + ", ".join(unknown[:5]))
    if unbacked:
        flags.append("citation not backed by returned evidence: " + ", ".join(unbacked[:5]))
    hunk["flags"] = flags
    return hunk


def _numbers(text: str) -> set[Any]:
    """Numbers in the prose, not in citation keys, labels or reference names."""
    return numbers(_KEYED.sub(" ", text))


def latex_flags(old: str, new: str) -> list[str]:
    """LaTeX the suggestion dropped: commands (by count), math, and \\cite/\\ref/\\label arguments."""
    flags = []
    missing = Counter(_COMMAND.findall(old)) - Counter(_COMMAND.findall(new))
    missing.pop("cite", None)  # cite keys are checked by name below
    if missing:
        flags.append("LaTeX commands dropped: " + ", ".join(f"\\{c}" for c in sorted(missing)[:8]))
    old_math = Counter(m.strip() for m in _MATH.findall(old))
    if old_math - Counter(m.strip() for m in _MATH.findall(new)):
        flags.append("math changed or dropped")
    lost_keys = set(cited_keys(old)) - set(cited_keys(new))
    if lost_keys:
        flags.append("citations dropped: " + ", ".join(sorted(lost_keys)[:8]))
    lost_refs = {m.group(0) for m in _ARG_COMMANDS.finditer(old)} - {m.group(0) for m in _ARG_COMMANDS.finditer(new)}
    if lost_refs:
        flags.append("references or labels dropped: " + ", ".join(sorted(lost_refs)[:8]))
    return flags


def decide(conn: sqlite3.Connection, doc: Document, suggestion_id: str, accepted: list[int]) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM writing_suggestions WHERE id=? AND document_id=?",
                       (suggestion_id, doc.id)).fetchone()
    if row is None:
        raise LookupError("suggestion not found")
    if row["status"] != "pending":
        raise AssistError("this suggestion was already decided")
    total = len(json.loads(row["hunks_json"]))
    accepted = sorted({int(i) for i in accepted if 0 <= int(i) < total})
    status = "rejected" if not accepted else "accepted" if len(accepted) == total else "partial"
    conn.execute("UPDATE writing_suggestions SET status=?, accepted_json=?, decided_at=? WHERE id=?",
                 (status, dumps(accepted), now(), suggestion_id))
    if accepted:
        create_snapshot(doc, f"after {row['task']} suggestion {suggestion_id[:8]} ({len(accepted)}/{total} accepted, "
                             f"{row['model']})")
    return {"status": status, "accepted": accepted}


def applied_text(original: str, hunks: list[dict[str, Any]], accepted: list[int]) -> str:
    """The original with only the accepted hunks applied (the page does the same before saving)."""
    out, pos = [], 0
    for hunk in sorted(hunks, key=lambda h: h["start"]):
        out.append(original[pos:hunk["start"]])
        out.append(hunk["new"] if hunk["index"] in accepted else hunk["old"])
        pos = hunk["end"]
    out.append(original[pos:])
    return "".join(out)

