"""Retries on AgentsClient._request and the terminal-event helpers."""

from __future__ import annotations

import json
import threading
import urllib.error
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Deque, Dict, List, Tuple

import pytest

from wauldo import agents as agents_mod
from wauldo.agents import AgentsClient, _retry_delay
from wauldo.agents_types import StateTransition

Step = Tuple[int, Dict[str, str], object]  # status, headers, json body or None

TASK = {
    "task_id": "t1",
    "tenant_id": "tn",
    "status": "completed",
    "prompt": "hi",
    "created_at": 1,
    "updated_at": 2,
}
RUN = {"task_id": "t1", "agent_id": "a", "status": "queued", "created_at": 0}


class _Handler(BaseHTTPRequestHandler):
    plan: Dict[str, Deque[Step]] = {}
    hits: List[str] = []

    def log_message(self, *_a, **_k) -> None:
        pass

    def _serve(self, method: str) -> None:
        n = int(self.headers.get("Content-Length", "0"))
        if n:
            self.rfile.read(n)
        key = f"{method} {self.path.split('?')[0]}"
        _Handler.hits.append(key)
        steps = _Handler.plan[key]
        status, headers, body = steps[0] if len(steps) == 1 else steps.popleft()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        if body is not None:
            payload = json.dumps(body).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):  # noqa: N802
        self._serve("GET")

    def do_POST(self):  # noqa: N802
        self._serve("POST")


@pytest.fixture
def server(monkeypatch):
    _Handler.plan = {}
    _Handler.hits = []
    slept: List[float] = []
    monkeypatch.setattr(agents_mod, "_sleep", slept.append)
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", _Handler, slept
    srv.shutdown()
    srv.server_close()


def test_a_503_is_retried_until_the_server_answers(server):
    url, handler, slept = server
    handler.plan["GET /v1/tasks/t1"] = deque([(503, {}, None), (503, {}, None), (200, {}, TASK)])
    task = AgentsClient(base_url=url, retry_backoff_sec=0.01).get_task("t1")
    assert task.task_id == "t1"
    assert len(handler.hits) == 3
    assert len(slept) == 2


def test_a_500_on_post_is_not_retried(server):
    url, handler, _ = server
    handler.plan["POST /v1/agents/a/runs"] = deque([(500, {}, None)])
    with pytest.raises(urllib.error.HTTPError) as exc:
        AgentsClient(base_url=url).run("a", "hi")
    assert exc.value.code == 500
    assert len(handler.hits) == 1


def test_a_429_on_post_is_retried_after_the_header_delay(server):
    url, handler, slept = server
    handler.plan["POST /v1/agents/a/runs"] = deque(
        [(429, {"Retry-After": "2"}, None), (201, {}, RUN)]
    )
    out = AgentsClient(base_url=url).run("a", "hi")
    assert out.task_id == "t1"
    assert len(handler.hits) == 2
    assert slept == [2.0]


def test_max_retries_zero_fails_on_the_first_503(server):
    url, handler, _ = server
    handler.plan["GET /v1/tasks/t1"] = deque([(503, {}, None)])
    with pytest.raises(urllib.error.HTTPError) as exc:
        AgentsClient(base_url=url, max_retries=0).get_task("t1")
    assert exc.value.code == 503
    assert len(handler.hits) == 1


def test_a_refused_connection_is_retried_on_get_only(monkeypatch):
    slept: List[float] = []
    monkeypatch.setattr(agents_mod, "_sleep", slept.append)
    # Nothing listens on port 1: every attempt is refused.
    client = AgentsClient(base_url="http://127.0.0.1:1", max_retries=2, timeout=2)
    with pytest.raises(urllib.error.URLError):
        client.get_task("t1")
    assert len(slept) == 2
    slept.clear()
    with pytest.raises(urllib.error.URLError):
        client.run("a", "hi")
    assert slept == []


def test_retry_delay_honours_retry_after_up_to_its_cap():
    assert _retry_delay(0, 0.5, "2") == 2.0
    assert _retry_delay(0, 0.5, "600") == 30.0
    assert 0.5 <= _retry_delay(0, 0.5, "soon") < 0.75
    assert 1.0 <= _retry_delay(1, 0.5, None) < 1.25
    assert 8.0 <= _retry_delay(9, 0.5, None) < 8.25


def test_terminal_payload_is_read_off_the_last_event():
    terminal = StateTransition.from_dict(
        {
            "state_name": "TASK_COMPLETED",
            "to_state": None,
            "condition": "terminal",
            "raw_output": json.dumps({"task_id": "t1", "status": "completed", "result": "30 days"}),
            "timestamp": 0,
            "success": True,
        }
    )
    assert terminal.is_terminal
    assert terminal.terminal_payload()["result"] == "30 days"
    ordinary = StateTransition.from_dict(
        {"state_name": "Analysis", "to_state": "Answer", "raw_output": "{}", "timestamp": 0}
    )
    assert not ordinary.is_terminal
    assert ordinary.terminal_payload() is None
    withheld = StateTransition.from_dict(
        {"state_name": "TASK_COMPLETED", "to_state": None, "raw_output": ""}
    )
    assert withheld.terminal_payload() is None
