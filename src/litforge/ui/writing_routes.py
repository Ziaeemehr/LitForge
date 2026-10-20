"""HTTP routes of the Writing Studio ("Write" view). GETs only read; POSTs change files or start jobs."""

from __future__ import annotations

import sqlite3
import threading
from typing import TYPE_CHECKING, Any

from litforge.writing import archive as warchive
from litforge.writing import assist as wassist
from litforge.writing import chat as wchat
from litforge.writing import cite as wcite
from litforge.writing import compile as wc
from litforge.writing import documents as wd
from litforge.writing import gitsync as wg
from litforge.writing import notes as wn
from litforge.writing import preview as wp
from litforge.writing import synctex as wsync

if TYPE_CHECKING:
    from litforge.ui.server import App, Job

MAX_FIGURE_UPLOAD = wd.MAX_FIGURE_BYTES
MAX_ZIP_UPLOAD = 100 * 1024 * 1024


class Conflict(Exception):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error"))
        self.payload = payload


def _doc(conn: sqlite3.Connection, app: App, query: dict[str, Any]) -> wd.Document:
    return wd.get_document(conn, app.paths, str(query.get("doc") or ""))


_SYNC_CACHE: dict[str, tuple[tuple[int, int], wsync.SyncTex]] = {}
_START = threading.Lock()  # one model job per document and kind  # document id -> (file stamp, parsed)


def synctex_lookup(doc: wd.Document, query: dict[str, str]) -> dict[str, Any] | None:
    """Forward (file, line) or inverse (page, x, y) lookup in the last compile's SyncTeX file."""
    path = wd.resolve_inside(doc.folder, doc.output(".synctex.gz"), allow_hidden=True)
    if not path.is_file():
        raise LookupError("no position data yet; compile the document first")
    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _SYNC_CACHE.get(doc.id)
    if cached is None or cached[0] != stamp:
        try:
            cached = (stamp, wsync.load(path, doc.folder.resolve()))
        except wsync.SyncTexError as exc:  # e.g. read while a compile is still writing it
            raise LookupError(f"{exc}; compile again") from None
        _SYNC_CACHE[doc.id] = cached
    sync = cached[1]
    try:
        if "page" in query:
            return wsync.inverse(sync, int(query["page"]), float(query.get("x", "")), float(query.get("y", "")))
        name = query.get("file") or "main.tex"
        wd.resolve_inside(doc.folder, name)
        return wsync.forward(sync, name, int(query.get("line", "")))
    except ValueError as exc:
        raise ValueError(f"bad position: {exc}") from None


def get(app: App, conn: sqlite3.Connection, path: str, query: dict[str, str]) -> Any:
    """JSON payload, or bytes, content type and optional disposition for downloads."""
    if path == "/api/write/git":
        return wg.status(_doc(conn, app, query))
    if path == "/api/write/figure":
        return wp.figure(_doc(conn, app, query), query.get("name", ""))
    if path == "/api/write/notes":
        return {"notes": wn.list_notes(conn, _doc(conn, app, query), query.get("file", ""))}
    if path == "/api/write/docs":
        return {"documents": [d.as_dict() for d in wd.list_documents(conn, app.paths, query.get("project", ""))],
                "templates": wd.TEMPLATES, "engines": wc.engine_status(app.paths)}
    if path == "/api/write/doc":
        doc = _doc(conn, app, query)
        return {**doc.as_dict(), "files": wd.list_files(doc), "main_candidates": wd.main_candidates(doc),
                "pdf": (doc.folder / doc.output(".pdf")).is_file()}
    if path == "/api/write/file":
        doc = _doc(conn, app, query)
        text, digest = wd.read_file(doc, query.get("name") or "main.tex")
        return {"name": query.get("name") or "main.tex", "text": text, "hash": digest}
    if path == "/api/write/snapshots":
        doc = _doc(conn, app, query)
        return {"snapshots": [{k: s[k] for k in ("id", "created_at", "note", "name", "files", "added", "removed")}
                              for s in wd.snapshot_summaries(doc)]}
    if path == "/api/write/diff":
        return wd.diff_versions(_doc(conn, app, query), query.get("a") or "current",
                                query.get("b") or "current", query.get("file") or "main.tex")
    if path == "/api/write/cite/search":
        doc = _doc(conn, app, query)
        q = query.get("q", "")
        if query.get("tab") == "evidence":
            return {"results": wcite.search_evidence(conn, doc.project_id, q)}
        return {"results": wcite.search_library(conn, doc.project_id, q)}
    if path == "/api/write/quote":
        return wcite.quotes_for_key(conn, _doc(conn, app, query), query.get("key", ""))
    if path == "/api/write/synctex":
        return synctex_lookup(_doc(conn, app, query), query)
    if path == "/api/write/export.zip":
        doc = _doc(conn, app, query)
        return warchive.export_zip(doc), "application/zip", f'attachment; filename="{doc.slug}.zip"'
    if path == "/api/write/pdf":
        doc = _doc(conn, app, query)
        pdf = wd.resolve_inside(doc.folder, doc.output(".pdf"), allow_hidden=True)
        if not pdf.is_file():
            raise LookupError("no PDF yet; compile the document first")
        return pdf.read_bytes(), "application/pdf"
    raise LookupError("not found")


