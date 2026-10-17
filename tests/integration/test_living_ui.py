from __future__ import annotations

from typing import Any

from discovery_fakes import FakeTransport, default_routes
from lens_fakes import evidence_id, seed
from test_ui_lens import LensServer

from litforge.analysis.gateway import ModelGateway
from litforge.config import Config, Paths
from litforge.discovery.http import HttpClient
from litforge.screening.criteria import Criteria, save_criteria
from litforge.screening.llm import FakeLlm
from litforge.ui.server import App


def test_snapshots_refresh_background_and_repro_endpoints(paths: Paths) -> None:
    config = Config()
    fake = FakeLlm(lambda m: {"status": "reported", "value": "Results",
                              "evidence_ids": [evidence_id(m)]})
    app = App(paths, config, make_http=lambda: HttpClient(transport=FakeTransport(default_routes())),
              make_gateway=lambda c: ModelGateway(c, config, providers={"local": fake}))
    conn = app.db()
    seed(conn, text="Results: code availability at https://github.com/lab/code.")
    save_criteria(conn, "proj", Criteria(question="Results"))
    conn.close()
    server = LensServer(app, fake, "", "")
    status, panel = server.call("/api/snapshots?project=demo")
    assert status == 200 and panel["snapshots"] == []
    status, snap = server.call("/api/snapshots", {"project": "demo", "label": "<img src=x>"})
    assert status == 200 and snap["id"]
    job = server.job("/api/refresh", {"project": "demo", "screen": False, "extract": False, "claims": False})
    assert job["status"] == "done" and job["report"]["status"] == "ok"
    status, panel = server.call("/api/snapshots?project=demo")
    assert status == 200 and len(panel["snapshots"]) == 2
    assert panel["latest"]["signals"] == [] and "No significant signals" in panel["report"]
    status, card = server.call("/api/papers/p/repro?project=demo")
    assert status == 200 and len(card["checklist"]) == 12 and card["links"][0]["kind"] == "code"
    assert card["sections"] and card["evidence"] and "brief" not in card
    assert fake.calls == 0
    status, matrix = server.call("/api/repro/matrix", {"project": "demo"})
    assert status == 200 and len(matrix["columns"]) == 12
    job = server.job("/api/matrices/" + matrix["id"] + "/extract", {"project": "demo", "paper_ids": ["p"]})
    assert job["status"] == "done" and fake.calls > 0
    assert server.call("/api/papers/p/repro?project=absent")[0] in (400, 404)
    assert server.call("/api/refresh", {"project": "demo", "screen": "invalid"})[0] == 400
    assert server.call("/api/refresh", {"project": "demo"}, origin="https://elsewhere.invalid")[0] == 403


def test_refresh_public_errors_are_sanitized(paths: Paths) -> None:
    config = Config()
    def bad(m: list[dict[str, str]]) -> dict[str, Any]:
        raise RuntimeError("SECRET_IN_PROVIDER")
    fake = FakeLlm(bad)
    app = App(paths, config, make_http=lambda: HttpClient(transport=FakeTransport(default_routes())),
              make_gateway=lambda c: ModelGateway(c, config, providers={"local": fake}))
    conn = app.db()
    seed(conn)
    save_criteria(conn, "proj", Criteria(question="gain"))
    from litforge.analysis.snapshots import take_snapshot
    take_snapshot(conn, "proj")
    seed(conn, "q")
    conn.close()
    server = LensServer(app, fake, "", "")
    job = server.job("/api/refresh", {"project": "demo"})
    assert job["status"] == "failed" and "SECRET" not in str(job)


def test_living_panel_counts_are_from_saved_snapshot(paths: Paths) -> None:
    from litforge.analysis.snapshots import take_snapshot
    from litforge.ui.api import living_panel
    app = App(paths, Config())
    conn = app.db()
    seed(conn)
    take_snapshot(conn, "proj")
    seed(conn, "q")
    panel = living_panel(conn, "proj")
    assert panel["snapshots"][0]["counts"] == {"papers": 1, "matrix": 0, "claims": 0, "answers": 0}
    conn.close()
