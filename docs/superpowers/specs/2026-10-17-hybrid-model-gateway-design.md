# Hybrid Model Gateway — design

**Date:** 2026-10-17

## Goal

Add a provider-neutral model layer so high-volume and sensitive work remains local while selected evidence can be sent, with explicit policy and budget controls, to an OpenAI-compatible cloud or university endpoint.

## Scope

This phase delivers the shared gateway only. It does not add Evidence Lens, cloud-backed UI chat, automatic provider signup, live-provider tests, or automatic fallback.

The gateway supports the existing Ollama behavior, an OpenAI-compatible JSON-chat provider, three policies (local_only, ask_before_cloud, hybrid), evidence packets containing selected excerpts rather than PDF bytes, hard budget checks, and immutable call metadata. Fake transports cover tests; no real credential is required.

## Architecture

litforge.llm becomes the provider-neutral package. ModelProvider defines health checking and structured JSON chat. OllamaClient keeps its current public behavior through compatibility imports. OpenAICompatibleClient uses an injected HTTP transport and reads the key only from its configured environment variable at call time.

ModelRouter returns a route decision or typed confirmation requirement. local_only rejects remote profiles. ask_before_cloud never dispatches remotely without approval. hybrid may select remote only when the task permits cloud use and the evidence packet is not sensitive.

BudgetGuard estimates maximum cost before dispatch and validates actual usage afterward. Missing pricing metadata prevents automatic remote routing. It never silently selects a different provider.

## Data flow

1. A feature builds an EvidencePacket from retrieved chunks.
2. The router evaluates policy, sensitivity, task kind and requested profile.
3. The budget guard rejects calls outside configured limits before network access.
4. The provider returns validated JSON plus usage metadata.
5. LitForge records provider, model, input hash, evidence IDs, usage, latency, cost and status without credentials.
6. The feature consumes the structured result.

## Privacy and credentials

- allow_cloud defaults to false.
- Full PDFs and arbitrary filesystem content are outside the request contract.
- Sensitive packets require local routing unless explicitly approved.
- API keys are read from environment variables; config stores only the variable name.
- Logs redact authorization headers.
- Provider changes and fallback require an explicit route decision.

## Compatibility

Current imports for Llm, LlmError, LlmUnavailable, OllamaClient, FakeLlm and ollama_from_config continue to work. Existing screening and vision behavior remains unchanged.

## Acceptance criteria

- Existing 141-test baseline remains green.
- Unit tests prove all policies, sensitivity rules, budget rejection and missing-key behavior.
- HTTP contract tests prove request/response parsing, usage extraction and redacted errors without network access.
- Integration tests prove screening works through the provider protocol.
- Persistence tests prove call metadata is stored without credentials.
- Ruff and strict mypy pass.

## Three execution loops

1. **Contract loop:** provider types, evidence packet and OpenAI-compatible client; focused tests and fixes.
2. **Policy loop:** routing, privacy, budgets and persistence; integration tests and fixes.
3. **Regression loop:** compatibility, full suite, Ruff, mypy and Copilot efficiency review; fix Important findings and rerun checks.

