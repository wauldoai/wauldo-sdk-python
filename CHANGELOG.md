# Changelog

All notable changes to the Wauldo Python SDK.

## [0.23.0] - 2026-11-23

### Added
- `HttpClient.upload_file(file_path, title=None, *, timeout_ms=None, pii_policy=None)`: upload a PDF, DOCX, image or text file (`POST /v1/upload-file`, multipart, 10 MB at most for the whole request: file, file name and form fields) from the synchronous client, as `AsyncHttpClient.upload_file` already did. A missing or empty file, or one whose request would exceed the limit, raises `ValidationError` before anything is sent (same rule in `AsyncHttpClient.upload_file`). `MockHttpClient.upload_file` mirrors it.
- `Verdict.INSUFFICIENT_CLAIMS`: the verdict of an answer too short or empty to extract a claim to verify.
- `STREAM_LAGGED`: the `state_name` of the notice the server sends when the stream fell behind the run. It is not a workflow state and not terminal: the run goes on, the terminal event still comes, and `get_task` holds the full run. `stream_task` passes it on and keeps reading.
- `TaskVerification.honesty_signals` (`HonestySignals`), `contract_attestation` (`ContractAttestation`), `failure_reason` and `failure_stage`; `Task.share_id`, `Task.share_url` and `Task.race_result` (`RaceResult`, `RaceCandidate`). All optional, `None` when the server does not send them; the new types are exported from `wauldo`.

### Fixed
- `rag_upload` (sync and async) checked the length of the text in characters against the 10 MB limit, but the limit applies to the request body in bytes: a text of 4 million CJK characters (12 MB) passed the check and was refused by the server. The check now measures the body exactly as it is sent (file name included) and raises `ValidationError` before sending. The body is sent as UTF-8 JSON, like the TypeScript and Rust SDKs (it escaped non-ASCII characters, up to 12 bytes each).
- `AsyncHttpClient.upload_file` set its multipart `Content-Type` on the whole client while the upload was in flight, so a concurrent request of the same client could be sent with it instead of JSON. The content type now goes with the upload's own request only. The upload is also encoded like the synchronous one (a random boundary, quotes and line breaks in the file name replaced, an empty file refused), the file is closed after reading, and it is read off the event loop.
- `wauldo upload` on a `.pdf`, `.docx` or image file raised `AttributeError` (the synchronous client had no `upload_file`), and passed a `tags` argument the upload does not take. It now uploads the file; `--tags` is accepted and ignored, as documents are not tagged.

## [0.22.0] - 2026-11-22

