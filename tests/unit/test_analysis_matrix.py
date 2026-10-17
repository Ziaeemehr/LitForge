from __future__ import annotations

import csv
import io
import sqlite3

import pytest
from lens_fakes import evidence_id, gateway, seed

from litforge.analysis.ledger import to_packet
from litforge.analysis.matrix import (
    CellDraft,
    MatrixColumn,
    add_column,
    create_matrix,
    export_matrix,
    extract_matrix,
    get_matrix,
    matrix_warnings,
    plan_calls,
    validate_cell,
)
from litforge.indexing.fts import search
from litforge.screening.llm import FakeLlm


def column(key: str = "gain", type: str = "number") -> MatrixColumn:
    return MatrixColumn(key=key, label="Gain", instruction="Results gain", type=type)


@pytest.mark.parametrize(("status", "value", "ids", "kind", "options", "expected", "flags"), [
    ("reported", "3.25", True, "number", None, "reported", []),
    ("reported", 99, True, "number", None, "reported", ["number_not_in_evidence"]),
    ("reported", "abc", True, "number", None, "reported", ["type_mismatch"]),
    ("reported", True, True, "number", None, "reported", ["type_mismatch"]),
    ("reported", "other", True, "categorical", ["yes"], "reported", ["option_mismatch"]),
    ("reported", "x", False, "text", None, "not_found", ["uncited_value"]),
    ("not_reported", None, False, "number", None, "not_reported", []),
    ("not_applicable", None, False, "number", None, "not_applicable", []),
    ("reported", "yes", True, "boolean", None, "reported", ["type_mismatch"]),
    ("reported", True, True, "boolean", None, "reported", []),
])
def test_cell_rules(conn: sqlite3.Connection, status: str, value: object, ids: bool, kind: str,
                    options: list[str] | None, expected: str, flags: list[str]) -> None:
    span = seed(conn)
    col = MatrixColumn(key="gain", label="Gain", instruction="gain", type=kind, options=options)
    draft = CellDraft(status=status, value=value, evidence_ids=[span.id, "unknown"] if ids else ["unknown"])
    validated, actual = validate_cell(draft, col, to_packet([span]))
    assert validated.status == expected and actual == flags
    assert "unknown" not in validated.evidence_ids


def test_cache_incremental_and_exports(conn: sqlite3.Connection) -> None:
    seed(conn)
    gw, fake = gateway(conn)
    fake.answer = lambda m: {"status": "reported", "value": 3.25, "unit": "mV", "note": "",
                             "evidence_ids": [evidence_id(m)]}
    matrix = create_matrix(conn, "proj", "gains", "Results?", [column()])
    assert len(plan_calls(conn, gw, matrix.id)) == 1 and fake.calls == 0
    first = extract_matrix(conn, gw, matrix.id)
    assert fake.calls == 1 and first.cells[0].status == "reported"
    assert get_matrix(conn, matrix.id).cells == first.cells
    assert plan_calls(conn, gw, matrix.id) == []
    extract_matrix(conn, gw, matrix.id)
    assert fake.calls == 1
    add_column(conn, matrix.id, column("second"))
    extract_matrix(conn, gw, matrix.id)
    assert fake.calls == 2
    extract_matrix(conn, gw, matrix.id, force=True)
    assert fake.calls == 4
    md = export_matrix(conn, matrix.id, "markdown")
    assert "[^ev_" in md and "Title p, p.1" in md and "3.25" in md
    rows = list(csv.DictReader(io.StringIO(export_matrix(conn, matrix.id, "csv"))))
    assert rows[0]["gain_status"] == "reported" and rows[0]["gain_evidence"].startswith("ev_")
    assert "parse_run_id" in export_matrix(conn, matrix.id, "json")
    assert column().column_hash == column().column_hash
    assert column().column_hash != column("gain", "text").column_hash
    with pytest.raises(ValueError):
        add_column(conn, matrix.id, column())


def test_scope_misses_errors_and_warnings(conn: sqlite3.Connection) -> None:
    seed(conn)
    seed(conn, "q", "Results: gain 3.25")
    assert search(conn, "gain", paper_ids=[]) == []
    assert {h.paper_id for h in search(conn, "gain", paper_ids=["p"])} == {"p"}
    gw, fake = gateway(conn)
    fake.answer = lambda m: {"status": "reported", "value": 3.25,
                             "unit": "mV" if "Title p" in m[-1]["content"] else "V",
                             "evidence_ids": [evidence_id(m)], "note": ""}
    matrix = create_matrix(conn, "proj", "gains", "gain", [column()])
    result = extract_matrix(conn, gw, matrix.id)
    assert any("unit" in w for w in matrix_warnings(result))
    conn.execute("UPDATE chunks SET text='unrelated' WHERE paper_id='p'")
    fake.answer = lambda m: (_ for _ in ()).throw(RuntimeError("broken"))
    result = extract_matrix(conn, gw, matrix.id, force=True)
    assert {c.status for c in result.cells} == {"not_found", "error"}
    assert any("not_found" in w for w in matrix_warnings(result))
    assert "model provider failed" in next(c.note for c in result.cells if c.status == "error")
    assert isinstance(fake, FakeLlm)


