# Title/Abstract Screening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Screen the top N papers of a discovery run against the project's research question and
inclusion/exclusion criteria with a local Ollama model, and show/sort/export the result.

**Architecture:** New package `src/litforge/screening/` (criteria, prompt, llm client, pure decision
rules, run orchestration). Results live in a new `screening_results` table keyed by (project, paper).
The web UI gets JSON endpoints and a screening panel on the run view; CLI gets `litforge screen`;
Markdown export adds screening fields.

**Tech Stack:** Python 3.12 stdlib (`urllib`, `http.server`), pydantic v2, SQLite, typer, vanilla JS.
Tests: pytest. Lint: ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-10-14-screening-design.md`

## Global Constraints

- No new runtime dependencies; the Ollama client uses `urllib` only.
- Default model `qwen3:4b`; Ollama URL `http://localhost:11434`, overridden by env `LITFORGE_OLLAMA_URL`.
- Ollama request: `stream: false`, `think: false`, `format: <JSON schema>`, `options.temperature: 0`.
- Decision and score are computed in Python, never taken from model text.
- A missing abstract can never produce `exclude`.
- Screening runs only on explicit request (UI button or CLI), never after a search.
- The user's ✓/✗ marks are separate and always win; screening never writes `paper_marks`.
- Tests never need a GPU or a running Ollama, except the opt-in live test.
- All checks must pass: `uv run pytest`, `uv run ruff check src tests`, `uv run mypy`.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Criteria edited after a screening: the old results must show as stale and be re-screened on the next run (Task 3, Task 5 tests).
2. Ollama not running or the model not pulled: the job fails before touching any row, naming `ollama pull <model>` (Task 2 test, Task 3 test).
3. Model output with the wrong number of criterion entries, unknown status strings or out-of-range relevance: padded/clamped, never a crash (Task 1 test).
4. Paper without an abstract: screened on title, never excluded (Task 1 test).
5. `top` larger than the run, or `top < 1`: screens the whole run / rejects with a clear error (Task 3 test).

## File Structure

| File | Responsibility |
|---|---|
| Create `src/litforge/screening/__init__.py` | package marker, docstring |
| Create `src/litforge/screening/criteria.py` | `Criteria` model, load/save on `projects`, `criteria_hash` |
| Create `src/litforge/screening/decide.py` | pure decision rules, score, evidence check |
| Create `src/litforge/screening/prompt.py` | prompt version, messages, JSON schema, fingerprint |
| Create `src/litforge/screening/llm.py` | `OllamaClient`, `FakeLlm`, `LlmError`, `ollama_from_config` |
| Create `src/litforge/screening/run.py` | `screen_run` orchestration |
| Create `src/litforge/storage/migrations/0006_screening.sql` | `screening_results` table |
| Modify `src/litforge/config.py`, `configs/default.yaml` | `ScreeningConfig` |
| Modify `src/litforge/cli.py` | `screen` command |
| Modify `src/litforge/ui/api.py`, `src/litforge/ui/server.py` | criteria + screen endpoints, screening fields |
| Modify `src/litforge/ui/static/{index.html,app.js,app.css}` | screening panel, badge, sort, filter, drawer |
| Modify `src/litforge/export/markdown.py` | screening front matter and README column |
| Modify `README.md` | document screening |
| Tests | `tests/unit/test_screening.py`, `tests/integration/test_screening.py`, additions to `tests/integration/test_ui_server.py`, `tests/integration/test_pipeline.py` |

---

### Task 1: Criteria model and pure decision rules

**Files:**
- Create: `src/litforge/screening/__init__.py`, `src/litforge/screening/criteria.py`, `src/litforge/screening/decide.py`
- Test: `tests/unit/test_screening.py`

**Interfaces:**
- Produces:
  - `Criteria(BaseModel)`: `question: str`, `include: list[str]`, `exclude: list[str]`; `Criteria.from_text(question, include_text, exclude_text)`; `is_empty() -> bool`
  - `criteria_hash(c: Criteria) -> str`
  - `load_criteria(conn, project_id) -> Criteria`, `save_criteria(conn, project_id, c) -> None` (caller owns the transaction)
  - `CriterionResult(BaseModel)`: `criterion: str`, `status: str`, `evidence: str`, `verified: bool | None`
  - `Decision(BaseModel)`: `decision: str`, `score: float`, `relevance: int`, `inclusion: list[CriterionResult]`, `exclusion: list[CriterionResult]`, `reason: str`, `error: str | None = None`
  - `decide(raw: dict[str, Any], c: Criteria, title: str | None, abstract: str | None) -> Decision`
  - `failed_decision(c: Criteria, error: str) -> Decision`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_screening.py
"""Screening decision rules (pure functions, no model)."""

from __future__ import annotations

import pytest

from litforge.screening.criteria import Criteria, criteria_hash
from litforge.screening.decide import decide, failed_decision

ABSTRACT = "We fit a Jansen-Rit neural mass model to EEG recordings with simulation-based inference."
C = Criteria(question="Inference for neural mass models", include=["uses a neural mass model", "uses EEG or MEG"],
             exclude=["animal study only"])


def raw(relevance: int = 4, inc: tuple[str, ...] = ("met", "met"), exc: tuple[str, ...] = ("not_violated",),
        ev: str = "Jansen-Rit neural mass model") -> dict:
    return {"relevance": relevance,
            "inclusion": [{"status": s, "evidence": ev} for s in inc],
            "exclusion": [{"status": s, "evidence": ""} for s in exc],
            "reason": "fits"}


def test_include_when_all_met_and_relevant() -> None:
    d = decide(raw(), C, "Title", ABSTRACT)
    assert d.decision == "include"
    assert d.score == pytest.approx(0.9)


def test_exclude_when_any_exclusion_violated() -> None:
    assert decide(raw(exc=("violated",)), C, "Title", ABSTRACT).decision == "exclude"


def test_uncertain_when_inclusion_not_met_or_low_relevance() -> None:
    assert decide(raw(inc=("met", "not_met")), C, "T", ABSTRACT).decision == "uncertain"
    assert decide(raw(relevance=2), C, "T", ABSTRACT).decision == "uncertain"


def test_unclear_counts_half_in_score() -> None:
    d = decide(raw(relevance=5, inc=("met", "unclear")), C, "T", ABSTRACT)
    assert d.decision == "include" and d.score == pytest.approx(0.875)


def test_missing_abstract_is_never_excluded() -> None:
    assert decide(raw(exc=("violated",)), C, "Title", None).decision == "uncertain"
    assert decide(raw(exc=("violated",)), C, "Title", "  ").decision == "uncertain"


def test_wrong_counts_unknown_status_and_bad_relevance_are_normalized() -> None:
    d = decide({"relevance": 9, "inclusion": [{"status": "yes"}], "exclusion": [], "reason": 3}, C, "T", ABSTRACT)
    assert d.relevance == 5
    assert [r.status for r in d.inclusion] == ["unclear", "unclear"]
    assert [r.status for r in d.exclusion] == ["unclear"]
    assert [r.criterion for r in d.inclusion] == C.include
    assert d.reason == "3"
    d = decide({"relevance": "high"}, C, "T", ABSTRACT)
    assert d.relevance == 0 and d.decision == "uncertain"


def test_evidence_verified_against_title_and_abstract() -> None:
    d = decide(raw(ev="neural   mass MODEL to EEG"), C, "T", ABSTRACT)
    assert d.inclusion[0].verified is True
    d = decide(raw(ev="fMRI connectivity"), C, "T", ABSTRACT)
    assert d.inclusion[0].verified is False
    assert d.exclusion[0].verified is None  # empty evidence is not checked


def test_no_inclusion_criteria_scores_relevance_only() -> None:
    c = Criteria(question="q")
    d = decide({"relevance": 3, "inclusion": [], "exclusion": [], "reason": ""}, c, "T", ABSTRACT)
    assert d.decision == "include" and d.score == 3 / 5


