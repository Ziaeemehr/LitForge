"""Screening against a fake Ollama HTTP server and a fake LLM (no GPU needed)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from litforge.config import Paths
from litforge.llm import LlmError as ProviderLlmError
from litforge.llm import ModelReply
from litforge.projects import create_project
from litforge.screening.criteria import Criteria, save_criteria
from litforge.screening.llm import FakeLlm, LlmError, OllamaClient
from litforge.screening.prompt import build_messages, response_schema
from litforge.screening.run import screen_run
from litforge.storage.db import new_id, now, transaction


class _Ollama(BaseHTTPRequestHandler):
    models: list[str] = ["qwen3:4b"]
    reply: str = '{"relevance": 4, "inclusion": [], "exclusion": [], "reason": "ok"}'
    seen: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _json(self, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._json({"models": [{"name": m} for m in self.models]})

    def do_POST(self) -> None:  # noqa: N802
        _Ollama.seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self._json({"message": {"role": "assistant", "content": self.reply}})


@pytest.fixture()
def ollama() -> Iterator[str]:
    _Ollama.models, _Ollama.seen = ["qwen3:4b"], []
    _Ollama.reply = '{"relevance": 4, "inclusion": [], "exclusion": [], "reason": "ok"}'
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_ollama_client_sends_schema_and_parses_json(ollama: str) -> None:
    c = Criteria(question="q")
    llm = OllamaClient(ollama, "qwen3:4b")
    llm.check()
    out = llm.chat_json(build_messages(c, "t", "a", None, None), response_schema(c))
    assert out["relevance"] == 4
    sent = _Ollama.seen[0]
    assert sent["model"] == "qwen3:4b" and sent["stream"] is False and sent["think"] is False
    assert sent["format"]["type"] == "object" and sent["options"]["temperature"] == 0


def test_ollama_client_accepts_latest_tag(ollama: str) -> None:
    _Ollama.models = ["qwen3:latest"]
    OllamaClient(ollama, "qwen3").check()


def test_ollama_missing_model_names_pull_command(ollama: str) -> None:
    _Ollama.models = ["llama3:8b"]
    with pytest.raises(LlmError, match="ollama pull qwen3:4b"):
        OllamaClient(ollama, "qwen3:4b").check()


def test_ollama_unreachable_is_llm_error() -> None:
    with pytest.raises(LlmError, match="not reachable"):
        OllamaClient("http://127.0.0.1:9", "qwen3:4b", timeout_s=1).check()


def test_ollama_invalid_json_is_llm_error(ollama: str) -> None:
    _Ollama.reply = "not json"
    with pytest.raises(LlmError, match="invalid JSON"):
        OllamaClient(ollama, "qwen3:4b").chat_json([{"role": "user", "content": "x"}], {})


CRIT = Criteria(question="neural mass model inference", include=["uses a neural mass model"],
                exclude=["animal study only"])


def make_run(conn: sqlite3.Connection, paths: Paths,
             papers: list[tuple[str, str | None]]) -> tuple[str, str, list[str]]:
    """A project with one discovery run; papers ranked in the given order (first = highest priority)."""
    pid = create_project(conn, paths, "scr")
    run_id, ids = new_id(), []
    with transaction(conn):
        save_criteria(conn, pid, CRIT)
        conn.execute("INSERT INTO discovery_runs(id, project_id, kind, status, started_at) VALUES (?,?,?,?,?)",
                     (run_id, pid, "search", "done", now()))
        for i, (title, abstract) in enumerate(papers):
            paper_id = new_id()
            ids.append(paper_id)
            conn.execute("INSERT INTO papers(id, title, abstract, created_at, updated_at) VALUES (?,?,?,?,?)",
                         (paper_id, title, abstract, now(), now()))
            conn.execute("INSERT INTO project_papers(project_id, paper_id, source, added_at) VALUES (?,?,?,?)",
                         (pid, paper_id, "discovery", now()))
            conn.execute("INSERT INTO discovery_results(run_id, paper_id, score, n_hits, new_to_project, priority)"
                         " VALUES (?,?,?,?,?,?)", (run_id, paper_id, 1 - i / 10, 1, 1, 1 - i / 10))
    return pid, run_id, ids


def answer(messages: list[dict[str, str]]) -> dict[str, Any]:
    text = messages[-1]["content"]
    mouse = "mice" in text
    return {"relevance": 1 if mouse else 4,
            "inclusion": [{"status": "met", "evidence": "neural mass model"}],
            "exclusion": [{"status": "violated" if mouse else "not_violated", "evidence": "in mice" if mouse else ""}],
            "reason": "r"}


PAPERS = [("Neural mass model fit", "A neural mass model fitted to EEG."),
          ("Mouse study", "A neural mass model in mice only."),
          ("No abstract", None)]


def test_screen_run_writes_results_and_caches(conn: sqlite3.Connection, paths: Paths) -> None:
    pid, run_id, ids = make_run(conn, paths, PAPERS)
    llm = FakeLlm(answer)
    report = screen_run(conn, llm, run_id, top=10)
    assert (report.candidates, report.screened, report.cached, report.failed) == (3, 3, 0, 0)
    rows = {r["paper_id"]: r for r in conn.execute("SELECT * FROM screening_results WHERE project_id=?", (pid,))}
    assert rows[ids[0]]["decision"] == "include"
    assert rows[ids[1]]["decision"] == "exclude"
    assert rows[ids[2]]["model"] == "fake"
    again = screen_run(conn, llm, run_id, top=10)
    assert (again.screened, again.cached, llm.calls) == (0, 3, 3)


def test_screen_run_top_limits_and_order(conn: sqlite3.Connection, paths: Paths) -> None:
    _, run_id, ids = make_run(conn, paths, PAPERS)
    report = screen_run(conn, FakeLlm(answer), run_id, top=1)
    assert report.candidates == 1
    assert [r[0] for r in conn.execute("SELECT paper_id FROM screening_results")] == [ids[0]]
    with pytest.raises(ValueError, match="at least 1"):
        screen_run(conn, FakeLlm(answer), run_id, top=0)


def test_changed_criteria_rescreen(conn: sqlite3.Connection, paths: Paths) -> None:
    pid, run_id, _ = make_run(conn, paths, PAPERS[:1])
    llm = FakeLlm(answer)
    screen_run(conn, llm, run_id, top=5)
    with transaction(conn):
        save_criteria(conn, pid, Criteria(question="other question", include=["x"], exclude=["y"]))
    assert screen_run(conn, llm, run_id, top=5).screened == 1 and llm.calls == 2


def test_model_failure_retries_once_then_records_uncertain(conn: sqlite3.Connection, paths: Paths) -> None:
    _, run_id, ids = make_run(conn, paths, PAPERS[:1])
    llm = FakeLlm(lambda m: "garbage")
    report = screen_run(conn, llm, run_id, top=5)
    assert report.failed == 1 and llm.calls == 2
    row = conn.execute("SELECT decision, error FROM screening_results WHERE paper_id=?", (ids[0],)).fetchone()
    assert row["decision"] == "uncertain" and row["error"]
    assert screen_run(conn, FakeLlm(answer), run_id, top=5).screened == 1  # failed results are retried


def test_provider_reply_and_shared_error_follow_screening_contract(
    conn: sqlite3.Connection, paths: Paths
) -> None:
    _, run_id, ids = make_run(conn, paths, PAPERS[:1])

    class Provider:
        name = "large-model"

        def __init__(self) -> None:
            self.calls = 0

        def check(self) -> None:
            pass

        def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> ModelReply:
            self.calls += 1
            if self.calls == 1:
                raise ProviderLlmError("temporary invalid response")
            return ModelReply(content=answer(messages), provider="openai_compatible", model=self.name)

    report = screen_run(conn, Provider(), run_id, top=1)
    row = conn.execute("SELECT decision, model FROM screening_results WHERE paper_id=?", (ids[0],)).fetchone()
    assert report.screened == 1
    assert row["decision"] == "include"
    assert row["model"] == "large-model"


def test_no_criteria_or_unreachable_model_touches_nothing(conn: sqlite3.Connection, paths: Paths) -> None:
    pid, run_id, _ = make_run(conn, paths, PAPERS)
    with pytest.raises(LlmError):
        screen_run(conn, OllamaClient("http://127.0.0.1:9", "qwen3:4b", timeout_s=1), run_id, top=5)
    with transaction(conn):
        save_criteria(conn, pid, Criteria(question=""))
    with pytest.raises(ValueError, match="criteria"):
        screen_run(conn, FakeLlm(answer), run_id, top=5)
    assert conn.execute("SELECT COUNT(*) FROM screening_results").fetchone()[0] == 0
    with pytest.raises(LookupError):
        screen_run(conn, FakeLlm(answer), "nope", top=5)


def test_cli_screen_latest_run(conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from litforge import cli

    make_run(conn, paths, PAPERS)
    monkeypatch.setattr(cli, "_make_llm", lambda config, model: FakeLlm(answer), raising=False)
    result = CliRunner().invoke(cli.app, ["--data-dir", str(paths.root), "screen", "--project", "scr", "--top", "2"])
    assert result.exit_code == 0, result.output
    assert "include 1" in result.output and "2 screened" in result.output


def test_export_includes_screening(conn: sqlite3.Connection, paths: Paths, tmp_path: Any) -> None:
    from litforge.export.markdown import export_markdown

    pid, run_id, _ = make_run(conn, paths, PAPERS[:2])
    screen_run(conn, FakeLlm(answer), run_id, top=5)
    root, n = export_markdown(conn, paths, "scr", pid, out_dir=tmp_path / "out")
    assert n == 2
    notes = "\n".join(p.read_text() for p in (root / "knowledge_base").glob("*.md"))
    assert "screening_decision: include" in notes and "screening_decision: exclude" in notes
    assert "| Screening |" in (root / "README.md").read_text()
    assert all(not k.startswith("s_") for e in json.loads((root / "manifest.json").read_text()) for k in e)


@pytest.mark.skipif("not __import__('os').environ.get('LITFORGE_LIVE_LLM')",
                    reason="set LITFORGE_LIVE_LLM=1 with Ollama running and qwen3:4b pulled")
def test_live_ollama_screens_obvious_cases(conn: sqlite3.Connection, paths: Paths) -> None:
    from litforge.config import load_config
    from litforge.screening.llm import ollama_from_config

    _, run_id, ids = make_run(conn, paths, [
        ("Simulation-based inference for a Jansen-Rit neural mass model",
         "We fit a neural mass model to human EEG recordings using simulation-based inference."),
        ("Crop yields under drought", "We study wheat yields in dry seasons in a field trial.")])
    screen_run(conn, ollama_from_config(load_config().screening), run_id, top=2)
    got = {r["paper_id"]: r["decision"] for r in conn.execute("SELECT paper_id, decision FROM screening_results")}
    assert got[ids[0]] == "include" and got[ids[1]] != "include"


def test_ollama_dying_mid_run_aborts_and_keeps_earlier_results(conn: sqlite3.Connection, paths: Paths) -> None:
    from litforge.screening.llm import LlmUnavailable

    _, run_id, ids = make_run(conn, paths, PAPERS)

    def flaky(messages: list[dict[str, str]]) -> dict[str, Any]:
        if "Mouse" in messages[-1]["content"]:
            raise LlmUnavailable("Ollama not reachable")
        return answer(messages)

    with pytest.raises(LlmUnavailable):
        screen_run(conn, FakeLlm(flaky), run_id, top=5)
    assert [r[0] for r in conn.execute("SELECT paper_id FROM screening_results")] == [ids[0]]


def test_ollama_unreachable_error_is_unavailable() -> None:
    from litforge.screening.llm import LlmUnavailable

    with pytest.raises(LlmUnavailable):
        OllamaClient("http://127.0.0.1:9", "qwen3:4b", timeout_s=1).chat_json([], {})


def test_cli_screen_rejects_top_zero(conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from litforge import cli

    make_run(conn, paths, PAPERS)
    monkeypatch.setattr(cli, "_make_llm", lambda config, model: FakeLlm(answer))
    result = CliRunner().invoke(cli.app, ["--data-dir", str(paths.root), "screen", "--project", "scr", "--top", "0"])
    assert result.exit_code == 2 and conn.execute("SELECT COUNT(*) FROM screening_results").fetchone()[0] == 0


@pytest.mark.parametrize("entrypoint", ["cli", "ui"])
def test_screen_uses_task_mapping_without_setup(conn, paths, monkeypatch, entrypoint) -> None:
    from typer.testing import CliRunner

    from litforge import cli
    from litforge.analysis.gateway import ModelGateway
    from litforge.config import Config
    from litforge.ui.server import App

    _, run_id, _ = make_run(conn, paths, PAPERS[:1])
    (paths.root / "config.yaml").write_text('models:\n  tasks: {screen: {profile: routed}}\n'
        '  profiles:\n    routed: {provider: ollama, model: mapped-model}\n')
    cfg = Config.model_validate({"models": {"tasks": {"screen": {"profile": "routed"}},
        "profiles": {"routed": {"provider": "ollama", "model": "mapped-model"}}}})
    provider = FakeLlm(answer, name="mapped-model")
    def gateway(c, config):
        return ModelGateway(c, config, providers={"routed": provider})
    if entrypoint == "cli":
        monkeypatch.setattr(cli, "ModelGateway", gateway)
        result = CliRunner().invoke(cli.app, ["--data-dir", str(paths.root), "screen", "--project", "scr"])
        assert result.exit_code == 0, result.output
    else:
        application = App(paths, cfg, make_gateway=lambda c: gateway(c, cfg))
        job = application.screen_job({"run_id": run_id, "top": 1})
        import time
        deadline = time.monotonic() + 5
        while job.status in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.01)
        assert job.status == "done", job.error
    assert provider.calls == 1
    assert conn.execute("SELECT model FROM screening_results").fetchone()[0] == "mapped-model"


@pytest.mark.parametrize("entrypoint", ["cli", "ui"])
def test_screen_explicit_model_overrides_task_map(conn, paths, monkeypatch, entrypoint) -> None:
    from typer.testing import CliRunner

    from litforge import cli
    from litforge.config import Config
    from litforge.ui.server import App

    _, run_id, _ = make_run(conn, paths, PAPERS[:1])
    (paths.root / "config.yaml").write_text('models:\n  tasks: {screen: {profile: missing}}\n')
    provider = FakeLlm(answer, name="explicit-ollama")
    def legacy(config, model):
        assert model == "explicit-ollama"
        return provider
    if entrypoint == "cli":
        monkeypatch.setattr(cli, "_make_llm", legacy)
        result = CliRunner().invoke(cli.app, ["--data-dir", str(paths.root), "screen", "--project", "scr",
                                             "--model", "explicit-ollama"])
        assert result.exit_code == 0, result.output
    else:
        application = App(paths, Config(models={"tasks": {"screen": {"profile": "missing"}}}),
                          make_llm=lambda model: legacy(None, model))
        job = application.screen_job({"run_id": run_id, "top": 1, "model": "explicit-ollama"})
        import time
        deadline = time.monotonic() + 5
        while job.status in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.01)
        assert job.status == "done", job.error
    assert provider.calls == 1
    assert conn.execute("SELECT model FROM screening_results").fetchone()[0] == "explicit-ollama"
