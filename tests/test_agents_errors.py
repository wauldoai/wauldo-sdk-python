"""Agents errors: root exports, HttpError in the WauldoError
hierarchy, and the parsed ``code: message`` of a failed task."""

from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import wauldo
from wauldo import (
    TASK_ERROR_CODES,
    AgentsClient,
    HttpError,
    MemoryClient,
    ServerError,
    Task,
    TaskErrorCode,
    TaskErrorInfo,
    WauldoError,
    WorkflowsClient,
)

ERROR_BODY = {"error": {"message": "Invalid API key", "type": "unauthorized"}}


class _Unauthorized(BaseHTTPRequestHandler):
    def log_message(self, *_a, **_k) -> None:
        pass

    def _deny(self) -> None:
        n = int(self.headers.get("Content-Length", "0"))
        if n:
            self.rfile.read(n)
        payload = json.dumps(ERROR_BODY).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _deny  # noqa: N815
    do_POST = _deny  # noqa: N815
    do_PUT = _deny  # noqa: N815


@pytest.fixture
def base_url():
    srv = HTTPServer(("127.0.0.1", 0), _Unauthorized)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_root_exports_the_agents_surface():
    from wauldo.agents import AgentsClient as FromModule
    from wauldo.agents_types import Task as TaskFromModule

    assert wauldo.AgentsClient is FromModule
    assert wauldo.Task is TaskFromModule
    for name in ("TaskVerification", "StateTransition", "TaskDecision", "TaskTimeoutError"):
        assert name in wauldo.__all__, name


def _assert_agents_http_error(err: BaseException) -> None:
    # The documented hierarchy catches it...
    assert isinstance(err, WauldoError)
    assert isinstance(err, ServerError)
    # ...and so does what these clients raised before.
    assert isinstance(err, urllib.error.HTTPError)
    assert isinstance(err, HttpError)
    assert err.code == 401
    assert json.loads(err.body) == ERROR_BODY
    assert str(err).startswith("HTTP Error 401: ")
    assert "Invalid API key" in str(err)


def test_agents_client_http_error_is_a_wauldo_error(base_url):
    with pytest.raises(WauldoError) as exc:
        AgentsClient(base_url=base_url, max_retries=0).get_task("t1")
    _assert_agents_http_error(exc.value)


def test_agents_client_http_error_still_caught_as_urllib_http_error(base_url):
    with pytest.raises(urllib.error.HTTPError) as exc:
        AgentsClient(base_url=base_url, max_retries=0).run("a", "hi")
    _assert_agents_http_error(exc.value)


def test_stream_task_http_error_is_a_wauldo_error(base_url):
    with pytest.raises(WauldoError) as exc:
        list(AgentsClient(base_url=base_url, max_retries=0).stream_task("t1"))
    _assert_agents_http_error(exc.value)


def test_memory_and_workflows_http_errors_are_wauldo_errors(base_url):
    with pytest.raises(WauldoError) as mem:
        MemoryClient(base_url=base_url).get("ns", "k")
    _assert_agents_http_error(mem.value)
    with pytest.raises(WauldoError) as wf:
        WorkflowsClient(base_url=base_url).get("wf1")
    _assert_agents_http_error(wf.value)


@pytest.mark.parametrize(
    "code",
    [
        "invalid_preset",
        "invalid_agent_config",
        "rate_limited",
        "timeout",
        "model_unavailable",
        "run_failed",
    ],
)
def test_task_error_info_known_codes(code):
    info = TaskErrorInfo.parse(f"{code}: Something to read.")
    assert info == TaskErrorInfo(code=code, message="Something to read.")
    assert code in TASK_ERROR_CODES


def test_task_error_info_keeps_unknown_codes_and_colons():
    info = TaskErrorInfo.parse("quota_exceeded: Limit: 10 runs")
    assert info.code == "quota_exceeded"
    assert info.message == "Limit: 10 runs"
    assert info.code not in TASK_ERROR_CODES


def test_task_error_info_without_code_keeps_the_whole_error():
    for raw in (
        "Cancelled: no approval decision within 7 days",
        "internal error: the run did not complete",
        "plain text",
    ):
        assert TaskErrorInfo.parse(raw) == TaskErrorInfo(code=None, message=raw)
    assert TaskErrorInfo.parse(None) is None
    assert TaskErrorInfo.parse("") is None


def test_task_error_info_on_a_failed_task():
    task = Task.from_dict(
        {
            "task_id": "t1",
            "status": "failed",
            "error": "invalid_preset: Unknown preset: my_presett",
        }
    )
    assert task.error_info == TaskErrorInfo(
        code=TaskErrorCode.INVALID_PRESET, message="Unknown preset: my_presett"
    )
    # The raw string is unchanged.
    assert task.error == "invalid_preset: Unknown preset: my_presett"
    assert Task.from_dict({"task_id": "t2", "status": "completed"}).error_info is None
