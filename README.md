# Wauldo Python SDK

[![PyPI](https://img.shields.io/pypi/v/wauldo.svg)](https://pypi.org/project/wauldo/)
[![Downloads](https://img.shields.io/pypi/dm/wauldo.svg)](https://pypi.org/project/wauldo/)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

> **Verified AI answers from your documents.** Every response includes source citations, a confidence score and an audit trail — and when your documents don't hold the answer, it says so.

Official Python SDK for the [Wauldo API](https://wauldo.com) — answers grounded in your documents, with sources, a confidence score and smart model routing.

## Why Wauldo?

- **Grounded answers** — every answer is checked against your source documents, with citations
- **Smart model routing** — auto-selects the cheapest model that meets quality
- **One API, 7+ providers** — OpenAI, Anthropic, Google, Qwen, Meta, Mistral, DeepSeek with automatic fallback
- **OpenAI-compatible** — swap your `base_url`, keep your existing code
- **Full audit trail** — confidence score, grounded status, model used, latency on every response

## Quick Start

```python
from wauldo import HttpClient

client = HttpClient(base_url="https://api.wauldo.com", api_key="YOUR_API_KEY")

reply = client.chat_simple("auto", "What is Python?")
print(reply)
```

> Got your key from RapidAPI? Use the RapidAPI host instead — see [RapidAPI](#rapidapi).

## Installation

```bash
pip install wauldo
```

**Requirements:** Python 3.9+

## Features

### Chat Completions

```python
from wauldo import HttpClient, ChatRequest, HttpChatMessage

client = HttpClient(base_url="https://api.wauldo.com", api_key="YOUR_API_KEY")

request = ChatRequest(
    model="auto",
    messages=[
        HttpChatMessage.system("You are a helpful assistant."),
        HttpChatMessage.user("Explain Python decorators"),
    ],
)
response = client.chat(request)
print(response.choices[0].message.content)
```

### RAG — Upload & Query

```python
# Upload a document
upload = client.rag_upload(content="Contract text here...", filename="contract.txt")
print(f"Indexed {upload.chunks_count} chunks")

# Or a file (PDF, DOCX, image, text; 10 MB max for the whole request) — text extraction happens server-side
upload = client.upload_file("contract.pdf", title="Q3 Contract")
print(f"Indexed {upload.chunks_count} chunks ({upload.content_type})")

# Query with verified answer
result = client.rag_query("What are the payment terms?")
print(f"Answer: {result.answer}")
print(f"Confidence: {result.get_confidence():.0%}")
print(f"Grounded: {result.audit.grounded}")
for source in result.sources:
    print(f"  Source ({source.score:.0%}): {source.content[:80]}")
```

### Guard — Fact-Check Any Text

```python
# Verify a response against ground-truth sources
result = client.fact_check(
    text="Returns are accepted within 60 days.",
    source_context="Our return policy: 14 days.",
)
print(result.verdict)  # rejected
print(result.action)   # block

# Optional: score relevance to the original question (decoupled from factuality)
result = client.fact_check(
    text="Rust was first released in 2010 by Mozilla Research.",
    source_context="Rust is a systems language released in 2010 by Mozilla Research.",
    query="What year was Rust released?",
)
print(result.verdict)  # verified
if result.relevance:   # None when relevance could not be computed (see relevance_warning)
    print(result.relevance.verdict)  # relevant | partial | off_topic
    print(result.relevance.score)    # cosine similarity — interpret through verdict, not absolute value
```

A response can be fully **verified** against sources and still **off-topic**
for the question asked — the `relevance` block never influences the factual
verdict. Currently only `relevance_mode="fast"` (embedding cosine) is supported.

### Deployed Agents — create, run, stream

`AgentsClient` (plural) runs your deployed agents over the REST API. It is not
`AgentClient`, the client of the local MCP server.

```python
from wauldo import AgentsClient

agents = AgentsClient(base_url="https://api.wauldo.com", api_key="YOUR_API_KEY", tenant="my-tenant")
# Transient failures are retried (max_retries=3, Retry-After honoured): 429/503 on any call,
# 500/502/504 and connection errors on GET only, so a POST never runs an agent twice.

agent = agents.create(
    "support-bot",
    '[agent]\nname = "support-bot"\n[model]\nprovider = "openrouter"\nname = "auto"',
    description="Answers refund questions",
    preset="general_task",
)

# One call: the server holds the response while the run completes,
# so a quick run comes back verified in a single round-trip.
task = agents.run_and_wait(agent.id, "Can I return a shirt 30 days after purchase?")
print(task.result)                        # The answer
print(task.verification.verdict)          # SAFE | UNVERIFIED | BLOCK | ...
print(task.verification.support_score)    # 0.0 – 1.0

# Need progress events? Start the run, then stream each workflow state as it completes.
run = agents.run(agent.id, "Can I return a shirt 30 days after purchase?")
for event in agents.stream_task(run.task_id):
    # STREAM_LAGGED is a notice, not a state: the stream fell behind, the run goes on and
    # the terminal event still comes (agents.get_task holds every state of the run).
    print(f"  {event.state_name}: {event.duration_ms}ms")
    if event.is_terminal:  # last event: result + verification, nothing left to fetch
        payload = event.terminal_payload()  # None when the server withheld the output
        print(payload["result"] if payload else event.condition)

# Batch job: no polling, the server POSTs the task to your webhook when the run ends.
run = agents.run(agent.id, "Summarise ticket #4521", webhook_url="https://example.com/hooks/wauldo")

# A public link to the run, at creation.
run = agents.run(agent.id, "Summarise ticket #4521", auto_share=True)
print(run.share_url)

# Waiting has a limit (timeout_sec, default 360 s: enough for ordinary runs); the task
# keeps running past it. For long runs, pass a larger timeout_sec, stream, or use a webhook.
from wauldo import TaskTimeoutError

try:
    task = agents.wait_for_task(run.task_id, timeout_sec=60)
except TaskTimeoutError as e:  # also a TimeoutError
    print(f"{e.task_id} still {e.last_status}")
    task = agents.wait_for_task(e.task_id)  # keep waiting

# Human approval: pause a task, then approve (it runs again) or reject (cancelled).
# The reason is stored with the decision (task.decisions). A task left paused 7 days
# without a decision is cancelled; resume_task() runs it again as a new task.
agents.pause_task(run.task_id, reason="check the refund amount")
agents.approve_task(run.task_id)   # or agents.reject_task(run.task_id)

# An inline workflow at creation, used from the first run.
flow_agent = agents.create("triage-bot", toml, custom_preset=my_workflow_contract)

# A failed task is not an exception: its error is "code: message", parsed for you.
if task.status == "failed":
    info = task.error_info                 # TaskErrorInfo(code, message)
    if info.code in ("invalid_preset", "invalid_agent_config"):
        print(f"Fix the agent: {info.message}")
    else:                                  # any other code, known or new: transient
        retry = agents.resume_task(task.task_id)  # run it again later
```

| `error_info.code` | Meaning | What to do |
|---|---|---|
| `invalid_preset` | The agent names a workflow preset that does not exist | Fix the agent |
| `invalid_agent_config` | The agent's inline workflow could not be loaded | Fix the agent |
| `rate_limited` | The model service is saturated | Retry in a few minutes |
| `timeout` | The run did not finish within its time limit | Retry, or simplify the request |
| `model_unavailable` | The model service is temporarily unavailable | Retry later |
| `run_failed` | The run failed for another reason | Retry later |

New codes may be added: handle an unknown one like `run_failed`. An error that
does not start with a code (a task cancelled after 7 days without an approval
decision) has `code` set to `None` and the whole error in `message`.

### Streaming (SSE)

```python
from wauldo import ChatRequest, HttpChatMessage

request = ChatRequest(model="auto", messages=[HttpChatMessage.user("Hello!")])
for chunk in client.chat_stream(request):
    print(chunk, end="", flush=True)
```

### Conversation Helper

```python
conv = client.conversation(system="You are an expert on Python.", model="auto")
reply = conv.say("What are list comprehensions?")
follow_up = conv.say("Give me a nested example")
```

## Error Handling

```python
from wauldo import WauldoError, ServerError, AgentTimeoutError

try:
    response = client.chat(ChatRequest.quick("auto", "Hello"))
except ServerError as e:
    print(f"Server error: {e}")
except AgentTimeoutError:
    print("Request timed out")
except WauldoError as e:
    print(f"SDK error: {e}")
```

`AgentsClient`, `MemoryClient` and `WorkflowsClient` raise `HttpError` on an
HTTP error status: a `ServerError` (so a `WauldoError`) that is also a
`urllib.error.HTTPError`, so existing `except HTTPError` code keeps working.
A wait that runs out raises `TaskTimeoutError` (an `AgentTimeoutError`).

```python
from wauldo import AgentsClient, HttpError, TaskTimeoutError

try:
    task = agents.run_and_wait(agent_id, "Qualify acme.com")
except HttpError as e:
    print(e.code, e.body)        # HTTP status, and the server's JSON error
except TaskTimeoutError as e:
    task = agents.wait_for_task(e.task_id)
```

## RapidAPI

Keys from the free RapidAPI plan work on the RapidAPI host, not on `api.wauldo.com` (Enterprise keys):

```python
client = HttpClient(
    base_url="https://smart-rag-api.p.rapidapi.com",
    headers={
        "X-RapidAPI-Key": "YOUR_RAPIDAPI_KEY",
        "X-RapidAPI-Host": "smart-rag-api.p.rapidapi.com",
    },
)
```

Get your free API key (500 verifications/month): [RapidAPI](https://rapidapi.com/binnewzzin/api/smart-rag-api)

## Links

- [Website](https://wauldo.com)
- [Documentation](https://wauldo.com/docs)
- [Live Demo](https://wauldo.com/demo)
- [Benchmarks](https://wauldo.com/benchmarks)
- [Pricing](https://wauldo.com/pricing)

## Contributing

Found a bug? Have a feature request? [Open an issue](https://github.com/wauldoai/wauldo-sdk-python/issues).

## License

MIT — see [LICENSE](./LICENSE)
