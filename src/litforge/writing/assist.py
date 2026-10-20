"""Model actions in the editor: fix a compile error, rewrite or translate a selection, suggest citations, propose
a title or an abstract from the document, and LaTeX for an equation or table from an image. Nothing is applied
here: each result is a list of hunks the user accepts or rejects, with guard flags computed before it is shown."""

from __future__ import annotations

import base64
import binascii
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
    write_image_messages,
    write_rewrite_messages,
    write_summary_messages,
)
from litforge.analysis.retrieve import retrieve_for_question
from litforge.analysis.validation import QuoteLocator, numbers
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.storage.db import dumps, new_id, now
from litforge.writing.bib import citation_key, paper_for_key
from litforge.writing.cite import cited_keys
from litforge.writing.documents import Document, create_snapshot

ACTIONS = {"fix": "write_fix", "clearer": "write_rewrite", "shorter": "write_rewrite",
           "translate-en": "write_rewrite", "translate-fa": "write_rewrite", "cite": "write_cite",
           "title": "write_summary", "abstract": "write_summary",
           "equation-image": "write_image", "table-image": "write_image"}
SUMMARY = {"title", "abstract"}  # the text is the current title or abstract; the document comes as context
IMAGE = {"equation-image", "table-image"}  # no text: the result is inserted at the cursor
MAX_SELECTION = 12_000
MAX_CONTEXT = 24_000  # characters of the document read for a title or an abstract (about 6000 tokens)
MAX_IMAGE = 8 * 1024 * 1024
MAX_PIXELS = 40_000_000  # a full-page scan at 600 dpi is about 35 megapixels
_IMAGE_TYPES = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
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
_CITE_COMMANDS = r"\cite \citep \citet \textcite \parencite \autocite \footcite"  # what the cite action may insert
_CITE_ONLY = re.compile(r"\\[a-zA-Z]*cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{[^}]*\}")
_MATH = re.compile(r"\$\$.+?\$\$|\$[^$]+\$|\\\(.+?\\\)|\\\[.+?\\\]", re.S)


class AssistError(ValueError):
    pass


@dataclass
class Request:
    action: str
    text: str
    error: str = ""
    first_line: int = 1
    error_line: int = 0  # 1-based line of the error inside the excerpt, for "fix"
    wrap: tuple[str, str] | None = None  # the selection's own "\\lr{" ... "}", kept out of the model's text
    markers: list[str] = field(default_factory=list)  # \\cite/\\ref/\\label and math behind ⟦1⟧, ⟦2⟧ ... (rewrite)
    task: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
    packet: EvidencePacket | None = None
    schema: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)  # evidence id -> EvidenceSpan for citations
    context: str = ""  # the document, for a title or an abstract (numbers in it are allowed)
    images: list[bytes] = field(default_factory=list)
    pad: tuple[bool, bool] = (False, False)  # a newline before / after an inserted block
    new_env: bool = False  # the abstract is new: its text comes inside \\begin{abstract} ... \\end{abstract}


