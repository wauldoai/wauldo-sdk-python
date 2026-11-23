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
    ... )
    >>> run = agents.run(agent["id"], "Qualify acme.com")
    >>> print(run["task_id"])
"""

from __future__ import annotations

import http.client
import json
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Literal, Optional

from .agents_hitl import _HitlMixin
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
from .exceptions import HttpError, TaskTimeoutError
from .http_pool import ConnectionPool

#: The verification modes the server accepts; anything else is a 400.
VerificationMode = Literal["strict", "balanced", "permissive"]

#: Max bytes the client will accept from a single response. Protects against
#: hostile or misbehaving servers that try to stream gigabytes.
MAX_RESPONSE_SIZE: int = 10 * 1024 * 1024  # 10 MB

#: Statuses retried whatever the method: the server did not take the request
#: (rate limit, overload), so sending it again cannot duplicate anything.
RETRY_STATUSES: frozenset = frozenset({429, 503})
#: Statuses retried only on a read: the server may have acted before failing,
#: and a replayed ``POST`` would run the agent twice; a replayed ``PUT`` (cancel)
#: or ``DELETE`` that had succeeded answers 409 or 404 for a call that worked.
RETRY_STATUSES_IDEMPOTENT: frozenset = frozenset({500, 502, 504})
IDEMPOTENT_METHODS: frozenset = frozenset({"GET"})
#: Longest wait honoured from a ``Retry-After`` header.
MAX_RETRY_AFTER_SEC: float = 30.0
#: Longest computed backoff, before jitter.
MAX_BACKOFF_SEC: float = 8.0

# Patched by the tests; ``time.sleep`` otherwise.
_sleep = time.sleep


def _should_retry(method: str, status: int) -> bool:
    if status in RETRY_STATUSES:
        return True
    return status in RETRY_STATUSES_IDEMPOTENT and method in IDEMPOTENT_METHODS


def _retry_delay(attempt: int, backoff_sec: float, retry_after: Optional[str]) -> float:
    """Seconds to wait before attempt ``attempt + 1``.

    ``Retry-After`` (seconds) wins when present and sane, capped at
    :data:`MAX_RETRY_AFTER_SEC`; otherwise exponential backoff from
    ``backoff_sec``, capped at :data:`MAX_BACKOFF_SEC`, plus up to 250 ms of
    jitter so clients retrying the same outage do not line up.
    """
    if retry_after:
        try:
            value = float(retry_after)
            if value >= 0:
                return min(value, MAX_RETRY_AFTER_SEC)
        except ValueError:
            pass
    return min(backoff_sec * (2**attempt), MAX_BACKOFF_SEC) + random.uniform(0, 0.25)


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


#: Longest the server holds a request open for ``wait``, in seconds.
#: Larger values are clamped server-side, not refused.
MAX_WAIT_SEC = 50

#: Default wait of :meth:`AgentsClient.wait_for_task` and
#: :meth:`AgentsClient.run_and_wait`, in seconds. It covers ordinary runs,
#: queueing and verification included; it is not a bound on how long the
#: server may take. For a long run (many workflow states, tools, retries),
#: pass a larger ``timeout_sec``, follow it with :meth:`AgentsClient.stream_task`,
#: or give the run a ``webhook_url``. When the wait runs out, the task keeps
#: running: :class:`~wauldo.exceptions.TaskTimeoutError` carries its id, to
#: wait again. Only the wait is long: each request inside it stays a long poll
#: of :data:`LONG_POLL_SEC`.
DEFAULT_WAIT_TIMEOUT_SEC: float = 360.0

#: Length of each long-poll request issued by :meth:`AgentsClient.wait_for_task`.
#: Kept under :data:`MAX_WAIT_SEC` and well under the client timeout so a
#: slow run still reports progress every half minute.
LONG_POLL_SEC = 30

#: Longest :meth:`AgentsClient.stream_task` waits for the task once the stream
#: is gone (no stream on the server, or closed before its terminal event): the
#: lifetime the server gives a stream.
STREAM_FALLBACK_TIMEOUT_SEC: float = 1800.0

#: ``condition`` of the terminal event: the one the server sends when the task
#: was already over, and the one :meth:`AgentsClient.stream_task` gives the
#: event it builds from the task when the stream could not deliver it.
ALREADY_FINISHED_CONDITION = "Already finished"

#: What the stream says in place of ``result`` when it withheld the run's
#: output (``Task.output_withheld``).
WITHHELD_RESULT = "Output withheld: injection detected"


def _is_transient(error: Exception) -> bool:
    """A failure a deploy or restart causes (429, 5xx, refused or reset
    connection, timeout): worth reading the task again."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return True


