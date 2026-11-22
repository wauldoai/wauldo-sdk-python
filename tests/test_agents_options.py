"""Run options (webhook, retries, share), custom_preset at creation, HITL
calls and the typed wait timeout of AgentsClient — against a stubbed
``_request``."""

from __future__ import annotations

import inspect
from typing import Any, Dict, List, Optional, Tuple

import pytest

from wauldo import TaskTimeoutError as ExportedTaskTimeoutError
from wauldo.agents import DEFAULT_WAIT_TIMEOUT_SEC, AgentsClient
from wauldo.agents_hitl import PAUSED_EXPIRED_ERROR
from wauldo.agents_types import AgentRunResponse, HitlResponse, Task, TaskDecision
from wauldo.exceptions import AgentTimeoutError, TaskTimeoutError

AGENT = {
    "id": "a1",
    "tenant_id": "t",
    "name": "bot",
    "description": "",
    "wauldo_toml": "[agent]",
    "model_provider": "openrouter",
    "model_name": "qwen",
    "created_at": 0,
    "updated_at": 0,
}
RUNNING = {
    "task_id": "t1",
    "tenant_id": "t",
    "status": "running",
    "prompt": "hi",
    "created_at": 1,
    "updated_at": 2,
}

Call = Tuple[str, str, Optional[Dict[str, Any]]]


class _Stub(AgentsClient):
    """AgentsClient whose HTTP layer records the calls and answers ``reply``."""

    def __init__(self, reply: Any) -> None:
        super().__init__(base_url="http://stub")
        self.reply = reply
        self.calls: List[Call] = []

    def _request(self, method, path, body=None, extra_headers=None):  # type: ignore[override]
        self.calls.append((method, path, body))
        return self.reply


def test_run_sends_webhook_retries_and_auto_share():
    client = _Stub({"task_id": "t1", "agent_id": "a1", "status": "queued", "created_at": 0,
                    "share_id": "r_x", "share_url": "https://wauldo.com/r/r_x"})
    resp = client.run(
        "a1", "hi", webhook_url="https://example.com/hook", max_retries=5, auto_share=True
    )
    method, path, body = client.calls[0]
    assert (method, path) == ("POST", "/v1/agents/a1/runs")
    assert body == {
        "input": "hi",
        "webhook_url": "https://example.com/hook",
        "max_retries": 5,
        "auto_share": True,
    }
    assert resp.share_id == "r_x"
    assert resp.share_url == "https://wauldo.com/r/r_x"


def test_run_without_options_sends_only_the_input():
    client = _Stub({"task_id": "t1", "agent_id": "a1", "status": "queued", "created_at": 0})
    resp = client.run("a1", "hi")
    assert client.calls[0][2] == {"input": "hi"}
    assert resp.share_url is None


def test_run_and_wait_passes_the_run_options_through():
    settled = dict(RUNNING, status="completed")
    client = _Stub({"task_id": "t1", "agent_id": "a1", "status": "completed",
                    "created_at": 0, "task": settled})
    client.run_and_wait("a1", "hi", webhook_url="https://example.com/h", auto_share=False)
    body = client.calls[0][2]
    assert body["webhook_url"] == "https://example.com/h"
    assert body["auto_share"] is False
    assert "max_retries" not in body


def test_create_sends_custom_preset():
    client = _Stub(AGENT)
    contract = {"name": "flow", "states": []}
    client.create("bot", "[agent]", custom_preset=contract)
    assert client.calls[0][0:2] == ("POST", "/v1/agents")
    assert client.calls[0][2]["custom_preset"] == contract


def test_create_without_custom_preset_does_not_send_it():
    client = _Stub(AGENT)
    client.create("bot", "[agent]")
    assert "custom_preset" not in client.calls[0][2]


@pytest.mark.parametrize("action", ["pause", "approve", "reject"])
def test_hitl_calls_post_the_transition(action):
    client = _Stub({"task_id": "t1", "previous_status": "running",
                    "new_status": "paused_for_approval"})
    out = getattr(client, f"{action}_task")("t1")
    assert client.calls == [("POST", f"/v1/tasks/t1/{action}", {})]
    assert isinstance(out, HitlResponse)
    assert out.previous_status == "running"
    assert out.new_status == "paused_for_approval"


def test_hitl_reason_is_sent_when_given():
    client = _Stub({"task_id": "t1", "previous_status": "paused_for_approval",
                    "new_status": "cancelled"})
    client.reject_task("t1", reason="wrong account")
    assert client.calls[0][2] == {"reason": "wrong account"}


