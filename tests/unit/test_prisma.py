"""PRISMA provenance, retrieval conservation and safe SVG output."""
import xml.etree.ElementTree as ET

from test_screening_status import mark, paper, project, result, versions

from litforge.discovery.versions import primary_paper_ids
from litforge.screening.prisma import prisma_counts, prisma_svg


def hit(conn, pid, source="openalex", relation="query", kind="search", run="r"):
    conn.execute("INSERT OR IGNORE INTO discovery_runs(id,project_id,kind,status,started_at) "
                 "VALUES (?,'p',?,'done','now')", (run, kind))
    conn.execute("INSERT INTO discovery_hits(run_id,paper_id,source,relation) VALUES (?,?,?,?)",
                 (run, pid, source, relation))


def seed(conn):
    project(conn)
    for i in range(8):
        paper(conn, str(i))
    for i in range(5):
        hit(conn, str(i))
    for i in (3, 4, 5):
        hit(conn, str(i), "crossref")
    for i in (6, 7):
        hit(conn, str(i), "import:refs.ris", "import", "import", "imp")


def invariant(conn, p):
    assert sum(p.identified_by_source.values()) + sum(p.identified_other.values()) - p.duplicates_removed == len(
        primary_paper_ids(conn, "p"))
    assert p.screened + p.awaiting_screening == len(primary_paper_ids(conn, "p"))
    assert p.sought - p.not_retrieved == p.assessed + p.awaiting_fulltext


def parsed(conn, pid, current=1):
    conn.execute("INSERT INTO artifacts(id,paper_id,sha256,pdf_path,size_bytes,status,created_at) "
                 "VALUES (?, ?, ?, 'a.pdf', 1, 'ok', 'now')", (pid, pid, pid))
    conn.execute("INSERT INTO parse_runs(id,artifact_id,parser,parser_version,config_hash,status,"
                 "is_current,output_dir,created_at) VALUES (?,?,'fake','v','c','ok',?,'parsed','now')",
                 (pid, pid, current))


def test_seeded_library_and_repeated_search(conn):
    seed(conn)
    p = prisma_counts(conn, "p")
    assert p.identified_by_source == {"openalex": 5, "crossref": 3, "import": 2}
    assert p.duplicates_removed == 2
    assert p.awaiting_screening == 8
    invariant(conn, p)
    for i in range(5):
        hit(conn, str(i), run="repeat")
    assert prisma_counts(conn, "p") == p


def test_other_methods_removed_and_no_hit_fallback(conn):
    seed(conn)
    for pid, source in [("cite", "discovery"), ("manual", "manual"), ("orphan", "import"), ("sim", "discovery"),
                        ("link", "discovery")]:
        paper(conn, pid, source)
    hit(conn, "cite", relation="references", kind="citations", run="cite")
    hit(conn, "sim", relation="keywords", kind="similar", run="sim")
    hit(conn, "link", source="link", relation="link", kind="links", run="link")
    conn.execute("DELETE FROM project_papers WHERE paper_id='0'")
    p = prisma_counts(conn, "p")
    assert p.identified_by_source == {"openalex": 4, "crossref": 3, "import": 2}
    assert p.identified_other == {"citations": 1, "manual": 1, "import": 1, "similar": 1, "links": 1}
    invariant(conn, p)


def test_linked_versions_count_one_primary(conn):
    seed(conn)
    versions(conn, "0", "1")
    p = prisma_counts(conn, "p")
    assert p.duplicates_removed == 3
    assert p.awaiting_screening == 7
    invariant(conn, p)


def test_retrieval_final_totals_and_reasons(conn):
    seed(conn)
    result(conn, "0", "exclude")
    result(conn, "1", "include")  # sought, missing
    result(conn, "2", "uncertain")
    parsed(conn, "2")  # awaiting full text
    result(conn, "3", "include", "S2")
    result(conn, "4", "exclude", "S2", exclusion=[
        {"criterion": "Wrong population <&> " + "long reason " * 8, "status": "violated", "verified": True}])
    result(conn, "5", "include", "S2")
    mark(conn, "5", "irrelevant")
    result(conn, "6", "uncertain")
    parsed(conn, "6", current=0)
    p = prisma_counts(conn, "p")
    assert (p.screened, p.awaiting_screening, p.excluded_s1, p.sought) == (7, 1, 1, 6)
    assert (p.assessed, p.not_retrieved, p.awaiting_fulltext, p.included, p.uncertain) == (3, 2, 1, 2, 2)
    assert sum(p.excluded_s2_by_reason.values()) == 2
    assert p.excluded_s2_by_reason["excluded by reviewer"] == 1
    invariant(conn, p)
    root = ET.fromstring(prisma_svg(p, "مرور <&>"))
    text = " ".join(root.itertext())
    assert "مرور <&>" in text
    assert all(el.get("direction", "ltr") in {"ltr", "rtl"} for el in root.iter())
    for value in p.model_dump().values():
        for count in (value.values() if isinstance(value, dict) else [value]):
            assert str(count) in text
    assert not any(el.tag.endswith(("script", "foreignObject")) for el in root.iter())
    assert not any("href" in key or key.startswith("on") for el in root.iter() for key in el.attrib)
