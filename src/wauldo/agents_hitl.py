"""Human-in-the-loop calls of :class:`~wauldo.agents.AgentsClient`.

A task can be paused for a human decision, then approved (it is queued and
runs again) or rejected (it is cancelled). A task left paused 7 days without
a decision is cancelled by the server. A failed or cancelled task can be run
again as a new task with ``resume_task``. Kept apart from ``agents.py`` so
that module stays readable; :class:`~wauldo.agents.AgentsClient` inherits
these methods.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .agents_types import AgentRunResponse, HitlResponse

#: The ``error`` of a task the server cancelled because it stayed paused for
#: approval 7 days without a decision.
PAUSED_EXPIRED_ERROR = "Cancelled: no approval decision within 7 days"

#: Longest ``reason`` the server accepts with a decision, in characters
#: (a longer one is an HTTP 400).
MAX_HITL_REASON_CHARS = 1000


class _HitlMixin:
    """``POST /v1/tasks/:id/pause|approve|reject|resume``. Needs ``_request``."""

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        raise NotImplementedError  # provided by AgentsClient

    def _hitl(self, task_id: str, action: str, reason: Optional[str]) -> HitlResponse:
        body: Dict[str, Any] = {} if reason is None else {"reason": reason}
        return HitlResponse.from_dict(
            self._request("POST", f"/v1/tasks/{task_id}/{action}", body)
        )

    def pause_task(self, task_id: str, reason: Optional[str] = None) -> HitlResponse:
        """POST /v1/tasks/:id/pause — stop a queued or running task until a
        human decides (status ``paused_for_approval``).

        The run in flight is stopped, not only relabelled. ``wait_for_task``
        and ``run_and_wait`` return as soon as a task is paused. ``reason``
        is an optional note (at most 1000 characters, HTTP 400 above) stored
        with the decision: it is listed in ``Task.decisions``, which keeps the
        20 most recent decisions (the oldest is dropped past that).

        A task left paused 7 days without a decision is cancelled by the
        server, with the error :data:`PAUSED_EXPIRED_ERROR`; approving or
        rejecting it then answers HTTP 409, and :meth:`resume_task` runs it
        again as a new task. An HTTP 409 here means the task is in a status
        that cannot be paused (already over, or already paused); 404, that it
        does not exist.
        """
        return self._hitl(task_id, "pause", reason)

    def approve_task(self, task_id: str, reason: Optional[str] = None) -> HitlResponse:
        """POST /v1/tasks/:id/approve — resume a paused task: it is queued and
        runs again. ``reason`` is stored with the decision (see
        :meth:`pause_task`). HTTP 409 if the task is not paused for approval,
        including a task cancelled after 7 days without a decision
        (``Task.error`` is then :data:`PAUSED_EXPIRED_ERROR`)."""
        return self._hitl(task_id, "approve", reason)

    def reject_task(self, task_id: str, reason: Optional[str] = None) -> HitlResponse:
        """POST /v1/tasks/:id/reject — cancel a paused task (final status
        ``cancelled``). ``reason`` is stored with the decision (see
        :meth:`pause_task`). HTTP 409 if the task is not paused for approval,
        including a task already cancelled after 7 days without a decision."""
        return self._hitl(task_id, "reject", reason)

    def resume_task(self, task_id: str) -> AgentRunResponse:
        """POST /v1/tasks/:id/resume — run a failed or cancelled task again.

        The server creates a **new** task from the same prompt and settings
        (webhook included); the original stays as it was. Returns the new
        task's id and status (``queued``): follow it with ``wait_for_task``
        or ``stream_task``. HTTP 409 if the task is not failed or cancelled;
        404 if it does not exist.
        """
        return AgentRunResponse.from_dict(
            self._request("POST", f"/v1/tasks/{task_id}/resume", {})
        )
