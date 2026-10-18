from __future__ import annotations

import json
import sqlite3

import pytest
from lens_fakes import seed

from litforge.analysis.ask import SentenceDraft, validate_sentences
from litforge.analysis.figures import analyze_asset, compare_equations, glossary, relevant_asset_spans
from litforge.analysis.gaps import design_gap, detect_gaps, export_gap_card, review_gap
from litforge.analysis.gateway import ModelGateway
from litforge.analysis.ledger import to_packet
from litforge.analysis.matrix import CellDraft, MatrixColumn, create_matrix, validate_cell
from litforge.analysis.runs import get_run
from litforge.config import Config
from litforge.llm.evidence import EvidenceItem, EvidencePacket
from litforge.storage.db import now


class FakeVisionProvider:
    def __init__(self, answer: dict[str, object]) -> None:
        self.answer = answer
        self.images: list[list[bytes]] = []

    def vision_json(self, messages: list[dict[str, object]], schema: dict[str, object],
                    images: list[bytes]) -> dict[str, object]:
        self.images.append(images)
        return self.answer

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, object]) -> dict[str, object]:
        return self.answer


def add_asset(conn: sqlite3.Connection, paths, asset_id: str = "asset-table",
              caption: str = "Table 1: Parameter values in mV.", kind: str = "table") -> bytes:
    crop = b"only the asset crop"
    out = paths.root / "parsed"
    out.mkdir(exist_ok=True)
    (out / "crop.png").write_bytes(crop)
    conn.execute(
        """INSERT INTO assets(id,parse_run_id,kind,label,caption,page,bbox_json,path,data_json,quality)
           VALUES (?,?,?,?,?,1,'[]','parsed/crop.png',NULL,'verified')""",
        (asset_id, "rp", kind, "Table 1", caption),
    )
    return crop


def test_asset_analysis_stores_spans_and_fidelity(conn: sqlite3.Connection, paths) -> None:
    seed(conn, text="Results report gains 3.25 mV and 2.0 mV. The parameter alpha is the gain.")
    crop = add_asset(conn, paths)
    provider = FakeVisionProvider({
        "columns": ["Parameter", "Estimate"], "rows": [["gain", "3.25"], ["bias", "9.0"]],
        "units": {"Estimate": "mV"}, "footnotes": ["Mean values"],
    })
    gateway = ModelGateway(conn, Config(), providers={"local_vision": provider})  # type: ignore[arg-type]
    analysis = analyze_asset(conn, gateway, paths, "asset-table", "table")
    assert provider.images == [[crop]]
    assert analysis.result["rows"] == [["gain", "3.25"], ["bias", "9.0"]]
    assert analysis.fidelity["table_number_fraction"] == pytest.approx(0.5)
    assert analysis.fidelity["units_recognized"] == ["mV"]
    evidence = conn.execute(
        "SELECT kind,from_image,text FROM evidence_spans WHERE id IN "
        "(SELECT value FROM json_each(?))",
        (json.dumps([span.id for span in analysis.evidence]),),
    ).fetchall()
    assert len(evidence) == 4 and {row["kind"] for row in evidence} == {"table_cell"}
    assert all(row["from_image"] == 1 for row in evidence)
    assert all(item.from_image for item in to_packet(analysis.evidence).items)
    selected = relevant_asset_spans(conn, "p", "rp", "parameter estimate")
    assert {span.id for span in selected} == {span.id for span in analysis.evidence}
    assert relevant_asset_spans(conn, "p", "rp", "unrelated topic") == []


