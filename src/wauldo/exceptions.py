"""
Custom exceptions for Wauldo SDK.
"""

import urllib.error
from typing import Any, Optional


class WauldoError(Exception):
    """Base exception for all Wauldo errors."""

    def __init__(self, message: str, code: Optional[int] = None, data: Optional[Any] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data

    def __str__(self) -> str:
        if self.code:
            return f"[{self.code}] {self.message}"
        return self.message


class AgentConnectionError(WauldoError):
    """Raised when connection to MCP server fails."""

    def __init__(self, message: str = "Failed to connect to MCP server"):
        super().__init__(message, code=-32000)


class ServerError(WauldoError):
    """Raised when server returns an error response."""

    pass


class HttpError(ServerError, urllib.error.HTTPError):
    """An HTTP error status (4xx/5xx) from the agents, tasks, memory or
    workflows API (``AgentsClient``, ``MemoryClient``, ``WorkflowsClient``).

    A :class:`ServerError`, so ``except WauldoError`` catches it with the
    SDK's other errors, and a ``urllib.error.HTTPError`` (what these clients
    raised before), so existing ``except HTTPError`` code keeps working and
    the message reads as before.

    Attributes:
        code: the HTTP status (also ``status``).
        body: the response body, as text (the server's JSON error, usually).
        headers: the response headers (``Retry-After`` on a 429, for one).
    """

    def __init__(self, url: str, status: int, reason: str, headers: Any, body: str):
        message = f"{reason}: {body}"
        urllib.error.HTTPError.__init__(self, url, status, message, headers, None)
        # Set directly: WauldoError.__init__ would chain into HTTPError.__init__.
        self.message = message
        self.data = None
        self.body = body

    def __str__(self) -> str:
        return urllib.error.HTTPError.__str__(self)


class ValidationError(WauldoError):
    """Raised when input validation fails."""

    def __init__(self, message: str, field: Optional[str] = None):
        super().__init__(message, code=-32602)
        self.field = field


class AgentTimeoutError(WauldoError):
    """Raised when operation times out."""

    def __init__(self, message: str = "Operation timed out", timeout: Optional[float] = None):
        super().__init__(message, code=-32001)
        self.timeout = timeout


class TaskTimeoutError(AgentTimeoutError, TimeoutError):
    """Raised when a task has not settled within the time a wait allowed.

    Raised by ``AgentsClient.wait_for_task``, ``run_and_wait`` and the
    fallback of ``stream_task``. The task itself is not affected: it keeps
    running on the server, and ``get_task(task_id)`` reads it later.

    Also an :class:`AgentTimeoutError` and a builtin :class:`TimeoutError`,
    so ``except TimeoutError`` (what these methods raised before) still
    catches it.

    Attributes:
        task_id: the task waited for.
        last_status: its status at the last read (``"queued"``, ``"running"``…).
        timeout: the wait, in seconds.
    """

    def __init__(self, message: str, *, task_id: str, last_status: str, timeout: float):
        super().__init__(message, timeout=timeout)
        self.task_id = task_id
        self.last_status = last_status

    def __str__(self) -> str:
        # The message alone, as the builtin TimeoutError raised before printed it.
        return self.message


class ToolNotFoundError(WauldoError):
    """Raised when requested tool is not available."""

    def __init__(self, tool_name: str):
        super().__init__(f"Tool not found: {tool_name}", code=-32601)
        self.tool_name = tool_name