def _still_running(task_id: str) -> str:
    """What a wait that ran out says after its first sentence."""
    return (
        f"; the task may still be running on the server: call "
        f"wait_for_task('{task_id}') to keep waiting, or get_task('{task_id}') to read it"
    )


def _task_query(wait_sec: Optional[float], include_journal: Optional[bool]) -> str:
    """The query string of ``GET /v1/tasks/:id``: ``?wait=N`` and/or
    ``include_journal=true|false``, the empty string when neither is set."""
    query = _wait_query(wait_sec)
    if include_journal is None:
        return query
    flag = f"include_journal={'true' if include_journal else 'false'}"
    return f"{query}&{flag}" if query else f"?{flag}"


def _wait_query(wait_sec: Optional[float]) -> str:
    """``?wait=N`` for a positive wait, the empty string otherwise.

    Rounded up: the server takes whole seconds, and asking for one more is
    better than returning before the caller's own deadline.
    """
    if wait_sec is None or wait_sec <= 0:
        return ""
    import math

    return f"?wait={math.ceil(min(wait_sec, MAX_WAIT_SEC))}"


def _read_sse_transitions(resp) -> Iterator[StateTransition]:
    """The ``data:`` frames of an SSE response, as transitions. Keep-alives
    and frames that are not a JSON object are skipped."""
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
            continue
        if isinstance(obj, dict):
            yield StateTransition.from_dict(obj)


def _terminal_transition(raw: Dict[str, Any]) -> StateTransition:
    """The terminal event the server would have sent for the task ``raw``
    (the body of ``GET /v1/tasks/:id``): same state name, condition and
    payload (``task_id``, ``status``, ``result``, ``error``, ``verification``
    as the server sent it). A run whose stream withheld its output carries
    :data:`WITHHELD_RESULT` in place of its result, as on the stream."""
    task = Task.from_dict(raw)
    payload = {
        "task_id": task.task_id,
        "status": task.status,
        "result": WITHHELD_RESULT if task.output_withheld else task.result,
        "error": task.error,
        "verification": raw.get("verification"),
    }
    return StateTransition(
        state_name=f"TASK_{task.status.upper()}",
        condition=ALREADY_FINISHED_CONDITION,
        raw_output=json.dumps(payload),
        timestamp=int(time.time()),
        success=task.status == "completed",
        retry_count=0,
        duration_ms=0,
        prompt_tokens=0,
        completion_tokens=0,
        repair_count=0,
        cache_hit=False,
        to_state=None,
    )


