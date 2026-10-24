import json
from types import SimpleNamespace

import pytest

from litforge.analysis.search_strategy import (
    Concept,
    Strategy,
    assemble_queries,
    build_strategy,
    openalex_queries,
    validate_query,
)
from litforge.projects import create_project
from litforge.screening.criteria import Criteria, save_criteria

CONCEPTS = [Concept(name="graph neural network", synonyms=["GNN", "graph neural networks"]),
            Concept(name="epilepsy", synonyms=["seizure"])]


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.content = {"concepts": [{"name": "network", "synonyms": [f"term{i}" for i in range(9)]},
                                     {"name": "epilepsy", "synonyms": ["seizure", "ictal", "convulsion"]}]}

    def preview(self, *args, **kwargs):
        return SimpleNamespace(blocked_reason=None, requires_approval=False)

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(content=self.content)


def test_assemble_queries_per_database():
    queries = assemble_queries(CONCEPTS, ["pubmed", "scopus", "wos", "arxiv", "openalex"])
    inner = "(\"graph neural network\" OR GNN OR \"graph neural networks\") AND (epilepsy OR seizure)"
    assert queries["pubmed"] == (
        "(\"graph neural network\"[tiab] OR GNN[tiab] OR \"graph neural networks\"[tiab])"
        " AND (epilepsy[tiab] OR seizure[tiab])")
    assert queries["scopus"] == "TITLE-ABS-KEY(" + inner + ")"
    assert queries["wos"] == "TS=(" + inner + ")"
    assert queries["arxiv"] == (
        "((ti:\"graph neural network\" OR abs:\"graph neural network\") OR (ti:GNN OR abs:GNN)"
        " OR (ti:\"graph neural networks\" OR abs:\"graph neural networks\"))"
        " AND ((ti:epilepsy OR abs:epilepsy) OR (ti:seizure OR abs:seizure))")
    assert queries["openalex"] == "graph neural network epilepsy"
    assert all(not validate_query(db, q) for db, q in queries.items())


def test_validate_query_flags_unbalanced_and_unknown_tag():
    assert validate_query("pubmed", "(\"word[bad]")
    assert validate_query("pubmed", "word[bad]")
    assert validate_query("scopus", "AUTHOR(word)")
    assert validate_query("wos", "AU=(word)")
    assert validate_query("arxiv", "author:word")
    assert validate_query("pubmed", ")(")


def test_model_reply_with_extra_text_is_rejected(conn, paths):
    pid = create_project(conn, paths, "strategy", question="Which networks?")
    gateway = FakeGateway()
    strategy = build_strategy(conn, gateway, pid, ["pubmed"])
    assert len(strategy.concepts[0].synonyms) == 8
    assert strategy.warnings
    gateway.content = "extra prose {\"concepts\": []}"
    save_criteria(conn, pid, Criteria(question="Changed question"))
    with pytest.raises(ValueError):
        build_strategy(conn, gateway, pid, ["pubmed"])


def test_empty_criteria_raises(conn, paths):
    pid = create_project(conn, paths, "empty")
    gateway = FakeGateway()
    with pytest.raises(ValueError, match="question|criteria"):
        build_strategy(conn, gateway, pid, ["pubmed"])
    assert not gateway.calls


def test_openalex_queries():
    strategy = Strategy(concepts=CONCEPTS, queries={}, warnings=[])
    assert openalex_queries(strategy) == [
        "graph neural network epilepsy", "graph neural network seizure", "GNN epilepsy", "GNN seizure"]
    strategy.concepts += [Concept(name="third", synonyms=["three", "ignored"])]
    queries = openalex_queries(strategy)
    assert len(queries) == 6
    assert all("ignored" not in q and "graph neural networks" not in q for q in queries)


def test_criteria_hash_cache(conn, paths):
    pid = create_project(conn, paths, "cache", question="Networks?")
    gateway = FakeGateway()
    first = build_strategy(conn, gateway, pid, ["pubmed"])
    assert build_strategy(conn, gateway, pid, ["pubmed"]) == first
    assert len(gateway.calls) == 1
    build_strategy(conn, gateway, pid, ["scopus"])
    assert len(gateway.calls) == 2
    save_criteria(conn, pid, Criteria(question="Networks?", include=["human studies"]))
    build_strategy(conn, gateway, pid, ["pubmed"])
    assert len(gateway.calls) == 3


def test_untrusted_terms_are_sanitised_and_dropped(conn, paths):
    pid = create_project(conn, paths, "unsafe", question="Which studies?")
    gateway = FakeGateway()
    gateway.content["concepts"][0] = {
        "name": "graph\" network[tiab]",
        "synonyms": ["GNN*", "[mh]", "TITLE-ABS-KEY(epilepsy)", "ti:neuron", "(brain)"]}
    result = build_strategy(conn, gateway, pid, ["pubmed", "scopus", "wos", "arxiv"])
    assert result.concepts[0].name == "graph network"
    assert result.concepts[0].synonyms == ["GNN", "epilepsy", "neuron", "brain"]
    assert any("Dropped term" in warning for warning in result.warnings)
    assert all(not validate_query(db, q) for db, q in result.queries.items())
    args, kwargs = gateway.calls[0]
    assert args[0] == "search_strategy"
    assert [item.text for item in args[1].items] == [args[2][-1]["content"]]
    assert set(json.loads(args[2][-1]["content"])) == {"question", "include", "exclude"}
    assert kwargs["approved"] is False


