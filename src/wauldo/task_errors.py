"""What a failed task's ``error`` says, parsed.

The server writes a failed task's ``error`` as ``code: message``: the code is
stable, for programs; the message is for people. :class:`TaskErrorInfo`
splits the two, and :class:`TaskErrorCode` names the codes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


class TaskErrorCode(str):
    """Stable codes a failed task's ``error`` starts with (``code: message``).

    New codes may be added: handle an unknown one like :attr:`RUN_FAILED`.
    """

    #: The agent names a workflow preset that does not exist. Fix the agent.
    INVALID_PRESET = "invalid_preset"
    #: The agent's inline workflow could not be loaded. Fix the agent.
    INVALID_AGENT_CONFIG = "invalid_agent_config"
    #: The model service is saturated. Retry in a few minutes.
    RATE_LIMITED = "rate_limited"
    #: The run did not finish within its time limit. Retry, or simplify the request.
    TIMEOUT = "timeout"
    #: The model service is temporarily unavailable. Retry later.
    MODEL_UNAVAILABLE = "model_unavailable"
    #: The run failed for another reason. Retry later.
    RUN_FAILED = "run_failed"


#: Every code of :class:`TaskErrorCode` this SDK version knows.
TASK_ERROR_CODES = frozenset(
    {
        TaskErrorCode.INVALID_PRESET,
        TaskErrorCode.INVALID_AGENT_CONFIG,
        TaskErrorCode.RATE_LIMITED,
        TaskErrorCode.TIMEOUT,
        TaskErrorCode.MODEL_UNAVAILABLE,
        TaskErrorCode.RUN_FAILED,
    }
)

_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]*")


@dataclass(frozen=True)
class TaskErrorInfo:
    """A task's ``error``, split into its stable ``code`` and its ``message``.

    A value, not an exception. ``code`` is kept as sent, even one this SDK
    does not know (see :data:`TASK_ERROR_CODES`); it is ``None`` when the
    error does not start with a code (the 7-day approval cancellation, for
    one), and ``message`` is then the whole error.
    """

    code: Optional[str]
    message: str

    @classmethod
    def parse(cls, raw: Optional[str]) -> Optional["TaskErrorInfo"]:
        """Parse a task's ``error`` (also the ``error`` of a stream's terminal
        payload). ``None`` when there is no error."""
        if not raw:
            return None
        code, sep, message = raw.partition(": ")
        if sep and _ERROR_CODE.fullmatch(code):
            return cls(code=code, message=message)
        return cls(code=None, message=raw)
