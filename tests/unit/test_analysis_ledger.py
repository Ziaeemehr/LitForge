from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from litforge.analysis.ledger import get_spans, record_spans, to_packet
from litforge.analysis.runs import finish_run, get_run, link_evidence, start_run
from litforge.indexing.fts import SearchHit


@pytest.fixture()
def hit(conn: sqlite3.Connection) -> SearchHit:
    conn.execute("INSERT INTO papers(id,title,created_at,updated_at) VALUES ('p','Title','now','now')")
    conn.execute("""INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at)
                    VALUES ('a','p','sha','file.pdf',1,'ok','now')""")
    conn.execute("""INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,created_at)
                    VALUES ('r','a','pymupdf','1','h','ok','now')""")
    conn.execute("""INSERT INTO chunks(id,parse_run_id,paper_id,kind,order_idx,text,token_count,
                    page_start,page_end,bbox_json,hash) VALUES ('c','r','p','text',0,
                    'alpha beta gamma delta',4,1,2,'[{"page":2}]','hash')""")
    return SearchHit(chunk_id="c", paper_id="p", title="Title", section="Methods", kind="text",
                     page=2, page_start=1, page_end=2, score=-1, snippet="[gamma]")


def test_snapshots_are_idempotent_and_survive_source_deletion(conn: sqlite3.Connection, hit: SearchHit) -> None:
    first = record_spans(conn, [hit])[0]
    assert first == record_spans(conn, [hit])[0]
    assert first.text == "alpha beta gamma delta" and first.page == 2
    content_hash = hashlib.sha256(first.text.encode()).hexdigest()
    assert first.content_hash == content_hash
    assert first.id == "ev_" + hashlib.sha256(f"r|c|{content_hash}".encode()).hexdigest()[:16]
    assert first.parser == "pymupdf" and first.parser_version == "1"
    assert first.citation == "Title, p.2"
    conn.execute("DELETE FROM chunks")
    conn.execute("DELETE FROM parse_runs")
    assert get_spans(conn, [first.id, first.id]) == [first, first]
    with pytest.raises(KeyError):
        get_spans(conn, ["missing"])
    for sql in ["UPDATE evidence_spans SET text='forged'", "DELETE FROM evidence_spans"]:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(sql)


def test_packet_truncation_and_content_versions(conn: sqlite3.Connection, hit: SearchHit) -> None:
    first = record_spans(conn, [hit])[0]
    packet = to_packet([first], sensitive=True, max_chars_per_item=12)
    assert packet.items[0].text == "alpha beta…" and packet.sensitive
    assert packet.items[0].id == first.id and len(packet.items[0].text) <= 12
    assert to_packet([first]).items[0].text == first.text
    conn.execute("UPDATE chunks SET text='changed text'")
    second = record_spans(conn, [hit])[0]
    assert second.id != first.id and get_spans(conn, [first.id])[0].text == first.text


def test_runs_hash_links_and_completion(conn: sqlite3.Connection, hit: SearchHit) -> None:
    run = start_run(conn, "ask_evidence", None, {"z": 1, "a": 2}, {"k": 8}, "v1")
    same = start_run(conn, "ask_evidence", None, {"a": 2, "z": 1}, {})
    assert get_run(conn, run).input_hash == get_run(conn, same).input_hash
    spans = record_spans(conn, [hit])
    link_evidence(conn, run, spans, [-1.0])
    row = conn.execute("SELECT * FROM run_evidence WHERE run_id=?", (run,)).fetchone()
    assert row["evidence_id"] == spans[0].id and row["rank"] == 1 and row["score"] == -1
    finish_run(conn, run, "ok", {"count": 1})
    record = get_run(conn, run)
    assert record.status == "ok" and record.finished_at and record.prompt_version == "v1"
    assert json.loads(record.result_json) == {"count": 1}
    finish_run(conn, same, "error", {}, "failed")
    assert get_run(conn, same).error == "failed"


def test_first_snapshot_page_is_immutable(conn: sqlite3.Connection, hit: SearchHit) -> None:
    span = record_spans(conn, [hit])[0]
    assert record_spans(conn, [hit.model_copy(update={"page": 1})])[0] == span
    conn.execute("UPDATE papers SET title=NULL WHERE id='p'")
    assert get_spans(conn, [span.id])[0].citation == "p, p.2"


@pytest.mark.parametrize("conflict", ["OR REPLACE", "OR IGNORE"])
def test_duplicate_inserts_cannot_replace_spans(conn: sqlite3.Connection, hit: SearchHit, conflict: str) -> None:
    span = record_spans(conn, [hit])[0]
    conn.execute("PRAGMA recursive_triggers=OFF")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(f"INSERT {conflict} INTO evidence_spans SELECT * FROM evidence_spans")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(f"INSERT {conflict} INTO evidence_spans SELECT id,paper_id,parse_run_id,chunk_id,parser,"
                     "parser_version,kind,section,page,page_start,page_end,bbox_json,'forged',content_hash,created_at,"
                     "from_image "
                     "FROM evidence_spans")
    assert get_spans(conn, [span.id]) == [span]
    assert record_spans(conn, [hit, hit]) == [span, span]


def test_per_run_pages_and_packet_overrides(conn: sqlite3.Connection, hit: SearchHit) -> None:
    spans = record_spans(conn, [hit])
    for page in [2, 1]:
        run = start_run(conn, "ask_evidence", None, {}, {})
        link_evidence(conn, run, spans, [hit.score], pages=[page])
        row = conn.execute("SELECT page FROM run_evidence WHERE run_id=?", (run,)).fetchone()
        assert row["page"] == page
        assert to_packet(spans, pages=[page]).items[0].page == page
    assert get_spans(conn, [spans[0].id]) == spans
    assert to_packet(spans).items[0].page == 2
    with pytest.raises(ValueError):
        to_packet(spans, pages=[])
