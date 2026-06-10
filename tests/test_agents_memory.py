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
        agents_md="---\nname: x\n---",
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