def test_image_numbers_are_not_text_reported() -> None:
    from litforge.analysis.ledger import EvidenceSpan, to_packet

    source = EvidenceSpan(
        id="ev_image", paper_id="p", parse_run_id="r", chunk_id="a", parser="fake", parser_version="1",
        kind="table_cell", section=None, page=1, page_start=1, page_end=1, bbox_json="[]", text="Value 10",
        content_hash="h", created_at="now", from_image=True,
    )
    packet = to_packet([source])
    cell, flags = validate_cell(
        CellDraft(status="reported", value=10, evidence_ids=[source.id]),
        MatrixColumn(key="value", label="Value", instruction="value", type="number"), packet,
    )
    sentences, _ = validate_sentences(
        [SentenceDraft(text="Value 10.", kind="reported_fact", evidence_ids=[source.id])], packet,
    )
    assert cell.status == "reported" and flags == ["from_image"]
    assert sentences[0].flags == ["from_image", "unquoted_fact"]
    text = source.model_copy(update={"id": "ev_text", "kind": "text", "from_image": False})
    text_packet = to_packet([source, text])
    _, text_flags = validate_cell(
        CellDraft(status="reported", value=10, evidence_ids=[source.id, text.id]),
        MatrixColumn(key="value", label="Value", instruction="value", type="number"), text_packet,
    )
    assert text_flags == []


def test_ask_retrieves_selected_asset_evidence_without_text_hits(conn: sqlite3.Connection, paths) -> None:
    seed(conn, text="Results describe a separate outcome.")
    add_asset(conn, paths, caption="Table 1: parameter estimates for gain.")
    vision = FakeVisionProvider({
        "columns": ["Parameter", "Estimate"], "rows": [["gain", "3.25"]],
        "units": {}, "footnotes": [],
    })
    vision_gateway = ModelGateway(conn, Config(), providers={"local_vision": vision})  # type: ignore[arg-type]
    analyze_asset(conn, vision_gateway, paths, "asset-table", "table")

    class AnswerProvider:
        def chat_json(self, messages: list[dict[str, str]], schema: dict[str, object]) -> dict[str, object]:
            import re

            evidence_ids = re.findall(r'<evidence id="([^"]+)"', messages[-1]["content"])
            assert len(evidence_ids) == 2
            return {"sentences": [{"text": "The gain is 3.25.", "kind": "reported_fact",
                                  "evidence_ids": [evidence_ids[1]]}], "insufficient": False}

    answer_gateway = ModelGateway(conn, Config(), providers={"local": AnswerProvider()})  # type: ignore[arg-type]
    from litforge.analysis.ask import ask

    answer = ask(conn, answer_gateway, "parameter estimate")
    assert answer.evidence[0].from_image and answer.sentences[0].flags == ["from_image", "unquoted_fact"]


def test_chart_and_equation_fidelity_and_glossary(conn: sqlite3.Connection, paths) -> None:
    span = seed(conn, text="Results define alpha as the synaptic gain. The legend shows control and treatment.")
    add_asset(conn, paths, "asset-chart", "Figure 1: control and treatment", "figure")
    chart = FakeVisionProvider({
        "axes": {"x": "time (s)", "y": "gain"}, "legend": ["control", "invented"],
        "series": [{"name": "control", "points": [[0, 1], [1, 2]]},
                   {"name": "treatment", "points": [[0, 2], [1, 3]]}],
        "error_bars": None,
    })
    gateway = ModelGateway(conn, Config(), providers={"local_vision": chart})  # type: ignore[arg-type]
    result = analyze_asset(conn, gateway, paths, "asset-chart", "chart")
    assert result.fidelity["legend_entries_found"] == ["control"]
    assert all(series["point_label"] == "digitized_estimate" for series in result.result["series"])
    assert all(len(series["evidence_ids"]) == len(series["points"]) for series in result.result["series"])

    add_asset(conn, paths, "asset-equation", "Equation 1", "equation")
    equation = FakeVisionProvider({"latex": "y = alpha x", "symbols": [
        {"symbol": "alpha", "meaning": "synaptic gain", "evidence": "text"},
        {"symbol": "zeta", "meaning": "unknown", "evidence": "unknown"},
    ]})
    gateway = ModelGateway(conn, Config(), providers={"local_vision": equation})  # type: ignore[arg-type]
    eq = analyze_asset(conn, gateway, paths, "asset-equation", "equation")
    assert eq.fidelity["symbols_found"] == ["alpha"]
    terms = glossary(conn, "proj")
    assert any(t["normalised_meaning"] == "synaptic gain" and
               any(symbol["symbol"] == "alpha" for symbol in t["symbols"]) for t in terms)
    assert span.id


