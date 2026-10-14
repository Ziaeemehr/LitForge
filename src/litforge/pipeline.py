"""Add the user's own PDFs to a project in one step: import, parse, index, and record a run.

Used by `litforge add` and the web UI's "Add PDFs" tab, so both behave the same and the added papers
show up in the run history like a search, ready to mark ✓ and export.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from litforge.config import Config, Paths
from litforge.discovery.run import DiscoveryReport, _Run
from litforge.indexing.fts import current_runs, index_run
from litforge.ingest import IngestResult, ingest_file, iter_pdfs
from litforge.observability import NULL_LOG, EventLog
from litforge.parsing.run import ParseResult, parse_artifact

UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{12}-")  # added by the web UI's upload handler


def add_pdfs(
    conn: sqlite3.Connection,
    paths: Paths,
    config: Config,
    project_id: str,
    sources: Iterable[Path],
    label: str,
    log: EventLog = NULL_LOG,
    progress: Callable[[str], None] = lambda _: None,
) -> tuple[DiscoveryReport, list[IngestResult], list[ParseResult]]:
    """Safe to re-run: identical files are recognized and nothing is parsed or indexed twice."""
    pdfs = iter_pdfs(sources)
    run = _Run(conn, project_id, "pdfs", {"title": label, "files": len(pdfs)})
    before = {r[0] for r in conn.execute("SELECT paper_id FROM project_papers WHERE project_id=?", (project_id,))}
    results: list[IngestResult] = []
    for i, pdf in enumerate(pdfs, 1):
        progress(f"[{i}/{len(pdfs)}] importing {pdf.name}")
        try:
            res = ingest_file(conn, paths, pdf, project_id, log=log)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            log.emit("ingest.error", path=str(pdf), error=repr(exc))
            res = IngestResult(path=str(pdf), status="error", message=repr(exc))
        results.append(res)
        if res.status in ("quarantined", "error"):
            run.report.errors.append(f"{pdf.name}: {res.message}"[:300])

    parsed: list[ParseResult] = []
    artifacts = list(dict.fromkeys(r.artifact_id for r in results if r.artifact_id and r.paper_id))
    for i, artifact_id in enumerate(artifacts, 1):
        artifact = conn.execute("SELECT * FROM artifacts WHERE id=? AND status='ok'", (artifact_id,)).fetchone()
        if artifact is None:
            continue
        progress(f"[{i}/{len(artifacts)}] extracting text")
        parsed.append(parse_artifact(conn, paths, config, artifact, log=log))
    wanted = set(artifacts)
    for parse_run in current_runs(conn, project_id):
        if parse_run["artifact_id"] in wanted:
            index_run(conn, paths, config, parse_run)

    ok = [r for r in results if r.paper_id]
    ids = list(dict.fromkeys(r.paper_id for r in ok if r.paper_id))
    run.add_papers(ids, "pdf", "pdf", set(ids) - before)
    # The folder's own order is the ranking; each paper says which file it came from.
    scores = {pid: round(1 - i / max(len(ids), 1), 4) for i, pid in enumerate(ids)}
    reasons: dict[str, list[str]] = {}
    for r in ok:
        assert r.paper_id is not None
        name = UPLOAD_PREFIX.sub("", Path(r.path).name)
        note = f"your PDF: {name}" + (" (already in library)" if r.status == "duplicate" else "")
        reasons.setdefault(r.paper_id, []).append(note)
    return run.finish(None, 0, scores, reasons), results, parsed