def post(app: App, conn: sqlite3.Connection, path: str, body: dict[str, Any]) -> Any:
    if path == "/api/write/create":
        doc = wd.create_document(conn, app.paths, str(body.get("project") or ""), str(body.get("name") or ""),
                                 str(body.get("template") or "article"))
        return doc.as_dict()
    if path == "/api/write/git/clone":
        url = wg.validate_url(str(body.get("url") or ""))

        def work(job: Job) -> None:
            db = app.db()
            try:
                job.report = wg.clone(db, app.paths, str(body.get("project") or ""),
                                      str(body.get("name") or ""), url).as_dict()
            finally:
                db.close()

        return app.start_job("writing-git-clone", "", work).__dict__
    doc = _doc(conn, app, body)
    if path.startswith("/api/write/git/"):
        with _START:
            if app_busy(app, doc.id):
                raise ValueError("wait for the compile to finish")
            if git_busy(app, doc.id):
                raise ValueError("a git operation is already running for this document")
            if path == "/api/write/git/sync":
                def sync_work(job: Job) -> None:
                    job.report = wg.sync(doc)
                return app.start_job("writing-git", doc.id, sync_work).__dict__
            if path == "/api/write/git/connect":
                return wg.connect(doc, str(body.get("url") or ""))
            if path == "/api/write/git/abort":
                return wg.abort_merge(doc)
            if path == "/api/write/git/disconnect":
                return wg.disconnect(doc)
            raise LookupError("not found")
    if path == "/api/write/preview":
        return wp.preview_math(doc, app.paths, str(body.get("tex") or "")), "application/pdf", "inline"
    if path == "/api/write/note":
        args = (str(body.get("file") or ""), body.get("line"), str(body.get("anchor") or ""),
                str(body.get("body") or ""))
        if body.get("id"):
            return wn.update(conn, doc, str(body["id"]), *args)
        return wn.add(conn, doc, *args)
    if path == "/api/write/note/delete":
        wn.delete(conn, doc, str(body.get("id") or ""))
        return {"ok": True}
    if path == "/api/write/notes/move":
        notes = body.get("notes")
        if not isinstance(notes, list) or not all(isinstance(n, dict) for n in notes):
            raise ValueError("notes must be a list of positions")
        return {"moved": wn.move(conn, doc, str(body.get("file") or ""), notes)}
    if path == "/api/write/save":
        try:
            digest = wd.save_file(conn, doc, str(body.get("name") or "main.tex"), str(body.get("text") or ""),
                                  body.get("hash") or None)
        except wd.SaveConflict as exc:
            raise Conflict({"error": str(exc), "conflict": True, "hash": exc.current_hash}) from exc
        return {"hash": digest}
    if path == "/api/write/rename":
        return wd.rename_document(conn, app.paths, doc.id, str(body.get("name") or "")).as_dict()
    if path == "/api/write/settings":
        if app_busy(app, doc.id):  # the running compile uses the settings it started with
            raise ValueError("wait for the compile to finish before changing the settings")
        draft = body.get("draft")
        return wd.set_settings(conn, app.paths, doc.id, engine=body.get("engine") or None,
                               main_file=body.get("main_file") or None,
                               draft=draft if isinstance(draft, bool) else None).as_dict()
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
        return {"id": wd.create_snapshot(doc, str(body.get("note") or "manual")[:200],
                                         str(body.get("name") or ""))}
    if path == "/api/write/snapshot/name":
        snap_id = str(body.get("snapshot") or "")
        return {"id": snap_id, "name": wd.name_snapshot(doc, snap_id, str(body.get("name") or ""))}
    if path == "/api/write/cite":
        return wcite.cite(conn, doc, str(body.get("paper_id") or ""), body.get("evidence_id") or None)
    if path == "/api/write/assist":
        return assist(app, conn, doc, body)
    if path == "/api/write/chat":
        return chat(app, conn, doc, body)
    if path == "/api/write/assist/decide":
        return wassist.decide(conn, doc, str(body.get("suggestion") or ""), list(body.get("accepted") or []))
    if path == "/api/write/compile":
        return compile_job(app, doc).__dict__
    raise LookupError("not found")


def import_archive(app: App, conn: sqlite3.Connection, query: dict[str, str], data: bytes) -> dict[str, Any]:
    return warchive.import_zip(conn, app.paths, query.get("project", ""), query.get("name", ""), data).as_dict()


def upload(app: App, conn: sqlite3.Connection, doc_id: str, name: str, data: bytes) -> dict[str, Any]:
    doc = wd.get_document(conn, app.paths, doc_id)
    return {"name": wd.upload_figure(doc, name, data)}


