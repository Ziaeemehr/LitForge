from pathlib import Path

import pytest
from discovery_fakes import ARXIV_FEED, FRONTIER_CROSSREF, FRONTIER_OA, FakeTransport, ok

from litforge.discovery.fetch import candidate_urls, citation_pdf_url, plain_reason, unpaywall_locations
from litforge.discovery.http import BudgetExhausted, HttpClient
from litforge.discovery.importers import parse_bibtex, parse_csv, parse_ris
from litforge.discovery.rank import lexical_score, rank, terms
from litforge.discovery.records import WorkRecord, normalize_arxiv_id
from litforge.discovery.sources import Arxiv, Crossref, OpenAlex


def test_normalize_arxiv_id() -> None:
    assert normalize_arxiv_id("arXiv:1905.07488v2") == "1905.07488"
    assert normalize_arxiv_id("https://arxiv.org/abs/2007.09114") == "2007.09114"
    assert normalize_arxiv_id("https://arxiv.org/pdf/1412.6980v9.pdf") == "1412.6980"
    assert normalize_arxiv_id("hep-th/9901001v1") == "hep-th/9901001"
    assert normalize_arxiv_id("10.1073/pnas.1912789117") is None


def test_record_normalization() -> None:
    rec = WorkRecord(source="x", source_id="1", doi="https://doi.org/10.48550/arXiv.2007.09114",
                     title="  A\n title ", abstract="<jats:p>Hello</jats:p>", year=99999).normalized()
    assert rec.doi == "10.48550/arxiv.2007.09114" and rec.arxiv_id == "2007.09114"
    assert rec.title == "A title" and rec.abstract == "Hello" and rec.year is None


def test_source_parsers_agree_on_identifiers() -> None:
    oa = OpenAlex.parse(FRONTIER_OA)
    cr = Crossref.parse(FRONTIER_CROSSREF)
    ax = Arxiv.parse_feed(ARXIV_FEED)[0]
    assert oa.doi == cr.doi == ax.doi == "10.1073/pnas.1912789117"
    assert oa.arxiv_id == ax.arxiv_id == "1911.01429"
    assert oa.openalex_id == "W2984417636" and oa.abstract == "Many domains use simulators"
    assert oa.venue == "Proceedings of the National Academy of Sciences" and oa.doi_is_verified
    assert ax.title == "The frontier of simulation-based inference" and not ax.doi_is_verified
    assert cr.abstract == "Many domains of science use simulators." and cr.year == 2020


def test_arxiv_query_builder() -> None:
    assert Arxiv.build_query('"neural mass" EEG inference') == 'all:"neural mass" AND all:EEG AND all:inference'


def test_http_retries_caches_and_tracks_cost(tmp_path: Path) -> None:
    sleeps: list[float] = []
    responses = iter([(429, {"Retry-After": "2"}, b""), ok({"a": 1}, {"X-RateLimit-Cost-USD": "0.001"})])
    calls: list[str] = []

    def transport(url, headers, timeout):  # type: ignore[no-untyped-def]
        calls.append(url)
        return next(responses)

    http = HttpClient(cache_dir=tmp_path, transport=transport, sleep=sleeps.append, min_interval={})
    url = http.url("https://api.openalex.org/works", {"filter": "x", "api_key": "SECRET"})
    assert http.get_json(url) == {"a": 1}
    assert 2.0 in sleeps and len(calls) == 2 and http.cost_usd == pytest.approx(0.001)
    assert http.get_json(url) == {"a": 1} and http.cache_hits == 1 and len(calls) == 2
    cached = list(tmp_path.rglob("*.json"))
    assert cached and all("SECRET" not in p.read_text() for p in cached)


def test_http_gives_up_on_long_retry_after(tmp_path: Path) -> None:
    http = HttpClient(transport=FakeTransport([("", (429, {"Retry-After": "70000"}, b""))]),
                      sleep=lambda s: None, min_interval={})
    with pytest.raises(BudgetExhausted):
        http.get_json("https://api.openalex.org/works?x=1")


def test_rank_prefers_relevant_titles() -> None:
    q = terms("neural mass model inference")
    assert lexical_score(q, "Neural mass model inference from EEG", None) > lexical_score(q, "Cosmology", None)
    hits = [("a", "openalex", "q", 1), ("b", "openalex", "q", 2), ("b", "crossref", "q", 1)]
    scores = rank(hits, {"a": ("unrelated topic", None), "b": ("neural mass model inference", None)},
                  ["neural mass model inference"])
    assert scores["b"] > scores["a"]