def test_equation_comparison_is_a_run_and_requires_evidence_or_unknown(conn: sqlite3.Connection) -> None:
    seed(conn)
    provider = FakeVisionProvider({"shared_assumptions": [{"text": "same", "evidence_ids": ["ev_a"]}],
                                   "different_assumptions": [{"text": "different", "evidence_ids": []}]})
    gateway = ModelGateway(conn, Config(), providers={"local": provider})  # type: ignore[arg-type]
    a = EvidencePacket(items=[EvidenceItem(id="ev_a", paper_id="a", text="alpha", page=1)])
    b = EvidencePacket(items=[EvidenceItem(id="ev_b", paper_id="b", text="beta", page=1)])
    result = compare_equations(conn, gateway, "proj", {"latex": "x=1", "evidence_ids": ["ev_a"]},
                               {"latex": "x=2", "evidence_ids": ["ev_b"]}, [a.items[0], b.items[0]])
    assert result["shared_assumptions"][0]["evidence_ids"] == ["ev_a"]
    assert result["different_assumptions"][0]["label"] == "unknown"
    run = conn.execute("SELECT * FROM analysis_runs WHERE task='equation_compare'").fetchone()
    assert run is not None and run["status"] == "ok"
    assert get_run(conn, run["id"]).project_id == "proj"
    assert conn.execute("SELECT run_id FROM model_calls").fetchone()[0] == run["id"]


def _claim(conn: sqlite3.Connection, claim_id: str, paper_id: str, cluster: str,
           evidence_id: str, label: str) -> None:
    conn.execute("INSERT OR IGNORE INTO claim_clusters(id,project_id,label,created_at) VALUES (?,?,?,?)",
                 (cluster, "proj", label, now()))
    run_id = conn.execute("SELECT id FROM analysis_runs LIMIT 1").fetchone()
    if run_id is None:
        from litforge.analysis.runs import start_run
        run = start_run(conn, "claim_extract", "proj", {}, {})
    else:
        run = run_id[0]
    conn.execute(
        """INSERT INTO claims(id,project_id,paper_id,text,canonical_text,canonical_hash,type,polarity,
           status,run_id,model,prompt_version,created_at,cluster_id)
           VALUES (?,'proj',?,?,?,?,'finding','positive','accepted',?,'fake','v1',?,?)""",
        (claim_id, paper_id, label, label, claim_id, run, now(), cluster),
    )
    conn.execute("INSERT INTO claim_evidence(claim_id,evidence_id,relation,strength) VALUES (?,?,'supports','direct')",
                 (claim_id, evidence_id))


