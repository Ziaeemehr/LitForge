from __future__ import annotations

import sqlite3
from dataclasses import replace
from typing import Any

import pytest

from litforge.analysis.gateway import ModelGateway
from litforge.config import Paths
from litforge.console import actions, planner, workflows
from litforge.console.actions import ActionContext
from litforge.console.planner import PlanValidationError
from litforge.projects import create_project


class FakeLlm:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"steps": [{"action": "filter_papers", "args": {"year_from": 2020}}]}
        self.messages: list[dict[str, str]] = []
        self.calls = 0

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        self.messages = messages
        self.schema = schema
        return self.response


def _project(conn: sqlite3.Connection, paths: Paths) -> str:
    project_id = create_project(conn, paths, "console-test")
    for paper_id, title, year in (("p1", "Chosen title", 2020), ("p2", "Other title", 2018)):
        conn.execute("INSERT INTO papers(id,title,year,created_at,updated_at) VALUES (?,?,?,?,?)",
                     (paper_id, title, year, "now", "now"))
        conn.execute("INSERT INTO project_papers VALUES (?,?,?,?)", (project_id, paper_id, "manual", "now"))
    return project_id


def _context(conn: sqlite3.Connection, config: Any, paths: Paths, project_id: str) -> ActionContext:
    return ActionContext(conn, ModelGateway(conn, config), project_id, "console-test", paths)


def test_registry_is_closed_typed_and_reports_cost_and_writes() -> None:
    assert set(actions.REGISTRY) == {
        "filter_papers", "search_library", "ask", "create_matrix", "add_column", "extract_matrix",
        "extract_claims", "cluster_claims", "relate_claims", "screen_fulltext", "export_matrix",
        "write_brief", "snapshot", "refresh",
    }
    for spec in actions.REGISTRY.values():
        assert spec.description
        assert spec.cost_kind in ("none", "local_model", "model", "network")
        assert isinstance(spec.writes, bool)
        assert issubclass(spec.args_model, actions.ActionArgs)
        assert callable(spec.executor)
    with pytest.raises(ValueError, match="unknown action"):
        actions.validate_action("shell", {})


def test_planner_sends_only_request_and_registry_schema(conn: sqlite3.Connection, paths: Paths,
                                                         config: Any) -> None:
    project_id = _project(conn, paths)
    fake = FakeLlm()
    gateway = ModelGateway(conn, config, providers={"local": fake})
    planned = planner.plan(conn, gateway, "Select newer papers", project_id)
    assert planned.steps[0].action == "filter_papers"
    sent = "\n".join(message["content"] for message in fake.messages)
    assert "Select newer papers" in sent
    assert "Chosen title" not in sent
    assert "Other title" not in sent
    assert set(fake.schema["properties"]) == {"steps"}
    assert set(fake.schema["x-action-descriptions"]) == set(actions.REGISTRY)
    assert fake.calls == 1


