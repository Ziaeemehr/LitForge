from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from lens_fakes import evidence_id
from typer.testing import CliRunner

from litforge.analysis.gateway import ModelGateway
from litforge.cli import app
from litforge.config import Config, Paths
from litforge.screening.llm import FakeLlm


def test_pdf_to_answer_brief_incremental_matrix(paths: Paths, corpus: Path, conn: sqlite3.Connection,
                                               tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    base = ["--data-dir", str(paths.root)]
    def reply(m: list[dict[str, str]]) -> dict[str, object]:
        if "Audit" in m[0]["content"]:
            return {"verdict": "supported", "note": "yes"}
        if "Extract" in m[0]["content"]:
            return {"status": "reported", "value": "neural mass", "unit": None,
                    "evidence_ids": [evidence_id(m)], "note": ""}
        return {"sentences": [{"text": "Neural mass models are used.", "kind": "reported_fact",
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
    invoke(["new", "demo"])
    invoke(["add", str(corpus / "single_column.pdf")])
    invoke(["ask", "neural mass", "--preview"])
    assert fake.calls == 0
    answer = json.loads(invoke(["ask", "neural mass", "--json"]))
    assert fake.calls == 2 and answer["sentences"][0]["audit_verdict"] == "supported"
    out = tmp_path / "brief.md"
    invoke(["brief", answer["run_id"], "-o", str(out)])
    assert "[^ev_" in out.read_text()
    columns = tmp_path / "cols.yaml"
    columns.write_text("columns:\n  - key: model\n    label: Model\n    instruction: neural mass\n    type: text\n")
    invoke(["matrix", "create", "studies", "--question", "neural mass", "--columns", str(columns)])
    invoke(["matrix", "extract", "studies", "--preview"])
    assert fake.calls == 2
    invoke(["matrix", "extract", "studies"])
    assert fake.calls == 3
    invoke(["matrix", "add-column", "studies", "method", "--label", "Method", "--instruction", "neural mass",
            "--type", "text"])
    invoke(["matrix", "extract", "studies"])
    assert fake.calls == 4
    assert "model_status" in invoke(["matrix", "export", "studies", "--format", "csv"])
    assert "studies" in invoke(["matrix", "list"])
    invoke(["review", "--json"])
    bench = tmp_path / "bench.yaml"
    paper = conn.execute("SELECT id FROM papers LIMIT 1").fetchone()[0]
    bench.write_text(f"""name: tiny
questions:
  - id: q
    question: neural mass
    relevant:
      - paper: {paper}
matrix:
  - paper: {paper}
    column: model
    status: reported
    value: neural mass
""")
    report = json.loads(invoke(["eval", "matrix", str(bench), "--matrix", "studies", "--json"]))
    assert report["status_accuracy"] == report["value_accuracy"] == report["citation_validity"] == 1
    report = json.loads(invoke(["eval", "answers", str(bench), "--json"]))
    assert report["citation_precision"] == 1 and report["unsupported_sentence_rate"] == 0


@pytest.mark.parametrize("command", ["ask", "audit", "matrix"])
def test_preview_and_approval_exit_before_dispatch(command: str, conn: sqlite3.Connection,
                                                   paths: Paths, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from lens_fakes import gateway, seed

    from litforge.analysis.ask import ask
    from litforge.analysis.matrix import MatrixColumn, create_matrix

    seed(conn)
    gw, _ = gateway(conn)
    answer = ask(conn, gw, "gain", project_id="proj", sensitive=True)
    create_matrix(conn, "proj", "m", "gain",
                  [MatrixColumn(key="gain", label="Gain", instruction="gain", type="number")])
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
    runner = CliRunner()
    base = ["--data-dir", str(paths.root), "--config", str(config)]
    args = {"ask": ["ask", "gain", "-p", "demo"], "audit": ["audit", answer.run_id],
            "matrix": ["matrix", "extract", "m", "-p", "demo"]}[command]
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("preview or missing approval dispatched a provider")
    monkeypatch.setattr(ModelGateway, "_provider", forbidden)
    before = conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
    result = runner.invoke(app, base + args + ["--profile", "paid", "--preview"])
    assert result.exit_code == 0, result.output
    assert "remote" in result.output and "estimated_input_tokens" in result.output
    result = runner.invoke(app, base + args + ["--profile", "paid"])
    assert result.exit_code == 3, (result.output, result.exception)
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == before

    def reply(messages: list[dict[str, str]]) -> dict[str, object]:
        if "Audit" in messages[0]["content"]:
            return {"verdict": "supported", "note": "checked"}
        if "Extract" in messages[0]["content"]:
            return {"status": "not_reported", "value": None, "evidence_ids": []}
        return {"sentences": [{"text": "Gain 3.25.", "kind": "reported_fact",
                               "evidence_ids": [evidence_id(messages)]}], "insufficient": False}
    fake = FakeLlm(reply)
    monkeypatch.setattr(ModelGateway, "_provider", lambda *args: fake)
    result = runner.invoke(app, base + args + ["--profile", "paid", "--approve"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert fake.calls == (2 if command == "ask" else 1)


def test_different_audit_profile_requires_approval_before_ask(conn: sqlite3.Connection, paths: Paths,
                                                            tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from lens_fakes import seed

    seed(conn)
    config = tmp_path / "cfg.yaml"
    config.write_text("""models:
  policy: ask_before_cloud
  profiles:
    auditor:
      provider: fake
      model: audit-model
      remote: true
      pricing: {input_per_million_usd: 1, output_per_million_usd: 1}
privacy:
  allow_cloud: true
""")
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("answer dispatched before auditor approval")
    monkeypatch.setattr(ModelGateway, "_provider", forbidden)
    result = CliRunner().invoke(app, ["--data-dir", str(paths.root), "--config", str(config),
                                     "ask", "gain", "--audit-profile", "auditor"])
    assert result.exit_code == 3, result.output
    plan = json.loads(result.output)
    assert plan["number_of_calls"] == 9 and plan["profiles"] == ["auditor", "local"]


@pytest.mark.parametrize("command", ["ask", "audit"])
def test_cli_validation_error_hides_model_content(conn: sqlite3.Connection, paths: Paths,
                                                  monkeypatch: pytest.MonkeyPatch, command: str) -> None:
    from lens_fakes import gateway, seed

    from litforge.analysis.ask import ask

    seed(conn)
    gw, _ = gateway(conn)
    answer = ask(conn, gw, "gain")
    secret = "PRIVATE_TOKEN_987654321"
    fake = FakeLlm(lambda _: {"verdict": secret, "sentences": secret})
    monkeypatch.setattr(ModelGateway, "_provider", lambda *args: fake)
    args = ["ask", "gain", "--no-audit"] if command == "ask" else ["audit", answer.run_id]
    result = CliRunner().invoke(app, ["--data-dir", str(paths.root), *args])
    assert result.exit_code == 1
    assert secret not in result.output
    assert result.output.strip() == "ValidationError: invalid model response"


def test_ask_prints_capped_quotes_and_readable_flags(conn: sqlite3.Connection, paths: Paths,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from lens_fakes import gateway, seed

    span = seed(conn, text="Results: " + "gain improves patient outcomes. " * 30)
    payload = {"sentences": [
        {"text": "Gain improves outcomes.", "kind": "reported_fact", "evidence_ids": [span.id],
         "quotes": [{"evidence_id": span.id, "text": "gain improves patient outcomes. " * 20}]},
        {"text": "Gain changes outcomes.", "kind": "reported_fact", "evidence_ids": [span.id],
         "quotes": [{"evidence_id": span.id, "text": "An invented supporting passage."}]},
    ], "insufficient": False}
    _, fake = gateway(conn, payload)
    original = ModelGateway.__init__

    def init(self: ModelGateway, db: sqlite3.Connection, config: Config) -> None:
        original(self, db, config, providers={"local": fake})

    monkeypatch.setattr(ModelGateway, "__init__", init)
    result = CliRunner().invoke(app, ["--data-dir", str(paths.root), "ask", "gain", "--no-audit"])
    assert result.exit_code == 0, result.output
    assert "Quote not in evidence" in result.output and "No exact quote" in result.output
    assert "quote_not_in_evidence" not in result.output and "unquoted_fact" not in result.output
    quote_lines = [line for line in result.output.splitlines() if line.startswith("   > ")]
    assert len(quote_lines) == 1 and len(quote_lines[0].split("”")[0].removeprefix("   > “")) <= 400
    assert result.output.index("Gain improves outcomes.") < result.output.index("   > ")
