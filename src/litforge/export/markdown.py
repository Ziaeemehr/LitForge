"""Obsidian/Git-friendly Markdown export of a project's papers (relative asset links)."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections.abc import Collection
from pathlib import Path

import yaml

from litforge.config import Paths
from litforge.parsing.canonical import read_canonical, to_markdown
from litforge.projects import MARK_SCOPES


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "paper"


def default_export_dir(conn: sqlite3.Connection, paths: Paths, project: str, project_id: str) -> Path:
    row = conn.execute("SELECT export_dir FROM projects WHERE id=?", (project_id,)).fetchone()
    return Path(row["export_dir"]).expanduser() if row and row["export_dir"] else paths.project_dir(project) / "exports"


def export_markdown(
    conn: sqlite3.Connection,
    paths: Paths,
    project: str,
    project_id: str,
    out_dir: Path | None = None,
    only: str = "all",
    paper_ids: Collection[str] | None = None,
) -> tuple[Path, int]:
    """Write exports/markdown/{README.md, knowledge_base/*.md, assets/<slug>/...}; returns (root, papers).
    `only` filters by the project's marks and `paper_ids` limits to an explicit selection. Notes and
    assets that the previous export wrote (listed in its manifest.json) but this one does not are
    removed, so the folder matches the selection; other files are never touched, and an empty
    selection leaves the folder as it is. Private by default: contains full extracted text and figure
    crops, so do not publish without a copyright review."""
    if only not in MARK_SCOPES:
        raise ValueError(f"only must be one of {sorted(MARK_SCOPES)}")
    root = (out_dir or default_export_dir(conn, paths, project, project_id)) / "markdown"
    kb = root / "knowledge_base"
    rows = conn.execute(
        f"""SELECT p.id, p.title, p.year, p.doi, p.doi_status, p.abstract, a.sha256, r.parser, r.status, r.output_dir
           FROM project_papers pp
           JOIN papers p ON p.id = pp.paper_id
           LEFT JOIN artifacts a ON a.paper_id = p.id AND a.status = 'ok'
           LEFT JOIN parse_runs r ON r.artifact_id = a.id AND r.is_current = 1
           LEFT JOIN paper_marks m ON m.paper_id = p.id AND m.project_id = pp.project_id
           WHERE pp.project_id = ?{MARK_SCOPES[only]} ORDER BY p.year, p.title""",
        (project_id,),
    ).fetchall()
    if paper_ids is not None:
        wanted = set(paper_ids)
        rows = [r for r in rows if r["id"] in wanted]
    if not rows:
        return root, 0
    kb.mkdir(parents=True, exist_ok=True)
    previous = _previous_slugs(root / "manifest.json")
    written: dict[str, str] = {}  # paper id -> slug
    index_lines = [
        f"# {project}\n",
        "> Private export: contains full extracted text and figure crops.\n",
        "| Paper | Year | Status | PDF |",
        "|---|---|---|---|",
    ]
    for row in rows:
        title = row["title"] or f"Untitled ({(row['sha256'] or row['id'])[:8]})"
        slug = f"{slugify(title)}-{row['id'][:8]}"
        written[row["id"]] = slug
        front = {
            "paper_id": row["id"],
            "title": title,
            "year": row["year"],
            "doi": row["doi"],
            "doi_status": row["doi_status"],
            "project": project,
            "parse_status": row["status"] or ("not_parsed" if row["sha256"] else "no_pdf"),
            "parser": row["parser"],
            "pdf_sha256": row["sha256"],
        }
        body = "_Not parsed yet._\n" if row["sha256"] else f"# {title}\n\n_No PDF in the library yet._\n"
        if not row["sha256"] and row["abstract"]:
            body += f"\n## Abstract\n\n{row['abstract']}\n"
        if row["output_dir"]:
            out_dir = paths.root / row["output_dir"]
            doc = read_canonical(out_dir)
            if (out_dir / "assets").is_dir():
                dest = root / "assets" / slug / "assets"
                shutil.copytree(out_dir / "assets", dest, dirs_exist_ok=True)
            body = to_markdown(doc, title, asset_prefix=f"../assets/{slug}/")
            if row["doi"]:
                body += f"\n## Links\n\n- DOI ({row['doi_status']}): https://doi.org/{row['doi']}\n"
        header = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n\n"
        (kb / f"{slug}.md").write_text(header + body, encoding="utf-8")
        index_lines.append(
            f"| [{title.replace('|', '/')}](knowledge_base/{slug}.md) | {row['year'] or ''} "
            f"| {front['parse_status']} | {(row['sha256'] or '')[:10]} |"
        )
    for slug in previous - set(written.values()):
        (kb / f"{slug}.md").unlink(missing_ok=True)
        shutil.rmtree(root / "assets" / slug, ignore_errors=True)
    (root / "README.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps([{**dict(r), "slug": written[r["id"]]} for r in rows], indent=1, ensure_ascii=False),
        encoding="utf-8",
    )
    return root, len(rows)


def _previous_slugs(manifest: Path) -> set[str]:
    """Slugs the last export wrote; only these may be cleaned up (never the user's own files)."""
    try:
        entries = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {e["slug"] for e in entries if isinstance(e, dict) and re.fullmatch(r"[a-z0-9-]+", str(e.get("slug")))}
