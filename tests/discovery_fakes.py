"""Canned API responses and a fake HTTP transport for discovery tests (no network)."""

from __future__ import annotations

import json
from collections.abc import Mapping

from litforge.discovery.http import Response

FRONTIER_OA = {
    "id": "https://openalex.org/W2984417636",
    "doi": "https://doi.org/10.1073/pnas.1912789117",
    "title": "The frontier of simulation-based inference",
    "publication_year": 2020,
    "type": "article",
    "cited_by_count": 988,
    "authorships": [{"author": {"display_name": "Kyle Cranmer"}}, {"author": {"display_name": "Johann Brehmer"}}],
    "primary_location": {"landing_page_url": "https://doi.org/10.1073/pnas.1912789117",
                         "source": {"display_name": "Proceedings of the National Academy of Sciences"}},
    "best_oa_location": {"landing_page_url": "https://arxiv.org/abs/1911.01429", "pdf_url": "https://arxiv.org/pdf/1911.01429"},
    "open_access": {"oa_url": "https://arxiv.org/pdf/1911.01429"},
    "abstract_inverted_index": {"Many": [0], "domains": [1], "use": [2], "simulators": [3]},
    "referenced_works": ["https://openalex.org/WR1", "https://openalex.org/WR2", "https://openalex.org/WR3"],
    "related_works": ["https://openalex.org/W2945000001"],
}
APT_OA = {
    "id": "https://openalex.org/W2945000001",
    "doi": None,
    "title": "Automatic Posterior Transformation for Likelihood-Free Inference",
    "publication_year": 2019,
    "cited_by_count": 300,
    "authorships": [{"author": {"display_name": "David Greenberg"}}],
    "primary_location": {"landing_page_url": "https://arxiv.org/abs/1905.07488", "source": {"display_name": "ICML"}},
    "best_oa_location": None,
    "abstract_inverted_index": {"simulation": [0], "inference": [1]},
    "referenced_works": ["https://openalex.org/WR1", "https://openalex.org/WR2"],
}
CITING_OA = {
    "id": "https://openalex.org/W3000000003",
    "doi": "https://doi.org/10.5555/citing.1",
    "title": "Neural mass model inference with simulation-based methods",
    "publication_year": 2023,
    "cited_by_count": 12,
    "authorships": [],
    "primary_location": {"source": {"display_name": "NeuroImage"}},
    "referenced_works": ["https://openalex.org/W2984417636", "https://openalex.org/WR3"],
}
FRONTIER_CROSSREF = {
    "DOI": "10.1073/PNAS.1912789117",
    "title": ["The frontier of simulation-based inference"],
    "author": [{"given": "Kyle", "family": "Cranmer"}],
    "issued": {"date-parts": [[2020, 5, 29]]},
    "container-title": ["Proceedings of the National Academy of Sciences"],
    "abstract": "<jats:p>Many domains of science use simulators.</jats:p>",
    "is-referenced-by-count": 900,
    "type": "journal-article",
}
UNRELATED_CROSSREF = {
    "DOI": "10.2172/2282440",
    "title": ["Strong lensing cosmology"],
    "issued": {"date-parts": [[2024]]},
}
ARXIV_FEED = b"""<?xml version='1.0' encoding='UTF-8'?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/1911.01429v3</id>
    <published>2019-11-04T18:00:00Z</published>
    <title>The frontier of simulation-based
      inference</title>
    <summary>Many domains of science have developed complex simulations.</summary>
    <author><name>Kyle Cranmer</name></author>
    <arxiv:doi>10.1073/pnas.1912789117</arxiv:doi>
    <link href="https://arxiv.org/pdf/1911.01429v3" rel="related" type="application/pdf" title="pdf"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/1905.07488v1</id>
    <published>2019-05-17T00:00:00Z</published>
    <title>Automatic Posterior Transformation for Likelihood-Free Inference</title>
    <summary>We present APT.</summary>
    <author><name>David Greenberg</name></author>
  </entry>
</feed>"""


def ok(payload: object, headers: Mapping[str, str] | None = None) -> Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return 200, dict(headers or {}), body


class FakeTransport:
    """Routes by URL substring; first match wins. Records every call."""

    def __init__(self, routes: list[tuple[str, Response]]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Response:
        self.calls.append(url)
        for fragment, response in self.routes:
            if fragment in url:
                return response
        return 404, {}, b"{}"


def default_routes() -> list[tuple[str, Response]]:
    cost = {"X-RateLimit-Cost-USD": "0.001", "X-RateLimit-Remaining-USD": "0.09"}
    return [
        ("filter=cited_by:W2984417636", ok({"results": [APT_OA]}, cost)),
        ("filter=cites:W2984417636", ok({"results": [CITING_OA]}, cost)),
        ("filter=ids.openalex:", ok({"results": [APT_OA]}, cost)),
        ("filter=cited_by:", ok({"results": []}, cost)),  # other works: no data, like the real API
        ("filter=cites:", ok({"results": [CITING_OA]}, cost)),
        ("works/doi:10.1073/pnas.1912789117", ok(FRONTIER_OA)),
        ("works/doi:10.48550/arXiv.1905.07488", ok(APT_OA)),
        ("api.openalex.org/works?", ok({"results": [FRONTIER_OA, APT_OA]}, cost)),
        ("api.crossref.org/works?", ok({"message": {"items": [FRONTIER_CROSSREF, UNRELATED_CROSSREF]}})),
        ("export.arxiv.org", ok(ARXIV_FEED)),
    ]
