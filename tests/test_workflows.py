"""Unit tests for the Workflows SDK type parsers.

Network paths are exercised in the E2E smoke test; here we only verify
the wire format → dataclass conversion for ``Workflow``,
``WorkflowList``, ``StartRunResponse``, and ``WorkflowExecution``.
"""

from __future__ import annotations

from wauldo.workflows import (
    StartRunResponse,
    Workflow,
    WorkflowExecution,
    WorkflowList,
    WorkflowsClient,
)


def test_workflow_from_dict_minimal():
    wf = Workflow.from_dict({
        "id": "wf_1",
        "tenant_id": "t1",
        "name": "triage",
        "start_at": "Compute",
        "states": {"Compute": {"type": "Succeed"}},
        "version": "1.0",
        "created_at": 100,
        "updated_at": 200,
    })
    assert wf.id == "wf_1"
    assert wf.name == "triage"
    assert wf.start_at == "Compute"
    assert "Compute" in wf.states
    assert wf.description is None


def test_workflow_list_from_dict():
    payload = {
        "workflows": [
            {
                "id": "wf_1",
                "tenant_id": "t1",
                "name": "a",
                "start_at": "S",
                "states": {"S": {"type": "Succeed"}},
                "version": "1.0",
                "created_at": 1,
                "updated_at": 2,
            },
        ]
    }
    lst = WorkflowList.from_dict(payload)
    assert len(lst.workflows) == 1
    assert lst.workflows[0].id == "wf_1"


def test_start_run_response_from_dict():
    r = StartRunResponse.from_dict({
        "execution_id": "wfr_abc",
        "workflow_id": "wf_1",
        "status": "running",
    })
    assert r.execution_id == "wfr_abc"
    assert r.workflow_id == "wf_1"
    assert r.status == "running"


def test_workflow_execution_terminal_helpers():
    succeeded = WorkflowExecution.from_dict({
        "id": "wfr_1",
        "workflow_id": "wf_1",
        "tenant_id": "t1",
        "status": "succeeded",
        "started_at": 100,
        "ended_at": 110,
        "output": {"output": "42"},
        "input": {"a": 1},
    })
    assert succeeded.is_terminal
    assert succeeded.succeeded
    assert succeeded.ended_at == 110

    running = WorkflowExecution.from_dict({
        "id": "wfr_2",
        "workflow_id": "wf_1",
        "tenant_id": "t1",
        "status": "running",
        "started_at": 100,
        "current_state": "Compute",
        "input": None,
    })
    assert not running.is_terminal
    assert not running.succeeded
    assert running.ended_at is None
    assert running.current_state == "Compute"

    failed = WorkflowExecution.from_dict({
        "id": "wfr_3",
        "workflow_id": "wf_1",
        "tenant_id": "t1",
        "status": "failed",
        "started_at": 100,
        "ended_at": 105,
        "error": "transition limit exceeded",
        "input": None,
    })
    assert failed.is_terminal
    assert not failed.succeeded
    assert failed.error == "transition limit exceeded"


def test_workflows_client_headers():
    c = WorkflowsClient(base_url="http://x", api_key="k", tenant="t")
    h = c._headers()
    assert h["Authorization"] == "Bearer k"
    assert h["x-rapidapi-user"] == "t"
    assert h["Content-Type"] == "application/json"


def test_workflows_client_no_auth_headers():
    c = WorkflowsClient(base_url="http://x")
    h = c._headers()
    assert "Authorization" not in h
    assert "x-rapidapi-user" not in h
