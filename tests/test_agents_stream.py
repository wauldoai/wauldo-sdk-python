"""``AgentsClient.stream_task`` always ends on the terminal event:
when the server has no stream (404) or the stream closes before its last
event, the task is read and the terminal event is built from it."""

from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Tuple, Union

import pytest

import wauldo.agents as agents_module
from wauldo.agents import WITHHELD_RESULT, AgentsClient

Reply = Tuple[int, str, str]  # status, content type, body

VERIFICATION = {
    "verdict": "SAFE",
    "hallucination_rate": 0.0,
    "confidence": 0.9,
    "trust_score": 1.0,
    "verification_source": "sources",
    "claims": [{"text": "30 days", "supported": True, "confidence": 0.9}],
}


def _task(status: str, **extra: object) -> Dict[str, object]:
    task: Dict[str, object] = {
        "task_id": "t1",
        "tenant_id": "tn",
        "status": status,
        "prompt": "q",
        "created_at": 1,
        "updated_at": 2,
        "result": "30 days" if status == "completed" else None,
        "verification": VERIFICATION if status == "completed" else None,
    }
    task.update(extra)
    return task


def _json(status: int, body: object) -> Reply:
    return status, "application/json", json.dumps(body)


def _sse(*events: Dict[str, object]) -> Reply:
    frames = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return 200, "text/event-stream", ":keep-alive\n\n" + frames


def _transition(name: str) -> Dict[str, object]:
    return {
        "state_name": name,
        "to_state": "NEXT",
        "condition": "ok",
        "raw_output": "draft",
        "timestamp": 1,
        "success": True,
    }


class _Handler(BaseHTTPRequestHandler):
    # A list of replies is served in order, its last one repeated.
    routes: Dict[str, Union[Reply, List[Reply]]] = {}
    hits: List[str] = []

    def log_message(self, *_a, **_k) -> None:
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        _Handler.hits.append(path)
        route = _Handler.routes[path]
        if isinstance(route, list):
            status, ctype, body = route.pop(0) if len(route) > 1 else route[0]
        else:
            status, ctype, body = route
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype.replace(";cut", ""))
        if ctype.endswith(";cut"):
            # A chunk announced longer than what is sent before the connection
            # closes: the read fails mid-stream (IncompleteRead), as on a reset.
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(f"{len(payload) + 4096:x}\r\n".encode() + payload)
            return
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server():
    _Handler.routes = {}
    _Handler.hits = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield AgentsClient(base_url=f"http://127.0.0.1:{srv.server_address[1]}"), _Handler
    srv.shutdown()
    srv.server_close()


def test_stream_404_falls_back_to_the_task_and_ends_on_its_terminal_event(server):
    client, handler = server
    handler.routes = {
        "/v1/tasks/t1/stream": _json(404, {"error": "No active stream for task: t1"}),
        "/v1/tasks/t1": _json(200, _task("completed")),
    }
    # Without the fallback, the 404 was raised as an HTTPError here.
    events = list(client.stream_task("t1"))
    assert len(events) == 1
    last = events[0]
    assert last.is_terminal
    assert last.state_name == "TASK_COMPLETED"
    assert last.condition == "Already finished"
    assert last.success
    payload = last.terminal_payload()
    assert payload is not None
    assert payload["status"] == "completed"
    assert payload["result"] == "30 days"
    assert payload["verification"]["verdict"] == "SAFE"
    assert payload["verification"]["claims"][0]["text"] == "30 days"


def test_stream_closed_before_its_terminal_event_is_completed_from_the_task(server):
    client, handler = server
    handler.routes = {
        "/v1/tasks/t1/stream": _sse(_transition("PLAN")),
        "/v1/tasks/t1": _json(200, _task("failed", error="boom")),
    }
    events = list(client.stream_task("t1"))
    # Without the fallback, the generator ended on PLAN, with no result.
    assert [e.state_name for e in events] == ["PLAN", "TASK_FAILED"]
    assert events[-1].is_terminal
    assert not events[-1].success
    assert events[-1].terminal_payload()["error"] == "boom"


def test_stream_cut_mid_body_is_completed_from_the_task(server):
    # The read raises mid-stream (truncated chunk): without the except
    # branch in stream_task, IncompleteRead reached the caller.
    client, handler = server
    status, ctype, body = _sse(_transition("PLAN"))
    handler.routes = {
        "/v1/tasks/t1/stream": (status, ctype + ";cut", body),
        "/v1/tasks/t1": _json(200, _task("completed")),
    }
    events = list(client.stream_task("t1"))
    assert [e.state_name for e in events] == ["PLAN", "TASK_COMPLETED"]
    assert events[-1].terminal_payload()["result"] == "30 days"


