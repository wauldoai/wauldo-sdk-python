# Changelog

All notable changes to the Wauldo Python SDK.

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