def test_failed_decision_is_uncertain_with_error() -> None:
    d = failed_decision(C, "bad json")
    assert d.decision == "uncertain" and d.score == 0 and d.error == "bad json"
    assert len(d.inclusion) == 2 and len(d.exclusion) == 1


def test_criteria_from_text_and_hash() -> None:
    c = Criteria.from_text(" q ", "a\n\n b \n", "")
    assert c == Criteria(question="q", include=["a", "b"], exclude=[])
    assert not c.is_empty() and Criteria(question=" ").is_empty()
    assert criteria_hash(c) == criteria_hash(Criteria.from_text("q", "a\nb", ""))
    assert criteria_hash(c) != criteria_hash(Criteria.from_text("q", "a", ""))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_screening.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'litforge.screening'`

- [ ] **Step 3: Implement**

```python
# src/litforge/screening/__init__.py
"""Title/abstract screening with a local LLM (Ollama): criteria, prompt, decision rules, runs."""
```

```python
# src/litforge/screening/criteria.py
"""A project's screening criteria, stored in projects.research_question and projects.criteria_json."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from pydantic import BaseModel, Field


class Criteria(BaseModel):
    question: str
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)

    @classmethod
    def from_text(cls, question: str, include_text: str, exclude_text: str) -> Criteria:
        """Build from form fields: one criterion per non-empty line."""
        def lines(text: str) -> list[str]:
            return [ln.strip() for ln in text.splitlines() if ln.strip()]
        return cls(question=question.strip(), include=lines(include_text), exclude=lines(exclude_text))

    def is_empty(self) -> bool:
        return not self.question.strip()


def criteria_hash(c: Criteria) -> str:
    return hashlib.sha256(json.dumps(c.model_dump(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def load_criteria(conn: sqlite3.Connection, project_id: str) -> Criteria:
    row = conn.execute("SELECT research_question, criteria_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise LookupError("project not found")
    data = json.loads(row["criteria_json"] or "{}")
    return Criteria(question=row["research_question"] or "", include=list(data.get("include", [])),
                    exclude=list(data.get("exclude", [])))


def save_criteria(conn: sqlite3.Connection, project_id: str, c: Criteria) -> None:
    conn.execute("UPDATE projects SET research_question=?, criteria_json=? WHERE id=?",
                 (c.question, json.dumps({"include": c.include, "exclude": c.exclude}, ensure_ascii=False),
                  project_id))
```

```python
# src/litforge/screening/decide.py
"""Deterministic screening decision from the model's structured answer.

- exclude   if any exclusion criterion is `violated` (never when the abstract is missing);
- include   if no inclusion criterion is `not_met` and relevance >= 3;
- uncertain otherwise.
score = 0.5 * relevance/5 + 0.5 * inclusion fraction (met 1, unclear 0.5, not_met 0); relevance/5 alone
when there are no inclusion criteria. Evidence quotes are checked against title + abstract.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from litforge.screening.criteria import Criteria

INCLUSION = ("met", "not_met", "unclear")
EXCLUSION = ("violated", "not_violated", "unclear")
INCLUDE_MIN_RELEVANCE = 3
_MET_VALUE = {"met": 1.0, "unclear": 0.5, "not_met": 0.0}


class CriterionResult(BaseModel):
    criterion: str
    status: str
    evidence: str = ""
    verified: bool | None = None  # None: no evidence given


class Decision(BaseModel):
    decision: str  # include | uncertain | exclude
    score: float
    relevance: int
    inclusion: list[CriterionResult]
    exclusion: list[CriterionResult]
    reason: str
    error: str | None = None


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _items(raw: Any, criteria: list[str], allowed: tuple[str, ...], source: str) -> list[CriterionResult]:
    entries = raw if isinstance(raw, list) else []
    out = []
    for i, criterion in enumerate(criteria):
        entry = entries[i] if i < len(entries) and isinstance(entries[i], dict) else {}
        status = entry.get("status")
        evidence = str(entry.get("evidence") or "").strip()
        out.append(CriterionResult(
            criterion=criterion, status=status if status in allowed else "unclear", evidence=evidence,
            verified=(_norm(evidence) in source) if _norm(evidence) else None))
    return out


def decide(raw: dict[str, Any], c: Criteria, title: str | None, abstract: str | None) -> Decision:
    source = _norm(f"{title or ''} {abstract or ''}")
    try:
        relevance = max(0, min(5, int(raw.get("relevance", 0))))
    except (TypeError, ValueError):
        relevance = 0
    inclusion = _items(raw.get("inclusion"), c.include, INCLUSION, source)
    exclusion = _items(raw.get("exclusion"), c.exclude, EXCLUSION, source)
    has_abstract = bool((abstract or "").strip())
    if has_abstract and any(r.status == "violated" for r in exclusion):
        decision = "exclude"
    elif all(r.status != "not_met" for r in inclusion) and relevance >= INCLUDE_MIN_RELEVANCE:
        decision = "include"
    else:
        decision = "uncertain"
    score = relevance / 5
    if inclusion:
        score = 0.5 * score + 0.5 * sum(_MET_VALUE[r.status] for r in inclusion) / len(inclusion)
    return Decision(decision=decision, score=round(score, 4), relevance=relevance, inclusion=inclusion,
                    exclusion=exclusion, reason=str(raw.get("reason") or "").strip())


def failed_decision(c: Criteria, error: str) -> Decision:
    """Recorded when the model gave no usable answer: uncertain, so a person looks at it."""
    return Decision(decision="uncertain", score=0.0, relevance=0,
                    inclusion=[CriterionResult(criterion=x, status="unclear") for x in c.include],
                    exclusion=[CriterionResult(criterion=x, status="unclear") for x in c.exclude],
                    reason="", error=error)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_screening.py -q && uv run ruff check src tests && uv run mypy`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/litforge/screening tests/unit/test_screening.py
git commit -m "Add screening criteria and deterministic decision rules

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Prompt, JSON schema and Ollama client

**Files:**
- Create: `src/litforge/screening/prompt.py`, `src/litforge/screening/llm.py`
- Modify: `src/litforge/config.py` (add `ScreeningConfig`), `configs/default.yaml`
- Test: `tests/unit/test_screening.py` (append), `tests/integration/test_screening.py` (new, Ollama fake server)

**Interfaces:**
- Consumes: `Criteria` (Task 1)
- Produces:
  - `PROMPT_VERSION: str = "screen-v1"`
  - `response_schema(c: Criteria) -> dict[str, Any]`
  - `build_messages(c: Criteria, title: str | None, abstract: str | None, year: int | None, venue: str | None) -> list[dict[str, str]]`
  - `fingerprint(c: Criteria, model: str, title: str | None, abstract: str | None) -> str`
  - `class LlmError(RuntimeError)`
  - `class Llm(Protocol)`: `name: str`; `check() -> None`; `chat_json(messages, schema) -> dict[str, Any]`
  - `OllamaClient(url: str, model: str, timeout_s: float = 120)` implementing `Llm`
  - `FakeLlm(answer: Callable[[list[dict[str, str]]], dict[str, Any] | str], name: str = "fake")`, attribute `calls: int`
  - `ollama_from_config(cfg: ScreeningConfig, model: str | None = None) -> OllamaClient`
  - `ScreeningConfig`: `model: str = "qwen3:4b"`, `ollama_url: str = "http://localhost:11434"`, `default_top: int = 100`, `timeout_s: float = 120`; `Config.screening`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_screening.py`:

```python
from litforge.screening.prompt import PROMPT_VERSION, build_messages, fingerprint, response_schema


def test_schema_has_one_entry_per_criterion() -> None:
    s = response_schema(C)
    assert s["properties"]["inclusion"]["minItems"] == 2 == s["properties"]["inclusion"]["maxItems"]
    assert s["properties"]["exclusion"]["maxItems"] == 1
    assert set(s["required"]) == {"relevance", "inclusion", "exclusion", "reason"}


def test_messages_number_criteria_and_mark_missing_abstract() -> None:
    msgs = build_messages(C, "A title", None, 2020, "J Neuro")
    user = msgs[-1]["content"]
    assert "I1. uses a neural mass model" in user and "E1. animal study only" in user
    assert "(no abstract available)" in user and "A title" in user and "2020" in user


def test_fingerprint_changes_with_model_criteria_and_text() -> None:
    base = fingerprint(C, "qwen3:4b", "t", "a")
    assert base == fingerprint(C, "qwen3:4b", "t", "a")
    assert base != fingerprint(C, "qwen3:8b", "t", "a")
    assert base != fingerprint(C, "qwen3:4b", "t", "a2")
    assert base != fingerprint(Criteria(question="other"), "qwen3:4b", "t", "a")
    assert PROMPT_VERSION
```

Create `tests/integration/test_screening.py`:

```python
"""Screening against a fake Ollama HTTP server and a fake LLM (no GPU needed)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from litforge.screening.criteria import Criteria
from litforge.screening.llm import LlmError, OllamaClient
from litforge.screening.prompt import build_messages, response_schema


class _Ollama(BaseHTTPRequestHandler):
    models: list[str] = ["qwen3:4b"]
    reply: str = '{"relevance": 4, "inclusion": [], "exclusion": [], "reason": "ok"}'
    seen: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _json(self, data: Any) -> None:
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._json({"models": [{"name": m} for m in self.models]})

    def do_POST(self) -> None:  # noqa: N802
        _Ollama.seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self._json({"message": {"role": "assistant", "content": self.reply}})


@pytest.fixture()
def ollama() -> Iterator[str]:
    _Ollama.models, _Ollama.seen = ["qwen3:4b"], []
    _Ollama.reply = '{"relevance": 4, "inclusion": [], "exclusion": [], "reason": "ok"}'
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Ollama)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_ollama_client_sends_schema_and_parses_json(ollama: str) -> None:
    c = Criteria(question="q")
    llm = OllamaClient(ollama, "qwen3:4b")
    llm.check()
    out = llm.chat_json(build_messages(c, "t", "a", None, None), response_schema(c))
    assert out["relevance"] == 4
    sent = _Ollama.seen[0]
    assert sent["model"] == "qwen3:4b" and sent["stream"] is False and sent["think"] is False
    assert sent["format"]["type"] == "object" and sent["options"]["temperature"] == 0


def test_ollama_client_accepts_latest_tag(ollama: str) -> None:
    _Ollama.models = ["qwen3:latest"]
    OllamaClient(ollama, "qwen3").check()


def test_ollama_missing_model_names_pull_command(ollama: str) -> None:
    _Ollama.models = ["llama3:8b"]
    with pytest.raises(LlmError, match="ollama pull qwen3:4b"):
        OllamaClient(ollama, "qwen3:4b").check()


def test_ollama_unreachable_is_llm_error() -> None:
    with pytest.raises(LlmError, match="not reachable"):
        OllamaClient("http://127.0.0.1:9", "qwen3:4b", timeout_s=1).check()


def test_ollama_invalid_json_is_llm_error(ollama: str) -> None:
    _Ollama.reply = "not json"
    with pytest.raises(LlmError, match="invalid JSON"):
        OllamaClient(ollama, "qwen3:4b").chat_json([{"role": "user", "content": "x"}], {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_screening.py tests/integration/test_screening.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'litforge.screening.prompt'`

- [ ] **Step 3: Implement**

```python
# src/litforge/screening/prompt.py
"""Prompt and response schema for title/abstract screening. Bump PROMPT_VERSION on any wording change:
it is part of every result's fingerprint, so changed prompts re-screen papers."""

from __future__ import annotations

import hashlib
from typing import Any

from litforge.screening.criteria import Criteria
from litforge.screening.decide import EXCLUSION, INCLUSION

PROMPT_VERSION = "screen-v1"

SYSTEM = (
    "You screen scientific papers for a literature review using only the title and abstract given. "
    "For each inclusion criterion answer met, not_met or unclear; for each exclusion criterion answer "
    "violated, not_violated or unclear. Answer not_met or violated only when the text states it; when the "
    "text does not say, answer unclear. Evidence must be a short exact quote from the title or abstract, "
    "or empty. relevance is 0 (unrelated) to 5 (directly answers the research question). "
    "reason is one or two sentences. Reply with JSON only."
)


def response_schema(c: Criteria) -> dict[str, Any]:
    def items(statuses: tuple[str, ...], n: int) -> dict[str, Any]:
        return {"type": "array", "minItems": n, "maxItems": n, "items": {
            "type": "object", "required": ["status", "evidence"],
            "properties": {"status": {"type": "string", "enum": list(statuses)}, "evidence": {"type": "string"}}}}
    return {"type": "object", "required": ["relevance", "inclusion", "exclusion", "reason"], "properties": {
        "relevance": {"type": "integer", "minimum": 0, "maximum": 5},
        "inclusion": items(INCLUSION, len(c.include)),
        "exclusion": items(EXCLUSION, len(c.exclude)),
        "reason": {"type": "string"}}}


def build_messages(c: Criteria, title: str | None, abstract: str | None, year: int | None,
                   venue: str | None) -> list[dict[str, str]]:
    inc = "\n".join(f"I{i}. {x}" for i, x in enumerate(c.include, 1)) or "(none)"
    exc = "\n".join(f"E{i}. {x}" for i, x in enumerate(c.exclude, 1)) or "(none)"
    meta = " · ".join(str(x) for x in (year, venue) if x)
    user = (f"Research question: {c.question}\n\nInclusion criteria:\n{inc}\n\nExclusion criteria:\n{exc}\n\n"
            f"Paper title: {title or '(no title)'}\n" + (f"Published: {meta}\n" if meta else "") +
            f"Abstract: {(abstract or '').strip() or '(no abstract available)'}")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def fingerprint(c: Criteria, model: str, title: str | None, abstract: str | None) -> str:
    parts = [PROMPT_VERSION, model, c.model_dump_json(), title or "", abstract or ""]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
```

```python
# src/litforge/screening/llm.py
"""Local LLM access for screening: Ollama over HTTP (stdlib only) and a fake for tests."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Protocol

from litforge.config import ScreeningConfig


class LlmError(RuntimeError):
    pass


class Llm(Protocol):
    name: str

    def check(self) -> None: ...

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]: ...


class OllamaClient:
    def __init__(self, url: str, model: str, timeout_s: float = 120) -> None:
        self.url, self.name, self.timeout_s = url.rstrip("/"), model, timeout_s

    def _call(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, headers={"Content-Type": "application/json"},
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 - local URL
                return json.load(resp)  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            raise LlmError(f"Ollama error {exc.code}: {exc.read().decode(errors='replace')[:200]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise LlmError(f"Ollama not reachable at {self.url} ({exc}); start it with `ollama serve`") from exc

    def check(self) -> None:
        names = {m.get("name") for m in self._call("/api/tags").get("models", [])}
        if self.name not in names and f"{self.name}:latest" not in names:
            raise LlmError(f"model {self.name} is not pulled; run `ollama pull {self.name}`")

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
        reply = self._call("/api/chat", {"model": self.name, "messages": messages, "stream": False,
                                         "think": False, "format": schema, "options": {"temperature": 0}})
        content = (reply.get("message") or {}).get("content", "")
        try:
            out = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmError(f"invalid JSON from model: {content[:120]!r}") from exc
        if not isinstance(out, dict):
            raise LlmError("model reply is not a JSON object")
        return out


class FakeLlm:
    """Answers with a function of the messages; a str answer is parsed like a model reply."""

    def __init__(self, answer: Callable[[list[dict[str, str]]], dict[str, Any] | str], name: str = "fake") -> None:
        self.answer, self.name, self.calls = answer, name, 0

    def check(self) -> None:
        pass

    def chat_json(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        out = self.answer(messages)
        if isinstance(out, str):
            try:
                out = json.loads(out)
            except json.JSONDecodeError as exc:
                raise LlmError("invalid JSON from model") from exc
        return out  # type: ignore[return-value]


def ollama_from_config(cfg: ScreeningConfig, model: str | None = None) -> OllamaClient:
    url = os.environ.get("LITFORGE_OLLAMA_URL") or cfg.ollama_url
    return OllamaClient(url, model or cfg.model, cfg.timeout_s)
```