def test_budget_stops_remaining_cells_and_changed_parse(conn: sqlite3.Connection) -> None:
    from test_analysis_gateway import ReplyProvider

    from litforge.config import ModelProfileConfig
    from litforge.llm.base import ModelReply, ModelUsage
    from litforge.llm.budget import Pricing

    seed(conn)
    gw, _ = gateway(conn)
    matrix = create_matrix(conn, "proj", "budget", "gain", [column("one"), column("two"), column("three")])
    span_id = seed(conn, "q").id
    # One successful call consumes the run budget; later calls are refused before dispatch.
    gw.config.models.policy = "hybrid"
    gw.config.privacy.allow_cloud = True
    gw.config.models.profiles["remote"] = ModelProfileConfig(provider="fake", model="paid", remote=True,
        pricing=Pricing(input_per_million_usd=0, output_per_million_usd=1))
    gw.config.budget.max_per_run_usd = .0015
    provider = ReplyProvider(ModelReply(content={"status": "reported", "value": 3.25, "unit": None,
        "evidence_ids": [span_id], "note": ""}, model="paid", provider="fake", usage=ModelUsage(output_tokens=1000)))
    gw.providers["remote"] = provider
    result = extract_matrix(conn, gw, matrix.id, profile="remote")
    assert provider.calls == 1
    assert sum(c.status == "error" and c.note == "budget" for c in result.cells) == 5
    assert len({r[0] for r in conn.execute("SELECT run_id FROM model_calls")}) == 1
    assert all(c.review_status == "open" for c in result.cells)


def test_reparse_and_route_invalidate_cache(conn: sqlite3.Connection) -> None:
    seed(conn)
    gw, fake = gateway(conn, {"status": "not_reported", "value": None})
    matrix = create_matrix(conn, "proj", "m", "gain", [column()])
    extract_matrix(conn, gw, matrix.id)
    conn.execute("UPDATE parse_runs SET is_current=0 WHERE id='rp'")
    conn.execute("""INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,
                    status,created_at,is_current,output_dir)
                    VALUES ('new','ap','fake','2','h','ok','now',1,'parsed')""")
    conn.execute("UPDATE chunks SET parse_run_id='new'")
    extract_matrix(conn, gw, matrix.id)
    assert fake.calls == 2
    gw.config.models.local.model = "different"
    extract_matrix(conn, gw, matrix.id)
    assert fake.calls == 3


@pytest.mark.parametrize("status", ["not_reported", "not_applicable"])
def test_absence_statuses_are_preserved(conn: sqlite3.Connection, status: str) -> None:
    seed(conn)
    gw, fake = gateway(conn, {"status": status, "value": 123, "evidence_ids": ["invented"], "note": "absent"})
    matrix = create_matrix(conn, "proj", "m", "gain", [column()])
    result = extract_matrix(conn, gw, matrix.id)
    assert result.cells[0].status == status and result.cells[0].value is None
    assert not result.cells[0].needs_review and result.cells[0].evidence_ids == []
    extract_matrix(conn, gw, matrix.id)
    assert fake.calls == 1


def test_error_is_cached_until_force_and_empty_retrieval_never_calls(conn: sqlite3.Connection) -> None:
    seed(conn)
    gw, fake = gateway(conn, {"invalid": "response"})
    matrix = create_matrix(conn, "proj", "m", "gain", [column()])
    assert extract_matrix(conn, gw, matrix.id).cells[0].status == "error"
    extract_matrix(conn, gw, matrix.id)
    assert fake.calls == 1
    conn.execute("UPDATE chunks SET text='nothing relevant'")
    result = extract_matrix(conn, gw, matrix.id, force=True)
    assert fake.calls == 1 and result.cells[0].status == "not_found"
    assert result.cells[0].needs_review