def test_preview_approval_and_failed_runs(conn, paths):
    from litforge.llm.budget import BudgetExceeded
    from litforge.llm.router import CloudApprovalRequired

    pid = create_project(conn, paths, "approval", question="Which studies?")
    gateway = FakeGateway()
    gateway.preview = lambda *args, **kwargs: SimpleNamespace(blocked_reason=None, requires_approval=True)
    with pytest.raises(CloudApprovalRequired):
        build_strategy(conn, gateway, pid, ["pubmed"])
    assert not gateway.calls
    build_strategy(conn, gateway, pid, ["pubmed"], approved=True)
    assert gateway.calls[0][1]["approved"] is True

    def fail(*args, **kwargs):
        raise BudgetExceeded("test budget")

    gateway.call = fail
    with pytest.raises(BudgetExceeded):
        build_strategy(conn, gateway, pid, ["wos"], approved=True)
    assert conn.execute("SELECT status FROM analysis_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "error"


@pytest.mark.parametrize("synonyms", [["[mh]", "[tiab]", "()"], ["same", "same", "same"]])
def test_rejects_too_few_usable_synonyms(conn, paths, synonyms):
    pid = create_project(conn, paths, "unusable", question="Which networks?")
    gateway = FakeGateway()
    gateway.content["concepts"][0]["synonyms"] = synonyms
    with pytest.raises(ValueError, match="At least 2 usable concepts are required"):
        build_strategy(conn, gateway, pid, ["pubmed"])
    assert conn.execute("SELECT COUNT(*) FROM analysis_runs WHERE status='ok'").fetchone()[0] == 0


def test_rejects_unexpected_concept_fields(conn, paths):
    pid = create_project(conn, paths, "extra", question="Which networks?")
    gateway = FakeGateway()
    gateway.content["concepts"][0]["queries"] = {"pubmed": "model syntax"}
    with pytest.raises(ValueError):
        build_strategy(conn, gateway, pid, ["pubmed"])


def test_cli_preview_approval_json_and_cached_reopen(conn, paths, monkeypatch):
    from typer.testing import CliRunner

    from litforge.analysis.gateway import CallPreview
    from litforge.cli import app

    create_project(conn, paths, "CLI", question="Which networks?")
    gateway = FakeGateway()
    gateway.preview = lambda *args, **kwargs: CallPreview(
        profile="local", provider="test", model="test", remote=False, evidence_ids=["criteria"],
        estimated_input_tokens=100, max_output_tokens=1024, max_cost_usd=0, requires_approval=True)
    monkeypatch.setattr("litforge.cli.ModelGateway", lambda *args, **kwargs: gateway)
    runner = CliRunner()
    args = ["--data-dir", str(paths.root), "strategy", "-p", "CLI", "--db", "pubmed", "--db", "arxiv"]
    reply = runner.invoke(app, args + ["--preview", "--json"])
    assert reply.exit_code == 0, reply.output
    assert json.loads(reply.output)["requires_approval"] is True
    assert not gateway.calls
    reply = runner.invoke(app, args + ["--json"])
    assert reply.exit_code == 3, reply.output
    assert not gateway.calls
    reply = runner.invoke(app, args + ["--approve", "--json"])
    assert reply.exit_code == 0, reply.output
    assert set(json.loads(reply.output)["queries"]) == {"pubmed", "arxiv"}
    assert len(gateway.calls) == 1
    reply = runner.invoke(app, args + ["--json"])
    assert reply.exit_code == 0, reply.output
    assert len(gateway.calls) == 1


@pytest.mark.parametrize(("raw", "expected"), [
    ("Parkinson's disease", "Parkinson's disease"),
    ("Alzheimer’s disease", "Alzheimer's disease"),
    ("Alzheimer‘s", "Alzheimer's"),
])
def test_apostrophes_are_preserved_and_quoted(raw, expected):
    queries = assemble_queries([Concept(name=raw, synonyms=[])],
                               ["pubmed", "scopus", "wos", "arxiv", "openalex"])
    assert queries["pubmed"] == f'("{expected}"[tiab])'
    for db in ("scopus", "wos", "arxiv"):
        assert f'"{expected}"' in queries[db]
    assert queries["openalex"] == expected
    assert all(validate_query(db, query) == [] for db, query in queries.items())


@pytest.mark.parametrize(("raw", "expected"), [
    ("COVID-19: severe cases", "COVID-19 severe cases"),
    ("diagnosis: prognosis", "diagnosis prognosis"),
    *[(f"{tag}:neuron", "neuron") for tag in ("ti", "abs", "au", "all", "cat", "TI")],
])
def test_colons_preserve_words_except_known_field_prefixes(raw, expected):
    queries = assemble_queries([Concept(name=raw, synonyms=[])],
                               ["pubmed", "scopus", "wos", "arxiv", "openalex"])
    assert queries["openalex"] == expected
    assert all(validate_query(db, query) == [] for db, query in queries.items())



@pytest.mark.parametrize("synonyms", [
    ["[mh]", "[tiab]", "()"], ["same", "same", "same"], ["only"], [],
])
def test_drops_weak_concept_and_keeps_usable_concepts(conn, paths, synonyms):
    pid = create_project(conn, paths, "partial", question="Which networks?")
    gateway = FakeGateway()
    gateway.content["concepts"].insert(0, {"name": "weak", "synonyms": synonyms})
    result = build_strategy(conn, gateway, pid, ["pubmed"])
    assert [concept.name for concept in result.concepts] == ["network", "epilepsy"]
    assert "Dropped concept weak: fewer than 3 usable synonyms" in result.warnings
    assert "weak" not in result.queries["pubmed"]
    assert build_strategy(conn, gateway, pid, ["pubmed"]) == result
    assert conn.execute("SELECT status FROM analysis_runs").fetchone()[0] == "ok"
