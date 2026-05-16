# Changelog

All notable changes to the Wauldo Python SDK.

## [0.18.0] - 2026-05-16

### Added
- `WorkflowsClient.update(workflow_id, name, start_at, states, description=None)` — edit a workflow in place via `PATCH /v1/workflows/:id`. Body shape identical to `create`. Server keeps id/tenant_id/created_at, bumps `updated_at` and the monotonic `version`. Closes the SDK-parity gap left when the PATCH endpoint shipped in monorepo commit `54d533b` without the matching SDK method.

## [0.17.0] - 2026-05-14

### Added
- `wauldo.workflows.WorkflowsClient` — six methods covering the Wauldo Workflow Runtime surface (`create`, `list`, `get`, `delete`, `start_run`, `get_run`) plus a `wait_for_run` polling helper. Mirrors the `/v1/workflows*` endpoints shipped in rev 63 (Phase 1+2 runtime: Task / Choice / Wait / Pass / Fail / Succeed state machines).

## [0.16.0] - 2026-05-08

### Added
- **`auto_share=True`** on `wauldo.openai.with_verification(...)`. When set, every verified completion is auto-published as a public share BEFORE returning, and the URL lands on `response.wauldo.share_url` (or `stream.wauldo.share_url` for streaming responses). Honours the per-tenant cap + 30-day TTL (free tier).
- **`tenant=...`** parameter on `with_verification` — passes through to the upstream as `X-RapidAPI-User`. Required when the upstream Wauldo instance has `RAPIDAPI_SECRET` set (production).
- `WauldoFactCheck.share_url: Optional[str]` field surfaced on the response.

### Changed
- The middleware now calls `POST /v1/external-runs` instead of `POST /v1/fact-check`. The new endpoint runs the same fact-check engine + persists the run as a Task + optionally publishes — single round-trip for the full happy path. Fixes a v0.14 / 0.15 wire bug : the middleware was sending `{answer, context}` but the legacy endpoint expected `{text, source_context}`, so prod calls would have surfaced as 400 had they reached real traffic.
- `WauldoFactCheck.from_response` reads `support_score` / `halluc_rate` directly (Task-style verdict shape) instead of mapping from `trust_score` / `hallucination_rate`. Test fixtures using the old keys must be updated.

## [0.15.0] - 2026-05-08

### Changed
- **`wauldo.openai` middleware now supports streaming.** When `stream=True`, the wrapper buffers every chunk's delta content, yields chunks unchanged to the consumer, and computes the verdict ONCE the stream is fully consumed. Access the verdict on the stream object after the loop : `stream.wauldo.verdict`. Async clients (`AsyncOpenAI`) work the same via `async for`.
- Breaking : streaming consumers that previously read `response.wauldo` (always `None` in 0.14.0) should now read `stream.wauldo` AFTER iteration. Consumers who break out of the loop early will see `stream.wauldo == None` (a partial answer would be misleading to fact-check, so the absence is intentional).
- The stream wrapper passes through every other attribute access (`stream.close()`, `stream.response`, etc.) to the underlying iterator unchanged — existing tooling that reads those keeps working.

## [0.14.0] - 2026-05-08

### Added
- `wauldo.openai` — drop-in middleware for the OpenAI SDK. `with_verification(client, wauldo_api_key=...)` wraps any `OpenAI` / `AsyncOpenAI` client so every `chat.completions.create()` call returns a completion with a `.wauldo` attribute carrying `verdict`, `support_score`, `halluc_rate`, `claims_count`, `fact_check_mode`. Fail-open by default (Wauldo down → `response.wauldo.error` set, OpenAI response untouched). Streaming is pass-through (no verdict on a stream — v2 feature). Install via `pip install 'wauldo[openai]'`.

## [0.13.0] - 2026-05-08

### Added
- `AgentsClient.share_task(task_id) -> ShareResponse` — publish a verified run as a public URL (`https://wauldo.com/r/<id>`). Idempotent ; free tier gets a 30-day TTL, paid tenants get `expires_at = None`.
- `AgentsClient.unshare_task(task_id)` — revoke a published run.
- `wauldo.agents_types.ShareResponse` dataclass.

## [0.12.0] - 2026-05-05

### Added
- `AgentsClient.create_revision()`, `list_revisions()`, `get_revision()`, `set_active_revision()` — ECS-style immutable revisions for `custom_preset` agents (O(1) rollback, no LLM cost).
- Types: `AgentRevision`, `CreateRevisionResponse`, `ListRevisionsResponse` in `wauldo.agents_types`.

## [0.11.0] - 2026-04-30

### Added
- `src/wauldo/agents.py` + `agents_types.py` — Tasks API client.
- `src/wauldo/memory.py` — agent memory bindings.
- `src/wauldo/async_client.py` + `async_transport.py` — async HTTP path.
- `src/wauldo/cli.py` — CLI helper.
- `tests/test_agents_memory.py`, `test_agents_types.py`.
- `PUBLISH.md` — release procedure for PyPI.

### Changed
- Repository URL migrated to github.com/wauldoai.

## [0.1.0] - 2026-03-16

### Added
- `HttpClient` — REST API client (OpenAI-compatible)
  - `chat()`, `chat_simple()`, `chat_stream()`, `list_models()`, `embeddings()`
  - `rag_upload()`, `rag_query()`, `rag_ask()`
  - `orchestrate()`, `orchestrate_parallel()`
- `AgentClient` / `AsyncAgentClient` — MCP client (sync + async)
  - `reason()`, `extract_concepts()`, `plan_task()`
  - `chunk_document()`, `retrieve_context()`, `summarize()`
  - `search_knowledge()`, `add_to_knowledge()`
- `Conversation` — automatic chat history management
- `MockHttpClient` — offline testing without server
- Retry with exponential backoff (429/503/network errors)
- Structured logging via `logging.getLogger("wauldo")`
- Pydantic v2 response validation on all endpoints
- Full type hints + py.typed (PEP 561)
- Per-request `timeout_ms` override on `chat()`, `chat_simple()`, `rag_upload()`
- Event hooks: `on_request`, `on_response`, `on_error` callbacks