### Added
- `AgentsClient.run_and_wait(agent_id, input, ...)`: run an agent and get the settled `Task` back in one call. The server holds the response for up to `MAX_WAIT_SEC` (50 s), so a quick run comes back verified in a single round-trip; a longer one is followed with `wait_for_task`.
- `AgentsClient.run(..., wait_sec=)` and `get_task(..., wait_sec=)`: the server holds the response until the task settles or the delay elapses (`?wait=`). `AgentRunResponse.task` carries the task when `wait_sec` was given.
- `Task.is_settled` and `TaskStatus.PAUSED_FOR_APPROVAL`.
- `AgentsClient(max_retries=3, retry_backoff_sec=0.5)`: transient failures are retried — 429 and 503 on any call, 500/502/504 and connection failures on `GET` only, so a replayed `POST` can never run an agent twice and a replayed cancel never answers 409 for a cancel that worked. `Retry-After` is honoured (capped at 30 s), the wait otherwise doubles from `retry_backoff_sec` (capped at 8 s, with jitter); timeouts are never retried. `max_retries=0` disables.
- `a2a_invoke` goes through the same request path as the other calls (retries, errors).
- `StateTransition.is_terminal` and `StateTransition.terminal_payload()`: the stream's last event (`TASK_COMPLETED` / `TASK_FAILED` / `TASK_CANCELLED`) carries `task_id`, `status`, `result`, `error` and `verification`, so a streaming client has nothing left to fetch.
- `run(..., webhook_url=, max_retries=, auto_share=)` (and the same keywords on `run_and_wait`): the server POSTs the task to `webhook_url` when the run completes or fails (a private or internal address is refused with a 400); `max_retries` sets the retries of the run on the server (default 3, capped at 10 — not the client's HTTP retries); `auto_share=True` publishes the run at creation and `AgentRunResponse.share_id` / `share_url` carry the link.
- `create(..., custom_preset=)`: an inline workflow at creation, used from the agent's first run (it used to need `create` then `create_revision`).
- `pause_task`, `approve_task`, `reject_task` (optional `reason`), returning `HitlResponse(task_id, previous_status, new_status)`: pause a queued or running task for a human decision, then approve it (it is queued and runs again) or reject it (cancelled). The `reason` (at most 1000 characters) is stored with the decision and listed in `Task.decisions` (`TaskDecision(action, at, reason)`), which keeps the 20 most recent decisions. A task left paused 7 days without a decision is cancelled by the server (`Task.error` is `PAUSED_EXPIRED_ERROR`); approving or rejecting it then answers HTTP 409.
- `resume_task(task_id)`: run a failed or cancelled task again, as a new task (`AgentRunResponse` with the new `task_id`).
- `get_task(..., include_journal=False)`: leave the execution journal out of the response. Not sent by default.
- `AgentsClient` and its companions are exported from `wauldo` (`from wauldo import AgentsClient, Task, TaskVerification, StateTransition, ...`), as are `MemoryClient` and `WorkflowsClient`; `from wauldo.agents import AgentsClient` keeps working. The README's agents section uses the root import and says how `AgentsClient` (deployed agents) differs from `AgentClient` (local MCP server).
- `Task.error_info` and `TaskErrorInfo.parse(error)`: a failed task's `error` (`code: message`) split into its stable `code` and its `message` (`TaskErrorCode`, `TASK_ERROR_CODES`: `invalid_preset`, `invalid_agent_config`, `rate_limited`, `timeout`, `model_unavailable`, `run_failed`). An unknown code is kept as sent; an error that does not start with a code has `code=None`. `Task.error` is unchanged.
- `HttpError`: what `AgentsClient`, `MemoryClient` and `WorkflowsClient` raise on an HTTP error status. It is a `ServerError` (so `except WauldoError` catches it) and still a `urllib.error.HTTPError` with the same message, so existing `except HTTPError` code keeps working; `body` holds the response body.
- `TaskTimeoutError` (exported from `wauldo`), with `task_id`, `last_status` and `timeout`: raised when a wait runs out. It is both an `AgentTimeoutError` and a builtin `TimeoutError`, so existing `except TimeoutError` code keeps working. Its message starts as before, then says the task may still be running and how to keep waiting (`wait_for_task(task_id)` again).

### Fixed
- `cancel_task` sent `DELETE /v1/tasks/:id`, which the server maps to *deleting a finished task*: a running task could not be cancelled, and a finished one was erased. It now sends `PUT` (the server's cancel) and returns the cancelled `Task`. The old behaviour is available as `delete_task`.
- `wait_for_task` and `run_and_wait` gave up after 180 s by default: slower runs were reported as timed out although the server went on to finish them. The default is now 360 s (`DEFAULT_WAIT_TIMEOUT_SEC`), which covers ordinary runs; it is not a bound on how long a run may take. For a long run, pass a larger `timeout_sec`, use `stream_task`, or give the run a `webhook_url`. Each request inside the wait is still a 30 s long poll; the HTTP timeout is unchanged.
- `wait_for_task`, `run_and_wait` and the fallback of `stream_task` raised a bare builtin `TimeoutError`, which `except AgentTimeoutError` (the SDK's documented timeout error) did not catch. They raise `TaskTimeoutError`.
- `stream_task` always ends on the terminal event. When the server has no stream for the task (404: still queued, server restarted) or the stream closes before its last event (connection cut, 30-minute limit), the task is long-polled until it settles (`STREAM_FALLBACK_TIMEOUT_SEC`, waiting out the 429/5xx/refused connections of a server restarting) and a terminal event built from it is yielded — condition `"Already finished"`, same payload, with the verification exactly as the server sent it, and `WITHHELD_RESULT` in place of the result when the stream withheld the run's output (`Task.output_withheld`) — so a loop that stops on `is_terminal` always gets the result. It used to raise the 404, or end without the result. A task paused for approval ends the stream without a terminal event: approve it, then stream again.
- `StateTransition.is_terminal` also requires a terminal `condition` (`"terminal"` or `"Already finished"`, `TERMINAL_CONDITIONS`): a workflow state named `TASK_COMPLETED` no longer ends `stream_task` before the run's real terminal event.
- `Task.output_withheld`: true when the run's stream withheld its output.
- README: the failed-task example sent an unknown or missing code to "Fix the agent". Only `invalid_preset` and `invalid_agent_config` need a change to the agent; every other code, including one this version does not know, is retried, as in the other SDKs.

### Changed
- `AsyncClient.get_analytics_traffic()` requires an administrator credential: the endpoint reports traffic across every account of the server, so any other key or token gets HTTP 403 (`ServerError`, `code=403`).
- **BREAKING:** `wait_for_task` returns a task that is `paused_for_approval` (the caller has to act) instead of raising `TimeoutError` on it. It also long-polls the server (`LONG_POLL_SEC`, 30 s per request) instead of sleeping `poll_interval_sec` between polls, so the result arrives the moment it lands; the sleep remains as the fallback against a server that answers at once.

## [0.21.1] - 2026-11-15

### Changed
- README and package metadata: the RapidAPI example now uses the RapidAPI host (a free-plan key does not work on `api.wauldo.com`), the free tier is 500 verifications/month, claims that could not be checked were removed, and benchmarks link to the public weekly page. No code change.

## [0.21.0] - 2026-11-15

### Added
- `rag_upload` (sync + async) and `upload_file` (async): optional `pii_policy` — `"redact"` (server default) or `"keep"` to store contact details as sent. Unknown values raise `ValidationError`.

### Fixed
- `chat_stream` (sync + async): a failure reported by the server mid-stream (`data: {"error": {...}}`) now raises `ServerError` carrying the server's message and error body. It was skipped, so the stream ended as an empty, apparently successful answer.

### Removed
- **Breaking:** `agents_md` and `mcp_json` are removed from agent create/update and from the agent type. The server never used them and now ignores them; requests that still send them are accepted unchanged.
- **Breaking (async `upload_file`):** the `tags` argument is removed (the server never stored it), and `timeout_ms` / `pii_policy` are keyword-only.

## [0.20.0] - 2026-06-10

### Added
- `fact_check` / `guard` (sync + async): optional `query` and `relevance_mode` params — when `query` is provided, the response carries a `relevance` block (`RelevanceResult`: `score`, `verdict` relevant/partial/off_topic, `rationale`) scoring how well the text answers the question, fully decoupled from the factual verdict. `relevance_warning` explains when relevance could not be computed. Only `relevance_mode="fast"` is supported server-side today.

## [0.19.1] - 2026-06-07

### Fixed
- `fact_check` / `guard`: `source_context` is now **required** (was optional). The server rejects a missing context with 400, so the optional form always failed at runtime. Surfaced by a live dogfooding pass.
- `HttpClient` (sync) now exposes `guard()` — it was missing while the async / TS / Rust clients all had it.
- Async client now raises a clear `ImportError` ("pip install 'wauldo[async]'") when `aiohttp` is missing, instead of a raw `ModuleNotFoundError`.

### Added
- Client-side validation on `fact_check` (empty `text`, empty `source_context`, invalid `mode`) — fails fast with `ValueError` / `ValidationError` instead of a server round-trip.

## [0.19.0] - 2026-06-07

### Added
- `HttpClient.fact_check(text, source_context=None, mode="lexical")` (sync) and `HttpClient.verify_citation(text, sources=None, threshold=None)` — public verify surface on the sync client, mirroring the async client. Targets `POST /v1/fact-check` and `POST /v1/verify`.
- Typed response models `FactCheckResponse`, `ClaimResult`, `VerifyCitationResponse`, `CitationDetail`, `UploadFileResponse` — aligned with the real server DTOs. All exported from `wauldo`.

### Fixed
- `AsyncHttpClient` was unimportable: it referenced `FactCheckResponse` / `VerifyCitationResponse` / `UploadFileResponse`, none of which were defined (`ImportError`). All three now exist.

### Changed
- `AsyncHttpClient.fact_check` is the canonical name; `guard()` kept as a deprecated back-compat alias.

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