class AgentsClient(_HitlMixin):
    """HTTP client for the Wauldo Agents API.

    Methods return the typed records of :mod:`wauldo.agents_types`.
    An HTTP error status (4xx/5xx) raises :class:`~wauldo.exceptions.HttpError`,
    a :class:`~wauldo.exceptions.WauldoError` that is also a
    ``urllib.error.HTTPError``; network failures raise ``urllib.error.URLError``;
    JSON parse failures raise ``ValueError``. A failed task is not an
    exception: read ``Task.error_info`` (its stable ``code`` and ``message``).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:3000",
        api_key: Optional[str] = None,
        tenant: Optional[str] = None,
        timeout: float = 120.0,
        max_retries: int = 3,
        retry_backoff_sec: float = 0.5,
    ) -> None:
        """``max_retries``: attempts after the first on a 429 or 503 (any
        method) and on a 500/502/504 or a connection failure (``GET`` only — a
        replayed ``POST`` could run an agent twice, a replayed cancel answers
        409 for a cancel that worked). ``0`` disables retries. ``Retry-After``
        is honoured; otherwise the wait doubles from ``retry_backoff_sec``.
        Timeouts are never retried.
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.tenant = tenant
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_sec = retry_backoff_sec
        self._pool = ConnectionPool()

    # ── Helpers ──────────────────────────────────────────────────────

    def _wait_budget(self, wait_sec: float) -> float:
        """``wait_sec`` bounded by the client's HTTP timeout (5 s of margin).

        Otherwise a client configured with ``timeout=20`` would see every
        long poll of 30 s expire before the server answers.
        """
        return max(min(wait_sec, self.timeout - 5.0), 0.0)

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
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)
        attempt = 0
        while True:
            req = urllib.request.Request(
                url=url,
                data=data,
                headers=headers,
                method=method,
            )
            try:
                with self._pool.urlopen(req, self.timeout) as resp:
                    if resp.status == 204:
                        return None
                    raw = _bounded_read(resp)
                    if not raw:
                        return None
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                if attempt < self.max_retries and _should_retry(method, e.code):
                    retry_after = e.headers.get("Retry-After")
                    _sleep(_retry_delay(attempt, self.retry_backoff_sec, retry_after))
                    attempt += 1
                    continue
                body_text = e.read(MAX_RESPONSE_SIZE).decode("utf-8", errors="replace")
                raise HttpError(e.url, e.code, e.reason, e.headers, body_text) from None
            except urllib.error.URLError as e:
                # The request may not have reached the server (refused, reset):
                # replayed only when replaying is safe. A timeout is not retried
                # — on a long poll it would double the wait the caller chose.
                timed_out = isinstance(e.reason, (socket.timeout, TimeoutError))
                if attempt < self.max_retries and method in IDEMPOTENT_METHODS and not timed_out:
                    _sleep(_retry_delay(attempt, self.retry_backoff_sec, None))
                    attempt += 1
                    continue
                raise

    # ── CRUD ─────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        wauldo_toml: str,
        *,
        description: str = "",
        preset: Optional[str] = None,
        custom_preset: Optional[Dict[str, Any]] = None,
    ) -> Agent:
        """POST /v1/agents — register a new deployed agent.

        ``custom_preset`` is an inline workflow (the ``AgentContractV2`` JSON
        that :meth:`create_revision` takes), validated by the server at
        creation: the agent's first run already uses it, with no
        :meth:`create_revision` call in between. It takes precedence over
        ``preset``.
        """
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "wauldo_toml": wauldo_toml,
        }
        if preset is not None:
            body["preset"] = preset
        if custom_preset is not None:
            body["custom_preset"] = custom_preset
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
        preset: Optional[str] = None,
    ) -> Agent:
        """PATCH /v1/agents/:id — partial update."""
        patch: Dict[str, Any] = {}
        if description is not None:
            patch["description"] = description
        if wauldo_toml is not None:
            patch["wauldo_toml"] = wauldo_toml
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
        verification_mode: Optional[VerificationMode] = None,
        fact_check_mode: Optional[str] = None,
        *,
        wait_sec: Optional[float] = None,
        webhook_url: Optional[str] = None,
        max_retries: Optional[int] = None,
        auto_share: Optional[bool] = None,
    ) -> AgentRunResponse:
        """POST /v1/agents/:id/runs — trigger a verified run.

        Returns :class:`AgentRunResponse` with the ``task_id`` to poll via
        :meth:`get_task` or stream via :meth:`stream_task`.

        With ``wait_sec`` the server holds the response until the run
        settles or the delay elapses (clamped to :data:`MAX_WAIT_SEC`), and
        fills :attr:`AgentRunResponse.task` with the task as it stands —
        the verified result in one round-trip when the run is quick. Use
        :meth:`run_and_wait` to always get the final :class:`Task`.

        ``fact_check_mode`` selects the verifier engine: ``"lexical"``
        (default, ~1s), ``"hybrid"`` (lexical + embeddings), or
        ``"semantic"`` (embeddings + LLM-judge). Hybrid/semantic fall back
        to lexical if the server's BGE cache is unavailable.

        ``webhook_url``: the server POSTs the task record there when the run
        completes or fails (public http(s) URL; a private or internal
        destination is refused with a 400). ``max_retries``: retries of the
        run on the server (default 3, capped at 10) — not the HTTP retries
        of this client. ``auto_share=True`` publishes the run at creation:
        :attr:`AgentRunResponse.share_url` is then set.
        """
        body: Dict[str, Any] = {"input": input}
        if verification_mode is not None:
            body["verification_mode"] = verification_mode
        if fact_check_mode is not None:
            body["fact_check_mode"] = fact_check_mode
        if webhook_url is not None:
            body["webhook_url"] = webhook_url
        if max_retries is not None:
            body["max_retries"] = max_retries
        if auto_share is not None:
            body["auto_share"] = auto_share
        return AgentRunResponse.from_dict(
            self._request(
                "POST", f"/v1/agents/{agent_id}/runs{_wait_query(wait_sec)}", body
            )
        )

    def run_and_wait(
        self,
        agent_id: str,
        input: str,
        verification_mode: Optional[VerificationMode] = None,
        fact_check_mode: Optional[str] = None,
        *,
        timeout_sec: float = DEFAULT_WAIT_TIMEOUT_SEC,
        webhook_url: Optional[str] = None,
        max_retries: Optional[int] = None,
        auto_share: Optional[bool] = None,
    ) -> Task:
        """Run an agent and return the settled :class:`Task` — one call.

        The run is created with the server holding the response for up to
        :data:`MAX_WAIT_SEC`; a quick run therefore comes back verified in
        a single round-trip. A longer one is followed with
        :meth:`wait_for_task` until ``timeout_sec`` (default
        :data:`DEFAULT_WAIT_TIMEOUT_SEC`). ``webhook_url``, ``max_retries``
        and ``auto_share`` are passed to :meth:`run`.

        Raises :class:`~wauldo.exceptions.TaskTimeoutError` (a
        :class:`TimeoutError`) if the task has not settled by then; the run
        goes on on the server, and ``wait_for_task(err.task_id)`` keeps
        waiting. The default covers ordinary runs, not every run: for a long
        one, pass a larger ``timeout_sec``, or use :meth:`stream_task` or a
        ``webhook_url``.
        """
        import time

        deadline = time.monotonic() + timeout_sec
        resp = self.run(
            agent_id,
            input,
            verification_mode,
            fact_check_mode,
            wait_sec=self._wait_budget(min(timeout_sec, MAX_WAIT_SEC)),
            webhook_url=webhook_url,
            max_retries=max_retries,
            auto_share=auto_share,
        )
        if resp.task is not None and resp.task.is_settled:
            return resp.task
        remaining = max(deadline - time.monotonic(), 0.0)
        return self.wait_for_task(resp.task_id, timeout_sec=remaining)

    # ── Tasks (poll + stream) ────────────────────────────────────────

    def get_task(
        self,
        task_id: str,
        *,
        wait_sec: Optional[float] = None,
        include_journal: Optional[bool] = None,
    ) -> Task:
        """GET /v1/tasks/:id — fetch the current state of a task.

        With ``wait_sec`` the server holds the response until the task
        settles or the delay elapses (clamped to :data:`MAX_WAIT_SEC`): a
        long poll, so the result arrives the moment it lands. The task is
        returned as it stands either way.

        ``include_journal=False`` leaves the execution journal (every state's
        raw output) out of the response: ``journal`` is then ``None``. Not
        sent by default, so the journal is included.
        """
        return Task.from_dict(
            self._request("GET", f"/v1/tasks/{task_id}{_task_query(wait_sec, include_journal)}")
        )

    def cancel_task(self, task_id: str) -> Task:
        """PUT /v1/tasks/:id — cancel a queued or running task.

        Returns the task as cancelled. (Up to 0.21, this sent ``DELETE``,
        which the server maps to *deleting a finished task*: a running task
        could not be cancelled, and a finished one was erased.)
        """
        return Task.from_dict(self._request("PUT", f"/v1/tasks/{task_id}"))

    def delete_task(self, task_id: str) -> None:
        """DELETE /v1/tasks/:id — delete a finished task (completed, failed, cancelled)."""
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

        A share expires 30 days after it is published (``expires_at``,
        epoch milliseconds; ``None`` would mean no expiry, which no plan has
        today). The shared run stays
        available for as long as its link is valid: until it expires, is
        withdrawn with :meth:`unshare_task`, or the task is deleted.
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
        timeout_sec: float = DEFAULT_WAIT_TIMEOUT_SEC,
        poll_interval_sec: float = 2.0,
    ) -> Task:
        """Wait until the task settles — terminal, or paused for approval.

        Each request is a long poll (``?wait=``, :data:`LONG_POLL_SEC`): the
        server answers the moment the task settles, so the result is not
        delayed by a polling interval. Against a server that does not hold
        the request, this falls back to sleeping ``poll_interval_sec``
        between polls, as before.

        Blocks the calling thread. Raises
        :class:`~wauldo.exceptions.TaskTimeoutError` (a :class:`TimeoutError`,
        carrying ``task_id`` and ``last_status``) if the task has not settled
        after ``timeout_sec`` (default :data:`DEFAULT_WAIT_TIMEOUT_SEC`, which
        covers ordinary runs, not every run); the task keeps running on the
        server, and calling this again with ``err.task_id`` keeps waiting. Use
        :meth:`stream_task` when you need event-by-event progress instead of a
        single final snapshot.
        """
        import time

        deadline = time.monotonic() + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            started = time.monotonic()
            # Never ask the server to hold longer than what is left of the
            # caller's own deadline; at or past it, a plain read then the error.
            task = self.get_task(
                task_id, wait_sec=self._wait_budget(min(LONG_POLL_SEC, max(remaining, 0.0)))
            )
            if task.is_settled:
                return task
            if time.monotonic() >= deadline:
                raise TaskTimeoutError(
                    f"task {task_id} still in status '{task.status}' after {timeout_sec:.0f}s"
                    + _still_running(task_id),
                    task_id=task_id,
                    last_status=str(task.status),
                    timeout=timeout_sec,
                )
            # A reply within half a second means the server did not hold the
            # request (older server): pace the polls ourselves.
            if time.monotonic() - started < 0.5:
                time.sleep(min(poll_interval_sec, max(deadline - time.monotonic(), 0.0)))

    def stream_task(self, task_id: str) -> Iterator[StateTransition]:
        """Subscribe to ``GET /v1/tasks/:id/stream`` and yield typed events.

        Each yielded :class:`StateTransition` corresponds to one SSE
        ``data:`` frame emitted by the server when a workflow state
        completes. The last one is the terminal event
        (:attr:`StateTransition.is_terminal`), carrying the result.

        When the server has no stream for the task (404: still queued,
        already over, server restarted) or the stream closes before its
        terminal event (connection cut, 30-minute limit), the task is
        long-polled until it settles (up to :data:`STREAM_FALLBACK_TIMEOUT_SEC`,
        through the 429/5xx/refused connections of a server restarting) and a
        terminal event built from it, with condition ``"Already finished"``,
        ends the generator. A task paused for
        approval ends it without a terminal event: approve, then stream again.

        An event named :data:`STREAM_LAGGED` is a notice, not a state of the
        run: the stream fell behind, so some transitions were skipped and the
        next ones are withheld. It does not end the generator; the terminal
        event still comes, and :meth:`get_task` holds the full run.

        Usage::

            for event in agents.stream_task(run.task_id):
                print(event.state_name, event.duration_ms)
        """
        resp = self._open_stream(task_id)
        if resp is not None:
            try:
                for event in _read_sse_transitions(resp):
                    yield event
                    if event.is_terminal:
                        return
            except (OSError, http.client.HTTPException):
                # Cut mid-stream (reset, idle timeout, truncated chunk): the
                # task is read below, as when the stream closes early.
                pass
            finally:
                resp.close()
        event = self._settled_transition(task_id)
        if event is not None:
            yield event

    def _open_stream(self, task_id: str):
        """The open SSE response, or ``None`` when the server has no stream
        for the task (404): it asks for a poll instead. A task that does not
        exist answers 404 too; the poll then raises it."""
        url = f"{self.base_url}/v1/tasks/{task_id}/stream"
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        req = urllib.request.Request(url=url, headers=headers, method="GET")
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            body_text = e.read(MAX_RESPONSE_SIZE).decode("utf-8", errors="replace")
            if e.code == 404:
                return None
            raise HttpError(e.url, e.code, e.reason, e.headers, body_text) from None

    def _settled_transition(self, task_id: str) -> Optional[StateTransition]:
        """Wait for the task to settle; its terminal event if it is over,
        ``None`` if it is paused for approval."""
        raw = self._settled_task_through_outages(task_id)
        if not Task.from_dict(raw).is_done:
            return None
        return _terminal_transition(raw)

    def _settled_task_through_outages(self, task_id: str) -> Dict[str, Any]:
        """Long-poll the task (raw body) until it settles, within
        :data:`STREAM_FALLBACK_TIMEOUT_SEC`. A transient failure (429, 5xx,
        connection refused: the server restarting under a deploy) is waited
        out until that deadline, not only for ``max_retries`` attempts: the
        stream was most likely cut by that very restart."""
        deadline = time.monotonic() + STREAM_FALLBACK_TIMEOUT_SEC
        failures = 0
        while True:
            remaining = max(deadline - time.monotonic(), 0.0)
            started = time.monotonic()
            wait = self._wait_budget(min(LONG_POLL_SEC, remaining))
            try:
                raw = self._request("GET", f"/v1/tasks/{task_id}{_wait_query(wait)}")
            except (OSError, http.client.HTTPException) as e:
                if not _is_transient(e) or time.monotonic() >= deadline:
                    raise
                _sleep(min(_retry_delay(failures, self.retry_backoff_sec, None), remaining))
                failures += 1
                continue
            failures = 0
            task = Task.from_dict(raw or {})
            if task.is_settled:
                return raw
            if time.monotonic() >= deadline:
                raise TaskTimeoutError(
                    f"task {task_id} not settled after {STREAM_FALLBACK_TIMEOUT_SEC:.0f}s"
                    + _still_running(task_id),
                    task_id=task_id,
                    last_status=str(task.status),
                    timeout=STREAM_FALLBACK_TIMEOUT_SEC,
                )
            # The server did not hold the request (older server): pace the polls.
            if time.monotonic() - started < 0.5:
                _sleep(min(2.0, max(deadline - time.monotonic(), 0.0)))

    def a2a_invoke(
        self,
        agent_id: str,
        input: str,
        trace: Optional[List[str]] = None,
        verification_mode: Optional[VerificationMode] = None,
        fact_check_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST /v1/a2a/:agent_id — agent-to-agent call.

        Pass the current ``trace`` (list of previously-visited agent IDs)
        to let the server enforce cycle detection and depth limits.

        ``fact_check_mode`` mirrors :meth:`run` — defaults to ``"lexical"``.
        """
        if not input:
            raise ValueError("a2a_invoke requires a non-empty input")
        extra = {"x-a2a-trace": ",".join(trace)} if trace else None
        body: Dict[str, Any] = {"input": input}
        if verification_mode is not None:
            body["verification_mode"] = verification_mode
        if fact_check_mode is not None:
            body["fact_check_mode"] = fact_check_mode
        # Through ``_request`` like the TypeScript and Rust clients: same
        # retries (429/503 — a POST is never replayed on a 5xx) and errors.
        out = self._request("POST", f"/v1/a2a/{agent_id}", body, extra_headers=extra)
        return out if isinstance(out, dict) else {}
