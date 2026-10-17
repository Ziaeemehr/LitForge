from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from lens_fakes import evidence_id
from typer.testing import CliRunner

from litforge.analysis.gateway import ModelGateway
from litforge.cli import app
from litforge.config import Config, Paths
from litforge.screening.criteria import Criteria, save_criteria
from litforge.screening.llm import FakeLlm


def test_pdf_claim_graph_fulltext_queue_and_benchmark_cli(paths: Paths, corpus: Path, conn: sqlite3.Connection,
                                                         tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    base = ["--data-dir", str(paths.root)]
    def reply(m: list[dict[str, str]]) -> dict[str, Any]:
        system = m[0]["content"]
        if "Extract at most" in system:
            return {"claims": [{"text": "Neural mass parameters can be inferred.", "type": "finding",
                                "polarity": "positive", "outcome": "inference", "evidence_ids": [evidence_id(m)]}]}
        if "Classify" in system:
            return {"relation": "replicates", "explanation": "Same finding", "possible_causes": []}
        if "Screen" in system:
            return {"relevance": 4, "inclusion": [], "exclusion": [], "reason": "Neural mass inference"}
        return {"sentences": [{"text": "Neural mass parameters can be inferred.", "kind": "reported_fact",
                               "evidence_ids": [evidence_id(m)]}], "insufficient": False}
    fake = FakeLlm(reply)
    original = ModelGateway.__init__
    def init(self: ModelGateway, db: sqlite3.Connection, config: Config) -> None:
        original(self, db, config, providers={"local": fake})
    monkeypatch.setattr(ModelGateway, "__init__", init)
    def invoke(args: list[str]) -> str:
        result = runner.invoke(app, base + args)
        assert result.exit_code == 0, (result.output, result.exception)
        return result.output
    invoke(["new", "phase2"])
    invoke(["add", str(corpus / "single_column.pdf"), str(corpus / "two_column.pdf")])
    pid = conn.execute("SELECT id FROM projects WHERE name='phase2'").fetchone()[0]
    save_criteria(conn, pid, Criteria(question="neural mass inference"))
    invoke(["claims", "extract", "--preview"])
    assert fake.calls == 0
    invoke(["claims", "extract"])
    assert fake.calls == 2
    invoke(["claims", "cluster"])
    invoke(["claims", "relate", "--max-pairs", "1"])
    landscape = json.loads(invoke(["claims", "landscape", "--json"]))
    assert len(landscape) == 1 and landscape[0]["replication_count"] == 1
    claims = json.loads(invoke(["claims", "show", "--cluster", landscape[0]["cluster_id"], "--json"]))
    assert len(claims["claims"]) == 2 and claims["evidence"]
    cid = claims["claims"][0]["id"]
    invoke(["claims", "review", "accept", cid])
    invoke(["claims", "review", "edit", cid, "--text", "Reviewed finding."])
    report = json.loads(invoke(["screen", "--fulltext", "--json"]))
    assert report["screened"] == 2
    queue = json.loads(invoke(["queue", "-n", "2", "--json"]))
    assert len(queue) == 2 and all(q["stage"] == "S2" for q in queue)
    answer = json.loads(invoke(["ask", "neural mass", "--no-audit", "--json"]))
    out = tmp_path / "claims-brief.md"
    invoke(["claims", "cluster"])
    invoke(["brief", answer["run_id"], "--claims", "-o", str(out)])
    assert "## Claim landscape" in out.read_text() and "Evidence strength" in out.read_text()
    bench = tmp_path / "screen.yaml"
    paper = queue[0]["paper_id"]
    bench.write_text(f"name: screening\nscreening:\n  - paper: {paper}\n    relevant: true\n")
    assert json.loads(invoke(["eval", "screening", str(bench), "--json"]))["passed"]
    conn.execute("UPDATE fulltext_screening SET decision='exclude'")
    result = runner.invoke(app, base + ["eval", "screening", str(bench), "--json"])
    assert result.exit_code == 1 and not json.loads(result.output)["passed"]
    permissive = tmp_path / "permissive.yaml"
    permissive.write_text("screening:\n  max_false_negative_rate: 1\n")
    result = runner.invoke(app, base + ["--config", str(permissive), "eval", "screening", str(bench), "--json"])
    assert result.exit_code == 0 and json.loads(result.output)["threshold"] == 1


@pytest.mark.parametrize("command", ["extract", "relate", "fulltext"])
def test_phase2_cli_preview_and_approval_without_dispatch(command: str, conn: sqlite3.Connection,
                                                        paths: Paths, tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from lens_fakes import seed

    from litforge.analysis.claim_graph import cluster_claims
    from litforge.analysis.claims import extract_claims
    seed(conn)
    seed(conn, "q")
    save_criteria(conn, "proj", Criteria(question="gain"))
    fake = FakeLlm(lambda m: {"claims": [{"text": "Gain improves outcomes", "type": "finding",
                  "polarity": "positive", "evidence_ids": [evidence_id(m)]}]})
    extract_claims(conn, ModelGateway(conn, Config(), providers={"local": fake}), "proj")
    cluster_claims(conn, "proj")
    config = tmp_path / "paid.yaml"
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
        pytest.fail("preview or missing approval called provider")
    monkeypatch.setattr(ModelGateway, "_provider", forbidden)
    base = ["--data-dir", str(paths.root), "--config", str(config)]
    args = ["screen", "--fulltext"] if command == "fulltext" else ["claims", command]
    args += ["-p", "demo", "--profile", "paid"]
    runner = CliRunner()
    preview = runner.invoke(app, base + args + ["--preview"])
    assert preview.exit_code == 0, (preview.output, preview.exception)
    assert json.loads(preview.output)["number_of_calls"] >= 1
    assert runner.invoke(app, base + args).exit_code == 3


def test_claims_cli_reports_no_text(conn: sqlite3.Connection, paths: Paths) -> None:
    from lens_fakes import seed
    seed(conn)
    conn.execute("DELETE FROM chunks")
    conn.commit()
    result = CliRunner().invoke(app, ["--data-dir", str(paths.root), "claims", "extract", "-p", "demo"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "1 no_text" in result.output


def test_landscape_cli_prints_origin_title_year(conn: sqlite3.Connection, paths: Paths) -> None:
    from lens_fakes import seed

    from litforge.analysis.claim_graph import cluster_claims
    from litforge.analysis.claims import extract_claims
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="gain"))
    conn.execute("UPDATE papers SET year=2020 WHERE id='p'")
    fake = FakeLlm(lambda m: {"claims": [{"text": "Gain improves outcomes", "type": "finding",
                   "polarity": "positive", "evidence_ids": [evidence_id(m)]}]})
    extract_claims(conn, ModelGateway(conn, Config(), providers={"local": fake}), "proj")
    cluster_claims(conn, "proj")
    result = CliRunner().invoke(app, ["--data-dir", str(paths.root), "claims", "landscape", "-p", "demo"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "origin Title p (2020)" in result.output


def test_screening_aliases_do_not_change_cli_exit(conn: sqlite3.Connection, paths: Paths, tmp_path: Path) -> None:
    from lens_fakes import seed
    seed(conn)
    seed(conn, "q")
    conn.execute("UPDATE papers SET doi='10.1234/gain' WHERE id='p'")
    conn.execute("INSERT INTO screening_results(project_id,paper_id,decision,score,result_json,model,"
                 "prompt_version,criteria_hash,fingerprint,screened_at) "
                 "VALUES ('proj','q','exclude',0,'{}','fake','v','h','f','now')")
    bench = tmp_path / "aliases.yaml"
    bench.write_text("name: aliases\nscreening:\n  - {paper: p, relevant: true}\n"
                     "  - {paper: '10.1234/gain', relevant: true}\n  - {paper: q, relevant: true}\n")
    config = tmp_path / "threshold.yaml"
    config.write_text("screening:\n  max_false_negative_rate: 0.4\n")
    result = CliRunner().invoke(app, ["--data-dir", str(paths.root), "--config", str(config),
                                     "eval", "screening", str(bench), "-p", "demo", "--json"])
    assert result.exit_code == 1, (result.output, result.exception)
    report = json.loads(result.output)
    assert report["stages"]["S1"]["false_negative_rate"] == .5
    assert not report["passed"]