def prepare(conn: sqlite3.Connection, doc: Document, action: str, text: str, error: str = "",
            first_line: int = 1, error_line: int = 0, *, context: str = "", image: bytes | None = None,
            pad_before: bool = False, pad_after: bool = False, new_env: bool = False) -> Request:
    """Build what would be sent; only the selection (and, for citations, the retrieved evidence; for a title or
    an abstract, the document; for an image action, the image) is included."""
    if action not in ACTIONS:
        raise AssistError(f"unknown action; use one of {', '.join(ACTIONS)}")
    if action in IMAGE:
        return _prepare_image(action, image or b"", pad_before, pad_after)
    if new_env and action != "abstract":
        raise AssistError("only an abstract can be inserted as a new environment")
    if not text.strip() and action not in SUMMARY:
        raise AssistError("select some text first")
    if len(text) > MAX_SELECTION:
        raise AssistError(f"select at most {MAX_SELECTION} characters")
    req = Request(action=action, text=text, error=error, first_line=first_line, task=ACTIONS[action],
                  error_line=max(0, error_line - first_line + 1) if error_line else 0,
                  new_env=new_env, pad=(pad_before, pad_after) if new_env else (False, False))
    if action not in {"fix", *SUMMARY}:
        req.wrap = lr_wrapper(text)
    sent = model_text(req)
    # The user's draft is unpublished work: mark it sensitive so a cloud route always needs approval.
    selection = EvidenceItem(id="selection", paper_id="manuscript", text=text or "(empty)", page=1)
    if action == "fix":
        if not error.strip():
            raise AssistError("choose a compile error to fix")
        lines = text.split("\n")
        line = lines[req.error_line - 1] if 0 < req.error_line <= len(lines) else ""
        req.messages, req.schema = write_fix_messages(error[:2000], text, line), _REPLACEMENT
        req.packet = EvidencePacket(items=[selection], sensitive=True)
    elif action in SUMMARY:
        body = context[:MAX_CONTEXT]
        if not re.sub(r"\\[a-zA-Z]+\*?|[{}\[\]\s]", "", body):
            raise AssistError("the document has no text yet to write a " + action + " from")
        req.context = body
        # Math and citations behind markers, as for a rewrite: qwen3:8b wrote $\\backslash dot{x}$ for the
        # Persian document's $\\dot{x}$ (real run, 2026-10-20).
        masked, req.markers = protect(body)
        req.messages, req.schema = write_summary_messages(action, text, masked), _REPLACEMENT
        req.packet = EvidencePacket(items=[selection, EvidenceItem(id="document", paper_id="manuscript", text=body,
                                                                   page=1)], sensitive=True)
    elif action == "cite":
        hits = retrieve_for_question(conn, _plain(sent), doc.project_id, k=8, max_per_paper=2)
        if not hits:
            hint = ("; evidence is found by the papers' own words, so Persian text rarely matches English papers"
                    if _PERSIAN.search(sent) else "")
            raise AssistError("no evidence in this project's papers matches the selection" + hint)
        spans = record_spans(conn, hits)
        packet = to_packet(spans, sensitive=True, max_chars_per_item=1500)
        req.messages, req.schema = write_cite_messages(sent, packet, spans), _CITED
        req.packet = EvidencePacket(items=[selection, *packet.items], sensitive=True)
        req.evidence = {s.id: s for s in spans}
    else:
        masked, req.markers = protect(sent)
        req.messages, req.schema = write_rewrite_messages(masked, action), _REPLACEMENT
        req.packet = EvidencePacket(items=[selection], sensitive=True)
    return _with_schema(req)


def _with_schema(req: Request) -> Request:
    # The schema in words, in the first system message: without it gemma4 looped on fixes until the token
    # limit; with it the reply was valid every time (real run, 2026-10-19). Ollama's format option alone was not enough.
    req.messages[0] = {**req.messages[0], "content": req.messages[0]["content"] +
                       " Return only valid JSON for this schema: " + json.dumps(req.schema, sort_keys=True)}
    return req