def test_deterministic_gap_detectors_and_review_gate(conn: sqlite3.Connection) -> None:
    spans = [seed(conn, "p1", "Results population adults: drug A, baseline placebo, parameter rate 2.0."),
             seed(conn, "p2", "Results population adults: drug B, baseline placebo, parameter rate 3.0."),
             seed(conn, "p3", "Results population children: drug A, parameter rate 4.0. " +
                  "Supporting [quote] *data* " * 30)]
    conn.execute("INSERT INTO claim_clusters(id,project_id,label,created_at) VALUES "
                 "('single','proj','single source','now'),('conflict','proj','conflict','now'),"
                 "('multi','proj','adults','now')")
    _claim(conn, "c-single", "p3", "single", spans[2].id, "single source")
    _claim(conn, "c-conflict-a", "p1", "conflict", spans[0].id, "conflicting claim A")
    _claim(conn, "c-conflict-b", "p2", "conflict", spans[1].id, "conflicting claim B")
    conn.execute("INSERT INTO claim_relations(id,project_id,source_claim_id,target_claim_id,relation,explanation,"
                 "origin,created_at) VALUES ('rel','proj','c-conflict-a','c-conflict-b','contradicts','conflict',"
                 "'rule','now')")
    _claim(conn, "c-multi-a", "p1", "multi", spans[0].id, "adult outcome")
    _claim(conn, "c-multi-b", "p2", "multi", spans[1].id, "adult outcome")
    matrix = create_matrix(conn, "proj", "study", "population and baseline", [
        MatrixColumn(key="population", label="Population", instruction="Population", type="categorical",
                     options=["adults", "children"]),
        MatrixColumn(key="intervention", label="Intervention category", instruction="drug category",
                     type="categorical", options=["drug A", "drug B", "placebo"]),
        MatrixColumn(key="parameter-rate", label="Parameter rate", instruction="rate parameter", type="number",
                     parameter=True),
    ])
    from litforge.analysis.runs import start_run

    matrix_run = start_run(conn, "matrix_extract", "proj", {}, {})
    for p, pop, drug, val, span in [
        ("p1", "adults", "drug A", "2.0", spans[0]), ("p2", "adults", "drug B", "3.0", spans[1]),
        ("p3", "children", "drug A", "4.0", spans[2]),
    ]:
        for col, value in zip(matrix.columns, [pop, drug, val], strict=True):
            conn.execute(
                """INSERT INTO matrix_cells(id,matrix_id,paper_id,column_key,column_hash,status,value_json,
                   evidence_ids_json,model,provider,prompt_version,parse_run_id,cache_key,run_id,updated_at)
                   VALUES (?,?,?,?,?,'reported',?,?,'fake','fake','v1','rp','key',?,'now')""",
                (f"{p}-{col.key}", matrix.id, p, col.key, col.column_hash, json.dumps(value),
                 json.dumps([span.id]), matrix_run),
            )
    gaps = detect_gaps(conn, "proj")
    types = {gap.kind for gap in gaps}
    assert {"single_source_claim", "contradicted_cluster", "underrepresented_category",
            "missing_baseline", "parameter_without_sensitivity"} <= types
    assert all(g.evidence_ids for g in gaps)
    single = next(g for g in gaps if g.kind == "single_source_claim")
    contradicted = next(g for g in gaps if g.kind == "contradicted_cluster")
    parameter = next(g for g in gaps if g.kind == "parameter_without_sensitivity")
    assert single.evidence_ids == [spans[2].id]
    assert contradicted.evidence_ids == sorted([spans[0].id, spans[1].id])
    assert parameter.evidence_ids == sorted(span.id for span in spans)
    assert any(g.title == "Underrepresented Population: children" and g.evidence_ids == [spans[2].id]
               for g in gaps if g.kind == "underrepresented_category")
    assert any(set(g.evidence_ids) >= {spans[0].id, spans[1].id, spans[2].id}
               for g in gaps if g.kind == "missing_baseline")
    candidate = single
    provider = FakeVisionProvider({
        "why_it_matters": [{"text": "Why [it] matters", "evidence_ids": candidate.evidence_ids}],
        "minimal_experiment": [{"text": "Test a proposal.", "evidence_ids": [], "label": "proposal"}],
        "data": [{"text": "Data needed.", "evidence_ids": [], "label": "proposal"}],
        "controls": [{"text": "Use a control.", "evidence_ids": [], "label": "proposal"}],
        "outcome": [{"text": "Measure the outcome.", "evidence_ids": [], "label": "proposal"}],
        "failure_modes": [{"text": "Possible failure.", "evidence_ids": [], "label": "proposal"}],
        "what_model_does_not_know": [{"text": "The effect size is unknown.", "evidence_ids": [], "label": "unknown"}],
    })
    gateway = ModelGateway(conn, Config(), providers={"local": provider})  # type: ignore[arg-type]
    card = design_gap(conn, gateway, candidate.id)
    assert card.title == "Research proposal — not a literature finding"
    assert all(sentence.evidence_ids or sentence.label in ("proposal", "unknown")
               for sentences in card.content.values() for sentence in sentences)
    with pytest.raises(ValueError, match="accepted"):
        export_gap_card(conn, card.id)
    accepted = review_gap(conn, card.id, "accept", "reviewed against the evidence")
    assert accepted.review_status == "accepted"
    markdown = export_gap_card(conn, card.id)
    assert "not a literature finding" in markdown
    assert r"Why \[it\] matters" in markdown
    assert r"Supporting \[quote\] \*data\*" in markdown
    assert f"[^{spans[2].id}]" in markdown
    assert f"[^{spans[2].id}]: Title p3, p.1" in markdown
    from litforge.analysis.markdown_safe import excerpt, inert

    assert f"evidence ID={inert(spans[2].id)}" in markdown
    history = conn.execute(
        "SELECT action,old_json,new_json,reason FROM human_reviews WHERE target_kind='gap_card' "
        "AND target_id=? ORDER BY rowid", (card.id,),
    ).fetchall()
    assert len(history) == 1
    assert history[0]["action"] == "accept" and history[0]["reason"] == "reviewed against the evidence"
    assert json.loads(history[0]["old_json"])["review_status"] == "open"
    assert json.loads(history[0]["new_json"])["review_status"] == "accepted"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE human_reviews SET reason='changed' WHERE target_kind='gap_card' AND target_id=?",
                     (card.id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM human_reviews WHERE target_kind='gap_card' AND target_id=?", (card.id,))
    footnote = next(line for line in markdown.splitlines() if line.startswith(f"[^{spans[2].id}]:"))
    quote = footnote.split(' — "', 1)[1][:-1]
    assert quote == inert(excerpt(spans[2].text, 300))
    assert len(excerpt(spans[2].text, 300)) <= 300

    redesigned = design_gap(conn, gateway, candidate.id)
    assert redesigned.review_status == "open"
    with pytest.raises(ValueError, match="accepted"):
        export_gap_card(conn, redesigned.id)
    rejected = review_gap(conn, card.id, "reject", "not ready")
    assert rejected.review_status == "rejected"
    history = conn.execute(
        "SELECT action,old_json,new_json,reason FROM human_reviews WHERE target_kind='gap_card' "
        "AND target_id=? ORDER BY rowid", (card.id,),
    ).fetchall()
    assert len(history) == 2
    assert history[1]["action"] == "reject" and history[1]["reason"] == "not ready"
    assert json.loads(history[1]["old_json"])["review_status"] == "open"
    assert json.loads(history[1]["new_json"])["review_status"] == "rejected"


def test_gap_candidate_support_ids_and_detection_are_deterministic(conn: sqlite3.Connection) -> None:
    span = seed(conn)
    _claim(conn, "solo", "p", "only", span.id, "only study")
    first = detect_gaps(conn, "proj")
    second = detect_gaps(conn, "proj")
    assert [(g.id, g.evidence_ids) for g in first] == [(g.id, g.evidence_ids) for g in second]
    assert next(g for g in first if g.kind == "single_source_claim").evidence_ids == [span.id]


def test_vision_refuses_crop_outside_parse_output(conn: sqlite3.Connection, paths) -> None:
    seed(conn, text="Results: A parsed source.")
    add_asset(conn, paths)
    outside = paths.root / "outside.png"
    outside.write_bytes(b"not-a-crop")
    conn.execute("UPDATE assets SET path='outside.png' WHERE id='asset-table'")
    provider = FakeVisionProvider({
        "columns": [], "rows": [], "units": {}, "footnotes": [],
    })
    gateway = ModelGateway(conn, Config(), providers={"local_vision": provider})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="inside its parse output"):
        analyze_asset(conn, gateway, paths, "asset-table", "table")
    assert provider.images == []
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


