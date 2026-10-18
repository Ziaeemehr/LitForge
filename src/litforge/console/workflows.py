"""Typed YAML workflow templates that share the planner and execution path."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from litforge.console.planner import Plan, PlanStep, validate_plan

WORKFLOW_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class WorkflowParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    required: bool = False
    default: Any = None


class WorkflowTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    parameters: dict[str, WorkflowParameter]
    steps: list[PlanStep]


def _workflow_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "configs" / "workflows"


def list_workflows() -> list[dict[str, str]]:
    result = []
    for file in sorted(_workflow_dir().glob("*.yaml")):
        template = WorkflowTemplate.model_validate(yaml.safe_load(file.read_text(encoding="utf-8")))
        result.append({"name": template.name, "description": template.description})
    return result


def load_workflow(name: str) -> WorkflowTemplate:
    if not WORKFLOW_NAME.fullmatch(name):
        raise ValueError("invalid workflow name")
    path = _workflow_dir() / f"{name}.yaml"
    if not path.is_file():
        raise KeyError(f"workflow not found: {name}")
    template = WorkflowTemplate.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if template.name != name:
        raise ValueError("workflow name does not match its filename")
    return template


def _coerce(value: Any, parameter: WorkflowParameter, name: str) -> Any:
    types: dict[str, Any] = {"string": str, "integer": int, "number": float, "boolean": bool}
    if parameter.type not in types:
        raise ValueError(f"unsupported workflow parameter type for {name}: {parameter.type}")
    try:
        return TypeAdapter(types[parameter.type]).validate_python(value)
    except ValidationError as exc:
        raise ValueError(f"invalid value for workflow parameter {name}: {exc}") from exc


def _substitute(value: Any, params: dict[str, Any]) -> Any:
    if isinstance(value, str):
        match = re.fullmatch(r"\{\{([a-z][a-z0-9_]*)\}\}", value)
        if match:
            return params[match.group(1)]
        return value
    if isinstance(value, list):
        return [_substitute(item, params) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, params) for key, item in value.items()}
    return value


def instantiate_workflow(name: str, supplied: dict[str, Any]) -> Plan:
    template = load_workflow(name)
    unknown = sorted(set(supplied) - template.parameters.keys())
    if unknown:
        raise ValueError(f"unknown workflow parameter(s): {', '.join(unknown)}; {name} takes: "
                         f"{', '.join(template.parameters) or 'no parameters'}")
    params = {}
    for key, definition in template.parameters.items():
        value = supplied.get(key, definition.default)
        if value is None and definition.required:
            raise ValueError(f"missing required workflow parameter: {key}")
        params[key] = _coerce(value, definition, key) if value is not None else None
    step_payload = [{"action": step.action, "args": _substitute(step.args, params)} for step in template.steps]
    question = str(params.get("question") or params.get("query") or template.description)
    return validate_plan({"steps": step_payload}, f"Workflow {name}: {question}")
