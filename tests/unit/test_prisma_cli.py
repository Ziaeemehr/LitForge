"""PRISMA CLI writes UTF-8 SVG and emits machine-readable counts."""
import json
import xml.etree.ElementTree as ET

from typer.testing import CliRunner

from litforge.cli import app
from litforge.projects import create_project


def test_prisma_cli(conn, paths, tmp_path):
    create_project(conn, paths, "Review")
    runner = CliRunner()
    args = ["--data-dir", str(paths.root), "prisma", "-p", "Review"]
    reply = runner.invoke(app, args + ["--json"])
    assert reply.exit_code == 0, reply.output
    assert json.loads(reply.output)["included"] == 0
    out = tmp_path / "custom.svg"
    reply = runner.invoke(app, args + ["-o", str(out)])
    assert reply.exit_code == 0, reply.output
    assert str(out) in reply.output
    ET.fromstring(out.read_text())
    reply = runner.invoke(app, args)
    assert reply.exit_code == 0, reply.output
    assert (paths.default_exports("Review") / "prisma.svg").is_file()
