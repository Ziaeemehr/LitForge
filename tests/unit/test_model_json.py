from __future__ import annotations

import json

import pytest

from litforge.llm.json_schema import loads_model_json


def test_valid_json_is_read_unchanged() -> None:
    text = json.dumps({"replacement": "\\cite{a} \\textbf{b}\n", "n": 1})
    assert loads_model_json(text) == json.loads(text)


def test_single_backslash_latex_is_repaired() -> None:
    # Seen with gemma4 in the real run: \cite and \dot sent without doubling made the whole reply invalid.
    value = loads_model_json(r'{"replacement": "x \cite{k} and $\dot{x}$"}')
    assert value == {"replacement": "x \\cite{k} and $\\dot{x}$"}


def test_valid_escapes_keep_their_meaning_during_repair() -> None:
    value = loads_model_json(r'{"a": "\\ref{y} \"q\" \u00e9 \cite{k}"}')
    assert value == {"a": "\\ref{y} \"q\" é \\cite{k}"}


def test_other_errors_still_raise() -> None:
    with pytest.raises(json.JSONDecodeError):
        loads_model_json('{"a": ')
