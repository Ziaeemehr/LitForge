from pathlib import Path

from litforge.llm.audit import ModelCallRecord, record_model_call
from litforge.storage.db import latest_version, migrate, open_db, schema_version


def test_migrations_apply_once(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "x.sqlite")
    assert schema_version(conn) == latest_version() >= 1
    assert migrate(conn) == []  # idempotent
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_fts_triggers_follow_chunk_rows(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "x.sqlite")
    ts = "2026-01-01"
    conn.execute("INSERT INTO papers(id, created_at, updated_at) VALUES ('p', ?, ?)", (ts, ts))
    conn.execute(
        "INSERT INTO artifacts(id, paper_id, sha256, pdf_path, size_bytes, status, created_at)"
        " VALUES ('a', 'p', 'h', 'x.pdf', 1, 'ok', ?)", (ts,))
    conn.execute(
        "INSERT INTO parse_runs(id, artifact_id, parser, parser_version, config_hash, status, created_at)"
        " VALUES ('r', 'a', 'pymupdf', '1', 'c', 'ok', ?)", (ts,))
    conn.execute(
        "INSERT INTO chunks(id, parse_run_id, paper_id, kind, order_idx, text, token_count,"
        " page_start, page_end, hash) VALUES ('c1', 'r', 'p', 'text', 0, 'oscillation bursts', 2, 1, 1, 'h')")
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'oscillation'").fetchone()[0] == 1
    conn.execute("DELETE FROM parse_runs WHERE id='r'")  # cascades to chunks, trigger cleans FTS
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'oscillation'").fetchone()[0] == 0


def test_model_call_audit_excludes_credentials(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "x.sqlite")
    record_id = record_model_call(conn, ModelCallRecord(
        provider="openai_compatible", model="large", task="synthesis", input_hash="abc",
        evidence_ids=["e1"], input_tokens=10, output_tokens=2, latency_ms=20, cost_usd=0.01, status="ok",
    ))
    row = conn.execute("SELECT * FROM model_calls WHERE id=?", (record_id,)).fetchone()
    assert row["provider"] == "openai_compatible" and row["evidence_ids_json"] == '["e1"]'
    columns = set(row.keys())
    assert "api_key" not in columns and "authorization" not in columns and "headers" not in columns
