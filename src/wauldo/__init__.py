"""
Wauldo Python SDK

Client interfaces:
- AgentsClient — deployed agents: create, run, wait for or stream verified results
- HttpClient — REST API (OpenAI-compatible) for chat, embeddings, RAG, orchestrator
- MemoryClient, WorkflowsClient — agent memory and state-machine workflows
- AgentClient / AsyncAgentClient — MCP server (stdio JSON-RPC) for reasoning, planning, tools
"""

from .agents import (
    ALREADY_FINISHED_CONDITION,
    DEFAULT_WAIT_TIMEOUT_SEC,
    LONG_POLL_SEC,
    MAX_WAIT_SEC,
    RETRY_STATUSES,
    RETRY_STATUSES_IDEMPOTENT,
    STREAM_FALLBACK_TIMEOUT_SEC,
    WITHHELD_RESULT,
    AgentsClient,
)
from .agents_hitl import MAX_HITL_REASON_CHARS, PAUSED_EXPIRED_ERROR
from .agents_types import (
    TERMINAL_CONDITIONS,
    TERMINAL_STATE_NAMES,
    Agent,
    AgentList,
    AgentPagination,
    AgentRevision,
    AgentRunResponse,
    CreateRevisionResponse,
    HitlResponse,
    ListRevisionsResponse,
    ShareResponse,
    StateTransition,
    Task,
    TaskClaim,
    TaskDecision,
    TaskStatus,
    TaskVerification,
    Verdict,
)
from .client import AgentClient, AsyncAgentClient
from .conversation import Conversation
from .exceptions import (
    AgentConnectionError,
    AgentTimeoutError,
    HttpError,
    ServerError,
    TaskTimeoutError,
    ToolNotFoundError,
    ValidationError,
    WauldoError,
)
from .http_client import HttpClient
from .http_types import (
    ChatMessage as HttpChatMessage,
)
from .http_types import (
    ChatRequest,
    ChatResponse,
    CitationDetail,
    ClaimResult,
    EmbeddingResponse,
    FactCheckResponse,
    ModelList,
    OrchestratorResponse,
    RagAuditInfo,
    RagQueryResponse,
    RagSource,
    RagUploadResponse,
    RelevanceResult,
    UploadFileResponse,
    VerifyCitationResponse,
)
from .history import HistoryClient
from .memory import MemoryClient
from .mock_client import MockHttpClient
from .models import (
    Chunk,
    ChunkResult,
    Concept,
    ConceptResult,
    GraphNode,
    KnowledgeGraphResult,
    PlanResult,
    PlanStep,
    ReasoningResult,
    RetrievalResult,
    ToolDefinition,
    ToolsListResult,
)
from .task_errors import TASK_ERROR_CODES, TaskErrorCode, TaskErrorInfo
from .workflows import WorkflowsClient

__version__ = "0.22.0"
__all__ = [
    # Deployed agents + tasks
    "AgentsClient",
    "Agent",
    "AgentList",
    "AgentPagination",
    "AgentRevision",
    "AgentRunResponse",
    "CreateRevisionResponse",
    "ListRevisionsResponse",
    "ShareResponse",
    "HitlResponse",
    "Task",
    "TaskStatus",
    "TaskVerification",
    "TaskClaim",
    "TaskDecision",
    "Verdict",
    "StateTransition",
    "TaskErrorInfo",
    "TaskErrorCode",
    "TASK_ERROR_CODES",
    "TERMINAL_CONDITIONS",
    "TERMINAL_STATE_NAMES",
    "ALREADY_FINISHED_CONDITION",
    "WITHHELD_RESULT",
    "DEFAULT_WAIT_TIMEOUT_SEC",
    "LONG_POLL_SEC",
    "MAX_WAIT_SEC",
    "STREAM_FALLBACK_TIMEOUT_SEC",
    "RETRY_STATUSES",
    "RETRY_STATUSES_IDEMPOTENT",
    "PAUSED_EXPIRED_ERROR",
    "MAX_HITL_REASON_CHARS",
    "MemoryClient",
    "WorkflowsClient",
    # MCP Clients
    "AgentClient",
    "AsyncAgentClient",
    # HTTP Client
    "HttpClient",
    "HistoryClient",
    "MockHttpClient",
    "Conversation",
    "ChatRequest",
    "ChatResponse",
    "HttpChatMessage",
    "EmbeddingResponse",
    "ModelList",
    "OrchestratorResponse",
    "RagAuditInfo",
    "RagQueryResponse",
    "RagSource",
    "RagUploadResponse",
    # Fact-checking / Guard
    "FactCheckResponse",
    "ClaimResult",
    "RelevanceResult",
    "VerifyCitationResponse",
    "CitationDetail",
    "UploadFileResponse",
    # Exceptions
    "WauldoError",
    "AgentConnectionError",
    "AgentTimeoutError",
    "TaskTimeoutError",
    "HttpError",
    "ServerError",
    "ToolNotFoundError",
    "ValidationError",
    # MCP Models
    "ReasoningResult",
    "ConceptResult",
    "Concept",
    "ChunkResult",
    "Chunk",
    "RetrievalResult",
    "KnowledgeGraphResult",
    "PlanResult",
    "PlanStep",
    "GraphNode",
    "ToolDefinition",
    "ToolsListResult",
]