def app_busy(app: App, doc_id: str) -> bool:
    return any(j.kind == "writing-compile" and j.status == "running" and j.project == doc_id
               for j in app.jobs.values())


def git_busy(app: App, doc_id: str) -> bool:
    return any(j.kind == "writing-git" and j.status == "running" and j.project == doc_id
               for j in app.jobs.values())


def compile_job(app: App, doc: wd.Document) -> Job:
    with _START:
        if git_busy(app, doc.id):
            raise ValueError("wait for the git operation to finish")
        return _compile_job(app, doc)


def _compile_job(app: App, doc: wd.Document) -> Job:
    if app_busy(app, doc.id):
        raise ValueError("this document is already compiling")

    def work(job: Job) -> None:
        job.messages.append(f"Compiling with {doc.engine}…")
        if wc.first_tectonic_run(app.paths, doc.engine):
            job.messages.append("First compile: downloading the LaTeX packages (~50 MB), this takes a few minutes "
                                "once; later compiles take seconds.")
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


def chat(app: App, conn: sqlite3.Connection, doc: wd.Document, body: dict[str, Any]) -> Any:
    """The assistant, like assist(): preview first, approval for a cloud route, then a job whose report is the
    answer (and, for an edit of the selection, a suggestion to accept or reject)."""
    history = body.get("history") if isinstance(body.get("history"), list) else []
    args = (str(body.get("question") or ""), str(body.get("selection") or ""), str(body.get("context") or ""),
            history)
    pads = {"pad_before": body.get("pad_before") is True, "pad_after": body.get("pad_after") is True}
    req = wchat.prepare(conn, doc, *args, **pads)
    gateway = app.make_gateway(conn)
    preview = wchat.preview(gateway, req).model_dump()
    sent = {"characters": len(req.question) + len(req.selection or str(body.get("context") or "")[:wchat.MAX_CONTEXT]),
            "evidence": len(req.evidence)}
    if preview["blocked_reason"]:
        raise ValueError(f"the model route is blocked: {preview['blocked_reason']}")
    approved = body.get("approved") is True
    if preview["requires_approval"] and not approved:
        return {"needs_approval": True, "preview": preview, "sent": sent}
    file = str(body.get("file") or "main.tex")
    wd.resolve_inside(doc.folder, file)
    def work(job: Job) -> None:
        job.messages.append(f"Asking {preview['model'] or preview['profile']}…")
        conn = app.db()
        try:
            job.report = wchat.run(conn, app.make_gateway(conn), doc, wchat.prepare(conn, doc, *args, **pads), file,
                                   approved=approved)
        finally:
            conn.close()

    with _START:  # the check and the start together: two requests at once cannot both get through
        if any(j.kind == "writing-chat" and j.status == "running" and j.project == doc.id for j in app.jobs.values()):
            raise ValueError("the assistant is still answering the last question")
        job = app.start_job("writing-chat", doc.id, work)
    return {**job.__dict__, "preview": preview, "sent": sent}


def assist(app: App, conn: sqlite3.Connection, doc: wd.Document, body: dict[str, Any]) -> Any:
    """Preview first: a blocked route is refused, and a route needing approval returns the preview until the
    page sends approved=true. Otherwise the call runs as a job whose report is the suggestion."""
    args = (str(body.get("action") or ""), str(body.get("text") or ""), str(body.get("error") or ""),
            int(body.get("first_line") or 1), int(body.get("error_line") or 0))
    extra: dict[str, Any] = {"context": str(body.get("context") or ""),
                             "image": wassist.decode_image(str(body.get("image") or "")),
                             "pad_before": body.get("pad_before") is True, "pad_after": body.get("pad_after") is True,
                             "new_env": body.get("new_env") is True}
    req = wassist.prepare(conn, doc, *args, **extra)
    gateway = app.make_gateway(conn)
    preview = wassist.preview(gateway, req).model_dump()
    sent = {"characters": len(req.text) + len(req.context), "images": len(req.images),
            "evidence": len(req.evidence)}
    if preview["blocked_reason"]:
        raise ValueError(f"the model route is blocked: {preview['blocked_reason']}")
    approved = body.get("approved") is True
    if preview["requires_approval"] and not approved:
        return {"needs_approval": True, "preview": preview, "sent": sent}
    file = str(body.get("file") or "main.tex")
    wd.resolve_inside(doc.folder, file)
    def work(job: Job) -> None:
        job.messages.append(f"Asking {preview['model'] or preview['profile']}…")
        conn = app.db()
        try:
            gateway = app.make_gateway(conn)
            job.report = wassist.run(conn, gateway, doc, wassist.prepare(conn, doc, *args, **extra), file,
                                     approved=approved)
        finally:
            conn.close()

    with _START:
        if any(j.kind == "writing-assist" and j.status == "running" and j.project == doc.id for j in app.jobs.values()):
            raise ValueError("a suggestion is already being prepared for this document")
        job = app.start_job("writing-assist", doc.id, work)
    return {**job.__dict__, "preview": preview, "sent": sent}