@pytest.mark.parametrize("raw,match", [
    ({"steps": [{"action": "arbitrary", "args": {}}]}, "unknown action"),
    ({"steps": [{"action": "filter_papers", "args": {"year_from": "later"}}]}, "int_parsing"),
    ({"steps": [{"action": "filter_papers", "args": {"year_from": 2020, "sql": "DELETE"}}]}, "extra_forbidden"),
])
def test_invalid_plans_are_rejected_with_validation_details(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(PlanValidationError) as caught:
        planner.validate_plan(raw, "request")
    assert match in str(caught.value)


@pytest.mark.parametrize("reference", ["$step_0", "$step_2", "$step_3"])
def test_forward_self_and_zero_step_references_are_rejected(reference: str) -> None:
    with pytest.raises(PlanValidationError, match="reference"):
        planner.validate_plan({"steps": [
            {"action": "filter_papers", "args": {}},
            {"action": "extract_claims", "args": {"paper_ids": reference}},
        ]}, "request")


def test_preview_dry_evaluates_filters_without_execution_or_model_calls(
    conn: sqlite3.Connection, paths: Paths, config: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _project(conn, paths)
    fake = FakeLlm()
    context = _context(conn, config, paths, project_id)
    context.gateway.providers["local"] = fake
    spec = actions.REGISTRY["filter_papers"]
    monkeypatch.setitem(actions.REGISTRY, "filter_papers",
                        replace(spec, executor=lambda *_: pytest.fail("preview invoked an executor")))
    plan_value = planner.validate_plan({"steps": [
        {"action": "filter_papers", "args": {"year_from": 2020}},
        {"action": "extract_claims", "args": {"paper_ids": "$step_1"}},
    ]}, "request")
    result = planner.preview_plan(plan_value, context, seconds_per_model_call=3)
    assert result.steps[0].papers_in_scope == 1
    assert result.steps[1].papers_in_scope == 1
    assert result.steps[0].model_call_count == 0
    assert result.steps[0].writes is False
    assert result.time_estimate_seconds == 0
    assert fake.calls == 0


def test_execution_requires_approval_and_resumes_at_failed_step(conn: sqlite3.Connection, paths: Paths,
                                                                 config: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _project(conn, paths)
    plan_value = planner.validate_plan({"steps": [
        {"action": "snapshot", "args": {"label": "first"}},
        {"action": "snapshot", "args": {"label": "second"}},
    ]}, "request")
    run_id = planner.create_run(conn, project_id, plan_value)
    with pytest.raises(PermissionError, match="explicit approval"):
        planner.execute_run(conn, ModelGateway(conn, config), paths, project_id, run_id, approved=False)

    spec = actions.REGISTRY["snapshot"]
    executions = 0

    def fail_second(context: ActionContext, args: actions.SnapshotArgs) -> dict[str, str]:
        nonlocal executions
        executions += 1
        if executions == 2:
            raise RuntimeError("synthetic failure")
        return {"snapshot_id": args.label}

    monkeypatch.setitem(actions.REGISTRY, "snapshot", replace(spec, executor=fail_second))
    run = planner.execute_run(conn, ModelGateway(conn, config), paths, project_id, run_id, approved=True)
    assert run.status == "failed"
    assert run.current_step == 2
    assert len(run.results) == 1

    monkeypatch.setitem(actions.REGISTRY, "snapshot",
                        replace(spec, executor=lambda _context, args: {"snapshot_id": args.label}))
    resumed = planner.execute_run(conn, ModelGateway(conn, config), paths, project_id, run_id, approved=True)
    assert resumed.status == "done"
    assert resumed.current_step == 3
    assert [item["step"] for item in resumed.results] == [1, 2]


def test_cancel_is_observed_between_steps_and_resumes_after_checkpoint(conn: sqlite3.Connection, paths: Paths,
                                                                       config: Any,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _project(conn, paths)
    plan_value = planner.validate_plan({"steps": [
        {"action": "snapshot", "args": {"label": "one"}},
        {"action": "snapshot", "args": {"label": "two"}},
    ]}, "request")
    run_id = planner.create_run(conn, project_id, plan_value)
    spec = actions.REGISTRY["snapshot"]
    seen: list[str] = []

    def cancel_after_first(context: ActionContext, args: actions.SnapshotArgs) -> dict[str, str]:
        seen.append(args.label)
        if len(seen) == 1:
            planner.request_cancel(context.conn, run_id)
        return {"snapshot_id": args.label}

    monkeypatch.setitem(actions.REGISTRY, "snapshot", replace(spec, executor=cancel_after_first))
    run = planner.execute_run(conn, ModelGateway(conn, config), paths, project_id, run_id, approved=True)
    assert run.status == "cancelled"
    assert run.current_step == 2
    assert seen == ["one"]

    monkeypatch.setitem(actions.REGISTRY, "snapshot",
                        replace(spec, executor=lambda _context, args: {"snapshot_id": args.label}))
    resumed = planner.execute_run(conn, ModelGateway(conn, config), paths, project_id, run_id, approved=True)
    assert resumed.status == "done"
    assert [item["step"] for item in resumed.results] == [1, 2]


def test_exports_reject_paths_and_workflows_use_typed_validated_plans(conn: sqlite3.Connection, paths: Paths,
                                                                        config: Any) -> None:
    project_id = _project(conn, paths)
    context = _context(conn, config, paths, project_id)
    with pytest.raises(ValueError, match="simple filename"):
        actions._export_path(context, "../outside.md", {".md"})
    with pytest.raises(ValueError, match="simple filename"):
        actions._export_path(context, r"..\outside.md", {".md"})

    assert {item["name"] for item in workflows.list_workflows()} == {
        "systematic_review", "scoping_review", "deep_dive",
    }
    value = workflows.instantiate_workflow("systematic_review", {"question": "What changed?", "year_from": 2020})
    assert value.steps[0].args["year_from"] == 2020
    assert value.steps[-1].args["matrix_id"] == "$step_3"
    with pytest.raises(ValueError, match="missing required"):
        workflows.instantiate_workflow("scoping_review", {})


def test_migration_17_creates_checkpoint_table(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 17
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(console_runs)")}
    assert {"plan_json", "current_step", "results_json", "cancel_requested"} <= columns


def test_failed_step_stores_a_sanitised_error(conn: sqlite3.Connection, paths: Paths, config: Any,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _project(conn, paths)
    run_id = planner.create_run(conn, project_id, planner.validate_plan(
        {"steps": [{"action": "snapshot", "args": {}}]}, "request"))
    spec = actions.REGISTRY["snapshot"]

    def leak(_context: ActionContext, _args: actions.SnapshotArgs) -> dict[str, str]:
        raise ValueError("model said secret-output-42")

    monkeypatch.setitem(actions.REGISTRY, "snapshot", replace(spec, executor=leak))
    run = planner.execute_run(conn, ModelGateway(conn, config), paths, project_id, run_id, approved=True)
    assert run.status == "failed" and run.error and "secret-output-42" not in run.error


def test_interrupted_step_is_cancelled_and_resumable(conn: sqlite3.Connection, paths: Paths, config: Any,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _project(conn, paths)
    run_id = planner.create_run(conn, project_id, planner.validate_plan(
        {"steps": [{"action": "snapshot", "args": {}}]}, "request"))
    spec = actions.REGISTRY["snapshot"]

    def interrupt(_context: ActionContext, _args: actions.SnapshotArgs) -> dict[str, str]:
        raise KeyboardInterrupt

    monkeypatch.setitem(actions.REGISTRY, "snapshot", replace(spec, executor=interrupt))
    with pytest.raises(KeyboardInterrupt):
        planner.execute_run(conn, ModelGateway(conn, config), paths, project_id, run_id, approved=True)
    assert planner.get_run(conn, run_id).status == "cancelled"
    monkeypatch.setitem(actions.REGISTRY, "snapshot", replace(spec, executor=lambda _c, _a: {"snapshot_id": "s"}))
    assert planner.execute_run(conn, ModelGateway(conn, config), paths, project_id, run_id,
                               approved=True).status == "done"


def test_actions_cannot_reach_objects_of_another_project(conn: sqlite3.Connection, paths: Paths, config: Any) -> None:
    from litforge.analysis.matrix import MatrixColumn, create_matrix
    from litforge.analysis.runs import start_run
    mine = _project(conn, paths)
    other = create_project(conn, paths, "other-project")
    foreign = create_matrix(conn, other, "theirs", "q", [MatrixColumn(key="x", label="X", instruction="x",
                                                                       type="text")])
    foreign_run = start_run(conn, "ask", other, {}, {})
    context = _context(conn, config, paths, mine)
    for name, args in (("extract_matrix", {"matrix_id": foreign.id}),
                       ("add_column", {"matrix_id": foreign.id, "column": {"key": "y", "label": "Y",
                                                                          "instruction": "y", "type": "text"}}),
                       ("export_matrix", {"matrix_id": foreign.id}),
                       ("write_brief", {"answer_run_id": foreign_run})):
        spec, parsed = actions.validate_action(name, args)
        with pytest.raises(ValueError, match="another project"):
            spec.executor(context, parsed)


def test_preview_resolves_step_references_like_execution(conn: sqlite3.Connection, paths: Paths, config: Any) -> None:
    project_id = _project(conn, paths)
    plan_value = planner.validate_plan({"steps": [
        {"action": "filter_papers", "args": {"year_from": 2019}},
        {"action": "ask", "args": {"question": "What was chosen?"}},
        {"action": "write_brief", "args": {"answer_run_id": "$step_2", "filename": "brief.md"}},
        {"action": "extract_claims", "args": {"paper_ids": "$step_1"}},
    ]}, "request")
    preview = planner.preview_plan(plan_value, _context(conn, config, paths, project_id))
    assert [s.action for s in preview.steps] == ["filter_papers", "ask", "write_brief", "extract_claims"]
    assert preview.steps[0].papers_in_scope == 1 and preview.steps[3].papers_in_scope == 1


def test_preview_never_reports_zero_cost_for_model_steps_it_cannot_estimate(conn: sqlite3.Connection,
                                                                             paths: Paths, config: Any) -> None:
    project_id = _project(conn, paths)
    plan_value = planner.validate_plan({"steps": [
        {"action": "create_matrix", "args": {"name": "m", "question": "q", "columns": [
            {"key": "x", "label": "X", "instruction": "x", "type": "text"}]}},
        {"action": "extract_matrix", "args": {"matrix_id": "$step_1"}},
    ]}, "request")
    preview = planner.preview_plan(plan_value, _context(conn, config, paths, project_id))
    assert preview.steps[1].estimate_known is False and preview.steps[1].max_cost_usd is None
    assert preview.max_cost_usd is None and preview.steps[0].estimate_known is True


def test_checkpoints_store_compact_outputs(conn: sqlite3.Connection, paths: Paths, config: Any,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _project(conn, paths)
    run_id = planner.create_run(conn, project_id, planner.validate_plan(
        {"steps": [{"action": "snapshot", "args": {}}]}, "request"))
    spec = actions.REGISTRY["snapshot"]
    big = {"run_id": "r1", "answer": {"sentences": [{"text": "x" * 1000}] * 500, "insufficient": False},
           "hits": [{"snippet": "y" * 500}] * 500}
    monkeypatch.setitem(actions.REGISTRY, "snapshot", replace(spec, executor=lambda _c, _a: big))
    run = planner.execute_run(conn, ModelGateway(conn, config), paths, project_id, run_id, approved=True)
    stored = conn.execute("SELECT results_json FROM console_runs WHERE id=?", (run_id,)).fetchone()[0]
    assert run.status == "done" and len(stored) < 2000
    assert run.results[0]["output"]["run_id"] == "r1" and run.results[0]["output"]["answer"]["sentences"] == 500


def test_ask_action_respects_selected_papers(conn: sqlite3.Connection, paths: Paths, config: Any,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    project_id = _project(conn, paths)
    seen: dict[str, Any] = {}

    def fake_ask(_conn: Any, _gateway: Any, question: str, project: str | None, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(actions.ask, "ask", fake_ask)
    spec, args = actions.validate_action("ask", {"question": "q", "paper_ids": ["p1"]})
    with pytest.raises(RuntimeError):
        spec.executor(_context(conn, config, paths, project_id), args)
    assert seen["paper_ids"] == ["p1"]
    plan_value = workflows.instantiate_workflow("systematic_review", {"question": "q"})
    assert next(step for step in plan_value.steps if step.action == "ask").args["paper_ids"] == "$step_1"
