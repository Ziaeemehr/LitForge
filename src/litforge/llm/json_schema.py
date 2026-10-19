"""Shallow response checks; feature models remain responsible for full validation."""

from __future__ import annotations

import json
import re
from typing import Any

# A backslash JSON does not allow (\\c, \\s, \\l ... as in a LaTeX \\cite written without doubling) or a \\u
# that is not followed by four hex digits.
_BAD_ESCAPE = re.compile(r'(?<!\\)((?:\\\\)*)\\(?![\\"/bfnrt]|u[0-9a-fA-F]{4})')


def loads_model_json(content: str) -> Any:
    """json.loads, and when that fails, once more with stray backslashes doubled: models writing LaTeX often
    send \\cite{a} where JSON needs \\\\cite{a}. Valid JSON is never changed."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        repaired = _BAD_ESCAPE.sub(lambda m: m.group(1) + "\\\\", content)
        if repaired == content:
            raise
        return json.loads(repaired)


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
