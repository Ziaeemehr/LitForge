from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from discovery_fakes import FakeTransport, default_routes
from lens_fakes import evidence_id

from litforge.analysis.ask import ask
from litforge.analysis.claim_graph import cluster_claims
from litforge.analysis.claims import extract_claims
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.living import refresh_project
from litforge.analysis.matrix import MatrixColumn, create_matrix, extract_matrix
from litforge.analysis.repro import ensure_repro_matrix, repro_card
from litforge.analysis.snapshots import get_snapshot, take_snapshot
from litforge.config import Config, Paths
from litforge.discovery.http import HttpClient
from litforge.discovery.run import Sources
from litforge.pipeline import add_pdfs
from litforge.projects import create_project
from litforge.screening.criteria import Criteria, save_criteria
from litforge.screening.llm import FakeLlm


def test_synthetic_pdf_living_review_and_repro(conn: sqlite3.Connection, paths: Paths, config: Config,
                                             corpus: Path) -> None:
    pid = create_project(conn, paths, "living-pdfs")
    save_criteria(conn, pid, Criteria(question="neural mass inference"))
    add_pdfs(conn, paths, config, pid, [corpus / "single_column.pdf"], "baseline")
    def reply(messages: list[dict[str, str]]) -> dict[str, Any]:
        system = messages[0]["content"]
        if "matrix cell" in system:
            return {"status": "reported", "value": "neural mass", "evidence_ids": [evidence_id(messages)]}
        if "Extract at most" in system:
            return {"claims": [{"text": "Neural mass parameters can be inferred", "type": "finding",
                                "polarity": "positive", "evidence_ids": [evidence_id(messages)]}]}
        if "Classify" in system:
            return {"relation": "replicates", "explanation": "Same reported result"}
        if "Screen" in system or "screen" in system:
            return {"relevance": 5, "inclusion": [], "exclusion": [], "reason": "Neural mass inference"}
        return {"sentences": [{"text": "Neural mass models are studied", "kind": "reported_fact",
                               "evidence_ids": [evidence_id(messages)]}], "insufficient": False}
    fake = FakeLlm(reply)
    gw = ModelGateway(conn, config, providers={"local": fake})
    matrix = create_matrix(conn, pid, "studies", "neural mass", [MatrixColumn(
        key="model", label="Model", instruction="neural mass", type="text")])
    extract_matrix(conn, gw, matrix.id)
    extract_claims(conn, gw, pid)
    cluster_claims(conn, pid)
    ask(conn, gw, "neural mass", pid)
    before = take_snapshot(conn, pid, "Original review")
    add_pdfs(conn, paths, config, pid, [corpus / "two_column.pdf"], "new study")
    sources = Sources(HttpClient(transport=FakeTransport(default_routes())))
    watch = refresh_project(conn, gw, sources, pid)
    assert watch.status == "ok" and watch.delta is not None
    assert len(watch.delta.added_papers) == 1 and len(watch.delta.added_claims) == 1
    assert any(s.kind == "cluster_strength" for s in watch.signals)
    assert len(get_snapshot(conn, before).data.papers) == 1
    assert len(get_snapshot(conn, watch.after_snapshot).data.papers) == 2
    card_matrix = ensure_repro_matrix(conn, pid)
    extract_matrix(conn, gw, card_matrix.id)
    paper = watch.delta.added_papers[0].id
    card = repro_card(conn, paper)
    assert len(card.checklist) == 12 and "Compute estimate: not reported" in card.brief
    take_snapshot(conn, pid, "Repro baseline")
    calls = fake.calls
    unchanged = refresh_project(conn, gw, sources, pid)
    assert fake.calls == calls and not unchanged.signals
    assert conn.execute("SELECT COUNT(*) FROM model_calls WHERE run_id IS NULL OR prompt_version=''").fetchone()[0] == 0