In `src/litforge/config.py`, after `SimilarConfig`:

```python
class ScreeningConfig(BaseModel):
    model: str = "qwen3:4b"  # any Ollama model; qwen3:8b is slower but more careful
    ollama_url: str = "http://localhost:11434"  # LITFORGE_OLLAMA_URL overrides
    default_top: int = 100  # papers of a run screened when no count is given
    timeout_s: float = 120
```

and in `Config` add `screening: ScreeningConfig = Field(default_factory=ScreeningConfig)`.

Append to `configs/default.yaml` (before `privacy:`):

```yaml
screening:
  # Local model (Ollama) that reads title + abstract and checks the project's criteria.
  # qwen3:4b is fast; qwen3:8b is slower but more careful. Pull with `ollama pull <model>`.
  model: qwen3:4b
  ollama_url: http://localhost:11434   # LITFORGE_OLLAMA_URL overrides
  default_top: 100                     # papers of a run screened when no count is given
  timeout_s: 120
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_screening.py tests/integration/test_screening.py -q && uv run ruff check src tests && uv run mypy`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/litforge/screening src/litforge/config.py configs/default.yaml tests
git commit -m "Add screening prompt, JSON schema and Ollama client

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Migration and `screen_run`

**Files:**
- Create: `src/litforge/storage/migrations/0006_screening.sql`, `src/litforge/screening/run.py`
- Test: `tests/integration/test_screening.py` (append)

**Interfaces:**
- Consumes: Tasks 1–2
- Produces:
  - `ScreenReport(BaseModel)`: `run_id: str`, `project_id: str`, `model: str`, `candidates: int`, `screened: int`, `cached: int`, `failed: int`, `counts: dict[str, int]`
  - `screen_run(conn, llm: Llm, run_id: str, top: int, progress: Callable[[str], None] | None = None, log: EventLog | None = None) -> ScreenReport`
  - Raises `ValueError` for no criteria / `top < 1`, `LookupError` for unknown run, `LlmError` from `llm.check()`
  - Table `screening_results(project_id, paper_id, decision, score, result_json, error, model, prompt_version, criteria_hash, fingerprint, screened_at)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_screening.py`:

```python
import sqlite3

from litforge.config import Paths
from litforge.projects import create_project
from litforge.screening.criteria import save_criteria
from litforge.screening.llm import FakeLlm
from litforge.screening.run import screen_run
from litforge.storage.db import new_id, now, transaction

CRIT = Criteria(question="neural mass model inference", include=["uses a neural mass model"],
                exclude=["animal study only"])


def make_run(conn: sqlite3.Connection, paths: Paths, papers: list[tuple[str, str | None]]) -> tuple[str, str, list[str]]:
    """A project with one discovery run; papers ranked in the given order (first = highest priority)."""
    pid = create_project(conn, paths, "scr")
    run_id, ids = new_id(), []
    with transaction(conn):
        save_criteria(conn, pid, CRIT)
        conn.execute("INSERT INTO discovery_runs(id, project_id, kind, status, started_at) VALUES (?,?,?,?,?)",
                     (run_id, pid, "search", "done", now()))
        for i, (title, abstract) in enumerate(papers):
            paper_id = new_id()
            ids.append(paper_id)
            conn.execute("INSERT INTO papers(id, title, abstract, created_at, updated_at) VALUES (?,?,?,?,?)",
                         (paper_id, title, abstract, now(), now()))
            conn.execute("INSERT INTO project_papers(project_id, paper_id, source, added_at) VALUES (?,?,?,?)",
                         (pid, paper_id, "discovery", now()))
            conn.execute("INSERT INTO discovery_results(run_id, paper_id, score, n_hits, new_to_project, priority)"
                         " VALUES (?,?,?,?,?,?)", (run_id, paper_id, 1 - i / 10, 1, 1, 1 - i / 10))
    return pid, run_id, ids


def answer(messages: list[dict[str, str]]) -> dict[str, Any]:
    text = messages[-1]["content"]
    mouse = "mice" in text
    return {"relevance": 1 if mouse else 4,
            "inclusion": [{"status": "met", "evidence": "neural mass model"}],
            "exclusion": [{"status": "violated" if mouse else "not_violated", "evidence": "in mice" if mouse else ""}],
            "reason": "r"}


PAPERS = [("Neural mass model fit", "A neural mass model fitted to EEG."),
          ("Mouse study", "A neural mass model in mice only."),
          ("No abstract", None)]


def test_screen_run_writes_results_and_caches(conn: sqlite3.Connection, paths: Paths) -> None:
    pid, run_id, ids = make_run(conn, paths, PAPERS)
    llm = FakeLlm(answer)
    report = screen_run(conn, llm, run_id, top=10)
    assert (report.candidates, report.screened, report.cached, report.failed) == (3, 3, 0, 0)
    rows = {r["paper_id"]: r for r in conn.execute("SELECT * FROM screening_results WHERE project_id=?", (pid,))}
    assert rows[ids[0]]["decision"] == "include"
    assert rows[ids[1]]["decision"] == "exclude"
    assert rows[ids[2]]["model"] == "fake"
    again = screen_run(conn, llm, run_id, top=10)
    assert (again.screened, again.cached, llm.calls) == (0, 3, 3)


def test_screen_run_top_limits_and_order(conn: sqlite3.Connection, paths: Paths) -> None:
    _, run_id, ids = make_run(conn, paths, PAPERS)
    report = screen_run(conn, FakeLlm(answer), run_id, top=1)
    assert report.candidates == 1
    assert [r[0] for r in conn.execute("SELECT paper_id FROM screening_results")] == [ids[0]]
    with pytest.raises(ValueError, match="at least 1"):
        screen_run(conn, FakeLlm(answer), run_id, top=0)


def test_changed_criteria_rescreen(conn: sqlite3.Connection, paths: Paths) -> None:
    pid, run_id, _ = make_run(conn, paths, PAPERS[:1])
    llm = FakeLlm(answer)
    screen_run(conn, llm, run_id, top=5)
    with transaction(conn):
        save_criteria(conn, pid, Criteria(question="other question", include=["x"], exclude=["y"]))
    assert screen_run(conn, llm, run_id, top=5).screened == 1 and llm.calls == 2


def test_model_failure_retries_once_then_records_uncertain(conn: sqlite3.Connection, paths: Paths) -> None:
    _, run_id, ids = make_run(conn, paths, PAPERS[:1])
    llm = FakeLlm(lambda m: "garbage")
    report = screen_run(conn, llm, run_id, top=5)
    assert report.failed == 1 and llm.calls == 2
    row = conn.execute("SELECT decision, error FROM screening_results WHERE paper_id=?", (ids[0],)).fetchone()
    assert row["decision"] == "uncertain" and row["error"]
    ok = FakeLlm(answer)
    assert screen_run(conn, ok, run_id, top=5).screened == 1  # failed results are retried next time


def test_no_criteria_or_unreachable_model_touches_nothing(conn: sqlite3.Connection, paths: Paths) -> None:
    pid, run_id, _ = make_run(conn, paths, PAPERS)
    with pytest.raises(LlmError):
        screen_run(conn, OllamaClient("http://127.0.0.1:9", "qwen3:4b", timeout_s=1), run_id, top=5)
    with transaction(conn):
        save_criteria(conn, pid, Criteria(question=""))
    with pytest.raises(ValueError, match="criteria"):
        screen_run(conn, FakeLlm(answer), run_id, top=5)
    assert conn.execute("SELECT COUNT(*) FROM screening_results").fetchone()[0] == 0
    with pytest.raises(LookupError):
        screen_run(conn, FakeLlm(answer), "nope", top=5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/integration/test_screening.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'litforge.screening.run'`

