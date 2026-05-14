"""Agents API client — Wauldo Deploy deployed-agent registry.

Standalone client that talks to the /v1/agents endpoints. Designed to work
alongside the existing HttpClient without requiring modifications to it —
instantiate AgentsClient with the same base_url + api_key you use for
HttpClient.

Example:
    >>> from wauldo.agents import AgentsClient
    >>> agents = AgentsClient(base_url="http://localhost:3000", api_key="...")
    >>> agent = agents.create(
    ...     name="sdr-bot",
    ...     wauldo_toml=open("wauldo.toml").read(),
    ...     agents_md=open("AGENTS.md").read(),
    ... )
    >>> run = agents.run(agent["id"], "Qualify acme.com")
    >>> print(run["task_id"])
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

from .agents_types import (
    Agent,
    AgentList,
    AgentRevision,
    AgentRunResponse,
    CreateRevisionResponse,
    ListRevisionsResponse,
    ShareResponse,
    StateTransition,
    Task,
)

#: Max bytes the client will accept from a single response. Protects against
#: hostile or misbehaving servers that try to stream gigabytes.
MAX_RESPONSE_SIZE: int = 10 * 1024 * 1024  # 10 MB


def _bounded_read(resp, limit: int = MAX_RESPONSE_SIZE) -> bytes:
    """Read a urllib response body in chunks, capped at ``limit`` bytes.

    Raises ``ValueError`` when the body exceeds the cap. We deliberately
    do NOT pass ``limit`` to ``resp.read()`` directly because some
    servers send chunked responses that report a wrong Content-Length.
    """
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(
                f"response body too large: >{limit} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


class AgentsClient:
    """HTTP client for the Wauldo Agents API.

    All methods return dicts matching the server's JSON response shape.
    Network failures raise ``urllib.error.URLError`` (or HTTPError for
    4xx/5xx); JSON parse failures raise ``ValueError``.
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

    # ── Helpers ──────────────────────────────────────────────────────

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
            url=url,
            data=data,
            headers=self._headers(),
            method=method,
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
        wauldo_toml: str,
        agents_md: Optional[str] = None,
        mcp_json: Optional[str] = None,
        description: str = "",
        preset: Optional[str] = None,
    ) -> Agent:
        """POST /v1/agents — register a new deployed agent."""
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "wauldo_toml": wauldo_toml,
        }
        if agents_md is not None:
            body["agents_md"] = agents_md
        if mcp_json is not None:
            body["mcp_json"] = mcp_json
        if preset is not None:
            body["preset"] = preset
        return Agent.from_dict(self._request("POST", "/v1/agents", body))

    def list(self, limit: int = 20, offset: int = 0) -> AgentList:
        """GET /v1/agents — paginated list."""
        return AgentList.from_dict(
            self._request("GET", f"/v1/agents?limit={limit}&offset={offset}")
        )

    def get(self, agent_id: str) -> Agent:
        """GET /v1/agents/:id"""
        return Agent.from_dict(self._request("GET", f"/v1/agents/{agent_id}"))

    def update(
        self,
        agent_id: str,
        *,
        description: Optional[str] = None,
        wauldo_toml: Optional[str] = None,
        agents_md: Optional[str] = None,
        mcp_json: Optional[str] = None,
        preset: Optional[str] = None,
    ) -> Agent:
        """PATCH /v1/agents/:id — partial update."""
        patch: Dict[str, Any] = {}
        if description is not None:
            patch["description"] = description
        if wauldo_toml is not None:
            patch["wauldo_toml"] = wauldo_toml
        if agents_md is not None:
            patch["agents_md"] = agents_md
        if mcp_json is not None:
            patch["mcp_json"] = mcp_json
        if preset is not None:
            patch["preset"] = preset
        return Agent.from_dict(self._request("PATCH", f"/v1/agents/{agent_id}", patch))

    def delete(self, agent_id: str) -> None:
        """DELETE /v1/agents/:id — returns None on success."""
        self._request("DELETE", f"/v1/agents/{agent_id}")

    # ── Revisions (ECS-style versioning) ─────────────────────────────

    def create_revision(
        self,
        agent_id: str,
        custom_preset: Dict[str, Any],
        message: Optional[str] = None,
        set_active: bool = True,
    ) -> CreateRevisionResponse:
        """POST /v1/agents/:id/revisions — mint an immutable revision.

        ``custom_preset`` is a full ``AgentContractV2`` JSON payload. The
        server validates it (size, depth, states, cycle, tools, quota)
        and stores an immutable snapshot keyed by SHA-256. When
        ``set_active=True`` (default) the new revision becomes the agent's
        live revision; ``set_active=False`` stages it for review.
        """
        body: Dict[str, Any] = {
            "custom_preset": custom_preset,
            "set_active": set_active,
        }
        if message is not None:
            body["message"] = message
        return CreateRevisionResponse.from_dict(
            self._request("POST", f"/v1/agents/{agent_id}/revisions", body)
        )

    def list_revisions(self, agent_id: str) -> ListRevisionsResponse:
        """GET /v1/agents/:id/revisions — list revisions newest-first."""
        return ListRevisionsResponse.from_dict(
            self._request("GET", f"/v1/agents/{agent_id}/revisions")
        )

    def get_revision(self, agent_id: str, rev: int) -> AgentRevision:
        """GET /v1/agents/:id/revisions/:rev — fetch one revision verbatim."""
        return AgentRevision.from_dict(
            self._request("GET", f"/v1/agents/{agent_id}/revisions/{rev}")
        )

    def set_active_revision(self, agent_id: str, rev: int) -> Agent:
        """PATCH /v1/agents/:id/active-revision — O(1) rollback / promotion.

        No LLM cost — the revision is already validated and stored. Use
        this to roll back to a previous good revision when the current
        one breaks in production.
        """
        return Agent.from_dict(
            self._request(
                "PATCH",
                f"/v1/agents/{agent_id}/active-revision",
                {"rev": rev},
            )
        )

    # ── Runs ─────────────────────────────────────────────────────────

    def run(
        self,
        agent_id: str,
        input: str,
        verification_mode: Optional[str] = None,
        fact_check_mode: Optional[str] = None,
    ) -> AgentRunResponse:
        """POST /v1/agents/:id/runs — trigger a verified run.

        Returns :class:`AgentRunResponse` with the ``task_id`` to poll via
        :meth:`get_task` or stream via :meth:`stream_task`.

        ``fact_check_mode`` selects the verifier engine: ``"lexical"``
        (default, ~1s), ``"hybrid"`` (lexical + embeddings), or
        ``"semantic"`` (embeddings + LLM-judge). Hybrid/semantic fall back
        to lexical if the server's BGE cache is unavailable.
        """
        body: Dict[str, Any] = {"input": input}
        if verification_mode is not None:
            body["verification_mode"] = verification_mode
        if fact_check_mode is not None:
            body["fact_check_mode"] = fact_check_mode
        return AgentRunResponse.from_dict(
            self._request("POST", f"/v1/agents/{agent_id}/runs", body)
        )

    # ── Tasks (poll + stream) ────────────────────────────────────────

    def get_task(self, task_id: str) -> Task:
        """GET /v1/tasks/:id — fetch the current state of a task."""
        return Task.from_dict(self._request("GET", f"/v1/tasks/{task_id}"))

    def cancel_task(self, task_id: str) -> None:
        """DELETE /v1/tasks/:id — cancel a queued or running task."""
        self._request("DELETE", f"/v1/tasks/{task_id}")

    # ── Shareable runs ───────────────────────────────────────────────

    def share_task(self, task_id: str) -> ShareResponse:
        """POST /v1/tasks/:id/share — publish a run as a public URL.

        Idempotent : calling on an already-shared task returns the
        existing :class:`ShareResponse` without bumping the per-tenant
        cap. The returned ``url`` (form ``https://wauldo.com/r/<id>``)
        can be pasted anywhere — anyone with the link sees the verdict
        + claims + sources + timeline through a strict-whitelist
        projection (no ``custom_preset`` / ``wauldo_toml`` / system
        prompt / tool args ever leave the tenant).

        Free-tier tenants get a 30-day TTL ; paid tenants get
        ``expires_at = None`` (no expiration).
        """
        return ShareResponse.from_dict(
            self._request("POST", f"/v1/tasks/{task_id}/share", {})
        )

    def unshare_task(self, task_id: str) -> None:
        """DELETE /v1/tasks/:id/share — make a published run private again.

        Idempotent : calling on a never-published task returns 204.
        Subsequent ``GET /v1/runs/<share_id>`` for the cleared id
        returns 404.
        """
        self._request("DELETE", f"/v1/tasks/{task_id}/share")

    def wait_for_task(
        self,
        task_id: str,
        timeout_sec: float = 180.0,
        poll_interval_sec: float = 2.0,
    ) -> Task:
        """Poll :meth:`get_task` until the task reaches a terminal status.

        Blocks the calling thread. Raises :class:`TimeoutError` if the
        task is still running after ``timeout_sec``. Use
        :meth:`stream_task` when you need event-by-event progress
        instead of a single final snapshot.
        """
        import time

        deadline = time.monotonic() + timeout_sec
        while True:
            task = self.get_task(task_id)
            if task.is_done:
                return task
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"task {task_id} still in status '{task.status}' after {timeout_sec:.0f}s"
                )
            time.sleep(poll_interval_sec)

    def stream_task(self, task_id: str) -> Iterator[StateTransition]:
        """Subscribe to ``GET /v1/tasks/:id/stream`` and yield typed events.

        Each yielded :class:`StateTransition` corresponds to one SSE
        ``data:`` frame emitted by the server when a workflow state
        completes. The generator closes when the upstream stream closes
        (task reached terminal status) or on connection error.

        Usage::

            for event in agents.stream_task(run.task_id):
                print(event.state_name, event.duration_ms)
        """
        url = f"{self.base_url}/v1/tasks/{task_id}/stream"
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        req = urllib.request.Request(url=url, headers=headers, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body_text = e.read(MAX_RESPONSE_SIZE).decode("utf-8", errors="replace")
            raise urllib.error.HTTPError(
                e.url, e.code, f"{e.reason}: {body_text}", e.headers, None
            ) from None

        # SSE framing: lines like "data: {json}\n", blank line separates frames.
        # urllib gives us a file-like object we can iterate line by line.
        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    # Server may send keep-alives or partial frames. Skip.
                    continue
                if isinstance(obj, dict):
                    yield StateTransition.from_dict(obj)
        finally:
            resp.close()

    def a2a_invoke(
        self,
        agent_id: str,
        input: str,
        trace: Optional[List[str]] = None,
        verification_mode: Optional[str] = None,
        fact_check_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/a2a/:agent_id — agent-to-agent call.

        Pass the current ``trace`` (list of previously-visited agent IDs)
        to let the server enforce cycle detection and depth limits.

        ``fact_check_mode`` mirrors :meth:`run` — defaults to ``"lexical"``.
        """
        if not input:
            raise ValueError("a2a_invoke requires a non-empty input")
        headers = self._headers()
        if trace:
            headers["x-a2a-trace"] = ",".join(trace)
        body: Dict[str, Any] = {"input": input}
        if verification_mode is not None:
            body["verification_mode"] = verification_mode
        if fact_check_mode is not None:
            body["fact_check_mode"] = fact_check_mode
        data = json.dumps(body).encode("utf-8")
        url = f"{self.base_url}/v1/a2a/{agent_id}"
        req = urllib.request.Request(
            url=url,
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = _bounded_read(resp)
            return json.loads(raw.decode("utf-8")) if raw else {}