def decode_image(data: str) -> bytes:
    """An image sent by the page: a data URL (pasted or chosen file) or plain base64."""
    if not data:
        return b""
    payload = data.split(",", 1)[1] if data.startswith("data:") else data
    if len(payload) > MAX_IMAGE * 4 // 3 + 4:  # refused before decoding: base64 is 4 characters per 3 bytes
        raise AssistError(f"the image is too large: at most {MAX_IMAGE // (1024 * 1024)} MB")
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AssistError("the image could not be read") from exc


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from the header, or None for a damaged or truncated image. Read without decoding, so a
    small file declaring enormous dimensions is refused before any decoder sees it."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            if data[12:16] != b"IHDR" or not data.endswith(b"IEND\xaeB`\x82"):
                return None
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data.startswith(b"\xff\xd8"):
            pos = 2
            while pos + 9 < len(data):
                if data[pos] != 0xFF:
                    return None
                marker, length = data[pos + 1], int.from_bytes(data[pos + 2:pos + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    if b"\xff\xd9" not in data[pos:]:
                        return None
                    return int.from_bytes(data[pos + 7:pos + 9], "big"), int.from_bytes(data[pos + 5:pos + 7], "big")
                pos += 2 + length
            return None
        if data[:6] in (b"GIF87a", b"GIF89a"):
            if not data.rstrip(b"\0").endswith(b";"):
                return None
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if kind == b"VP8 ":
                return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
            if kind == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    except (IndexError, ValueError):
        return None
    return None


def _prepare_image(action: str, image: bytes, pad_before: bool, pad_after: bool) -> Request:
    if not image:
        raise AssistError("paste or choose an image first")
    if len(image) > MAX_IMAGE:
        raise AssistError(f"the image is too large: at most {MAX_IMAGE // (1024 * 1024)} MB")
    if not image.startswith(_IMAGE_TYPES) and not (image[:4] == b"RIFF" and image[8:12] == b"WEBP"):
        raise AssistError("use a PNG, JPEG, GIF or WebP image")
    size = image_size(image)
    if size is None:
        raise AssistError("the image could not be read: it is damaged or incomplete")
    if size[0] * size[1] > MAX_PIXELS:
        raise AssistError(f"the image has too many pixels ({size[0]}×{size[1]}); crop or shrink it")
    # Sensitive: a screenshot may come from unpublished work, so a cloud route always needs approval.
    item = EvidenceItem(id="image", paper_id="manuscript", text="(image)", page=1)
    req = Request(action=action, text="", task=ACTIONS[action], images=[image], pad=(pad_before, pad_after),
                  messages=write_image_messages(action), schema=_REPLACEMENT,
                  packet=EvidencePacket(items=[item], sensitive=True))
    return _with_schema(req)


_PERSIAN = re.compile(r"[\u0600-\u06FF]")


def lr_wrapper(text: str) -> tuple[str, str] | None:
    """When the whole selection is one \\lr{...} (an English passage in a Persian document), return the parts
    around its content. The model then sees only the English text; models tended to drop the wrapper."""
    stripped = text.strip()
    if not stripped.startswith("\\lr{") or not stripped.endswith("}"):
        return None
    depth = 0
    for i, ch in enumerate(stripped[3:], 3):
        if ch == "{" and stripped[i - 1] != "\\":
            depth += 1
        elif ch == "}" and stripped[i - 1] != "\\":
            depth -= 1
            if depth == 0 and i != len(stripped) - 1:
                return None  # the first group closes early: "\\lr{a} b \\lr{c}"
    if depth != 0:
        return None
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return lead + "\\lr{", "}" + trail


_MARKER = re.compile(r"\u27e6\s*(\d+)\s*\u27e7")


def protect(text: str) -> tuple[str, list[str]]:
    """Put citations, references, labels and math behind numbered markers, so a rewrite cannot alter them.
    Models garbled \\cite (gemma4 wrote \\text{\\textbackslash cite{...}}) and rewrapped math in the real run."""
    found = sorted({(m.start(), m.end()) for pattern in (_KEYED, _MATH) for m in pattern.finditer(text)})
    spans: list[tuple[int, int]] = []
    for start, end in found:  # drop spans inside an earlier one (a \\ref inside $...$)
        if not spans or start >= spans[-1][1]:
            spans.append((start, end))
    out, markers, pos = [], [], 0
    for start, end in spans:
        markers.append(text[start:end])
        out.append(text[pos:start] + f"\u27e6{len(markers)}\u27e7")
        pos = end
    return "".join(out) + text[pos:], markers


def restore(text: str, markers: list[str]) -> str:
    """Put the originals back; a marker the model invented stays visible (and is flagged)."""
    def back(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return markers[index] if 0 <= index < len(markers) else match.group(0)

    return _MARKER.sub(back, text)


def output_cap(req: Request) -> int:
    """The reply repeats the whole selection, so its token budget grows with it (about a token per character
    for Persian). The flat 1024 default cut gemma4's fixes off mid-string in the real run."""
    if req.action in IMAGE:
        return 2048  # a table of a few dozen cells; a typed table is rarely longer
    return min(8192, 1024 + len(req.text) + (1024 if req.action == "cite" else 0) +
               (512 if req.action == "abstract" else 0))


def model_text(req: Request) -> str:
    return req.text[len(req.wrap[0]):len(req.text) - len(req.wrap[1])] if req.wrap else req.text


def _plain(text: str) -> str:
    return re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?", " ", re.sub(r"(?<!\\)%.*", "", text))


def preview(gateway: ModelGateway, req: Request, profile: str | None = None) -> CallPreview:
    assert req.packet is not None
    if req.images:
        return gateway.preview_vision(req.task, req.packet, req.messages, profile, output_cap(req),
                                      image_count=len(req.images), schema=req.schema)
    return gateway.preview(req.task, req.packet, req.messages, profile, output_cap(req), schema=req.schema)


def run(conn: sqlite3.Connection, gateway: ModelGateway, doc: Document, req: Request, file: str,
        approved: bool = False, profile: str | None = None) -> dict[str, Any]:
    assert req.packet is not None
    if req.images:
        reply = gateway.call_vision(req.task, req.packet, req.messages, req.schema, req.images, profile=profile,
                                    approved=approved, max_output_tokens=output_cap(req),
                                    prompt_version=WRITE_PROMPT_VERSION)
    else:
        reply = gateway.call(req.task, req.packet, req.messages, req.schema, profile=profile, approved=approved,
                             max_output_tokens=output_cap(req), prompt_version=WRITE_PROMPT_VERSION)
    return suggestion_from(conn, doc, req, reply.content, f"{reply.provider}:{reply.model}", file)


def suggestion_from(conn: sqlite3.Connection, doc: Document, req: Request, content: dict[str, Any], model: str,
                    file: str, citations_in: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Turn a model reply into hunks with guard flags, recorded as a pending suggestion. Shared with the
    assistant chat, whose edits go through the same checks."""
    citations: list[dict[str, Any]] = []
    flags: list[str] = []
    sent = model_text(req)
    if req.action == "cite":
        # Repair JSON escapes first ("\textcite" arrives as a tab + "extcite"), or the evidence ids in it are
        # never turned into keys (real run).
        reply = repair_escapes(str(content.get("text") or ""), sent + " " + req.context + " " + _CITE_COMMANDS)
        new_text, citations, dropped = _resolve_citations(conn, req, reply, content.get("citations") or [])
        if dropped:
            flags.append(f"{dropped} citation(s) removed: their quote was not found in the evidence")
    else:
        new_text = restore(str(content.get("replacement") or ""), req.markers)
        citations = list(citations_in or [])
        if _MARKER.search(new_text):
            flags.append("the model invented markers that stand for nothing")
    if req.action in IMAGE:  # nothing to compare with: read escapes against common LaTeX math commands
        new_text = layout_latex(repair_image_escapes(new_text))
        flags.append("transcribed from an image: check every symbol and number against it")
    else:
        new_text = _keep_edges(sent, repair_escapes(new_text, sent + " " + req.context))
    if not new_text.strip():
        raise AssistError("the model returned no text")
    sent_new = new_text
    if req.new_env:
        new_text = "\\begin{abstract}\n" + new_text.strip() + "\n\\end{abstract}"
    if req.action in IMAGE or req.new_env or req.action == "insert":
        new_text = ("\n" if req.pad[0] else "") + new_text + ("\n" if req.pad[1] else "")
    if req.wrap:  # put the \\lr{...} back, except around a Persian translation, which must not be in it
        lead, trail = req.wrap[0][:-4], req.wrap[1][1:]
        new_text = lead + new_text + trail if req.action == "translate-fa" else req.wrap[0] + new_text + req.wrap[1]
    allowed = _numbers(req.text + " " + req.context).union(*(numbers(span.text) for span in req.evidence.values()))
    if req.action in IMAGE:
        allowed |= _numbers(new_text)  # the numbers come from the image, which only the user can compare
    if req.action == "insert":  # an added table or list may number its rows and columns (real run: "Col 1")
        allowed |= {n for n in _numbers(new_text) if n == n.to_integral_value() and 0 <= n <= 20}
    known = set(cited_keys(req.text)) | {c["key"] for c in citations}
    exempt: set[str] | None = None
    if req.action in {"fix", "insert", *IMAGE}:  # new LaTeX is the point of an insertion
        exempt = set()
    if req.action == "fix" and req.error_line:  # the broken command on the error line is meant to change
        lines = req.text.split("\n")
        exempt = set(_COMMAND.findall(lines[req.error_line - 1])) if req.error_line <= len(lines) else set()
    # A title or an abstract may use any command the document uses (its math, \\lr{...}).
    flags += latex_flags(sent, sent_new, exempt, set(_COMMAND.findall(req.context)))
    hunks = [_flag_hunk(h, allowed, known, conn) for h in diff_hunks(req.text, new_text)]
    if req.action == "cite":
        for hunk in hunks:
            if re.sub(r"\s+", "", _CITE_ONLY.sub("", hunk["new"])) != re.sub(r"\s+", "", hunk["old"]):
                hunk["flags"].append("wording changed: this action should only insert citations")
    suggestion_id = new_id()
    conn.execute(
        """INSERT INTO writing_suggestions(id, document_id, task, model, prompt_version, file, original, hunks_json,
           flags_json, citations_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?)""",
        (suggestion_id, doc.id, req.task, model, WRITE_PROMPT_VERSION, file, req.text,
         dumps(hunks), dumps(flags), dumps(citations), now()))
    provider, _, model_name = model.partition(":")
    return {"id": suggestion_id, "task": req.task, "action": req.action, "model": model_name,
            "provider": provider, "note": str(content.get("note") or "")[:500], "original": req.text,
            "hunks": hunks, "flags": flags, "citations": citations}


def verify_quotes(conn: sqlite3.Connection, evidence: dict[str, Any],
                  raw: list[Any]) -> tuple[dict[str, dict[str, Any]], int]:
    """The citations whose quote really is in their evidence span (by evidence id), and how many were not."""
    verified: dict[str, dict[str, Any]] = {}
    rejected = 0
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        evidence_id, quote = str(item.get("evidence_id") or ""), html.unescape(str(item.get("quote") or ""))
        span = evidence.get(evidence_id)
        if evidence_id in verified:
            continue
        located = QuoteLocator(span.text).locate(quote) if span is not None and len(quote.split()) >= 2 else None
        if span is None or located is None:
            rejected += 1
            continue
        verified[evidence_id] = {"evidence_id": evidence_id, "paper_id": span.paper_id,
                                 "key": citation_key(conn, span.paper_id), "page": span.page,
                                 "title": span.title, "quote": span.text[located[0]:located[1]]}
    return verified, rejected


def _resolve_citations(conn: sqlite3.Connection, req: Request, text: str,
                       raw: list[Any]) -> tuple[str, list[dict[str, Any]], int]:
    """Keep only citations whose quote is really in the evidence; turn evidence ids into citation keys."""
    verified, _ = verify_quotes(conn, req.evidence, raw)
    dropped = 0
    # Only citation commands the document already uses: \textcite without biblatex does not compile (real run).
    usable = set(_COMMAND.findall(req.text + " " + req.context)) | {"cite"}
    # "\\textcite" is a line break and the word "textcite": the model escaped its backslash twice (real run).
    text = re.sub(r"(?<!\\)\\\\(?=[a-zA-Z]*cite[a-zA-Z]*\*?[\[{])", r"\\", text)

    def replace(match: re.Match[str]) -> str:
        nonlocal dropped
        command = match.group(1)
        name = re.match(r"\\([a-zA-Z]*)", command).group(1)  # type: ignore[union-attr]
        if name not in usable:
            command = "\\cite" + command[len(name) + 1:]
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
        return f"{command}{{{','.join(keys)}}}" if keys else ""

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


_LAYOUT = re.compile(r"(?P<br>\\\\(?:\[[^\]]*\])?)|(?P<begin>\\begin\{(?P<env>[^}]*)\}(?:\[[^\]]*\])?)|"
                     r"(?P<end>\\end\{(?P<endenv>[^}]*)\})|(?P<open>\\\[)|(?P<close>\\\])|"
                     r"(?P<alone>\\(?:hline|centering)(?![a-zA-Z]))|(?P<starts>\\(?:caption|label)(?![a-zA-Z]))")
_INDENTED = {"equation", "align", "gather", "multline", "eqnarray", "split", "tabular", "tabularx", "array", "cases",
             "matrix", "pmatrix", "bmatrix", "vmatrix", "aligned", "gathered"}
_WITH_SPEC = {"tabular", "tabularx", "array"}  # \begin{tabular}{l c} keeps its column spec on its line


def layout_latex(text: str) -> str:
    """One LaTeX block laid out on lines: environments and \[ \] on their own lines, one table or align row per
    line, bodies indented, no blank lines (one inside an equation breaks the compile). The vision model returned
    whole tables on one line and copied the equation number "(1)" (real run). Text with a comment is kept as is:
    joining its lines would comment out the rest."""
    text = text.strip()
    if re.search(r"(?<!\\)%", text):
        return re.sub(r"\n[ \t]*(?=\n)", "", text)  # still no blank line: it would end the equation
    text = re.sub(r"^\(\d+(?:\.\d+)*\)\s*(?=\\begin|\\\[)", "", text)
    text = re.sub(r"(\\end\{[^}]*\}|\\\])\s*\(\d+(?:\.\d+)*\)$", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    lines: list[str] = []
    buffer, depth, pos = "", 0, 0
    group, groups = 0, []  # brace depth at each character: nothing inside \text{...} or \mbox{...} is laid out
    i = 0
    while i < len(text):
        groups.append(group)
        if text[i] == "\\" and i + 1 < len(text):
            groups.append(group)
            i += 2
            continue
        group += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    groups.append(group)

    def flush() -> None:
        nonlocal buffer
        if buffer.strip():
            lines.append("  " * depth + buffer.strip())
        buffer = ""

    for match in _LAYOUT.finditer(text):
        if match.start() < pos or groups[match.start()] > 0:
            continue
        buffer += text[pos:match.start()]
        token, pos = match.group(0), match.end()
        if match.group("br"):
            buffer = buffer.rstrip() + " " + token
            flush()
        elif match.group("begin") or match.group("open"):
            env = (match.group("env") or "").rstrip("*")
            if env in _WITH_SPEC and text[pos:pos + 1] == "{":
                depth_spec, end = 0, pos
                for end in range(pos, len(text)):
                    depth_spec += {"{": 1, "}": -1}.get(text[end], 0)
                    if depth_spec == 0:
                        break
                token, pos = token + text[pos:end + 1], end + 1
            flush()
            lines.append("  " * depth + token)
            depth += 1 if match.group("open") or env in _INDENTED else 0
        elif match.group("end") or match.group("close"):
            flush()
            env = (match.group("endenv") or "").rstrip("*")
            depth = max(0, depth - (1 if match.group("close") or env in _INDENTED else 0))
            lines.append("  " * depth + token)
        elif match.group("alone"):
            flush()
            lines.append("  " * depth + token)
        else:  # \caption, \label: start a line
            flush()
            buffer = token
    buffer += text[pos:]
    flush()
    return "\n".join(lines)


def latex_flags(old: str, new: str, exempt: set[str] | None = None, known: set[str] | None = None) -> list[str]:
    """LaTeX the suggestion dropped: commands (by count), math, and \\cite/\\ref/\\label arguments."""
    flags = []
    missing = Counter(_COMMAND.findall(old)) - Counter(_COMMAND.findall(new))
    for name in {"cite", *(exempt or set())}:  # cite keys are checked by name below
        missing.pop(name, None)
    if missing:
        flags.append("LaTeX commands dropped: " + ", ".join(f"\\{c}" for c in sorted(missing)[:8]))
    added = set(_COMMAND.findall(new)) - set(_COMMAND.findall(old)) - {"cite", "lr"} - (known or set())
    if added and exempt is None:  # a fix replaces a broken command with a correct one
        flags.append("LaTeX commands added: " + ", ".join(f"\\{c}" for c in sorted(added)[:8]))
    old_math = Counter(m.strip() for m in _MATH.findall(old))
    if old_math - Counter(m.strip() for m in _MATH.findall(new)):
        flags.append("math changed or dropped")
    lost_keys = set(cited_keys(old)) - set(cited_keys(new))
    if lost_keys:
        flags.append("citations dropped: " + ", ".join(sorted(lost_keys)[:8]))
    (old_net, old_broken), (new_net, new_broken) = _brace_state(old), _brace_state(new)
    if old_net != new_net or (new_broken and not old_broken):  # a dropped } breaks the compile (real run)
        flags.append("braces unbalanced: a { or } was added or dropped")
    lost_refs = {m.group(0) for m in _ARG_COMMANDS.finditer(old)} - {m.group(0) for m in _ARG_COMMANDS.finditer(new)}
    if lost_refs:
        flags.append("references or labels dropped: " + ", ".join(sorted(lost_refs)[:8]))
    return flags


_VERBATIM = re.compile(r"\\begin\{(verbatim\*?|lstlisting|minted)\}.*?\\end\{\1\}|\\verb\*?(.).*?\2", re.S)


def _brace_state(text: str) -> tuple[int, bool]:
    """Opening minus closing braces, and whether a } ever closes nothing. Escapes (\\{ \\} \\\\ \\%),
    comments and verbatim text are read as LaTeX reads them."""
    text = _VERBATIM.sub("", text)
    depth, broken, i = 0, False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2  # the escaped character, whatever it is
            continue
        if ch == "%":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            broken = broken or depth < 0
        i += 1
    return depth, broken


# Commands whose first letter is a JSON escape (\f \n \t \r \b), for text with no original to compare with.
_MATH_COMMANDS = " ".join("\\" + c for c in (
    "frac", "forall", "flat", "footnotesize", "nabla", "neq", "ne", "nu", "neg", "ni", "not", "notin", "nonumber",
    "newline", "nleq", "ngeq", "nmid", "nsubseteq", "times", "tau", "theta", "tilde", "text", "textbf", "textit",
    "textrm", "top", "to", "triangle", "tan", "tanh", "tfrac", "rho", "right", "rightarrow", "Rightarrow", "rangle",
    "rceil", "rfloor", "rm", "beta", "bar", "begin", "big", "bigl", "bigr", "Big", "binom", "bm", "boldsymbol",
    "bullet", "bf", "bot", "bmod", "hline", "theta", "Theta"))
_CONTROL = {"\t": "t", "\r": "r", "\f": "f", "\b": "b", "\n": "n"}


def repair_escapes(new: str, original: str) -> str:
    """Undo JSON escapes a model applied to unescaped LaTeX: "\\textbf" sent as \\t + "extbf" arrives as a tab.
    A control character followed by letters becomes a backslash command again when the command is in the
    original text, or (except for newlines, which are mostly real) when the original has no such character."""
    commands = set(_COMMAND.findall(original))

    def fix(match: re.Match[str]) -> str:
        char, letters = match.group(1), match.group(2)
        name = _CONTROL[char] + letters
        if name in commands or (char != "\n" and char not in original):
            return "\\" + name
        return match.group(0)

    return re.sub(r"([\t\r\f\b\n])([a-zA-Z]+)", fix, new)


def repair_image_escapes(new: str) -> str:
    """A reply with no backslash at all did not escape its JSON: every control character before a known command
    was a backslash. A reply that has backslashes escaped them itself, so its newlines and tabs are real ("\\nu"
    vs a line starting with "u"); only form feeds and backspaces, never real in LaTeX, are read as commands."""
    if "\\" not in new:
        return repair_escapes(new, _MATH_COMMANDS)
    commands = set(_COMMAND.findall(_MATH_COMMANDS))
    return re.sub(r"([\f\b])([a-zA-Z]+)",
                  lambda m: "\\" + _CONTROL[m.group(1)] + m.group(2) if _CONTROL[m.group(1)] + m.group(2) in commands
                  else m.group(0), new)


def _keep_edges(original: str, new: str) -> str:
    """Models trim or add surrounding blank space; keep the selection's own leading and trailing whitespace."""
    lead = original[:len(original) - len(original.lstrip())]
    trail = original[len(original.rstrip()):]
    return lead + new.strip() + trail if new.strip() else new


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

