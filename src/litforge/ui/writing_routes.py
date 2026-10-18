"""HTTP routes of the Writing Studio ("Write" view). GETs only read; POSTs change files or start jobs."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from litforge.writing import assist as wassist
from litforge.writing import cite as wcite
from litforge.writing import compile as wc
from litforge.writing import documents as wd

if TYPE_CHECKING:
    from litforge.ui.server import App, Job

MAX_FIGURE_UPLOAD = wd.MAX_FIGURE_BYTES


class Conflict(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error"))
        self.payload = payload


def _doc(conn: sqlite3.Connection, app: App, query: dict[str, Any]) -> wd.Document:
    return wd.get_document(conn, app.paths, str(query.get("doc") or ""))


def get(app: App, conn: sqlite3.Connection, path: str, query: dict[str, str]) -> Any:
    """JSON payload for a read-only route, or bytes and a content type for the PDF."""
    if path == "/api/write/docs":
        return {"documents": [d.as_dict() for d in wd.list_documents(conn, app.paths, query.get("project", ""))],
                "templates": wd.TEMPLATES, "engines": wc.engine_status(app.paths)}
    if path == "/api/write/doc":
        doc = _doc(conn, app, query)
        return {**doc.as_dict(), "files": wd.list_files(doc), "pdf": (doc.folder / ".build" / "main.pdf").is_file()}
    if path == "/api/write/file":
        doc = _doc(conn, app, query)
        text, digest = wd.read_file(doc, query.get("name") or "main.tex")
        return {"name": query.get("name") or "main.tex", "text": text, "hash": digest}
    if path == "/api/write/snapshots":
        doc = _doc(conn, app, query)
        return {"snapshots": [{k: s[k] for k in ("id", "created_at", "note", "files")}
                              for s in wd.list_snapshots(doc)]}
    if path == "/api/write/cite/search":
        doc = _doc(conn, app, query)
        q = query.get("q", "")
        if query.get("tab") == "evidence":
            return {"results": wcite.search_evidence(conn, doc.project_id, q)}
        return {"results": wcite.search_library(conn, doc.project_id, q)}
    if path == "/api/write/quote":
        return wcite.quotes_for_key(conn, _doc(conn, app, query), query.get("key", ""))
    if path == "/api/write/pdf":
        doc = _doc(conn, app, query)
        pdf = wd.resolve_inside(doc.folder, ".build/main.pdf", allow_hidden=True)
        if not pdf.is_file():
            raise LookupError("no PDF yet; compile the document first")
        return pdf.read_bytes(), "application/pdf"
    raise LookupError("not found")


def post(app: App, conn: sqlite3.Connection, path: str, body: dict[str, Any]) -> Any:
    if path == "/api/write/create":
        doc = wd.create_document(conn, app.paths, str(body.get("project") or ""), str(body.get("name") or ""),
                                 str(body.get("template") or "article"))
        return doc.as_dict()
    doc = _doc(conn, app, body)
    if path == "/api/write/save":
        try:
            digest = wd.save_file(conn, doc, str(body.get("name") or "main.tex"), str(body.get("text") or ""),
                                  body.get("hash") or None)
        except wd.SaveConflict as exc:
            raise Conflict({"error": str(exc), "conflict": True, "hash": exc.current_hash}) from exc
        return {"hash": digest}
    if path == "/api/write/rename":
        return wd.rename_document(conn, app.paths, doc.id, str(body.get("name") or "")).as_dict()
    if path == "/api/write/engine":
        return wd.set_engine(conn, app.paths, doc.id, str(body.get("engine") or "")).as_dict()
    if path == "/api/write/delete":
        if app_busy(app, doc.id):
            raise ValueError("wait for the compile to finish")
        wd.delete_document(conn, app.paths, doc.id)
        return {"ok": True}
    if path == "/api/write/restore":
        return {"restored": wd.restore_snapshot(conn, doc, str(body.get("snapshot") or ""))}
    if path == "/api/write/snapshot":
        return {"id": wd.create_snapshot(doc, str(body.get("note") or "manual")[:200])}
    if path == "/api/write/cite":
        return wcite.cite(conn, doc, str(body.get("paper_id") or ""), body.get("evidence_id") or None)
    if path == "/api/write/assist":
        return assist(app, conn, doc, body)
    if path == "/api/write/assist/decide":
        return wassist.decide(conn, doc, str(body.get("suggestion") or ""), list(body.get("accepted") or []))
    if path == "/api/write/compile":
        return compile_job(app, doc).__dict__
    raise LookupError("not found")


def upload(app: App, conn: sqlite3.Connection, doc_id: str, name: str, data: bytes) -> dict[str, Any]:
    doc = wd.get_document(conn, app.paths, doc_id)
    return {"name": wd.upload_figure(doc, name, data)}


def app_busy(app: App, doc_id: str) -> bool:
    return any(j.kind == "writing-compile" and j.status == "running" and j.project == doc_id
               for j in app.jobs.values())


def compile_job(app: App, doc: wd.Document) -> Job:
    if app_busy(app, doc.id):
        raise ValueError("this document is already compiling")

    def work(job: Job) -> None:
        job.messages.append(f"Compiling with {doc.engine}…")
        conn = app.db()
        try:
            wcite.sync_bib(conn, doc)  # entries for every library key the text cites
        finally:
            conn.close()
        result = wc.compile_document(doc, app.paths)
        job.report = result.as_dict()
        job.report.pop("log", None)
        job.report["log_tail"] = result.log[-8000:]
        job.messages.append("PDF ready." if result.ok else f"{len(result.errors)} error(s).")

    # The job's "project" field holds the document id so one compile runs per document.
    return app.start_job("writing-compile", doc.id, work)


def install_tectonic_job(app: App, body: dict[str, Any]) -> Job:
    if body.get("confirmed") is not True:
        raise ValueError("Confirm the Tectonic download size first")
    if any(j.kind == "tectonic-install" and j.status == "running" for j in app.jobs.values()):
        raise ValueError("Tectonic installation is already running")

    def work(job: Job) -> None:
        job.report = wc.install_tectonic(app.paths, job.messages.append)

    return app.start_job("tectonic-install", "", work)


def assist(app: App, conn: sqlite3.Connection, doc: wd.Document, body: dict[str, Any]) -> Any:
    """Preview first: a blocked route is refused, and a route needing approval returns the preview until the
    page sends approved=true. Otherwise the call runs as a job whose report is the suggestion."""
    args = (str(body.get("action") or ""), str(body.get("text") or ""), str(body.get("error") or ""),
            int(body.get("first_line") or 1), int(body.get("error_line") or 0))
    req = wassist.prepare(conn, doc, *args)
    gateway = app.make_gateway(conn)
    preview = wassist.preview(gateway, req).model_dump()
    sent = {"characters": len(req.text), "evidence": max(0, len(req.packet.items) - 1) if req.packet else 0}
    if preview["blocked_reason"]:
        raise ValueError(f"the model route is blocked: {preview['blocked_reason']}")
    approved = body.get("approved") is True
    if preview["requires_approval"] and not approved:
        return {"needs_approval": True, "preview": preview, "sent": sent}
    file = str(body.get("file") or "main.tex")
    wd.resolve_inside(doc.folder, file)
    if any(j.kind == "writing-assist" and j.status == "running" and j.project == doc.id for j in app.jobs.values()):
        raise ValueError("a suggestion is already being prepared for this document")

    def work(job: Job) -> None:
        job.messages.append(f"Asking {preview['model'] or preview['profile']}…")
        conn = app.db()
        try:
            gateway = app.make_gateway(conn)
            job.report = wassist.run(conn, gateway, doc, wassist.prepare(conn, doc, *args), file,
                                     approved=approved)
        finally:
            conn.close()

    job = app.start_job("writing-assist", doc.id, work)
    return {**job.__dict__, "preview": preview, "sent": sent}
