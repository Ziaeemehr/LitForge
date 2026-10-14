from pathlib import Path

from litforge.discovery.importance import importance_scores, priority
from litforge.discovery.merge import upsert_record
from litforge.discovery.records import WorkRecord
from litforge.storage.db import open_db


def _add(conn, oa: str, **kw) -> str:  # type: ignore[no-untyped-def]
    pid, _ = upsert_record(conn, WorkRecord(source="openalex", source_id=oa, openalex_id=oa,
                                            title=f"Paper {oa} with a long enough title", **kw), "t")
    return pid


def test_importance_combines_available_signals(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "x.sqlite")
    core = _add(conn, "W1", year=2015, cited_by_count=900, citation_percentile=0.99, recent_citations=120)
    mid = _add(conn, "W2", year=2020, cited_by_count=40, citation_percentile=0.6, recent_citations=10,
               references=["W1"])
    new = _add(conn, "W3", year=2026, cited_by_count=1, recent_citations=1, references=["W1", "W2"])
    bad = _add(conn, "W4", year=2018, cited_by_count=500, citation_percentile=0.97, is_retracted=True)
    scores = importance_scores(conn, [core, mid, new, bad])
    assert scores[core]["score"] > scores[mid]["score"] > scores[new]["score"]
    assert scores[core]["components"]["centrality"] == 1.0  # cited by both other papers in the set
    assert "top 1% cited in its field" in scores[core]["notes"]
    assert "cited by 2 papers in this set" in scores[core]["notes"]
    assert scores[core]["components"]["venue"] is None  # no venue data: weight redistributed
    assert scores[bad]["score"] == 0.0 and "RETRACTED" in scores[bad]["notes"]
    assert priority(1.0, 0.0) == 0.65 and priority(0.5, 1.0) == 0.675
