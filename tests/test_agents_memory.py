"""Unit tests for AgentsClient + MemoryClient.

These tests hit a mock HTTP server via urllib + a stub handler so we
never need a real Wauldo instance running. They validate URL
construction, header injection, body shapes, and error propagation.
"""

from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional

import pytest

from wauldo.agents import AgentsClient
from wauldo.memory import MemoryClient


# ─── Fake server ──────────────────────────────────────────────────────


class _Handler(BaseHTTPRequestHandler):
    server_version = "WauldoMock/1.0"
    requests: List[Dict[str, Any]] = []
    responses: Dict[str, Any] = {}

    def log_message(self, *_args, **_kwargs) -> None:  # silence
        pass

    def _read_body(self) -> Optional[Dict[str, Any]]:
        n = int(self.headers.get("Content-Length", "0"))
        if n == 0:
            return None
        raw = self.rfile.read(n)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def _dispatch(self, method: str) -> None:
        body = self._read_body() if method in ("POST", "PATCH") else None
        # Normalize header names to lowercase so tests don't depend on the
        # capitalization dance between urllib.request and the stdlib
        # http.server (they don't agree on header-name case).
        headers = {k.lower(): v for k, v in self.headers.items()}
        _Handler.requests.append(
            {
                "method": method,
                "path": self.path,
                "headers": headers,
                "body": body,
            }
        )
        key = f"{method} {self.path.split('?')[0]}"
        handler = _Handler.responses.get(key)
        if handler is None:
            self.send_response(404)
            self.end_headers()
            return
        status, resp_body = handler(body) if callable(handler) else handler
        self.send_response(status)
        if resp_body is not None:
            payload = json.dumps(resp_body).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.end_headers()

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self):  # noqa: N802
        self._dispatch("PATCH")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self):  # noqa: N802
        self._dispatch("DELETE")


@pytest.fixture
def server():
    _Handler.requests = []
    _Handler.responses = {}
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", _Handler
    srv.shutdown()
    srv.server_close()


# ─── AgentsClient tests ───────────────────────────────────────────────


def test_agents_create_sends_full_body(server):
    url, handler = server
    handler.responses["POST /v1/agents"] = (
        201,
        {
            "id": "a1",
            "name": "bot",
            "wauldo_toml": "[agent]\n[model]",
            "model_provider": "openrouter",
            "model_name": "qwen",
            "tenant_id": "t",
            "created_at": 0,
            "updated_at": 0,
        },
    )
    client = AgentsClient(base_url=url, api_key="k", tenant="t")
    out = client.create(
        name="bot",
        wauldo_toml="[agent]\nname='x'\n[model]\nprovider='o'\nname='m'",
        description="test",
        preset="general_task",
    )
    assert out.id == "a1"
    req = handler.requests[0]
    assert req["path"] == "/v1/agents"
    assert req["body"]["name"] == "bot"
    assert req["body"]["preset"] == "general_task"
    assert req["headers"]["authorization"] == "Bearer k"
    assert req["headers"]["x-rapidapi-user"] == "t"


def test_agents_create_omits_none_optional_fields(server):
    url, handler = server
    handler.responses["POST /v1/agents"] = (
        201,
        {"id": "a", "tenant_id": "t", "name": "x", "wauldo_toml": "[agent]\n[model]"},
    )
    client = AgentsClient(base_url=url)
    client.create(name="x", wauldo_toml="[agent]\n[model]")
    body = handler.requests[0]["body"]
    # agents_md / mcp_json were removed from the client entirely; the server
    # no longer reads them and the CLI no longer uploads mcp.json secrets.
    assert "agents_md" not in body
    assert "mcp_json" not in body
    assert "preset" not in body


def test_agents_list_query_string(server):
    url, handler = server
    handler.responses["GET /v1/agents"] = (
        200,
        {"agents": [], "pagination": {"total": 0, "limit": 10, "offset": 5}},
    )
    client = AgentsClient(base_url=url)
    client.list(limit=10, offset=5)
    assert handler.requests[0]["path"] == "/v1/agents?limit=10&offset=5"


def test_agents_get(server):
    url, handler = server
    handler.responses["GET /v1/agents/abc"] = (
        200,
        {"id": "abc", "tenant_id": "t", "name": "bot", "wauldo_toml": "[agent]"},
    )
    client = AgentsClient(base_url=url)
    out = client.get("abc")
    assert out.id == "abc"