def _extract_numbers(conn: sqlite3.Connection, matrix_id: str) -> None:
    from litforge.analysis.matrix import extract_matrix
    from litforge.screening.llm import FakeLlm
    from tests.lens_fakes import evidence_id as eid
    gateway = ModelGateway(conn, Config(), providers={"local": FakeLlm(  # type: ignore[arg-type]
        lambda m: {"status": "reported", "value": 2.0, "evidence_ids": [eid(m)]})})
    extract_matrix(conn, gateway, matrix_id)


def test_sensitivity_coverage_is_per_parameter(conn: sqlite3.Connection) -> None:
    seed(conn, "p1", "Results: rate 2.0 and gain 2.0; sensitivity of gain was tested.")
    matrix = create_matrix(conn, "proj", "params", "parameters", [
        MatrixColumn(key="rate", label="Rate", instruction="rate", type="number", parameter=True),
        MatrixColumn(key="gain", label="Gain", instruction="gain", type="number", parameter=True),
        MatrixColumn(key="gain-sens", label="Gain sensitivity", instruction="Was gain varied?", type="boolean",
                     sensitivity_analysis=True, sensitivity_for=["gain"]),
    ])
    _extract_numbers(conn, matrix.id)
    flagged = {g.title for g in detect_gaps(conn, "proj") if g.kind == "parameter_without_sensitivity"}
    assert flagged == {"No sensitivity analysis flagged for Rate"}
    with pytest.raises(ValueError):
        MatrixColumn(key="x", label="X", instruction="x", type="text", sensitivity_for=["gain"])


