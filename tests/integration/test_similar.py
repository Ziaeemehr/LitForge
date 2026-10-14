"""Seed-based similar-paper search against canned API responses."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from discovery_fakes import FakeTransport, default_routes

from litforge.config import Config, Paths
from litforge.discovery.http import HttpClient
from litforge.discovery.run import Sources
from litforge.discovery.similar import run_similar
from litforge.projects import create_project


class FakeEmbedder:
    """Bag-of-letters vectors: deterministic, enough to exercise the embedding path."""

    name = "fake"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(t.lower().count(ch)) for ch in "abcdefghijklmnopqrstuvwxyz"] for t in texts]


@pytest.fixture()
def sources() -> Sources:
    return Sources(HttpClient(transport=FakeTransport(default_routes()), sleep=lambda s: None, min_interval={}))


@pytest.fixture()
def pid(conn: sqlite3.Connection, paths: Paths) -> str:
    return create_project(conn, paths, "sim")


def test_similar_from_doi_combines_channels(conn: sqlite3.Connection, paths: Paths, config: Config,
                                            sources: Sources, pid: str) -> None:
    report = run_similar(conn, paths, config, sources, pid, ["10.1073/pnas.1912789117"])
    assert report.status == "done", report.errors
    by_title = {p.title: p for p in report.top}
    seed = by_title["The frontier of simulation-based inference"]
    apt = by_title["Automatic Posterior Transformation for Likelihood-Free Inference"]
    citing = by_title["Neural mass model inference with simulation-based methods"]
    assert report.top[0] is seed and seed.reasons == ["seed paper"]
    assert 0 < citing.score < 1 and 0 < apt.score < 1
    assert "cited by the seed" in apt.reasons and "shares 2 references" in apt.reasons
    assert "related work (OpenAlex)" in apt.reasons and any(r.startswith("keywords:") for r in apt.reasons)
    assert "cites the seed" in citing.reasons and "shares 1 reference" in citing.reasons
    rels = conn.execute("SELECT COUNT(*) FROM paper_relations").fetchone()[0]
    assert rels == 2
    comps = conn.execute(
        "SELECT components_json FROM discovery_results WHERE run_id=? AND paper_id=?", (report.run_id, apt.paper_id)
    ).fetchone()[0]
    assert '"coupling": 1.0' in comps and '"direct": 1.0' in comps


def test_similar_from_abstract_only(conn: sqlite3.Connection, paths: Paths, config: Config,
                                    sources: Sources, pid: str) -> None:
    abstract = ("Neural posterior estimation enables simulation-based inference of model parameters. "
                "We apply simulation-based inference to mechanistic models.")
    report = run_similar(conn, paths, config, sources, pid, abstract=abstract, embedder=FakeEmbedder())
    assert report.status == "done", report.errors
    assert report.top and all(p.reasons and p.reasons != ["seed paper"] for p in report.top)
    kinds = {r["relation"] for r in conn.execute("SELECT relation FROM discovery_hits")}
    # No identifier: key-phrase search first, then the citations of the closest hits.
    assert kinds == {"keywords", "neighbours"}
    assert any(any(r.startswith("citation link to:") for r in p.reasons) for p in report.top)
    comp = conn.execute("SELECT components_json FROM discovery_results LIMIT 1").fetchone()[0]
    assert '"embedding": 0.0' not in comp


def test_similar_from_pdf_adds_it_to_the_project(conn: sqlite3.Connection, paths: Paths, config: Config,
                                                 sources: Sources, pid: str, corpus: Path) -> None:
    report = run_similar(conn, paths, config, sources, pid, [str(corpus / "single_column.pdf")])
    assert not [e for e in report.errors if "seed" in e]
    seed = report.top[0]  # not in OpenAlex, but still the seed: listed first, with its PDF
    assert seed.title == "Inferring Neural Mass Parameters" and seed.has_pdf and seed.reasons == ["seed paper"]
    assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE status='ok'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
    assert report.hits > 0  # key-phrase queries from the PDF's title/abstract


def test_unknown_seed_is_reported(conn: sqlite3.Connection, paths: Paths, config: Config,
                                  sources: Sources, pid: str) -> None:
    report = run_similar(conn, paths, config, sources, pid, ["10.0000/does.not.exist"])
    assert report.status == "failed" and "not found" in report.errors[0]


def test_cli_similar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from litforge import cli

    transport = FakeTransport(default_routes())
    monkeypatch.setattr(cli, "HttpClient", lambda **kw: HttpClient(
        **{**kw, "transport": transport, "sleep": lambda s: None, "min_interval": {}}))
    runner = CliRunner()
    base = ["--data-dir", str(tmp_path / "data")]
    result = runner.invoke(cli.app, base + ["similar", "10.1073/pnas.1912789117", "--no-embeddings"])
    assert result.exit_code == 0, result.output
    assert "created project 'default'" in result.output and "why: seed paper" in result.output
    abstract = tmp_path / "abstract.txt"
    abstract.write_text("Simulation-based inference with neural posterior estimation for mechanistic models.")
    result = runner.invoke(cli.app, base + ["similar", "--abstract-file", str(abstract), "--no-embeddings"])
    assert result.exit_code == 0, result.output
    assert runner.invoke(cli.app, base + ["similar"]).exit_code == 2  # nothing to search from


def test_similar_results_carry_importance(conn: sqlite3.Connection, paths: Paths, config: Config,
                                          sources: Sources, pid: str) -> None:
    report = run_similar(conn, paths, config, sources, pid, ["10.1073/pnas.1912789117"])
    seed = next(p for p in report.top if p.reasons == ["seed paper"])
    assert "top 1% cited in its field" in seed.importance_notes
    assert "cited by 1 paper in this set" in seed.importance_notes  # the citing paper
    assert any(n.startswith("venue mean citedness 8.8") for n in seed.importance_notes)
    assert seed.importance > 0.5 and seed.priority >= max(p.priority for p in report.top) - 1e-9
    row = conn.execute("SELECT mean_citedness FROM venues WHERE id='S125754415'").fetchone()
    assert row["mean_citedness"] == 8.84
    assert all(0 <= p.priority <= 1 for p in report.top)