def test_agents_update_only_sends_provided_fields(server):
    url, handler = server
    handler.responses["PATCH /v1/agents/id1"] = (
        200,
        {"id": "id1", "tenant_id": "t", "name": "bot", "wauldo_toml": "[agent]"},
    )
    client = AgentsClient(base_url=url)
    client.update("id1", description="new")
    body = handler.requests[0]["body"]
    assert body == {"description": "new"}


def test_agents_delete_returns_none(server):
    url, handler = server
    handler.responses["DELETE /v1/agents/xyz"] = (204, None)
    client = AgentsClient(base_url=url)
    assert client.delete("xyz") is None


def test_agents_run_forwards_verification_mode(server):
    url, handler = server
    handler.responses["POST /v1/agents/bot/runs"] = (
        201,
        {"task_id": "tk1", "agent_id": "bot", "status": "queued"},
    )
    client = AgentsClient(base_url=url)
    out = client.run("bot", "Hello", verification_mode="strict")
    assert out.task_id == "tk1"
    assert handler.requests[0]["body"]["verification_mode"] == "strict"


def test_agents_run_forwards_fact_check_mode(server):
    url, handler = server
    handler.responses["POST /v1/agents/bot/runs"] = (
        201,
        {"task_id": "tk1", "agent_id": "bot", "status": "queued"},
    )
    client = AgentsClient(base_url=url)
    client.run("bot", "Hello", fact_check_mode="hybrid")
    assert handler.requests[0]["body"]["fact_check_mode"] == "hybrid"


def test_agents_run_omits_fact_check_mode_when_none(server):
    url, handler = server
    handler.responses["POST /v1/agents/bot/runs"] = (
        201,
        {"task_id": "tk1", "agent_id": "bot", "status": "queued"},
    )
    client = AgentsClient(base_url=url)
    client.run("bot", "Hello")
    assert "fact_check_mode" not in handler.requests[0]["body"]


def test_agents_a2a_sends_trace_header(server):
    url, handler = server
    handler.responses["POST /v1/a2a/target"] = (
        201,
        {"task_id": "tk", "trace": ["caller", "target"], "depth": 2},
    )
    client = AgentsClient(base_url=url)
    out = client.a2a_invoke("target", input="do the thing", trace=["caller"])
    assert out["depth"] == 2
    assert handler.requests[0]["headers"]["x-a2a-trace"] == "caller"
    assert handler.requests[0]["body"]["input"] == "do the thing"


def test_agents_a2a_rejects_empty_input(server):
    url, _ = server
    client = AgentsClient(base_url=url)
    with pytest.raises(ValueError):
        client.a2a_invoke("target", input="")


def test_response_body_size_cap_enforced(server):
    """Oversized responses must raise ValueError, not OOM the client."""
    url, handler = server

    # Build a 12MB response body — above MAX_RESPONSE_SIZE (10MB).
    big_blob = "x" * (12 * 1024 * 1024)
    handler.responses["GET /v1/agents/huge"] = (200, {"blob": big_blob})

    client = AgentsClient(base_url=url)
    with pytest.raises(ValueError, match="too large"):
        client.get("huge")



def test_agents_http_error_propagates(server):
    url, handler = server
    handler.responses["GET /v1/agents/missing"] = (404, {"error": "not found"})
    client = AgentsClient(base_url=url)
    with pytest.raises(urllib.error.HTTPError):
        client.get("missing")


# ─── MemoryClient tests ───────────────────────────────────────────────


def test_memory_set_basic(server):
    url, handler = server
    handler.responses["POST /v1/memory/support"] = (
        200,
        {
            "id": "m1",
            "tenant_id": "t",
            "namespace": "support",
            "key": "k1",
            "value": "hello",
            "tags": [],
            "created_at": 0,
            "updated_at": 0,
        },
    )
    client = MemoryClient(base_url=url)
    out = client.set("support", "k1", "hello")
    assert out["id"] == "m1"
    body = handler.requests[0]["body"]
    assert body["key"] == "k1"
    assert body["value"] == "hello"
    assert "tags" not in body
    assert "embedding" not in body


def test_memory_set_with_tags_and_embedding(server):
    url, handler = server
    handler.responses["POST /v1/memory/ns"] = (200, {"id": "m"})
    client = MemoryClient(base_url=url)
    client.set("ns", "k", "v", tags=["urgent"], embedding=[0.1, 0.2])
    body = handler.requests[0]["body"]
    assert body["tags"] == ["urgent"]
    assert body["embedding"] == [0.1, 0.2]