def test_latest_current_parse_is_used_for_multiple_artifacts(conn: sqlite3.Connection) -> None:
    seed(conn)
    conn.execute("UPDATE parse_runs SET created_at='2025-01-01'")
    conn.execute("""INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at)
                    VALUES ('a2','p','sha2','file2.pdf',1,'ok','now')""")
    conn.execute("""INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,created_at,
                    is_current,output_dir) VALUES ('new','a2','fake','2','h','ok','2026-01-01',1,'parsed')""")
    conn.execute("""INSERT INTO chunks(id,parse_run_id,paper_id,kind,order_idx,text,token_count,
                    page_start,page_end,hash)
                    VALUES ('newchunk','new','p','text',0,'Results: gain 4.5',5,1,1,'newhash')""")
    gw, fake = gateway(conn)
    def reply(messages: list[dict[str, str]]) -> dict[str, object]:
        assert "4.5" in messages[-1]["content"] and "3.25" not in messages[-1]["content"]
        return {"status": "reported", "value": 4.5, "evidence_ids": [evidence_id(messages)]}
    fake.answer = reply
    matrix = create_matrix(conn, "proj", "m", "gain", [column()])
    cell = extract_matrix(conn, gw, matrix.id).cells[0]
    assert cell.status == "reported" and cell.parse_run_id == "new" and fake.calls == 1


def test_export_uses_extraction_hit_page(conn: sqlite3.Connection) -> None:
    import json

    seed(conn, text="Results alpha. beta marker.")
    conn.execute("UPDATE chunks SET page_end=2,bbox_json=?", (json.dumps([
        {"start": 0, "end": 15, "page": 1}, {"start": 15, "end": 27, "page": 2}]),))
    gw, _ = gateway(conn, {"status": "not_reported"})
    matrix = create_matrix(conn, "proj", "m", "beta", [MatrixColumn(key="beta", label="beta", instruction="beta",
                                                                               type="text")])
    gw.providers["local"] = FakeLlm(lambda m: {"status": "reported", "value": "marker",
                                              "evidence_ids": [evidence_id(m)]})
    extract_matrix(conn, gw, matrix.id)
    assert "Title p, p.2" in export_matrix(conn, matrix.id, "markdown")


