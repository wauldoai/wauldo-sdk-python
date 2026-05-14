"""Workflows API client — Wauldo Workflow Runtime (Step Functions style).

State-machine workflows authored as ``Task`` / ``Choice`` / ``Wait`` /
``Pass`` / ``Fail`` / ``Succeed`` states. Runs are async: ``start_run``
returns an ``execution_id``, then poll ``get_run`` (or use the
``wait_for_run`` helper) until a terminal status.

Example:
    >>> from wauldo.workflows import WorkflowsClient
    >>> wf = WorkflowsClient(base_url="https://api.wauldo.com", api_key="...")
    >>> created = wf.create(
    ...     name="triage",
    ...     start_at="Compute",
    ...     states={
    ...         "Compute": {"type": "Task", "resource": "tool:calculator", "next": "Done"},
    ...         "Done": {"type": "Succeed"},
    ...     },
    ... )
    >>> run = wf.start_run(created.id, input={"operation": "add", "a": 21, "b": 21})
    >>> final = wf.wait_for_run(created.id, run.execution_id)
    >>> print(final.status, final.output)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MAX_RESPONSE_SIZE: int = 10 * 1024 * 1024  # 10 MB

TERMINAL_STATUSES = ("succeeded", "failed", "timed_out")


def _bounded_read(resp, limit: int = MAX_RESPONSE_SIZE) -> bytes:
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"response body too large: >{limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


# ── Types ────────────────────────────────────────────────────────────────


@dataclass
class Workflow:
    """A workflow definition (``GET /v1/workflows/:id``)."""

    id: str
    tenant_id: str
    name: str
    start_at: str
    states: Dict[str, Any]
    version: str
    created_at: int
    updated_at: int
    description: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Workflow":
        return cls(
            id=d["id"],
            tenant_id=d["tenant_id"],
            name=d["name"],
            start_at=d["start_at"],
            states=d.get("states", {}),
            version=d.get("version", "1.0"),
            created_at=int(d.get("created_at", 0)),
            updated_at=int(d.get("updated_at", 0)),
            description=d.get("description"),
        )


@dataclass
class WorkflowList:
    workflows: List[Workflow]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkflowList":
        return cls(workflows=[Workflow.from_dict(w) for w in d.get("workflows", [])])


@dataclass
class StartRunResponse:
    """202 response from ``POST /v1/workflows/:id/runs``."""

    execution_id: str
    workflow_id: str
    status: str

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StartRunResponse":
        return cls(
            execution_id=d["execution_id"],
            workflow_id=d["workflow_id"],
            status=d.get("status", "running"),
        )


@dataclass
class WorkflowExecution:
    """A workflow execution record.

    ``status`` is one of ``running``, ``succeeded``, ``failed``,
    ``timed_out``. ``output`` is populated on success; ``error`` on
    terminal failure.
    """

    id: str
    workflow_id: str
    tenant_id: str
    status: str
    started_at: int
    input: Any = None
    output: Any = None
    current_state: Optional[str] = None
    ended_at: Optional[int] = None
    error: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkflowExecution":
        return cls(
            id=d["id"],
            workflow_id=d["workflow_id"],
            tenant_id=d["tenant_id"],
            status=d["status"],
            started_at=int(d.get("started_at", 0)),
            input=d.get("input"),
            output=d.get("output"),
            current_state=d.get("current_state"),
            ended_at=int(d["ended_at"]) if d.get("ended_at") is not None else None,
            error=d.get("error"),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


# ── Client ───────────────────────────────────────────────────────────────


class WorkflowsClient:
    """HTTP client for the Wauldo Workflows API.

    Use the same ``base_url`` + ``api_key`` you use for ``AgentsClient``.
    Network failures raise ``urllib.error.HTTPError`` (4xx/5xx) or
    ``urllib.error.URLError``; JSON parse failures raise ``ValueError``.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:3000",
        api_key: Optional[str] = None,
        tenant: Optional[str] = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tenant = tenant
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self.tenant:
            h["x-rapidapi-user"] = self.tenant
        return h

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url=url, data=data, headers=self._headers(), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 204:
                    return None
                raw = _bounded_read(resp)
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read(MAX_RESPONSE_SIZE).decode("utf-8", errors="replace")
            raise urllib.error.HTTPError(
                e.url, e.code, f"{e.reason}: {body_text}", e.headers, None
            ) from None

    # ── CRUD ─────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        start_at: str,
        states: Dict[str, Any],
        description: Optional[str] = None,
    ) -> Workflow:
        """POST /v1/workflows — create a workflow definition.

        ``states`` is a mapping of state-name → state-definition. Each
        state must include ``type`` (``Task``, ``Choice``, ``Wait``,
        ``Pass``, ``Fail``, ``Succeed``) and the fields that type
        requires. Server validates cycles, transition targets, choice
        operators, and tenant cap (100) before returning 201.
        """
        body: Dict[str, Any] = {"name": name, "start_at": start_at, "states": states}
        if description is not None:
            body["description"] = description
        resp = self._request("POST", "/v1/workflows", body)
        return Workflow.from_dict(resp["workflow"])

    def list(self) -> WorkflowList:
        """GET /v1/workflows — list workflows for the calling tenant."""
        return WorkflowList.from_dict(self._request("GET", "/v1/workflows"))

    def get(self, workflow_id: str) -> Workflow:
        """GET /v1/workflows/:id"""
        resp = self._request("GET", f"/v1/workflows/{workflow_id}")
        return Workflow.from_dict(resp["workflow"])

    def delete(self, workflow_id: str) -> None:
        """DELETE /v1/workflows/:id"""
        self._request("DELETE", f"/v1/workflows/{workflow_id}")

    # ── Runs ─────────────────────────────────────────────────────────

    def start_run(
        self,
        workflow_id: str,
        input: Optional[Any] = None,
    ) -> StartRunResponse:
        """POST /v1/workflows/:id/runs — start an async execution.

        Returns 202 with an ``execution_id`` immediately. Poll
        :meth:`get_run` or use :meth:`wait_for_run` to await completion.
        """
        body: Dict[str, Any] = {}
        if input is not None:
            body["input"] = input
        resp = self._request("POST", f"/v1/workflows/{workflow_id}/runs", body)
        return StartRunResponse.from_dict(resp)

    def get_run(self, workflow_id: str, execution_id: str) -> WorkflowExecution:
        """GET /v1/workflows/:id/runs/:execution_id — fetch one execution."""
        resp = self._request(
            "GET", f"/v1/workflows/{workflow_id}/runs/{execution_id}"
        )
        return WorkflowExecution.from_dict(resp["execution"])

    def wait_for_run(
        self,
        workflow_id: str,
        execution_id: str,
        timeout: float = 90.0,
        poll_interval: float = 1.0,
    ) -> WorkflowExecution:
        """Poll :meth:`get_run` until the run reaches a terminal status.

        Raises :class:`TimeoutError` if the run hasn't terminated within
        ``timeout`` seconds. The server enforces its own 60s wall-clock
        cap per run, so ``timeout`` larger than ~75s is just slack for
        polling overhead.
        """
        deadline = time.monotonic() + timeout
        while True:
            execution = self.get_run(workflow_id, execution_id)
            if execution.is_terminal:
                return execution
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"workflow run {execution_id} did not terminate within "
                    f"{timeout}s (last status: {execution.status})"
                )
            time.sleep(poll_interval)