def test_gaps_that_no_longer_qualify_are_stale_and_cannot_be_exported(conn: sqlite3.Connection) -> None:
    span = seed(conn, "p1", "Results: single finding.")
    _claim(conn, "c1", "p1", "cl", span.id, "lonely finding")
    gap = next(g for g in detect_gaps(conn, "proj") if g.kind == "single_source_claim")
    conn.execute("INSERT INTO gap_cards(id,candidate_id,title,content_json,review_status,run_id,created_at,"
                 "updated_at) VALUES ('card',?,'t','{}','accepted',(SELECT id FROM analysis_runs LIMIT 1),"
                 "'now','now')", (gap.id,))
    conn.execute("UPDATE claims SET status='rejected' WHERE id='c1'")
    assert all(g.id != gap.id for g in detect_gaps(conn, "proj"))
    with pytest.raises(ValueError, match="no longer"):
        export_gap_card(conn, "card")


def test_failed_analysis_runs_store_sanitised_errors(conn: sqlite3.Connection) -> None:
    from litforge.screening.llm import FakeLlm
    span = seed(conn, "p1", "Results: single finding.")
    _claim(conn, "c1", "p1", "cl", span.id, "lonely finding")
    gap = next(g for g in detect_gaps(conn, "proj") if g.kind == "single_source_claim")
    gateway = ModelGateway(conn, Config(), providers={"local": FakeLlm(  # type: ignore[arg-type]
        lambda m: {"why_it_matters": "secret-model-output-123"})})
    with pytest.raises(Exception):  # noqa: B017 - any validation failure
        design_gap(conn, gateway, gap.id)
    errors = [r[0] for r in conn.execute("SELECT error FROM analysis_runs WHERE task='gap_design'")]
    assert errors and all("secret-model-output-123" not in (e or "") for e in errors)


@pytest.mark.parametrize("count", [25, 26, 40, 101])
def test_chart_points_are_bounded_after_validation(conn, paths, count) -> None:
    seed(conn)
    add_asset(conn, paths, "chart-limit", "control", "figure")
    provider = FakeVisionProvider({"axes": {}, "legend": [], "cut_off": True,
        "series": [{"name": "control", "points": [[i, i * 2] for i in range(count)]}]})
    gateway = ModelGateway(conn, Config(), providers={"local_vision": provider})
    result = analyze_asset(conn, gateway, paths, "chart-limit", "chart")
    series = result.result["series"][0]
    expected = list(range(count)) if count <= 25 else [i * (count - 1) // 24 for i in range(25)]
    assert [p[0] for p in series["points"]] == expected
    assert len(series["evidence_ids"]) == len(expected) == len(result.evidence)
    assert ("points_downsampled" in result.fidelity.get("flags", [])) == (count > 25)
    assert "cut_off_crop" in result.fidelity["flags"]
