"""Obsidian/Git-friendly Markdown export of a project's papers (relative asset links)."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections.abc import Collection
from pathlib import Path

import yaml

from litforge.config import Config, Paths, load_config
from litforge.discovery.versions import format_version_label, primary_paper_ids, versions_for_paper
from litforge.jobs import check_cancelled
from litforge.parsing.canonical import read_canonical, to_markdown
from litforge.parsing.display import parse_status_label, parser_install_hint
from litforge.parsing.formulas import FORMULAS
from litforge.parsing.markitdown_parser import DOCUMENT_SUFFIXES
from litforge.parsing.vision import VISION, read_page_markdown
from litforge.projects import MARK_SCOPES


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "paper"


def default_export_dir(conn: sqlite3.Connection, paths: Paths, project: str, project_id: str) -> Path:
    row = conn.execute("SELECT export_dir FROM projects WHERE id=?", (project_id,)).fetchone()
    return Path(row["export_dir"]).expanduser() if row and row["export_dir"] else paths.default_exports(project)


def export_markdown(
    conn: sqlite3.Connection,
    paths: Paths,
    project: str,
    project_id: str,
    out_dir: Path | None = None,
    only: str = "all",
    paper_ids: Collection[str] | None = None,
    config: Config | None = None,
    all_versions: bool = False,
) -> tuple[Path, int]:
    """Write <export folder>/markdown/{README.md, knowledge_base/*.md, assets/<slug>/...} and
    <export folder>/pdfs/<slug>.pdf; returns (root, papers) with root the markdown folder. Each paper's
    PDF is copied under a readable name beside the markdown folder and linked from its note.
    `only` filters by the project's marks and `paper_ids` limits to an explicit selection. Notes and
    assets that the previous export wrote (listed in its manifest.json) but this one does not are
    removed, so the folder matches the selection; other files are never touched, and an empty
    selection leaves the folder as it is. Private by default: contains full extracted text and figure
    crops, so do not publish without a copyright review."""
    if only not in MARK_SCOPES:
        raise ValueError(f"only must be one of {sorted(MARK_SCOPES)}")
    root = (out_dir or default_export_dir(conn, paths, project, project_id)) / "markdown"
    kb = root / "knowledge_base"
    pdf_dir = root.parent / "pdfs"
    rows = conn.execute(
        f"""SELECT p.id, p.title, p.year, p.doi, p.doi_status, p.abstract,
                  a.sha256, a.pdf_path, a.version, a.version_source, a.source_url, a.version_label,
                  r.parser, r.status, r.output_dir,
                  s.decision AS s_decision, s.score AS s_score, s.result_json AS s_result
           FROM project_papers pp
           JOIN papers p ON p.id = pp.paper_id
           LEFT JOIN artifacts a ON a.paper_id = p.id AND a.status = 'ok'
           LEFT JOIN parse_runs r ON r.artifact_id = a.id AND r.is_current = 1
           LEFT JOIN paper_marks m ON m.paper_id = p.id AND m.project_id = pp.project_id
           LEFT JOIN screening_results s ON s.paper_id = p.id AND s.project_id = pp.project_id
           WHERE pp.project_id = ?{MARK_SCOPES[only]} ORDER BY p.year, p.title""",
        (project_id,),
    ).fetchall()
    if not all_versions and paper_ids is None:
        primary = primary_paper_ids(conn, project_id)
        rows = [r for r in rows if r["id"] in primary]
    if paper_ids is not None:
        wanted = set(paper_ids)
        rows = [r for r in rows if r["id"] in wanted]
    if not rows:
        return root, 0
    kb.mkdir(parents=True, exist_ok=True)
    screened = any(r["s_decision"] for r in rows)
    parse_hint = parser_install_hint((r["status"] for r in rows), paths, config or load_config(data_root=paths.root))
    previous = _previous_slugs(root / "manifest.json")
    written: dict[str, str] = {}  # paper id -> slug
    index_lines = [
        f"# {project}\n",
        "> Private export: contains full extracted text and figure crops.\n",
        "| Paper | Year | Version | Status | PDF |" + (" Screening |" if screened else ""),
        "|---|---|---|---|---|" + ("---|" if screened else ""),
    ]
    for row in rows:
        check_cancelled()  # a stopped export stops between papers
        title = row["title"] or f"Untitled ({(row['sha256'] or row['id'])[:8]})"
        version_label = format_version_label(row["version"] or "unknown", row["source_url"], row["version_label"])
        slug = f"{slugify(title)}-{row['id'][:8]}"
        written[row["id"]] = slug
        math = "raw"
        if row["output_dir"]:
            for name, level in ((VISION, "high"), (FORMULAS, "medium")):
                try:
                    data = json.loads((paths.root / row["output_dir"] / name).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if any(p.get("markdown") for p in (data.get("pages") or {}).values()):
                    math = level
                    break
        front = {
            "paper_id": row["id"],
            "title": title,
            "year": row["year"],
            "doi": row["doi"],
            "version": row["version"] or "unknown",
            "version_source": row["version_source"] or "unknown",
            "version_label": version_label,
            "versions": [
                {"paper_id": member["paper_id"], "doi": member["doi"], "role": member["role"]}
                for member in versions_for_paper(conn, row["id"]) if member["confirmed"]
            ] or [{"paper_id": row["id"], "doi": row["doi"]}],
            "doi_status": row["doi_status"],
            "project": project,
            "parse_status": row["status"] or ("not_parsed" if row["sha256"] else "no_pdf"),
            "parser": row["parser"],
            "math": math,
            "pdf_sha256": row["sha256"],
        }
        if row["status"] == "needs_docling" and parse_hint:
            front["parse_hint"] = parse_hint
        if row["s_decision"]:
            front["screening_decision"] = row["s_decision"]
            front["screening_score"] = round(row["s_score"], 3)
            front["screening_reason"] = json.loads(row["s_result"]).get("reason", "")
        pdf = None
        if row["pdf_path"] and (paths.root / row["pdf_path"]).is_file():
            suffix = Path(row["pdf_path"]).suffix.lower()  # a Word or HTML document keeps its own extension
            pdf = pdf_dir / f"{slug}{suffix}"
            pdf.parent.mkdir(exist_ok=True)
            if not pdf.exists() or pdf.stat().st_size != (paths.root / row["pdf_path"]).stat().st_size:
                shutil.copy2(paths.root / row["pdf_path"], pdf)
            front["pdf" if suffix == ".pdf" else "source_file"] = f"../../pdfs/{pdf.name}"
        file_label = "PDF" if pdf is None or pdf.suffix == ".pdf" else "Original file"
        links = ([f"- [{file_label}](../../pdfs/{pdf.name})"] if pdf else []) + (
            [f"- DOI ({row['doi_status']}): https://doi.org/{row['doi']}"] if row["doi"] else [])
        body = "_Not parsed yet._\n" if row["sha256"] else f"# {title}\n\n_No PDF in the library yet._\n"
        if not row["sha256"] and row["abstract"]:
            body += f"\n## Abstract\n\n{row['abstract']}\n"
        if row["output_dir"]:
            out_dir = paths.root / row["output_dir"]
            doc = read_canonical(out_dir)
            if (out_dir / "assets").is_dir():
                dest = root / "assets" / slug / "assets"
                shutil.copytree(out_dir / "assets", dest, dirs_exist_ok=True)
            body = to_markdown(doc, title, asset_prefix=f"../assets/{slug}/", page_md=read_page_markdown(out_dir))
        if version_label:  # an unknown version is left out rather than written as "None"
            body = f"> Source version: **{version_label}**\n\n" + body
        if links:
            body += "\n## Links\n\n" + "\n".join(links) + "\n"
        header = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n\n"
        (kb / f"{slug}.md").write_text(header + body, encoding="utf-8")
        index_lines.append(
            f"| [{title.replace('|', '/')}](knowledge_base/{slug}.md) | {row['year'] or ''} "
            f"| {version_label or ''} | {parse_status_label(front['parse_status'])}"
            + (f"; {parse_hint}" if row["status"] == "needs_docling" and parse_hint else "")
            + f" | {f'[{file_label}](../pdfs/{pdf.name})' if pdf else ''} |"
            + (f" {row['s_decision'] or ''} |" if screened else "")
        )
    for slug in previous - set(written.values()):
        (kb / f"{slug}.md").unlink(missing_ok=True)
        shutil.rmtree(root / "assets" / slug, ignore_errors=True)
        for suffix in (".pdf", *DOCUMENT_SUFFIXES):  # the copied PDF or document of a paper no longer exported
            (pdf_dir / f"{slug}{suffix}").unlink(missing_ok=True)
    _remove_pdfs(root / "pdfs", previous | set(written.values()))  # older exports kept PDFs in markdown/pdfs
    (root / "README.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps([{**{k: v for k, v in dict(r).items() if not k.startswith("s_")}, "slug": written[r["id"]]}
                    for r in rows], indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    return root, len(rows)


def remove_export(folder: Path) -> None:
    """Remove what export_markdown wrote into `folder`: its markdown folder and the PDFs it copied beside
    it. Other files in `folder`, including the user's own files in its pdfs folder, are kept."""
    root = folder / "markdown"
    _remove_pdfs(folder / "pdfs", _previous_slugs(root / "manifest.json"))
    shutil.rmtree(root, ignore_errors=True)


def _remove_pdfs(pdf_dir: Path, slugs: set[str]) -> None:
    """Delete the exported PDFs of these slugs, and the folder once it is empty."""
    if not pdf_dir.is_dir():
        return
    for slug in slugs:
        for suffix in (".pdf", *DOCUMENT_SUFFIXES):  # the PDF or the document copied for this note
            (pdf_dir / f"{slug}{suffix}").unlink(missing_ok=True)
    if not any(pdf_dir.iterdir()):
        pdf_dir.rmdir()


def _previous_slugs(manifest: Path) -> set[str]:
    """Slugs the last export wrote; only these may be cleaned up (never the user's own files)."""
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {e["slug"] for e in entries if isinstance(e, dict) and re.fullmatch(r"[a-z0-9-]+", str(e.get("slug")))}