@pytest.mark.parametrize("error", [sqlite3.OperationalError("database failure"), TypeError("programming bug")])
def test_unexpected_extraction_errors_abort(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
                                            error: Exception) -> None:
    from litforge.analysis import matrix as module

    seed(conn)
    gw, _ = gateway(conn, {"status": "not_reported"})
    matrix = create_matrix(conn, "proj", "m", "gain", [column()])
    def fail(*args: object) -> object:
        raise error
    monkeypatch.setattr(module, "validate_cell", fail)
    with pytest.raises(type(error), match=str(error)):
        extract_matrix(conn, gw, matrix.id)
    assert conn.execute("SELECT status FROM analysis_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0] == "error"
    assert get_matrix(conn, matrix.id).cells == []


def test_unavailable_provider_aborts_matrix(conn: sqlite3.Connection) -> None:
    from litforge.llm.base import LlmUnavailable

    seed(conn)
    gw, fake = gateway(conn)
    fake.answer = lambda _: (_ for _ in ()).throw(LlmUnavailable("local provider unavailable"))
    matrix = create_matrix(conn, "proj", "m", "gain", [column("one"), column("two")])
    with pytest.raises(LlmUnavailable):
        extract_matrix(conn, gw, matrix.id)
    assert fake.calls == 1
    row = conn.execute("SELECT status,error FROM analysis_runs ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row["status"] == "error" and row["error"] == "LlmUnavailable: local provider unavailable"


def test_removed_project_papers_are_hidden_but_preserved(conn: sqlite3.Connection) -> None:
    import json

    seed(conn)
    seed(conn, "q")
    gw, _ = gateway(conn, {"status": "not_reported"})
    matrix = create_matrix(conn, "proj", "m", "gain", [column()])
    extract_matrix(conn, gw, matrix.id)
    conn.execute("DELETE FROM project_papers WHERE paper_id='p'")
    assert {c.paper_id for c in get_matrix(conn, matrix.id).cells} == {"q"}
    assert len(conn.execute("SELECT * FROM matrix_cells").fetchall()) == 2
    assert {c["paper_id"] for c in json.loads(export_matrix(conn, matrix.id, "json"))["cells"]} == {"q"}
    assert "Title p" not in export_matrix(conn, matrix.id, "md")
    assert "p," not in export_matrix(conn, matrix.id, "csv")
    conn.execute("UPDATE matrices SET project_id=NULL WHERE id=?", (matrix.id,))
    assert len(get_matrix(conn, matrix.id).cells) == 2


def test_matrix_markdown_is_inert(conn: sqlite3.Connection) -> None:
    payload = "<script> [click](https://evil) `code` | **bold**"
    span = seed(conn, text="Results gain " + payload + " word" * 150)
    gw, _ = gateway(conn, {"status": "reported", "value": payload, "note": payload, "evidence_ids": [span.id]})
    matrix = create_matrix(conn, "proj", payload, "gain", [column(type="text")])
    extract_matrix(conn, gw, matrix.id)
    md = export_matrix(conn, matrix.id, "md")
    assert "<script>" not in md and "[click](" not in md and "`code`" not in md
    assert "\\|" in md and "…" in md
    assert "word" * 150 not in md


def test_unknown_cell_status_is_invalid_response(conn: sqlite3.Connection) -> None:
    seed(conn)
    gw, _ = gateway(conn, {"status": "typo", "note": "SECRET"})
    matrix = create_matrix(conn, "proj", "m", "gain", [column()])
    cell = extract_matrix(conn, gw, matrix.id).cells[0]
    assert cell.status == "error" and cell.note == "invalid model response"


@pytest.mark.parametrize("change", ["value", "evidence", "reparse"])
def test_override_reopens_when_extraction_changes(conn: sqlite3.Connection, change: str) -> None:
    import json

    from litforge.analysis.review import record_review, review_queue

    span = seed(conn)
    gw, fake = gateway(conn, {"status": "reported", "value": 3.25, "evidence_ids": [span.id]})
    matrix = create_matrix(conn, "proj", "m", "gain", [column()])
    cell = extract_matrix(conn, gw, matrix.id).cells[0]
    record_review(conn, "cell", cell.id, "edit", new_value=3.25, reason="checked source")
    history = dict(conn.execute("SELECT * FROM human_reviews").fetchone())
    unchanged = extract_matrix(conn, gw, matrix.id, force=True).cells[0]
    assert unchanged.review_status == "edited" and unchanged.flags == []
    value = 4.5 if change != "evidence" else 3.25
    if change == "reparse":
        conn.execute("UPDATE parse_runs SET is_current=0 WHERE id='rp'")
        conn.execute("""INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,
                        status,created_at,is_current,output_dir)
                        VALUES ('new','ap','fake','2','h','ok','now',1,'parsed')""")
        conn.execute("UPDATE chunks SET parse_run_id='new',text='Results gain 4.5',hash='newhash'")
    elif change == "evidence":
        conn.execute("UPDATE chunks SET text='Results gain 3.25 revised evidence',hash='newhash'")
    fake.answer = lambda m: {"status": "reported", "value": value, "evidence_ids": [evidence_id(m)]}
    changed = extract_matrix(conn, gw, matrix.id, force=True).cells[0]
    assert changed.review_status == "open"
    assert "evidence_changed_since_review" in changed.flags
    assert changed.value == changed.human_value == 3.25 and changed.model_value == value
    assert [i.target_id for i in review_queue(conn)] == [cell.id]
    assert dict(conn.execute("SELECT * FROM human_reviews").fetchone()) == history
    for fmt in ("markdown", "csv", "json"):
        exported = export_matrix(conn, matrix.id, fmt)
        assert "evidence_changed_since_review" in exported and str(value) in exported and "3.25" in exported
    exported_cell = json.loads(export_matrix(conn, matrix.id, "json"))["cells"][0]
    assert exported_cell["human_value"] == 3.25 and exported_cell["model_value"] == value
    # Repeating the new extraction must not erase the outstanding warning.
    assert "evidence_changed_since_review" in extract_matrix(conn, gw, matrix.id, force=True).cells[0].flags


@pytest.mark.parametrize(("value", "evidence", "flags"), [
    (2.5, "−2.5", ["number_not_in_evidence"]),
    (-2.5, "−2.5", []), ("−2.5", "-2.5", []),
    ("−2.5", "2.5", ["number_not_in_evidence"]),
])
def test_unicode_minus_cell_validation(conn: sqlite3.Connection, value: object, evidence: str,
                                       flags: list[str]) -> None:
    span = seed(conn, text="Results gain " + evidence)
    _, actual = validate_cell(CellDraft(status="reported", value=value, evidence_ids=[span.id]),
                              column(), to_packet([span]))
    assert actual == flags


def test_column_hash_ignores_gap_metadata_so_cached_cells_survive() -> None:
    from litforge.analysis.runs import input_hash
    plain = MatrixColumn(key="rate", label="Rate", instruction="rate", type="number", unit="Hz")
    tagged = plain.model_copy(update={"parameter": True, "sensitivity_analysis": True, "sensitivity_for": ["x"]})
    expected = input_hash({"label": "Rate", "instruction": "rate", "type": "number", "unit": "Hz", "options": None})
    assert plain.column_hash == tagged.column_hash == expected