def test_wait_timeout_is_typed_and_still_a_builtin_timeout():
    client = _Stub(RUNNING)
    with pytest.raises(TaskTimeoutError) as info:
        client.wait_for_task("t1", timeout_sec=0)
    err = info.value
    assert isinstance(err, TimeoutError), "except TimeoutError must keep catching it"
    assert isinstance(err, AgentTimeoutError), "the SDK's documented timeout error"
    assert err.task_id == "t1"
    assert err.last_status == "running"
    assert err.timeout == 0
    assert str(err).startswith("task t1 still in status 'running' after 0s")
    assert ExportedTaskTimeoutError is TaskTimeoutError


class _Replies(AgentsClient):
    """AgentsClient answering each ``_request`` with the next reply."""

    def __init__(self, *replies: Any) -> None:
        super().__init__(base_url="http://stub")
        self.replies = list(replies)
        self.paths: List[str] = []

    def _request(self, method, path, body=None, extra_headers=None):  # type: ignore[override]
        self.paths.append(path)
        return self.replies.pop(0)


def test_wait_timeout_says_the_task_may_still_run_and_waiting_again_gets_it():
    # The wait gives up while the run goes on: the error names the task and
    # says how to keep waiting, and doing so returns the run's result.
    client = _Replies(RUNNING, dict(RUNNING, status="completed", result="done"))
    with pytest.raises(TaskTimeoutError) as info:
        client.wait_for_task("t1", timeout_sec=0)
    message = str(info.value)
    assert "may still be running on the server" in message
    assert "wait_for_task('t1')" in message
    task = client.wait_for_task(info.value.task_id, timeout_sec=5)
    assert task.status == "completed"
    assert task.result == "done"
    assert all(p.startswith("/v1/tasks/t1") for p in client.paths)


def test_run_and_wait_timeout_is_typed():
    client = _Stub({"task_id": "t1", "agent_id": "a1", "status": "running",
                    "created_at": 0, "task": RUNNING})
    client.reply = RUNNING
    with pytest.raises(TaskTimeoutError):
        client.run_and_wait("a1", "hi", timeout_sec=0)


def test_default_wait_is_the_wait_of_both_helpers():
    # The default covers ordinary runs; it is not a bound on the server's run
    # time (a long run outlasts it: the timeout test above shows how the
    # caller keeps waiting).
    wait_default = inspect.signature(AgentsClient.wait_for_task).parameters["timeout_sec"].default
    run_default = inspect.signature(AgentsClient.run_and_wait).parameters["timeout_sec"].default
    assert wait_default == DEFAULT_WAIT_TIMEOUT_SEC
    assert run_default == DEFAULT_WAIT_TIMEOUT_SEC
    # Each request stays short: the HTTP timeout is not the wait.
    assert AgentsClient().timeout < DEFAULT_WAIT_TIMEOUT_SEC


def test_get_task_sends_include_journal_only_when_given():
    client = _Stub(RUNNING)
    client.get_task("t1")
    client.get_task("t1", include_journal=False)
    client.get_task("t1", include_journal=True, wait_sec=5)
    paths = [c[1] for c in client.calls]
    assert paths == [
        "/v1/tasks/t1",
        "/v1/tasks/t1?include_journal=false",
        "/v1/tasks/t1?wait=5&include_journal=true",
    ]


def test_wait_for_task_does_not_send_include_journal():
    # The journal is only filled at the end: the wait helpers leave the
    # server default alone.
    client = _Stub(dict(RUNNING, status="completed"))
    client.wait_for_task("t1")
    assert "include_journal" not in client.calls[0][1]


def test_resume_task_posts_and_returns_the_new_task():
    client = _Stub({"task_id": "t2", "status": "queued", "created_at": 9})
    out = client.resume_task("t1")
    assert client.calls == [("POST", "/v1/tasks/t1/resume", {})]
    assert isinstance(out, AgentRunResponse)
    assert out.task_id == "t2"
    assert out.status == "queued"


def test_task_decisions_are_parsed():
    task = Task.from_dict(dict(RUNNING, status="cancelled", error=PAUSED_EXPIRED_ERROR, decisions=[
        {"action": "pause", "reason": "check the total", "at": 10},
        {"action": "approve", "at": 11},
    ]))
    assert task.decisions == [
        TaskDecision(action="pause", at=10, reason="check the total"),
        TaskDecision(action="approve", at=11),
    ]
    assert "decisions" not in task._extra
    assert Task.from_dict(RUNNING).decisions == []