def test_memory_get(server):
    url, handler = server
    handler.responses["GET /v1/memory/ns/k"] = (200, {"key": "k", "value": "v"})
    client = MemoryClient(base_url=url)
    out = client.get("ns", "k")
    assert out["value"] == "v"


def test_memory_delete(server):
    url, handler = server
    handler.responses["DELETE /v1/memory/ns/k"] = (204, None)
    client = MemoryClient(base_url=url)
    assert client.delete("ns", "k") is None


def test_memory_list(server):
    url, handler = server
    handler.responses["GET /v1/memory/ns"] = (
        200,
        {"entries": [], "pagination": {"total": 0, "limit": 20, "offset": 0}},
    )
    client = MemoryClient(base_url=url)
    out = client.list("ns")
    assert out["pagination"]["limit"] == 20
    assert "limit=20" in handler.requests[0]["path"]


def test_memory_search_query_only(server):
    url, handler = server
    handler.responses["POST /v1/memory/ns/search"] = (
        200,
        {"results": [], "total_matched": 0, "mode": "lexical"},
    )
    client = MemoryClient(base_url=url)
    client.search("ns", query="hello")
    body = handler.requests[0]["body"]
    assert body["query"] == "hello"
    assert "tags" not in body


def test_memory_search_tags_and_query(server):
    url, handler = server
    handler.responses["POST /v1/memory/ns/search"] = (
        200,
        {"results": [], "total_matched": 0, "mode": "lexical"},
    )
    client = MemoryClient(base_url=url)
    client.search("ns", query="q", tags=["urgent", "vip"], limit=5)
    body = handler.requests[0]["body"]
    assert body["query"] == "q"
    assert body["tags"] == ["urgent", "vip"]
    assert body["limit"] == 5


def test_memory_search_requires_query_or_tags(server):
    url, _ = server
    client = MemoryClient(base_url=url)
    with pytest.raises(ValueError):
        client.search("ns", query="", tags=None)


def test_memory_client_injects_tenant_header(server):
    url, handler = server
    handler.responses["POST /v1/memory/ns"] = (200, {})
    client = MemoryClient(base_url=url, api_key="k", tenant="tenant-x")
    client.set("ns", "k", "v")
    hdrs = handler.requests[0]["headers"]
    assert hdrs["authorization"] == "Bearer k"
    assert hdrs["x-rapidapi-user"] == "tenant-x"


# ─── Wait / one-call result (0.22.0) ───────────────────────────────────────


def _task_json(status: str, **extra) -> Dict[str, Any]:
    base = {
        "task_id": "tk1",
        "tenant_id": "tn",
        "status": status,
        "prompt": "Hello",
        "created_at": 0,
        "updated_at": 0,
    }
    base.update(extra)
    return base


def test_agents_run_with_wait_adds_the_query_and_parses_the_task(server):
    url, handler = server
    handler.responses["POST /v1/agents/bot/runs"] = (
        201,
        {
            "task_id": "tk1",
            "agent_id": "bot",
            "status": "completed",
            "created_at": 0,
            "task": _task_json("completed", result="42"),
        },
    )
    client = AgentsClient(base_url=url)
    out = client.run("bot", "Hello", wait_sec=30)
    assert handler.requests[0]["path"] == "/v1/agents/bot/runs?wait=30"
    assert out.status == "completed"
    assert out.task is not None and out.task.result == "42"
    assert out.task.is_settled


def test_agents_run_without_wait_sends_no_query_and_no_task(server):
    url, handler = server
    handler.responses["POST /v1/agents/bot/runs"] = (
        201,
        {"task_id": "tk1", "agent_id": "bot", "status": "queued"},
    )
    client = AgentsClient(base_url=url)
    out = client.run("bot", "Hello")
    assert handler.requests[0]["path"] == "/v1/agents/bot/runs"
    assert out.task is None


def test_agents_wait_is_clamped_to_the_server_maximum(server):
    url, handler = server
    handler.responses["GET /v1/tasks/tk1"] = (200, _task_json("running"))
    client = AgentsClient(base_url=url)
    client.get_task("tk1", wait_sec=3600)
    assert handler.requests[0]["path"] == "/v1/tasks/tk1?wait=50"


