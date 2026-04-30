"""Typed response models for the Wauldo Agents + Tasks API.

These dataclasses mirror the JSON shapes returned by the server. The
SDK's :class:`AgentsClient` returns instances of these classes so callers
get IDE completion and type checking instead of raw ``Dict[str, Any]``.

Unknown fields on the wire are preserved in :attr:`_extra` so additions
on the server side don't break old SDK versions at parse time — the SDK
keeps working, new fields just aren't typed yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Enums (string values matching the server wire format) ────────────────


class Verdict(str):
    """Verification verdict returned on completed tasks.

    Server-side enum from ``wauldo-api::routes::tasks::types::TaskVerification``.
    String subclass so comparisons with raw strings keep working.
    """

    SAFE = "SAFE"
    UNCERTAIN = "UNCERTAIN"
    PARTIAL = "PARTIAL"
    BLOCK = "BLOCK"
    CONFLICT = "CONFLICT"
    UNVERIFIED = "UNVERIFIED"


class TaskStatus(str):
    """Task lifecycle status."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ── Agents ───────────────────────────────────────────────────────────────


@dataclass
class Agent:
    """Deployed-agent record (``GET /v1/agents/:id``)."""

    id: str
    tenant_id: str
    name: str
    description: str
    wauldo_toml: str
    model_provider: str
    model_name: str
    created_at: int
    updated_at: int
    agents_md: Optional[str] = None
    mcp_json: Optional[str] = None
    preset: Optional[str] = None
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Agent":
        known = {f.name for f in cls.__dataclass_fields__.values()} - {"_extra"}
        extra = {k: v for k, v in d.items() if k not in known}
        return cls(
            id=d["id"],
            tenant_id=d["tenant_id"],
            name=d["name"],
            description=d.get("description", ""),
            wauldo_toml=d["wauldo_toml"],
            model_provider=d.get("model_provider", ""),
            model_name=d.get("model_name", ""),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
            agents_md=d.get("agents_md"),
            mcp_json=d.get("mcp_json"),
            preset=d.get("preset"),
            _extra=extra,
        )


@dataclass
class AgentPagination:
    total: int
    limit: int
    offset: int


@dataclass
class AgentList:
    agents: List[Agent]
    pagination: AgentPagination

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentList":
        return cls(
            agents=[Agent.from_dict(a) for a in d.get("agents", [])],
            pagination=AgentPagination(**d.get("pagination", {"total": 0, "limit": 0, "offset": 0})),
        )


@dataclass
class AgentRunResponse:
    """Return shape of ``POST /v1/agents/:id/runs``."""

    task_id: str
    agent_id: str
    status: str
    created_at: int

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentRunResponse":
        return cls(
            task_id=d["task_id"],
            agent_id=d.get("agent_id", ""),
            status=d.get("status", "queued"),
            created_at=d.get("created_at", 0),
        )


# ── Tasks ────────────────────────────────────────────────────────────────


@dataclass
class TaskClaim:
    """A single verified claim from the agent output."""

    text: str
    supported: bool
    confidence: float