BIB = r"""
@article{cranmer2020,
  title = {The {Frontier} of Simulation-Based Inference},
  author = {Cranmer, Kyle and Brehmer, Johann},
  journal = {PNAS}, year = 2020,
  doi = {10.1073/pnas.1912789117},
}
@misc{greenberg2019, title="Automatic Posterior Transformation for Likelihood-Free Inference",
  eprint = {1905.07488}, archivePrefix = {arXiv}, year = {2019}}
@comment{ignored}
"""
RIS = """TY  - JOUR
TI  - The frontier of simulation-based inference
AU  - Cranmer, Kyle
PY  - 2020
DO  - 10.1073/pnas.1912789117
ER  -
TY  - JOUR
TI  - Second paper title here
ER  -
"""
CSV = '''Title,Authors,DOI,Year,Abstract
The frontier of simulation-based inference,"Cranmer, K; Brehmer, J",10.1073/pnas.1912789117,2020,Many domains
"A paper with, comma in title",Someone,,2021,
'''


def test_importers() -> None:
    bib = parse_bibtex(BIB, "import:x.bib")
    assert [r.title for r in bib] == ["The Frontier of Simulation-Based Inference",
                                      "Automatic Posterior Transformation for Likelihood-Free Inference"]
    assert bib[0].authors == ["Cranmer, Kyle", "Brehmer, Johann"] and bib[0].year == 2020
    assert bib[1].arxiv_id == "1905.07488"
    ris = parse_ris(RIS, "import:x.ris")
    assert len(ris) == 2 and ris[0].doi == "10.1073/pnas.1912789117" and ris[0].year == 2020
    rows = parse_csv(CSV, "import:x.csv")
    assert rows[0].authors == ["Cranmer, K", "Brehmer, J"] and rows[1].title == "A paper with, comma in title"
    with pytest.raises(ValueError):
        parse_csv("Name,Value\na,b\n", "import:bad.csv")


def test_non_paper_records_are_dropped() -> None:
    review = {**FRONTIER_CROSSREF, "DOI": "10.7554/elife.1.sa1", "type": "peer-review"}
    http = HttpClient(transport=FakeTransport([("crossref", ok({"message": {"items": [FRONTIER_CROSSREF, review]}}))]),
                      sleep=lambda s: None, min_interval={})
    assert [r.doi for r in Crossref(http).search("x", 5)] == ["10.1073/pnas.1912789117"]


def test_fetch_candidate_urls_and_landing_page_link() -> None:
    assert candidate_urls("2001.00001", "https://arxiv.org/pdf/2001.00001v2") == ["https://arxiv.org/pdf/2001.00001"]
    assert candidate_urls(None, "https://pub.example/a") == ["https://pub.example/a"]
    assert candidate_urls(None, "ftp://x") == [] and candidate_urls(None, None) == []
    assert candidate_urls("1", "https://pub.example/a", ["https://repo.example/r", "https://pub.example/a"],
                          ["https://pub.example/b"]) == [
        "https://arxiv.org/pdf/1", "https://repo.example/r", "https://pub.example/a", "https://pub.example/b"]
    page = b'<meta name="citation_pdf_url" content="/content/1.full.pdf">'
    assert citation_pdf_url(page, "https://pub.example/a/b") == "https://pub.example/content/1.full.pdf"
    assert citation_pdf_url(b"<meta name='description' content='x'>", "https://pub.example") is None


def test_unpaywall_locations_need_an_email() -> None:
    payload = {"oa_locations": [
        {"host_type": "publisher", "url_for_pdf": "https://pub.example/a.pdf", "url": "https://pub.example/a"},
        {"host_type": "repository", "url_for_pdf": None, "url": "https://repo.example/r"},
        {"host_type": "repository", "url_for_pdf": "https://repo.example/r.pdf", "url": "https://repo.example/r"},
    ]}
    transport = FakeTransport([("api.unpaywall.org/v2/10.1/x?email=me%40uni.edu", ok(payload))])
    http = HttpClient(transport=transport, sleep=lambda s: None, min_interval={})
    assert unpaywall_locations(http, "10.1/x", "me@uni.edu") == (
        ["https://repo.example/r.pdf", "https://repo.example/r"], ["https://pub.example/a.pdf"])
    assert unpaywall_locations(http, "10.1/x", None) == ([], []) and len(transport.calls) == 1
    assert unpaywall_locations(http, None, "me@uni.edu") == ([], [])
    assert unpaywall_locations(http, "10.1/missing", "me@uni.edu") == ([], [])  # 404: just fewer candidates


def test_plain_reason_explains_download_failures() -> None:
    assert plain_reason(["www.jneurosci.org: HTTP 403"]) == "the site blocks automatic downloads"
    assert plain_reason(["infoscience.epfl.ch: HTTP 202"]) == "the site asks for a human check first"
    assert plain_reason(["a.org: not a PDF and no PDF link on the page"]) == "the page has no direct PDF link"
    assert plain_reason(["a.org: HTTP 404", "b.org: HTTP 403"]) == "the site blocks automatic downloads"
    assert plain_reason(["a.org: timed out"]) == "a.org: timed out"
