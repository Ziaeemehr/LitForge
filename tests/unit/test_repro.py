from __future__ import annotations

import socket
import sqlite3
import urllib.request

import pytest
from lens_fakes import evidence_id, gateway, seed

from litforge.analysis.matrix import extract_matrix
from litforge.analysis.repro import ensure_repro_matrix, extract_links, repro_card
from litforge.screening.llm import FakeLlm


def test_repro_links_all_chunks_hosts_and_no_execution(conn: sqlite3.Connection) -> None:
    text = ("Results: https://github.com/lab/code. https://gitlab.com/a/b https://bitbucket.org/a/b "
            "https://zenodo.org/records/1 https://figshare.com/articles/1 https://osf.io/abc "
            "https://huggingface.co/datasets/lab/data https://huggingface.co/lab/model "
            "https://kaggle.com/datasets/a/b https://datadryad.org/stash/dataset/1 "
            "https://physionet.org/content/test https://openneuro.org/datasets/ds1 "
            "https://doi.org/10.5281/zenodo.123 https://doi.org/10.5061/dryad.abc "
            "https://doi.org/10.1234/paper https://github.com.evil.test/x javascript:alert(1)")
    span = seed(conn, text=text)
    conn.execute("INSERT INTO chunks(id,parse_run_id,paper_id,kind,order_idx,text,token_count,"
                 "page_start,page_end,hash) "
                 "VALUES ('late','rp','p','text',99,'See https://github.com/lab/supplement',10,9,9,'late')")
    links = extract_links(conn, "p")
    assert len(links) == 15
    assert sum(i.kind == "code" for i in links) == 4
    assert not any("evil" in i.url or "10.1234" in i.url for i in links)
    assert any(i.url == "https://github.com/lab/code" and i.evidence_id == span.id for i in links)
    kinds = {i.url: i.kind for i in links}
    # Zenodo, Figshare and OSF also host papers and software: a link there alone does not establish data.
    assert kinds["https://doi.org/10.5281/zenodo.123"] == kinds["https://zenodo.org/records/1"] == "other"
    assert kinds["https://doi.org/10.5061/dryad.abc"] == kinds["https://openneuro.org/datasets/ds1"] == "data"
    assert extract_links(conn, "p") == links
    # No current parse: the card must not present old links as current observations.
    conn.execute("UPDATE parse_runs SET is_current=0")
    assert extract_links(conn, "p") == []


def test_repro_template_matrix_checklist_and_cited_brief(conn: sqlite3.Connection) -> None:
    seed(conn, text="Results: dataset Alpha preprocessing software versions Python 3.12 "
                    "random seeds hyperparameters hardware GPU compute budget 2 hours metrics split code availability")
    matrix = ensure_repro_matrix(conn, "proj")
    assert len(matrix.columns) == 12 and ensure_repro_matrix(conn, "proj").id == matrix.id
    gw, _ = gateway(conn)
    gw.providers["local"] = FakeLlm(lambda m: {"status": "reported", "value": "Alpha",
                                                "evidence_ids": [evidence_id(m)]})
    extract_matrix(conn, gw, matrix.id)
    card = repro_card(conn, "p", "proj")
    assert len(card.checklist) == 12 and all(i.state == "present" for i in card.checklist)
    assert "[^ev_" in card.brief and "Compute estimate" in card.brief
    conn.execute("UPDATE matrix_cells SET status='not_reported',value_json='null' WHERE column_key='hardware'")
    card = repro_card(conn, "p", "proj")
    assert "Compute estimate: not reported" in card.brief
    assert any(i.key == "hardware" and i.state == "missing" for i in card.checklist)
    conn.execute("UPDATE matrix_cells SET flags_json='[\"type_mismatch\"]' WHERE column_key='metrics'")
    assert next(i for i in repro_card(conn, "p", "proj").checklist if i.key == "metrics").state == "unclear"
    assert "[proposal]" in repro_card(conn, "p", "proj").brief


def test_repro_empty_card_never_claims_absence_as_fact(conn: sqlite3.Connection) -> None:
    seed(conn)
    card = repro_card(conn, "p", "proj")
    assert all(i.state == "unclear" for i in card.checklist)
    assert "Compute estimate: not reported [unsupported]" in card.brief
    assert "[proposal]" in card.brief
    assert conn.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


def test_card_without_project_uses_only_unambiguous_matrix(conn: sqlite3.Connection) -> None:
    span = seed(conn, text="Results: hardware GPU")
    matrix = ensure_repro_matrix(conn, "proj")
    gw, _ = gateway(conn, {"status": "reported", "value": "GPU", "evidence_ids": [span.id]})
    extract_matrix(conn, gw, matrix.id)
    assert next(i for i in repro_card(conn, "p").checklist if i.key == "hardware").state == "present"
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('other','other','now')")
    conn.execute("INSERT INTO project_papers VALUES ('other','p','manual','now')")
    ensure_repro_matrix(conn, "other")
    assert all(i.state == "unclear" for i in repro_card(conn, "p").checklist)


def test_compute_estimate_requires_reported_hardware_and_duration(conn: sqlite3.Connection) -> None:
    span = seed(conn, text="Results: hardware GPU compute budget 2 hours")
    matrix = ensure_repro_matrix(conn, "proj")
    gw, _ = gateway(conn, {"status": "reported", "value": "GPU 2 hours", "evidence_ids": [span.id]})
    extract_matrix(conn, gw, matrix.id)
    assert "use the reported budget" in repro_card(conn, "p", "proj").brief
    conn.execute("UPDATE matrix_cells SET value_json='\"unknown\"' WHERE column_key='compute_budget'")
    assert "Compute estimate: not reported" in repro_card(conn, "p", "proj").brief


def test_repro_structured_sections_status_and_evidence(conn: sqlite3.Connection) -> None:
    span = seed(conn, text="Results: data availability dataset names data split hardware GPU compute budget 2 hours "
                           "https://github.com/lab/code")
    matrix = ensure_repro_matrix(conn, "proj")
    gw, _ = gateway(conn, {"status": "reported", "value": "GPU 2 hours", "evidence_ids": [span.id]})
    extract_matrix(conn, gw, matrix.id)
    card = repro_card(conn, "p", "proj")
    sections = {s.title: s.items for s in card.sections}
    assert list(sections) == ["Data needed", "Steps and parameters to clarify", "Compute estimate", "Links"]
    assert sections["Data needed"][0].status == "reported_fact"
    assert sections["Data needed"][0].evidence_ids == [span.id]
    assert sections["Compute estimate"][0].status == "interpretation"
    assert sections["Links"][0].evidence_ids == [span.id]
    assert {s.id for s in card.evidence} == {span.id}
    assert all("[^ev_" not in i.value for s in card.sections for i in s.items)
    conn.execute("UPDATE matrix_cells SET status='not_reported',value_json='null' WHERE column_key='hardware'")
    card = repro_card(conn, "p", "proj")
    estimate = next(s for s in card.sections if s.title == "Compute estimate").items[0]
    assert estimate.status == "unsupported" and estimate.evidence_ids == []
    assert any(i.status == "proposal" for s in card.sections for i in s.items)


def test_repro_never_opens_a_network_connection(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("repro must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    seed(conn, text="Results: code https://github.com/lab/code data https://doi.org/10.5061/dryad.abc")
    assert len(extract_links(conn, "p")) == 2
    assert repro_card(conn, "p", "proj").links
