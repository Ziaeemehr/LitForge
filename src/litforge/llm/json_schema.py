"""Shallow response checks; feature models remain responsible for full validation."""

from __future__ import annotations

from typing import Any


def check_json_structure(value: dict[str, Any], schema: dict[str, Any]) -> None:
    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        ref = node.get("$ref", "")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/").replace("~1", "/").replace("~0", "~")
            target = schema.get("$defs", {}).get(name)
            if isinstance(target, dict):
                return {**target, **node}
        return node

    def matches(item: Any, node: dict[str, Any]) -> bool:
        node = resolve(node)
        if "anyOf" in node:
            return any(matches(item, branch) for branch in node["anyOf"])
        kind = node.get("type")
        if isinstance(kind, list):
            return any(matches(item, {"type": entry}) for entry in kind)
        types = {
            "object": isinstance(item, dict),
            "array": isinstance(item, list),
            "string": isinstance(item, str),
            "boolean": isinstance(item, bool),
            "null": item is None,
            "number": isinstance(item, int | float) and not isinstance(item, bool),
            "integer": (isinstance(item, int) and not isinstance(item, bool))
                       or (isinstance(item, float) and item.is_integer()),
        }
        return types.get(kind, True) if isinstance(kind, str) else True

    root = resolve(schema)
    if not matches(value, root) or any(key not in value for key in root.get("required", [])):
        raise ValueError("response does not match top-level schema")
    for key, node in root.get("properties", {}).items():
        if key in value and not matches(value[key], node):
            raise ValueError("response field has the wrong JSON type")
