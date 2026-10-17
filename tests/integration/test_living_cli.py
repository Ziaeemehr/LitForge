from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from discovery_fakes import FakeTransport, default_routes
from lens_fakes import evidence_id, seed
from typer.testing import CliRunner

from litforge.analysis import watch
from litforge.analysis.gateway import ModelGateway
from litforge.cli import app
from litforge.config import Config, Paths
from litforge.discovery.http import HttpClient
from litforge.discovery.run import Sources
from litforge.screening.criteria import Criteria, save_criteria
from litforge.screening.llm import FakeLlm


def test_snapshot_delta_refresh_repro_and_explicit_schedule(conn: sqlite3.Connection, paths: Paths,
                                                          tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(conn, text="Results: gain data availability dataset names code hardware metrics compute budget")
    save_criteria(conn, "proj", Criteria(question="gain"))
    fake = FakeLlm(lambda m: {"status": "reported", "value": "gain", "evidence_ids": [evidence_id(m)]})
    original = ModelGateway.__init__
    def init(self: ModelGateway, db: sqlite3.Connection, config: Config) -> None:
        original(self, db, config, providers={"local": fake})
    monkeypatch.setattr(ModelGateway, "__init__", init)
    monkeypatch.setattr("litforge.cli._sources", lambda st, refresh: Sources(
        HttpClient(transport=FakeTransport(default_routes()))))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    base = ["--data-dir", str(paths.root)]
    runner = CliRunner()
    def invoke(args: list[str]) -> str:
        result = runner.invoke(app, base + args)
        assert result.exit_code == 0, (result.output, result.exception)
        return result.output
    first = invoke(["snapshot", "-p", "demo", "--label", "baseline"]).strip()
    second = invoke(["snapshot", "-p", "demo"]).strip()
    assert first in invoke(["snapshots", "-p", "demo"])
    md = tmp_path / "delta.md"
    invoke(["delta", first, second, "--md", str(md)])
    assert "No significant signals" in md.read_text()
    notified: list[object] = []
    monkeypatch.setattr(watch, "notify_signals", lambda signals: notified.append(signals))
    invoke(["refresh", "-p", "demo", "--no-screen", "--no-extract", "--no-claims", "--notify"])
    assert not notified
    seed(conn, "q")
    conn.execute("INSERT INTO paper_marks VALUES ('proj','q','relevant','now')")
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,status,started_at) "
                 "VALUES ('d','proj','pdfs','done','now')")
    conn.execute("INSERT INTO discovery_results(run_id,paper_id,score,n_hits,new_to_project,priority) "
                 "VALUES ('d','q',1,1,1,1)")
    invoke(["refresh", "-p", "demo", "--no-screen", "--no-extract", "--no-claims", "--notify"])
    assert len(notified) == 1
    assert not list(home.rglob("*.plist")) and not list(home.rglob("*.timer"))
    report = tmp_path / "repro.md"
    invoke(["repro", "-p", "demo", "--paper", "p", "--md", str(report)])
    assert "Reproduction brief" in report.read_text() and fake.calls > 0
    invoke(["watch", "install", "-p", "demo", "--every", "7d"])
    assert list(home.rglob("*.plist")) or list(home.rglob("*.timer"))
    invoke(["watch", "remove", "-p", "demo"])
    assert not list(home.rglob("*.plist")) and not list(home.rglob("*.timer"))


def test_repro_preview_and_approval_never_dispatches(conn: sqlite3.Connection, paths: Paths,
                                                   tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(conn, text="Results: gain data availability dataset names code hardware metrics compute budget")
    config = tmp_path / "remote.yaml"
    config.write_text("""models:
  policy: ask_before_cloud
  profiles:
    paid:
      provider: openai_compatible
      model: remote
      remote: true
      pricing: {input_per_million_usd: 1, output_per_million_usd: 1}
privacy:
  allow_cloud: true
""")
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("provider dispatched")
    monkeypatch.setattr(ModelGateway, "_provider", forbidden)
    base = ["--data-dir", str(paths.root), "--config", str(config), "repro", "-p", "demo", "--profile", "paid"]
    assert CliRunner().invoke(app, base + ["--preview"]).exit_code == 0
    assert CliRunner().invoke(app, base).exit_code == 3
    config.write_text(config.read_text() + "budget:\n  max_per_call_usd: 0\n")
    assert CliRunner().invoke(app, base + ["--approve"]).exit_code == 1
