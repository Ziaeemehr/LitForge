"""Export a brief only when every answer sentence is cited or explicitly labelled."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from litforge.analysis.ask import get_answer
from litforge.analysis.markdown_safe import excerpt, inert
from litforge.analysis.matrix import export_matrix, get_matrix, matrix_spans
from litforge.analysis.review import review_queue
from litforge.analysis.runs import get_run


def write_brief(conn: sqlite3.Connection, answer_run_id: str, matrix_id: str | None = None,
                *, out: Path, claims: bool = False) -> Path:
    answer = get_answer(conn, answer_run_id)
    run = get_run(conn, answer_run_id)
    config = json.loads(run.config_json)
    lines = [f"# {inert(answer.question)}", "", "## Method", "",
             f"Retrieval: {config.get("retrieval", "lexical")}; k={config.get("k", 8)}; "
             f"project={run.project_id or "library"}; date={run.started_at}; "
             f"model={answer.model or "none"}; provider={answer.provider or "none"}; prompt={run.prompt_version}."]
    audits = conn.execute("SELECT DISTINCT prompt_version FROM analysis_runs WHERE task='audit' "
                          "AND json_extract(config_json,'$.answer_run')=?", (answer_run_id,)).fetchall()
    if audits:
        lines.append("Audit prompts: " + ", ".join(r[0] for r in audits) + ".")
    if matrix_id:
        matrix = get_matrix(conn, matrix_id)
        versions = sorted({c.prompt_version for c in matrix.cells})
        routes = sorted({f"{c.provider}/{c.model}" for c in matrix.cells})
        lines.append(f"Matrix prompts: {", ".join(versions)}; matrix models: {", ".join(routes)}.")
    lines.extend(["", "## Answer", ""])
    known = {s.id for s in answer.evidence}
    rejected = []
    for sentence in answer.sentences:
        if sentence.review_status == "rejected":
            rejected.append(inert(sentence.text))
            continue
        if any(e not in known for e in sentence.evidence_ids):
            raise ValueError("sentence cites evidence outside the answer")
        label = {"unsupported": " *(unsupported)*",
                 "interpretation": " *(interpretation — needs review)*"}.get(sentence.kind, "")
        if not sentence.evidence_ids and not label:
            raise ValueError("sentence has neither evidence nor an explicit label")
        markers = "".join(f"[^{e}]" for e in sentence.evidence_ids)
        if sentence.audit_verdict in ("unsupported", "contradicted", "partial"):
            label += f" *(audit: {sentence.audit_verdict} — needs review)*"
        lines.append(inert(sentence.text) + markers + label)
    if rejected:
        lines.extend(["", "## Rejected by reviewer", "", *["- " + text for text in rejected]])
    counts = Counter(s.audit_verdict or "not audited" for s in answer.sentences)
    lines.extend(["", "## Audit", "", ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))])
    spans = {s.id: s for s in answer.evidence}
    target_ids = {s.id for s in answer.sentences}
    if matrix_id:
        matrix = get_matrix(conn, matrix_id)
        target_ids.update(c.id for c in matrix.cells)
        for span in matrix_spans(conn, matrix):
            spans.setdefault(span.id, span)
        # Appendix below owns all footnotes, including overlapping answer/matrix evidence.
        table = export_matrix(conn, matrix_id, "markdown")
        lines.extend(["", "\n".join(line for line in table.splitlines() if not line.startswith("[^"))])
    items = [i for i in review_queue(conn) if i.target_id in target_ids]
    lines.extend(["", "## Open Review Items", "", *[f"- {i.target_kind} {i.target_id}: {inert(i.text)} "
                             f"[{", ".join(i.flags)}; audit={i.audit_verdict or "not audited"}]" for i in items]])
    if not items:
        lines.append("None.")
    lines.extend(["", "## Evidence appendix", ""])
    for s in spans.values():
        quote = inert(excerpt(s.text))
        lines.extend([f"[^{s.id}]: {inert(s.citation)}; section={inert(s.section or s.kind)}; evidence ID={s.id}; "
                      f"parse={s.parse_run_id}; parser={s.parser} {s.parser_version} — \"{quote}\""])
    if claims:
        if run.project_id is None:
            raise ValueError("claim landscape requires a project-scoped answer")
        from litforge.analysis.claim_graph import landscape_markdown
        lines.extend(["", landscape_markdown(conn, run.project_id)])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