- [ ] **Step 3: Implement**

```sql
-- src/litforge/storage/migrations/0006_screening.sql
-- 0006: title/abstract screening by a local LLM. One result per (project, paper): a paper found by several
-- runs is screened once. criteria_hash tells the UI a result predates the current criteria.

CREATE TABLE screening_results (
    project_id     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id       TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    decision       TEXT NOT NULL,                 -- include | uncertain | exclude
    score          REAL NOT NULL,                 -- 0..1, computed in Python
    result_json    TEXT NOT NULL,                 -- Decision: per-criterion status, evidence, reason
    error          TEXT,                          -- set when the model gave no usable answer
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    criteria_hash  TEXT NOT NULL,
    fingerprint    TEXT NOT NULL,                 -- prompt + model + criteria + title + abstract
    screened_at    TEXT NOT NULL,
    PRIMARY KEY (project_id, paper_id)
);
```

```python
# src/litforge/screening/run.py
"""Screen the top papers of a discovery run, one model call per paper, resumable and cached."""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable

from pydantic import BaseModel

from litforge.observability import EventLog
from litforge.screening.criteria import criteria_hash, load_criteria
from litforge.screening.decide import Decision, decide, failed_decision
from litforge.screening.llm import Llm, LlmError
from litforge.screening.prompt import PROMPT_VERSION, build_messages, fingerprint, response_schema
from litforge.storage.db import now, transaction


class ScreenReport(BaseModel):
    run_id: str
    project_id: str
    model: str
    candidates: int
    screened: int = 0
    cached: int = 0
    failed: int = 0
    counts: dict[str, int] = {}


def screen_run(conn: sqlite3.Connection, llm: Llm, run_id: str, top: int,
               progress: Callable[[str], None] | None = None, log: EventLog | None = None) -> ScreenReport:
    if top < 1:
        raise ValueError("top must be at least 1")
    run = conn.execute("SELECT project_id FROM discovery_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise LookupError("run not found")
    project_id = run["project_id"]
    criteria = load_criteria(conn, project_id)
    if criteria.is_empty():
        raise ValueError("no screening criteria: fill in the research question first")
    llm.check()
    papers = conn.execute(
        """SELECT p.id, p.title, p.abstract, p.year, p.venue, s.fingerprint, s.error
           FROM discovery_results d JOIN papers p ON p.id = d.paper_id
           LEFT JOIN screening_results s ON s.project_id = ? AND s.paper_id = p.id
           WHERE d.run_id = ? ORDER BY COALESCE(d.priority, d.score) DESC, p.title LIMIT ?""",
        (project_id, run_id, top)).fetchall()
    say = progress or (lambda _m: None)
    report = ScreenReport(run_id=run_id, project_id=project_id, model=llm.name, candidates=len(papers))
    chash, schema, counts = criteria_hash(criteria), response_schema(criteria), Counter[str]()
    say(f"screening {len(papers)} papers with {llm.name}")
    for i, p in enumerate(papers, 1):
        fp = fingerprint(criteria, llm.name, p["title"], p["abstract"])
        if p["fingerprint"] == fp and not p["error"]:
            report.cached += 1
            continue
        messages = build_messages(criteria, p["title"], p["abstract"], p["year"], p["venue"])
        decision: Decision | None = None
        error = ""
        for _attempt in range(2):
            try:
                decision = decide(llm.chat_json(messages, schema), criteria, p["title"], p["abstract"])
                break
            except LlmError as exc:
                error = str(exc)
        if decision is None:
            decision = failed_decision(criteria, error)
            report.failed += 1
            if log:
                log.emit("screening.failed", paper=p["id"], error=error)
        else:
            report.screened += 1
        counts[decision.decision] += 1
        with transaction(conn):
            conn.execute(
                """INSERT INTO screening_results(project_id, paper_id, decision, score, result_json, error, model,
                       prompt_version, criteria_hash, fingerprint, screened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_id, paper_id) DO UPDATE SET decision=excluded.decision,
                       score=excluded.score, result_json=excluded.result_json, error=excluded.error,
                       model=excluded.model, prompt_version=excluded.prompt_version,
                       criteria_hash=excluded.criteria_hash, fingerprint=excluded.fingerprint,
                       screened_at=excluded.screened_at""",
                (project_id, p["id"], decision.decision, decision.score, decision.model_dump_json(),
                 decision.error, llm.name, PROMPT_VERSION, chash, fp, now()))
        say(f"[{i}/{len(papers)}] {decision.decision:<9} {(p['title'] or '(no title)')[:70]}")
    report.counts = dict(counts)
    if log:
        log.emit("screening.run", **report.model_dump())
    say(f"done: {report.screened} screened, {report.cached} unchanged, {report.failed} failed")
    return report
```

Check `EventLog.emit` signature in `src/litforge/observability.py` before using `**report.model_dump()`;
if it takes `(event: str, **fields)`, this is correct.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/integration/test_screening.py tests/unit/test_db.py -q && uv run ruff check src tests && uv run mypy`
Expected: all pass (test_db may assert the migration count/version — update its expected latest version to 6 if it does)

- [ ] **Step 5: Commit**

```bash
git add src/litforge/storage/migrations/0006_screening.sql src/litforge/screening/run.py tests
git commit -m "Add screening_results table and screen_run

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: CLI `litforge screen`

**Files:**
- Modify: `src/litforge/cli.py`
- Test: `tests/integration/test_screening.py` (append)

**Interfaces:**
- Consumes: `screen_run`, `ollama_from_config`, `LlmError`
- Produces: command `litforge screen [--run RUN_ID] [--top N] [--model M] [--project P] [--json]`; module-level hook `cli._make_llm(config, model) -> Llm` (tests monkeypatch it)

- [ ] **Step 1: Write the failing test**

```python
from typer.testing import CliRunner

from litforge import cli


def test_cli_screen_latest_run(conn: sqlite3.Connection, paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    _, run_id, _ = make_run(conn, paths, PAPERS)
    monkeypatch.setattr(cli, "_make_llm", lambda config, model: FakeLlm(answer))
    monkeypatch.setenv("LITFORGE_HOME", str(paths.root))
    result = CliRunner().invoke(cli.app, ["screen", "--project", "scr", "--top", "2"])
    assert result.exit_code == 0, result.output
    assert "include" in result.output and "2 screened" in result.output
```

Before writing it, read the top of `src/litforge/cli.py` (the `@app.callback()` that builds `State`) to
confirm `LITFORGE_HOME` selects the data root; if the callback takes a `--data-dir` option instead, pass
`["--data-dir", str(paths.root), "screen", ...]`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_screening.py::test_cli_screen_latest_run -q`
Expected: FAIL (`No such command 'screen'` or missing `_make_llm`)

- [ ] **Step 3: Implement** (add near the `similar` command; imports at the top of `cli.py`)

```python
from litforge.screening.llm import Llm, LlmError, ollama_from_config
from litforge.screening.run import screen_run


def _make_llm(config: Config, model: str | None) -> Llm:
    return ollama_from_config(config.screening, model)


