"""Generate docs/cli.md from the CLI itself: python scripts/gen_cli_docs.py > docs/cli.md"""
import sys

import typer
from typer.core import TyperArgument, TyperOption

from litforge.cli import app

root = typer.main.get_command(app)
GROUPS = [
    ("Projects and library", ["new", "project", "library", "status", "papers", "doctor"]),
    ("Adding papers", ["ingest", "add", "import", "discover", "similar", "enrich", "fetch"]),
    ("Reading PDFs", ["parse", "index", "enhance", "versions", "asset", "glossary", "tools"]),
    ("Searching and screening", ["search", "screen", "queue"]),
    ("Analysis", ["ask", "audit", "matrix", "claims", "gaps", "repro", "brief", "do", "console", "eval"]),
    ("Living review", ["refresh", "snapshot", "snapshots", "delta", "watch"]),
    ("Export", ["export"]),
    ("Models", ["models", "calls"]),
    ("App and maintenance", ["ui", "shortcut", "update"]),
]

def first_line(text):
    return (text or "").strip().split("\n\n")[0].replace("\n", " ").strip()

def options(cmd):
    rows = []
    for p in cmd.params:
        if isinstance(p, TyperArgument):
            rows.append((f"`{p.human_readable_name}`", "argument" + (" (required)" if p.required else "")))
        elif isinstance(p, TyperOption):
            if "--help" in p.opts:
                continue
            flags = ", ".join(f"`{o}`" for o in p.opts + p.secondary_opts)
            rows.append((flags, first_line(p.help)))
    return rows

def emit(cmd, path, level, out):
    out.append(f"{'#' * level} `{' '.join(path)}`\n")
    if first_line(cmd.help):
        out.append(first_line(cmd.help) + "\n")
    rows = options(cmd)
    if rows:
        out.append("| Option | Meaning |\n|---|---|")
        out += [f"| {a} | {b} |" for a, b in rows]
        out.append("")
    for name, sub in sorted(getattr(cmd, "commands", {}).items()):
        if not sub.hidden:
            emit(sub, path + [name], min(level + 1, 4), out)

out = ["# Command line\n",
       "Every LitForge feature is also available from the terminal. In an installed copy run commands from the",
       "install folder with `uv run litforge …`; with an activated environment just `litforge …`.",
       "Global options come before the command, e.g. `litforge --data-dir ~/research/data status`.",
       "Add `--help` to any command for the same information.\n",
       "This page is generated from the program itself (`python scripts/gen_cli_docs.py`), so it matches the installed",
       "version.\n"]
glob = options(root)
if glob:
    out.append("## Global options\n")
    out.append("| Option | Meaning |\n|---|---|")
    out += [f"| {a} | {b} |" for a, b in glob]
    out.append("")
seen = set()
for title, names in GROUPS:
    present = [n for n in names if n in root.commands and not root.commands[n].hidden]
    if not present:
        continue
    out.append(f"## {title}\n")
    for n in present:
        seen.add(n)
        emit(root.commands[n], ["litforge", n], 3, out)
rest = sorted(n for n, c in root.commands.items() if n not in seen and not c.hidden)
if rest:
    out.append("## Other\n")
    for n in rest:
        emit(root.commands[n], ["litforge", n], 3, out)
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]  # Windows consoles
print("\n".join(out))