def test_stream_ending_on_its_terminal_event_does_not_read_the_task(server):
    client, handler = server
    terminal = {
        "state_name": "TASK_COMPLETED",
        "to_state": None,
        "condition": "terminal",
        "raw_output": json.dumps({"task_id": "t1", "status": "completed", "result": "ok"}),
        "timestamp": 2,
        "success": True,
    }
    handler.routes = {"/v1/tasks/t1/stream": _sse(_transition("PLAN"), terminal)}
    events = list(client.stream_task("t1"))
    assert [e.condition for e in events] == ["ok", "terminal"]
    assert handler.hits == ["/v1/tasks/t1/stream"]


def test_stream_of_a_task_paused_for_approval_ends_without_a_terminal_event(server):
    client, handler = server
    handler.routes = {
        "/v1/tasks/t1/stream": _json(404, {"error": "No active stream"}),
        "/v1/tasks/t1": _json(200, _task("paused_for_approval")),
    }
    assert list(client.stream_task("t1")) == []


def test_stream_of_an_unknown_task_still_raises_the_404(server):
    client, handler = server
    handler.routes = {
        "/v1/tasks/t1/stream": _json(404, {"error": "Task not found: t1"}),
        "/v1/tasks/t1": _json(404, {"error": "Task not found: t1"}),
    }
    with pytest.raises(urllib.error.HTTPError) as err:
        list(client.stream_task("t1"))
    assert err.value.code == 404


def test_a_workflow_state_named_like_a_status_does_not_end_the_stream(server):
    client, handler = server
    # A custom workflow whose last state is called TASK_COMPLETED: the
    # sequential executor sends it with no next state.
    workflow_state = {
        "state_name": "TASK_COMPLETED",
        "to_state": None,
        "condition": "Sequential execution",
        "raw_output": '{"summary": "draft"}',
        "timestamp": 1,
        "success": True,
    }
    terminal = {
        "state_name": "TASK_COMPLETED",
        "to_state": None,
        "condition": "terminal",
        "raw_output": json.dumps({"task_id": "t1", "status": "completed", "result": "ok"}),
        "timestamp": 2,
        "success": True,
    }
    handler.routes = {"/v1/tasks/t1/stream": _sse(workflow_state, terminal)}
    events = list(client.stream_task("t1"))
    # Without the condition check, the generator stopped on the workflow state.
    assert [e.condition for e in events] == ["Sequential execution", "terminal"]
    assert not events[0].is_terminal
    assert events[-1].terminal_payload()["result"] == "ok"


def test_fallback_of_a_withheld_run_carries_the_marker_not_the_answer(server):
    client, handler = server
    handler.routes = {
        "/v1/tasks/t1/stream": _json(404, {"error": "No active stream"}),
        "/v1/tasks/t1": _json(
            200, _task("completed", output_withheld=True, verification=None)
        ),
    }
    events = list(client.stream_task("t1"))
    assert events[-1].terminal_payload()["result"] == WITHHELD_RESULT
    assert "30 days" not in events[-1].raw_output


def test_fallback_keeps_the_verification_the_server_sent(server):
    client, handler = server
    verification = {
        "verdict": "SOMETHING_NEW",
        "hallucination_rate": 1.0,
        "confidence": 0.0,
        "trust_score": 0.0,
        "verification_source": "sources",
        "claims": [],
        "failure_reason": "injection blocked at stage 2",
        "failure_stage": "guard",
    }
    handler.routes = {
        "/v1/tasks/t1/stream": _json(404, {"error": "No active stream"}),
        "/v1/tasks/t1": _json(200, _task("failed", verification=verification)),
    }
    events = list(client.stream_task("t1"))
    assert events[-1].terminal_payload()["verification"] == verification


def test_fallback_waits_out_a_restart(server, monkeypatch):
    client, handler = server
    monkeypatch.setattr(agents_module, "_sleep", lambda _s: None)
    unavailable = _json(503, {"error": "restarting"})
    handler.routes = {
        "/v1/tasks/t1/stream": _json(404, {"error": "No active stream"}),
        # More 503s than the client's own retry budget (1 + 3 attempts).
        "/v1/tasks/t1": [unavailable] * 9 + [_json(200, _task("completed"))],
    }
    events = list(client.stream_task("t1"))
    assert events[-1].terminal_payload()["result"] == "30 days"
    assert handler.hits.count("/v1/tasks/t1") == 10
