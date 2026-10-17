from __future__ import annotations

import re
import sqlite3
from typing import Any

from litforge.analysis.gateway import ModelGateway
from litforge.analysis.ledger import EvidenceSpan, record_spans
from litforge.config import Config
from litforge.indexing.fts import search
from litforge.screening.llm import FakeLlm


def seed(conn: sqlite3.Connection, paper: str = "p",
         text: str = "Results: gain 3.25 and 1,200 participants, 20%.") -> EvidenceSpan:
    conn.execute("INSERT INTO papers(id,title,created_at,updated_at) VALUES (?,?,'now','now')", (paper, "Title "+paper))
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES ('proj','demo','now') ON CONFLICT DO NOTHING")
    conn.execute("INSERT INTO project_papers(project_id,paper_id,source,added_at) "
                 "VALUES ('proj',?,'manual','now')", (paper,))
    conn.execute("""INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at)
                    VALUES (?,?,?,'file.pdf',1,'ok','now')""", ("a"+paper, paper, "sha"+paper))
    conn.execute("""INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,
                    status,created_at,is_current,output_dir)
                    VALUES (?,?,'fake','1','h','ok','now',1,'parsed')""", ("r"+paper, "a"+paper))
    conn.execute("""INSERT INTO chunks(id,parse_run_id,paper_id,kind,order_idx,text,token_count,
                    page_start,page_end,hash)
                    VALUES (?,?,?,'text',0,?,10,1,1,'hash')""", ("c"+paper, "r"+paper, paper, text))
    return record_spans(conn, search(conn, "Results", paper_ids=[paper]))[0]


def evidence_id(messages: list[dict[str, str]]) -> str:
    return re.findall("<evidence id=\"([^\"]+)\"", messages[-1]["content"])[0]


def gateway(conn: sqlite3.Connection, payload: dict[str, Any] | None = None) -> tuple[ModelGateway, FakeLlm]:
    fake = FakeLlm(lambda m: payload if payload is not None else {
        "sentences": [{"text": "The gain is 3.25.", "kind": "reported_fact", "evidence_ids": [evidence_id(m)]}],
        "insufficient": False,
    })
    return ModelGateway(conn, Config(), providers={"local": fake}), fake