@dataclass
class TaskVerification:
    """Verification block attached to completed tasks.

    See the ``verdict`` enum :class:`Verdict` for possible values. When
    ``verification_source == "prompt_only"`` the ``confidence`` and
    ``hallucination_rate`` fields reflect self-consistency only; rely on
    ``verdict`` + ``support_score`` + ``message`` as authoritative.

    .. note::
        ``support_score`` is the public name for the same numeric value
        the wire protocol calls ``trust_score`` (0-1 fraction of claims
        supported by the sources). Both attributes are populated and
        always equal — ``trust_score`` is kept for backward compatibility
        with code written before the 2026-04-17 reframe and the JSON
        wire field is unchanged.
    """

    verdict: str
    hallucination_rate: float
    confidence: float
    trust_score: float
    verification_source: str
    claims: List[TaskClaim] = field(default_factory=list)
    verification_retries: int = 0
    message: Optional[str] = None
    sources_cited: List[int] = field(default_factory=list)
    stripped_claims: List[str] = field(default_factory=list)

    @property
    def support_score(self) -> float:
        """Fraction of claims supported by the sources (0-1).

        Alias for :attr:`trust_score`. The wire format keeps
        ``trust_score`` for backward compatibility; new code should
        prefer ``support_score`` to match the public marketing name.
        """
        return self.trust_score

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskVerification":
        return cls(
            verdict=d.get("verdict", ""),
            hallucination_rate=float(d.get("hallucination_rate", 0.0)),
            confidence=float(d.get("confidence", 0.0)),
            trust_score=float(d.get("trust_score", 0.0)),
            verification_source=d.get("verification_source", ""),
            claims=[
                TaskClaim(
                    text=c.get("text", ""),
                    supported=bool(c.get("supported", False)),
                    confidence=float(c.get("confidence", 0.0)),
                )
                for c in d.get("claims", [])
            ],
            verification_retries=int(d.get("verification_retries", 0)),
            message=d.get("message"),
            sources_cited=list(d.get("sources_cited", [])),
            stripped_claims=list(d.get("stripped_claims", [])),
        )


@dataclass
class Task:
    """Full task record (``GET /v1/tasks/:id``)."""

    task_id: str
    tenant_id: str
    status: str
    prompt: str
    created_at: int
    updated_at: int
    preset: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    partial_result: Optional[str] = None
    verification: Optional[TaskVerification] = None
    journal: Optional[Dict[str, Any]] = None
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_done(self) -> bool:
        return self.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Task":
        known = {f.name for f in cls.__dataclass_fields__.values()} - {"_extra"}
        extra = {k: v for k, v in d.items() if k not in known}
        verif_raw = d.get("verification")
        return cls(
            task_id=d.get("task_id", d.get("id", "")),
            tenant_id=d.get("tenant_id", ""),
            status=d.get("status", ""),
            prompt=d.get("prompt", ""),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
            preset=d.get("preset"),
            result=d.get("result"),
            error=d.get("error"),
            partial_result=d.get("partial_result"),
            verification=TaskVerification.from_dict(verif_raw) if isinstance(verif_raw, dict) else None,
            journal=d.get("journal"),
            _extra=extra,
        )


# ── SSE streaming ────────────────────────────────────────────────────────


@dataclass
class StateTransition:
    """Single event yielded by ``GET /v1/tasks/:id/stream``.

    Each SSE ``data:`` line is a JSON-serialised StateTransition emitted
    when a workflow state completes. Iterate :meth:`AgentsClient.stream_task`
    to consume them in real time.
    """

    state_name: str
    condition: str
    raw_output: str
    timestamp: int
    success: bool
    retry_count: int
    duration_ms: int
    prompt_tokens: int
    completion_tokens: int
    repair_count: int
    cache_hit: bool
    to_state: Optional[str] = None
    validation_notes: List[str] = field(default_factory=list)
    _extra: Dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StateTransition":
        known = {f.name for f in cls.__dataclass_fields__.values()} - {"_extra"}
        extra = {k: v for k, v in d.items() if k not in known}
        return cls(
            state_name=d.get("state_name", ""),
            condition=d.get("condition", ""),
            raw_output=d.get("raw_output", ""),
            timestamp=int(d.get("timestamp", 0)),
            success=bool(d.get("success", False)),
            retry_count=int(d.get("retry_count", 0)),
            duration_ms=int(d.get("duration_ms", 0)),
            prompt_tokens=int(d.get("prompt_tokens", 0)),
            completion_tokens=int(d.get("completion_tokens", 0)),
            repair_count=int(d.get("repair_count", 0)),
            cache_hit=bool(d.get("cache_hit", False)),
            to_state=d.get("to_state"),
            validation_notes=list(d.get("validation_notes", [])),
            _extra=extra,
        )
