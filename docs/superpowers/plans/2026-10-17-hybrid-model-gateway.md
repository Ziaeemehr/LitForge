# Hybrid Model Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add a provider-neutral, privacy- and budget-controlled model gateway while preserving current Ollama screening and vision behavior.

**Architecture:** Add focused modules under src/litforge/llm for contracts, evidence packets, an OpenAI-compatible client, routing and budgets. Keep compatibility exports in screening/llm.py, persist model-call metadata, and test external behavior through injected fake transports.

**Tech Stack:** Python 3.12, Pydantic 2, urllib, SQLite, pytest, Ruff, mypy.

**Spec:** docs/superpowers/specs/2026-10-17-hybrid-model-gateway-design.md

## Global Constraints

- Cloud access is disabled by default.
- No full PDF bytes enter a model request.
- Secrets are read only from named environment variables and never persisted or logged.
- No automatic provider fallback.
- Existing screening and vision imports remain compatible.
- No live API call is required.

## Review Focus

- local_only must reject remote profiles before transport invocation.
- Sensitive evidence must not route remotely without explicit approval.
- Missing prices or excessive cost must fail before transport invocation.
- Malformed provider JSON must produce typed, redacted errors.
- Existing Ollama screening and vision must remain unchanged.

---

### Task 1: Provider contracts and evidence packets

**Files:** Create src/litforge/llm/__init__.py, base.py and evidence.py; test tests/unit/test_llm_gateway.py.

**Produces:** ModelProvider, ModelReply, ModelUsage, shared errors, EvidenceItem and EvidencePacket.

- [x] Write tests for evidence-ID uniqueness, empty packets, sensitivity and deterministic hashes.
- [x] Run focused tests and verify RED because the package is absent.
- [x] Implement typed contracts and evidence models.
- [x] Run focused tests and verify GREEN.
- [x] Commit the contract foundation.

### Task 2: OpenAI-compatible provider

**Files:** Create src/litforge/llm/openai_compatible.py; modify tests/unit/test_llm_gateway.py.

**Produces:** OpenAICompatibleClient.chat_json(messages, schema) returning ModelReply through an injected transport.

- [x] Add tests for URL, bearer auth, schema request, response parsing, usage, missing keys and redacted HTTP errors.
- [x] Run tests and verify RED.
- [x] Implement the minimum client with environment lookup at dispatch.
- [x] Run focused tests and refactor while green.
- [x] Commit execution loop 1.

### Task 3: Routing and budgets

**Files:** Create src/litforge/llm/router.py and budget.py; modify config.py, configs/default.yaml and gateway tests.

**Produces:** ModelRouter.route(request, approved=False) and BudgetGuard.check.

- [x] Add tests for all policies, sensitive packets, approval, missing prices and hard limits.
- [x] Run tests and verify RED.
- [x] Implement typed config, routing and deterministic cost calculation.
- [x] Run focused tests and verify GREEN.
- [x] Commit routing and budget behavior.

### Task 4: Call persistence

**Files:** Create migration 0007_model_calls.sql and src/litforge/llm/audit.py; modify database and gateway tests.

**Produces:** record_model_call(conn, record) and model_calls rows without secret material.

- [x] Add failing migration and persistence tests, including absence of secrets.
- [x] Run focused tests and verify RED.
- [x] Implement the migration and audit writer.
- [x] Run database and gateway tests.
- [x] Commit execution loop 2.

### Task 5: Backward-compatible screening integration

**Files:** Modify screening/llm.py, screening/run.py, cli.py and screening tests.

**Preserves:** legacy imports, current CLI defaults and vision integration.

- [x] Add tests exercising a gateway provider through screening and resolving legacy imports.
- [x] Run screening tests and verify the new path is RED while legacy tests stay green.
- [x] Adapt the screening boundary and keep Ollama as default.
- [x] Run screening, vision and integration tests.
- [x] Commit gateway integration.

### Task 6: Regression loop and documentation

**Files:** Modify docs/guide.md and docs/local_ai_research_roadmap_fa.md; test the full project.

- [x] Run pytest, Ruff and mypy.
- [x] Attempt a Copilot auto/efficiency read-only review. Automatic approval review blocked exporting private repository content, so the review was completed locally.
- [x] Re-grade findings and fix Important findings with a failing test first.
- [x] Rerun complete verification and inspect the branch diff.
- [x] Commit execution loop 3 and prepare handoff.