def test_wait_for_task_long_polls_and_returns_the_settled_task(server):
    url, handler = server
    calls: List[int] = []

    def get(_body):
        calls.append(1)
        return (200, _task_json("running" if len(calls) < 2 else "completed"))

    handler.responses["GET /v1/tasks/tk1"] = get
    client = AgentsClient(base_url=url)
    task = client.wait_for_task("tk1", timeout_sec=10, poll_interval_sec=0.01)
    assert task.status == "completed"
    assert all("?wait=" in r["path"] for r in handler.requests)
    # The wait asked for never exceeds what is left of the timeout.
    assert handler.requests[0]["path"] == "/v1/tasks/tk1?wait=10"


def test_wait_for_task_returns_a_task_paused_for_approval(server):
    url, handler = server
    handler.responses["GET /v1/tasks/tk1"] = (200, _task_json("paused_for_approval"))
    client = AgentsClient(base_url=url)
    task = client.wait_for_task("tk1", timeout_sec=10, poll_interval_sec=0.01)
    assert task.status == "paused_for_approval"
    assert task.is_settled and not task.is_done


def test_run_and_wait_returns_the_task_in_one_round_trip(server):
    url, handler = server
    handler.responses["POST /v1/agents/bot/runs"] = (
        201,
        {
            "task_id": "tk1",
            "agent_id": "bot",
            "status": "completed",
            "created_at": 0,
            "task": _task_json("completed", result="42"),
        },
    )
    client = AgentsClient(base_url=url)
    task = client.run_and_wait("bot", "Hello", verification_mode="strict")
    assert task.result == "42"
    assert len(handler.requests) == 1, "settled within the wait: no poll needed"
    assert handler.requests[0]["path"] == "/v1/agents/bot/runs?wait=50"
    assert handler.requests[0]["body"]["verification_mode"] == "strict"


def test_run_and_wait_falls_back_to_polling_when_the_run_is_longer(server):
    url, handler = server
    handler.responses["POST /v1/agents/bot/runs"] = (
        201,
        {
            "task_id": "tk1",
            "agent_id": "bot",
            "status": "running",
            "created_at": 0,
            "task": _task_json("running"),
        },
    )
    handler.responses["GET /v1/tasks/tk1"] = (200, _task_json("completed", result="late"))
    client = AgentsClient(base_url=url)
    task = client.run_and_wait("bot", "Hello", timeout_sec=10)
    assert task.result == "late"
    assert [r["method"] for r in handler.requests] == ["POST", "GET"]
    assert handler.requests[1]["path"].startswith("/v1/tasks/tk1?wait=")


def test_run_and_wait_times_out_when_the_task_never_settles(server):
    url, handler = server
    handler.responses["POST /v1/agents/bot/runs"] = (
        201,
        {"task_id": "tk1", "agent_id": "bot", "status": "running", "created_at": 0},
    )
    handler.responses["GET /v1/tasks/tk1"] = (200, _task_json("running"))
    client = AgentsClient(base_url=url)
    with pytest.raises(TimeoutError):
        client.run_and_wait("bot", "Hello", timeout_sec=0.05)


def test_wait_is_bounded_by_the_client_http_timeout(server):
    url, handler = server
    handler.responses["GET /v1/tasks/tk1"] = (200, _task_json("completed"))
    client = AgentsClient(base_url=url, timeout=12.0)
    client.wait_for_task("tk1", timeout_sec=60)
    # 30 s long poll, but the client would abort at 12 s: ask for 7 s.
    assert handler.requests[0]["path"] == "/v1/tasks/tk1?wait=7"


def test_wait_for_task_past_its_deadline_reads_once_without_holding(server):
    url, handler = server
    handler.responses["GET /v1/tasks/tk1"] = (200, _task_json("running"))
    client = AgentsClient(base_url=url)
    with pytest.raises(TimeoutError):
        client.wait_for_task("tk1", timeout_sec=0.0, poll_interval_sec=0.01)
    # One plain read to report the status, no server-side hold past the deadline.
    assert [r["path"] for r in handler.requests] == ["/v1/tasks/tk1"]


def test_cancel_task_sends_put_and_returns_the_cancelled_task(server):
    url, handler = server
    handler.responses["PUT /v1/tasks/tk1"] = (200, _task_json("cancelled"))
    client = AgentsClient(base_url=url)
    task = client.cancel_task("tk1")
    assert task.status == "cancelled"
    assert handler.requests[0]["method"] == "PUT"


def test_delete_task_sends_delete(server):
    url, handler = server
    handler.responses["DELETE /v1/tasks/tk1"] = (204, None)
    client = AgentsClient(base_url=url)
    assert client.delete_task("tk1") is None
    assert handler.requests[0]["method"] == "DELETE"
