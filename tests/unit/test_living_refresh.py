from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from discovery_fakes import FakeTransport, default_routes
from lens_fakes import evidence_id, gateway, seed

from litforge.analysis import living
from litforge.analysis.claim_graph import cluster_claims
from litforge.analysis.claims import extract_claims
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.matrix import MatrixColumn, create_matrix, extract_matrix
from litforge.analysis.snapshots import get_snapshot, take_snapshot
from litforge.config import Config, Paths
from litforge.discovery.http import HttpClient
from litforge.discovery.run import Sources, run_search
from litforge.screening.criteria import Criteria, save_criteria
from litforge.screening.llm import FakeLlm


def sources(paths: Paths) -> Sources:
    return Sources(HttpClient(paths.http_cache, transport=FakeTransport(default_routes())))


def reply(messages: list[dict[str, str]]) -> dict[str, Any]:
    if "Extract at most" in messages[0]["content"]:
        return {"claims": [{"text": "Gain improves outcomes", "type": "finding", "polarity": "positive",
                            "evidence_ids": [evidence_id(messages)]}]}
    if "Classify" in messages[0]["content"]:
        return {"relation": "replicates", "explanation": "Same"}
    if "matrix cell" in messages[0]["content"].lower():
        return {"status": "reported", "value": "gain", "evidence_ids": [evidence_id(messages)]}
    return {"relevance": 5, "inclusion": [], "exclusion": [], "reason": "match"}


def test_refresh_replays_unique_queries_new_only_and_writes_report(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="gain"))
    src = sources(paths)
    run_search(conn, src, "proj", ["gain", "gain"], ["openalex"], 5)
    run_search(conn, src, "proj", ["gain"], ["openalex"], 5)
    before = take_snapshot(conn, "proj")
    # A paper added after the previous snapshot, with a parse, must be screened at both stages.
    seed(conn, "new")
    fake = FakeLlm(reply)
    gw = ModelGateway(conn, Config(), providers={"local": fake})
    events: list[str] = []
    result = living.refresh_project(conn, gw, sources(paths), "proj", progress=events.append)
    assert result.status == "ok" and result.before_snapshot == before and result.after_snapshot
    assert len(result.delta.added_papers) == 1 and result.delta.added_papers[0].id == "new"
    assert conn.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM screening_results WHERE paper_id='new'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM screening_results WHERE paper_id='p'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fulltext_screening WHERE paper_id='new'").fetchone()[0] == 1
    assert Path(result.report_path).is_file()
    assert "Claims still without replication" in Path(result.report_path).read_text()
    assert events and get_snapshot(conn, before).data.papers[0].id != "new"
    calls = fake.calls
    again = living.refresh_project(conn, gw, sources(paths), "proj")
    assert again.status == "ok" and not again.signals and fake.calls == calls
    assert conn.execute("SELECT COUNT(*) FROM model_calls WHERE run_id IS NULL OR prompt_version=''").fetchone()[0] == 0


def test_refresh_matrix_incremental_and_relates_only_new_claims(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn)
    seed(conn, "q")
    save_criteria(conn, "proj", Criteria(question="gain"))
    fake = FakeLlm(reply)
    gw = ModelGateway(conn, Config(), providers={"local": fake})
    extract_claims(conn, gw, "proj")
    cluster_claims(conn, "proj")
    matrix = create_matrix(conn, "proj", "m", "gain", [MatrixColumn(
        key="gain", label="Gain", instruction="gain", type="text")])
    extract_matrix(conn, gw, matrix.id)
    take_snapshot(conn, "proj")
    seed(conn, "t")
    calls = fake.calls
    result = living.refresh_project(conn, gw, sources(paths), "proj", screen=False)
    assert result.status == "ok" and fake.calls == calls + 4  # cell + claim + two new pairs
    relations = conn.execute("SELECT source_claim_id,target_claim_id FROM claim_relations").fetchall()
    new_claim = conn.execute("SELECT id FROM claims WHERE paper_id='t'").fetchone()[0]
    assert len(relations) == 2 and all(new_claim in tuple(r) for r in relations)
    old = conn.execute("SELECT run_id FROM matrix_cells WHERE paper_id='p'").fetchone()[0]
    conn.execute("UPDATE parse_runs SET is_current=0 WHERE id='rp'")
    conn.execute("INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,created_at,"
                 "is_current,output_dir) VALUES ('rp2','ap','fake','2','changed','ok','later',1,'parsed')")
    conn.execute("UPDATE chunks SET parse_run_id='rp2' WHERE paper_id='p'")
    living.refresh_project(conn, gw, sources(paths), "proj", screen=False)
    assert conn.execute("SELECT run_id FROM matrix_cells WHERE paper_id='p'").fetchone()[0] != old


def test_refresh_flags_and_failure_provenance(conn: sqlite3.Connection, paths: Paths,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    seed(conn)
    gw, fake = gateway(conn)
    result = living.refresh_project(conn, gw, sources(paths), "proj", screen=False, extract=False, claims=False)
    assert not result.signals and fake.calls == 0
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private provider details")
    monkeypatch.setattr(living, "run_search", fail)
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,params_json,status,started_at) "
                 "VALUES ('d','proj','search',?,'done','now')",
                 (json.dumps({"queries": ["x"], "sources": ["openalex"], "limit": 1}),))
    with pytest.raises(RuntimeError, match="private"):
        living.refresh_project(conn, gw, sources(paths), "proj")
    row = conn.execute("SELECT * FROM watch_runs ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row["status"] == "error" and row["finished_at"] and "private" not in row["error"]


def test_refresh_replays_stored_similar_keyword_hits(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn)
    conn.execute("INSERT INTO discovery_runs(id,project_id,kind,params_json,status,started_at) "
                 "VALUES ('similar','proj','similar','{}','done','now')")
    conn.execute("INSERT INTO discovery_hits(run_id,paper_id,source,query,relation,rank) "
                 "VALUES ('similar','p','openalex','gain','keywords',1)")
    gw, _ = gateway(conn)
    result = living.refresh_project(conn, gw, sources(paths), "proj", screen=False, extract=False, claims=False)
    assert result.status == "ok"
    assert conn.execute("SELECT COUNT(*) FROM discovery_runs WHERE kind='search'").fetchone()[0] == 1


def test_interrupted_refresh_is_marked_cancelled_and_does_not_block(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn)
    take_snapshot(conn, "proj")
    gw = ModelGateway(conn, Config(), providers={"local": FakeLlm(reply)})

    def interrupt(_message: str) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        living.refresh_project(conn, gw, sources(paths), "proj", progress=interrupt)
    row = conn.execute("SELECT status, finished_at FROM watch_runs").fetchone()
    assert row["status"] == "cancelled" and row["finished_at"]
    assert living.refresh_project(conn, gw, sources(paths), "proj", screen=False).status in ("ok", "partial")


def test_stale_running_refresh_from_a_killed_process_is_released(conn: sqlite3.Connection, paths: Paths) -> None:
    seed(conn)
    snapshot = take_snapshot(conn, "proj")
    conn.execute("INSERT INTO watch_runs(id,project_id,before_snapshot,status,started_at) "
                 "VALUES ('old','proj',?,'running','2000-01-01T00:00:00+00:00')", (snapshot,))
    gw = ModelGateway(conn, Config(), providers={"local": FakeLlm(reply)})
    living.refresh_project(conn, gw, sources(paths), "proj", screen=False)
    old = conn.execute("SELECT status, error FROM watch_runs WHERE id='old'").fetchone()
    assert old["status"] == "error" and "interrupted" in old["error"]