@app.command()
def screen(
    ctx: typer.Context,
    run: Annotated[str | None, typer.Option("--run", help="Run id (default: the project's latest run)")] = None,
    top: Annotated[int | None, typer.Option(help="How many top papers of the run to screen")] = None,
    model: Annotated[str | None, typer.Option(help="Ollama model (default: screening.model)")] = None,
    project: ProjectOpt = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Screen a run's top papers against the project's criteria with a local model (set criteria in the web UI)."""
    st = _state(ctx)
    conn = st.db()
    name, pid = _project(st, conn, project, required=True)
    if run is None:
        row = conn.execute("SELECT id FROM discovery_runs WHERE project_id=? ORDER BY rowid DESC LIMIT 1",
                           (pid,)).fetchone()
        if row is None:
            typer.secho(f"no runs in '{name}' yet", fg="red", err=True)
            raise typer.Exit(2)
        run = row["id"]
    try:
        report = screen_run(conn, _make_llm(st.config, model), run, top or st.config.screening.default_top,
                            progress=None if as_json else typer.echo, log=st.log)
    except (ValueError, LookupError, LlmError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(2) from exc
    if as_json:
        _emit_json([report])
        return
    typer.echo(", ".join(f"{k} {v}" for k, v in sorted(report.counts.items())) or "nothing new to screen")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_screening.py -q && uv run ruff check src tests && uv run mypy`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/litforge/cli.py tests/integration/test_screening.py
git commit -m "Add litforge screen command

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Web API — criteria, screen job, screening fields

**Files:**
- Modify: `src/litforge/ui/api.py` (`run_results`, `result_stats`, `paper_detail`, new `get_criteria`, `put_criteria`)
- Modify: `src/litforge/ui/server.py` (`App.__init__` `make_llm` hook, `App.screen_job`, routes)
- Test: `tests/integration/test_ui_server.py` (append)

**Interfaces:**
- Consumes: Tasks 1–3
- Produces (consumed by Task 6's JS):
  - `GET /api/criteria?project=P` → `{"question": str, "include": [str], "exclude": [str], "model": str, "default_top": int}`
  - `POST /api/criteria {project, question, include: str|[str], exclude: str|[str]}` → same shape (strings are split by lines)
  - `POST /api/screen {run_id, top?, model?}` → job (`kind: "screen"`, `run_id` = the run on success); 400 if no criteria
  - Each paper in `GET /api/runs/<id>` gets `"screening": null | {"decision", "score", "reason", "error", "stale": bool}`
  - `stats` gets `"screened": int, "screen_include": int, "screen_uncertain": int, "screen_exclude": int`
  - `GET /api/papers/<id>?project=P` gets `"screening": null | {**Decision, "model", "screened_at", "stale"}`
  - `App(paths, config, make_http=None, make_llm=None)`; `make_llm: Callable[[str | None], Llm]`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_ui_server.py`. The existing `server` fixture builds `App(...)`; add a
second fixture that injects a fake LLM:

```python
from litforge.screening.llm import FakeLlm


@pytest.fixture()
def screen_server(tmp_path: Path) -> Iterator[str]:
    transport = FakeTransport(default_routes())
    config = load_config()
    config.similar.embedding_model = None

    def answer(messages: list[dict[str, str]]) -> dict[str, Any]:
        return {"relevance": 4, "inclusion": [{"status": "met", "evidence": ""}], "exclusion": [], "reason": "fits"}

    app = App(Paths(tmp_path / "data"), config,
              make_http=lambda: HttpClient(transport=transport, sleep=lambda s: None, min_interval={}),
              make_llm=lambda model: FakeLlm(answer))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_criteria_screen_job_and_screening_fields(screen_server: str) -> None:
    s = screen_server
    _, job = call(s, "/api/similar", {"project": "scr", "seeds": ["10.1073/pnas.1912789117"], "embeddings": False})
    run_id = wait_job(s, job["id"])["run_id"]
    status, err = call(s, "/api/screen", {"run_id": run_id, "top": 3})
    assert status == 400 and "criteria" in err["error"]

    status, crit = call(s, "/api/criteria", {"project": "scr", "question": "q", "include": "a\n\nb", "exclude": ""})
    assert status == 200 and crit["include"] == ["a", "b"] and crit["model"]
    assert call(s, "/api/criteria?project=scr")[1]["question"] == "q"

    status, job = call(s, "/api/screen", {"run_id": run_id, "top": 3})
    assert status == 200
    job = wait_job(s, job["id"])
    assert job["status"] == "done" and job["run_id"] == run_id, job

    _, data = call(s, f"/api/runs/{run_id}")
    screened = [p for p in data["papers"] if p["screening"]]
    assert len(screened) == min(3, len(data["papers"]))
    assert screened[0]["screening"]["decision"] == "include"  # "b" padded to unclear, nothing not_met, relevance 4
    assert data["stats"]["screened"] == len(screened)

    _, detail = call(s, f"/api/papers/{screened[0]['id']}?project=scr")
    assert detail["screening"]["inclusion"][0]["criterion"] == "a" and detail["screening"]["model"] == "fake"

    call(s, "/api/criteria", {"project": "scr", "question": "changed", "include": "", "exclude": ""})
    _, data = call(s, f"/api/runs/{run_id}")
    assert all(p["screening"]["stale"] for p in data["papers"] if p["screening"])
```


- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/integration/test_ui_server.py::test_criteria_screen_job_and_screening_fields -q`
Expected: FAIL (`unexpected keyword argument 'make_llm'`)

- [ ] **Step 3: Implement**

`src/litforge/ui/api.py` — new functions:

```python
from litforge.screening.criteria import Criteria, criteria_hash, load_criteria, save_criteria


def get_criteria(conn: sqlite3.Connection, project: str) -> dict[str, Any]:
    return load_criteria(conn, get_project_id(conn, project)).model_dump()


def put_criteria(conn: sqlite3.Connection, project: str, body: dict[str, Any]) -> dict[str, Any]:
    def text(value: Any) -> str:
        return "\n".join(map(str, value)) if isinstance(value, list) else str(value or "")
    c = Criteria.from_text(str(body.get("question") or ""), text(body.get("include")), text(body.get("exclude")))
    save_criteria(conn, get_project_id(conn, project), c)
    return c.model_dump()
```

In `run_results`: compute `current = criteria_hash(load_criteria(conn, run["project_id"]))`; extend the
SELECT with
`LEFT JOIN screening_results s ON s.project_id = ? AND s.paper_id = p.id` and columns
`s.decision AS s_decision, s.score AS s_score, s.result_json AS s_result, s.error AS s_error, s.criteria_hash AS s_hash`
(pass `run["project_id"]` a second time in the parameters, before `run_id`). In each paper dict add:

```python
"screening": None if r["s_decision"] is None else {
    "decision": r["s_decision"], "score": r["s_score"], "error": r["s_error"],
    "reason": json.loads(r["s_result"]).get("reason", ""), "stale": r["s_hash"] != current},
```

In `result_stats` add:

```python
screened = [p["screening"] for p in papers if p.get("screening")]
...
"screened": len(screened),
"screen_include": sum(s["decision"] == "include" for s in screened),
"screen_uncertain": sum(s["decision"] == "uncertain" for s in screened),
"screen_exclude": sum(s["decision"] == "exclude" for s in screened),
```

In `paper_detail`, when `project_id` is set:

```python
screening = None
if project_id:
    s = conn.execute("SELECT * FROM screening_results WHERE project_id=? AND paper_id=?",
                     (project_id, paper_id)).fetchone()
    if s:
        current = criteria_hash(load_criteria(conn, project_id))
        screening = {**json.loads(s["result_json"]), "model": s["model"], "screened_at": s["screened_at"],
                     "stale": s["criteria_hash"] != current}
```

and add `"screening": screening` to the returned dict.

`src/litforge/ui/server.py`:

```python
from litforge.screening.llm import Llm, LlmError, ollama_from_config
from litforge.screening.criteria import load_criteria
from litforge.screening.run import screen_run
```

`App.__init__` gains `make_llm: Callable[[str | None], Llm] | None = None` and sets
`self.make_llm = make_llm or (lambda model: ollama_from_config(config.screening, model))`.

```python
    def screen_job(self, body: dict[str, Any]) -> Job:
        run_id = str(body["run_id"])
        top = int(body.get("top") or self.config.screening.default_top)
        conn = self.db()
        try:
            run = conn.execute("SELECT r.project_id, p.name FROM discovery_runs r JOIN projects p"
                               " ON p.id = r.project_id WHERE r.id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("run not found")
            if load_criteria(conn, run["project_id"]).is_empty():
                raise ValueError("no screening criteria: fill in the research question first")
        finally:
            conn.close()
        if top < 1:
            raise ValueError("top must be at least 1")
        model = str(body.get("model") or "") or None

        def work(job: Job) -> str | None:
            c = self.db()
            try:
                screen_run(c, self.make_llm(model), run_id, top, progress=job.messages.append, log=self.log)
            except LlmError as exc:
                raise RuntimeError(str(exc)) from exc
            finally:
                c.close()
            return run_id

        return self.start_job("screen", str(run["name"]), work)
```

Routes — in `_get`:

```python
                if path == "/api/criteria":
                    return self._json({**api.get_criteria(conn, query["project"]),
                                       "model": app.config.screening.model,
                                       "default_top": app.config.screening.default_top})
```

In `do_POST` (after `/api/add`):

```python
                if url.path == "/api/screen":
                    return self._json(app.screen_job(body).__dict__)
                if url.path == "/api/criteria":
                    conn = app.db()
                    try:
                        with transaction(conn):
                            saved = api.put_criteria(conn, str(body["project"]), body)
                    finally:
                        conn.close()
                    return self._json({**saved, "model": app.config.screening.model,
                                       "default_top": app.config.screening.default_top})
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_ui_server.py -q && uv run ruff check src tests && uv run mypy`
Expected: all pass (existing tests unaffected)

- [ ] **Step 5: Commit**

```bash
git add src/litforge/ui/api.py src/litforge/ui/server.py tests/integration/test_ui_server.py
git commit -m "Add screening API: criteria, screen job, screening fields in results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Web page — screening panel, badge, sort, filter, drawer

**Files:**
- Modify: `src/litforge/ui/static/index.html`, `src/litforge/ui/static/app.js`, `src/litforge/ui/static/app.css`
- Test: `tests/integration/test_ui_server.py::test_page_and_assets_are_served` (append asserts); manual check in a browser

**Interfaces:**
- Consumes: Task 5 endpoints and fields exactly as listed there.

- [ ] **Step 1: Failing asset test** — append to `test_page_and_assets_are_served`:

```python
    assert b'id="screen-panel"' in html and b'value="screening"' in html
    _, js = call(server, "/static/app.js")
    assert b"/api/screen" in js and b"/api/criteria" in js
```

Run: `uv run pytest tests/integration/test_ui_server.py::test_page_and_assets_are_served -q` → FAIL.

- [ ] **Step 2: index.html** — inside `<section id="results">`, directly after `<div class="charts">…</div>`:

```html
      <details class="card" id="screen-panel">
        <summary><b>Screening</b> <span class="meta" id="screen-summary">set a research question to screen with the local model</span></summary>
        <p class="hint">A local model reads each paper's title and abstract and checks it against these criteria. It only
          suggests include / uncertain / exclude; your ✓/✗ marks decide. Nothing leaves this computer.</p>
        <label class="f">Research question</label>
        <textarea id="crit-question" rows="2"></textarea>
        <label class="f">Inclusion criteria <small>(one per line)</small></label>
        <textarea id="crit-include" rows="3"></textarea>
        <label class="f">Exclusion criteria <small>(one per line)</small></label>
        <textarea id="crit-exclude" rows="3"></textarea>
        <div class="screen-actions">
          <button class="btn small" id="btn-crit-save">Save criteria</button>
          <span class="sep"></span>
          Screen top <input type="number" id="screen-n" min="1" value="100"> papers
          <button class="btn small primary" id="btn-screen">Screen</button>
          <span class="hint" id="screen-model"></span>
        </div>
      </details>
```

In `<select id="sort">` add `<option value="screening">Sort: screening</option>` as the second option.
After the `<select id="sort">…</select>` add:

```html
          <select id="screen-filter" title="Show papers by screening decision">
            <option value="">All decisions</option>
            <option value="include">Include</option>
            <option value="uncertain">Uncertain</option>
            <option value="exclude">Exclude</option>
            <option value="none">Not screened</option>
          </select>
```

- [ ] **Step 3: app.js**

State: add `screenFilter: ''` to `state`.

Helpers (near `paperLinks`):

```javascript
  var SCREEN_ORDER = { include: 0, uncertain: 1, exclude: 2 };
  function screenRank(p) { return p.screening ? SCREEN_ORDER[p.screening.decision] : 3; }
  function screenBadge(p) {
    var s = p.screening;
    if (!s) return '';
    return '<span class="badge scr ' + s.decision + (s.stale ? ' stale' : '') + '" title="' +
      esc((s.stale ? 'Criteria changed since screening. ' : '') + (s.error ? 'Model error: ' + s.error : s.reason || '')) + '">' +
      s.decision + ' ' + s.score.toFixed(2) + '</span>';
  }
```

`visiblePapers()` filter: add before the text filter:

```javascript
      if (state.screenFilter === 'none' && p.screening) return false;
      if (state.screenFilter && state.screenFilter !== 'none' && (!p.screening || p.screening.decision !== state.screenFilter)) return false;
```

Sort: add as the first branch inside `list.sort`:

```javascript
      if (key === 'screening') {
        return screenRank(a) - screenRank(b) || (b.screening ? b.screening.score : 0) - (a.screening ? a.screening.score : 0) ||
          b.priority - a.priority;
      }
```

`renderList()`: prepend `screenBadge(p)` to `badges`.

`render()`: add a stats card `[s.screened ? s.screen_include + ' / ' + s.screen_uncertain + ' / ' + s.screen_exclude : '–', 'screened in / ? / out']`
to `cards`, and call `loadCriteria()` at the end of `render()` only when the project changed:

```javascript
  function loadCriteria() {
    if (!state.project || state.criteriaFor === state.project) return;
    state.criteriaFor = state.project;
    api('/api/criteria?project=' + encodeURIComponent(state.project)).then(showCriteria).catch(function (e) { toast(e.message); });
  }
  function showCriteria(c) {
    $('crit-question').value = c.question || '';
    $('crit-include').value = (c.include || []).join('\n');
    $('crit-exclude').value = (c.exclude || []).join('\n');
    $('screen-n').value = $('screen-n').value || c.default_top;
    $('screen-model').textContent = 'model: ' + c.model;
    var s = state.data && state.data.stats;
    $('screen-summary').textContent = !c.question ? 'set a research question to screen with the local model' :
      (s && s.screened ? s.screened + ' screened: ' + s.screen_include + ' include, ' + s.screen_uncertain +
        ' uncertain, ' + s.screen_exclude + ' exclude' : 'not screened yet');
    if (!c.question) $('screen-panel').open = true;
  }
  function saveCriteria() {
    return post('/api/criteria', { project: state.project, question: $('crit-question').value,
      include: $('crit-include').value, exclude: $('crit-exclude').value }).then(function (c) { showCriteria(c); return c; });
  }
```

`openDrawer()`: after the importance block and before "Found because", add:

```javascript
      var sc = d.screening;
      if (sc) {
        var row = function (r, ok) {
          return '<li><b class="scr-' + r.status + '">' + esc(r.status.replace('_', ' ')) + '</b> ' + esc(r.criterion) +
            (r.evidence ? '<br><q>' + esc(r.evidence) + '</q>' + (r.verified === false ? ' <small class="bad">not found in abstract</small>' : '') : '') + '</li>';
        };
        html += '<h3>Screening: ' + esc(sc.decision) + ' (' + sc.score.toFixed(2) + ')' +
          (sc.stale ? ' <small class="bad">criteria changed since</small>' : '') + '</h3>' +
          (sc.error ? '<p class="bad">Model error: ' + esc(sc.error) + '</p>' : '<p>' + esc(sc.reason) + '</p>') +
          '<ul class="reasons">' + sc.inclusion.map(row).join('') + sc.exclusion.map(row).join('') + '</ul>' +
          '<small class="hint">relevance ' + sc.relevance + '/5 · ' + esc(sc.model) + ' · ' + esc((sc.screened_at || '').slice(0, 16)) + '</small>';
      }
```

`setBusy()`: add `$('btn-screen').disabled = busy;`.

`init()` wiring:

```javascript
    $('btn-crit-save').onclick = function () {
      if (!state.project) { toast('Open a run first'); return; }
      saveCriteria().then(function () { toast('Criteria saved'); }).catch(function (e) { toast(e.message); });
    };
    $('btn-screen').onclick = function () {
      if (!state.runId) { toast('Open a run first'); return; }
      var n = Math.max(1, parseInt($('screen-n').value, 10) || 100);
      setBusy(true);
      saveCriteria().then(function () { return post('/api/screen', { run_id: state.runId, top: n }); })
        .then(function (job) { state.sort = 'screening'; store.set('sort', 'screening'); $('sort').value = 'screening'; poll(job); })
        .catch(function (e) { setBusy(false); toast(e.message); });
    };
    $('screen-filter').onchange = function () { state.screenFilter = this.value; state.anchor = -1; renderList(); };
```

`poll()`: in the failed-toast map add `screen: 'Screening failed'`. In `loadRun`, set `state.criteriaFor = null` is NOT needed (same project keeps its form), but after a screen job finishes `loadRun` re-renders and `showCriteria` must refresh the summary: call `showCriteria` from `render()` when `state.criteria` is cached — store the last criteria in `state.criteria` inside `showCriteria` and in `render()` do `if (state.criteria && state.criteriaFor === state.project) showCriteria(state.criteria); else loadCriteria();`.

- [ ] **Step 4: app.css** (append; use existing tokens)

```css
.badge.scr { text-transform: none; }
.badge.scr.include { background: var(--good-soft); color: var(--good); }
.badge.scr.uncertain { background: #fbf3dc; color: #8a6400; }
.badge.scr.exclude { background: var(--bad-soft); color: var(--bad); }
.badge.scr.stale { opacity: .55; text-decoration: line-through; }
#screen-panel { margin-bottom: 12px; }
#screen-panel summary { cursor: pointer; }
#screen-panel textarea { width: 100%; box-sizing: border-box; font: inherit; }
.screen-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.screen-actions input[type=number] { width: 64px; }
.scr-met, .scr-not_violated { color: var(--good); }
.scr-not_met, .scr-violated { color: var(--bad); }
.scr-unclear { color: var(--muted); }
```

Check `app.css` for a dark-mode block; if `#fbf3dc` is overridden there for `.chip.impact`, mirror that
override for `.badge.scr.uncertain`.

- [ ] **Step 5: Run tests and check by hand**

Run: `uv run pytest tests/integration/test_ui_server.py -q`
Then: `uv run litforge ui` (or the project's UI command), open a run, fill criteria, screen 10 papers with the real
`qwen3:4b`, sort by screening, open a drawer, filter by decision, change criteria and confirm badges turn stale.

- [ ] **Step 6: Commit**

```bash
git add src/litforge/ui/static tests/integration/test_ui_server.py
git commit -m "Add screening panel, badges, sort and filter to the web UI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Export screening fields, live test, docs

**Files:**
- Modify: `src/litforge/export/markdown.py`, `README.md`
- Test: `tests/integration/test_screening.py` (append export test + live test)

**Interfaces:**
- Consumes: `screening_results` table.

- [ ] **Step 1: Failing tests**

```python
from litforge.export.markdown import export_markdown


def test_export_includes_screening(conn: sqlite3.Connection, paths: Paths, tmp_path: Any) -> None:
    pid, run_id, _ = make_run(conn, paths, PAPERS[:2])
    screen_run(conn, FakeLlm(answer), run_id, top=5)
    root, n = export_markdown(conn, paths, "scr", pid, out_dir=tmp_path / "out")
    assert n == 2
    notes = "\n".join(p.read_text() for p in (root / "knowledge_base").glob("*.md"))
    assert "screening_decision: include" in notes and "screening_decision: exclude" in notes
    assert "| Screening |" in (root / "README.md").read_text()


@pytest.mark.skipif("not __import__('os').environ.get('LITFORGE_LIVE_LLM')",
                    reason="set LITFORGE_LIVE_LLM=1 with Ollama running and qwen3:4b pulled")
def test_live_ollama_screens_obvious_cases(conn: sqlite3.Connection, paths: Paths) -> None:
    from litforge.config import load_config
    from litforge.screening.llm import ollama_from_config
    _, run_id, ids = make_run(conn, paths, [
        ("Simulation-based inference for a Jansen-Rit neural mass model",
         "We fit a neural mass model to human EEG recordings using simulation-based inference."),
        ("Crop yields under drought", "We study wheat yields in dry seasons in a field trial.")])
    screen_run(conn, ollama_from_config(load_config().screening), run_id, top=2)
    got = {r["paper_id"]: r["decision"] for r in conn.execute("SELECT paper_id, decision FROM screening_results")}
    assert got[ids[0]] == "include" and got[ids[1]] != "include"
```

Run: `uv run pytest tests/integration/test_screening.py -q` → export test FAILS.

- [ ] **Step 2: Implement export**

In `export_markdown`, extend the SELECT with
`LEFT JOIN screening_results s ON s.paper_id = p.id AND s.project_id = pp.project_id` and columns
`s.decision AS s_decision, s.score AS s_score, s.result_json AS s_result`. Compute
`screened = any(r["s_decision"] for r in rows)` after the rows are filtered. In `front`, when
`row["s_decision"]`:

```python
            front["screening_decision"] = row["s_decision"]
            front["screening_score"] = round(row["s_score"], 3)
            front["screening_reason"] = json.loads(row["s_result"]).get("reason", "")
```

Header lines become conditional:

```python
        "| Paper | Year | Status | PDF |" + (" Screening |" if screened else ""),
        "|---|---|---|---|" + ("---|" if screened else ""),
```

and the row append adds `f" {row['s_decision'] or ''} |"` when `screened`. Note `manifest.json` dumps
`dict(r)` for each row: drop the `s_*` keys there (`{k: v for k, v in dict(r).items() if not k.startswith("s_")}`)
so the manifest format is unchanged.

- [ ] **Step 3: README** — add a section after "Relevance, importance and review priority":

```markdown
### Screening with a local model

A local model (Ollama, default `qwen3:4b`) reads the title and abstract of a run's top papers and checks them
against the project's research question and inclusion/exclusion criteria. It suggests include, uncertain or
exclude, with a reason and quotes from the abstract; your ✓/✗ marks decide. Nothing leaves your computer.

1. `ollama pull qwen3:4b` (once; `qwen3:8b` is slower but more careful — set `screening.model`).
2. In the web UI open a run, fill in **Screening** (question and criteria, one per line), press **Screen**.
3. Sort by **screening**, then **Mark top N ✓** and export the ✓ papers.

CLI: `uv run litforge screen --top 100` screens the active project's latest run.
Changing the criteria or the model marks earlier results as stale; screening again re-checks them.
```

Also add `uv run litforge screen` to the command list near `litforge doctor`, and in the "Status and
limitations" section replace "Planned: screening with a local …" wording with the actual status (Tier A done,
full-text screening planned).

- [ ] **Step 4: Full check**

Run: `uv run pytest -q && uv run ruff check src tests && uv run mypy`
Then live: `LITFORGE_LIVE_LLM=1 uv run pytest tests/integration/test_screening.py -q -k live`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add src/litforge/export/markdown.py README.md tests/integration/test_screening.py
git commit -m "Export screening results and document screening

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
