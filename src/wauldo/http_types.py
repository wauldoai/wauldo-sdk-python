"""HTTP API types for OpenAI-compatible endpoints."""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel

# ── Chat Completions ─────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    """A single message in a chat conversation."""

    role: str
    content: Optional[str] = None
    name: Optional[str] = None

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role="user", content=content)

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role="system", content=content)

    @classmethod
    def assistant(cls, content: str) -> ChatMessage:
        return cls(role="assistant", content=content)


class ChatRequest(BaseModel):
    """Request body for POST /v1/chat/completions."""

    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = None
    top_p: Optional[float] = None
    stop: Optional[List[str]] = None

    @classmethod
    def quick(cls, model: str, message: str) -> ChatRequest:
        return cls(model=model, messages=[ChatMessage.user(message)])


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Optional[str] = None


class ChatResponse(BaseModel):
    """Response from POST /v1/chat/completions."""

    id: str
    object: str
    created: int
    model: str
    choices: List[ChatChoice]
    usage: Usage

    def content(self) -> str:
        """Get the text content of the first choice, or empty string."""
        if self.choices and self.choices[0].message.content:
            return self.choices[0].message.content
        return ""


# ── Models ───────────────────────────────────────────────────────────────


class Model(BaseModel):
    id: str
    object: str
    created: int
    owned_by: str


class ModelList(BaseModel):
    """Response from GET /v1/models."""

    object: str
    data: List[Model]


# ── Embeddings ───────────────────────────────────────────────────────────


class EmbeddingData(BaseModel):
    embedding: List[float]
    index: int


class EmbeddingUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingResponse(BaseModel):
    """Response from POST /v1/embeddings."""

    data: List[EmbeddingData]
    model: str
    usage: EmbeddingUsage


# ── RAG ──────────────────────────────────────────────────────────────────


class RagUploadResponse(BaseModel):
    document_id: str
    chunks_count: int


class UploadFileResponse(BaseModel):
    """Response from POST /v1/upload-file (multipart document upload).

    Mirrors the server ``UploadResponse`` in wauldo-api. Only the two
    core fields are required; the richer fields are optional so the SDK
    stays forward/backward compatible across server versions.
    """

    document_id: str
    chunks_count: int
    indexed_at: Optional[str] = None
    content_type: Optional[str] = None
    trace_id: Optional[str] = None
    quality: Optional[dict[str, Any]] = None


class RagSource(BaseModel):
    document_id: str
    content: str
    score: float
    chunk_id: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class RagAuditInfo(BaseModel):
    """Audit trail for RAG responses — verification and accountability."""

    confidence: float
    retrieval_path: str
    sources_evaluated: int
    sources_used: int
    best_score: float
    grounded: bool
    confidence_label: str
    model: str
    latency_ms: int
    # Retrieval funnel diagnostics (v1.6.5+)
    candidates_found: Optional[int] = None
    candidates_after_tenant: Optional[int] = None
    candidates_after_score: Optional[int] = None
    query_type: Optional[str] = None


class RagQueryResponse(BaseModel):
    """Response from POST /v1/query with full audit trail."""

    answer: str
    sources: List[RagSource]
    audit: Optional[RagAuditInfo] = None
    # Legacy flat fields (servers < v1.6.5)
    confidence: Optional[float] = None
    grounded: Optional[bool] = None

    def get_confidence(self) -> Optional[float]:
        """Get confidence from audit (preferred) or legacy flat field."""
        if self.audit:
            return self.audit.confidence
        return self.confidence

    def get_grounded(self) -> Optional[bool]:
        """Get grounded from audit (preferred) or legacy flat field."""
        if self.audit:
            return self.audit.grounded
        return self.grounded


# ── Orchestrator ─────────────────────────────────────────────────────────


class OrchestratorResponse(BaseModel):
    final_output: str


# ── Fact-checking / Guard ────────────────────────────────────────────────
# Mirrors the server DTOs in wauldo-api/src/dtos/quality.rs. Keep field
# names in lockstep with `FactCheckResponse` / `ClaimResult` there — this
# is the honesty-critical surface every integration (NeMo, LangChain…)
# builds on, so it must reflect the real `/v1/fact-check` shape exactly.


class ClaimResult(BaseModel):
    """A single claim extracted from the verified text."""

    text: str
    claim_type: str
    supported: bool
    confidence: float
    confidence_label: str
    verdict: str  # verified | weak | rejected
    action: str  # allow | review | block
    reason: Optional[str] = None
    evidence: Optional[str] = None


class RelevanceResult(BaseModel):
    """Relevance of the answer to the user query — decoupled from factuality.

    A response can be fully verified against sources AND off-topic for the
    question asked. This block never influences ``verdict`` / ``confidence``.
    """

    score: float  # raw cosine similarity (0.0-1.0), model-specific scale
    verdict: str  # relevant | partial | off_topic
    rationale: Optional[str] = None  # only populated by future judge modes


class FactCheckResponse(BaseModel):
    """Response from POST /v1/fact-check (the standalone guard endpoint)."""

    verdict: str  # verified | weak | rejected
    action: str  # allow | review | block
    hallucination_rate: float
    mode: str
    total_claims: int
    supported_claims: int
    confidence: float
    claims: List[ClaimResult]
    mode_warning: Optional[str] = None
    # Only present when `query` was provided AND computable. Decoupled
    # from the factual verdict above.
    relevance: Optional[RelevanceResult] = None
    # Why relevance could not be computed (query provided but embeddings
    # unavailable). Never set when `relevance` is present.
    relevance_warning: Optional[str] = None
    processing_time_ms: int


class CitationDetail(BaseModel):
    """One citation validation result."""

    citation: str
    source_name: str
    is_valid: bool


class VerifyCitationResponse(BaseModel):
    """Response from POST /v1/verify (citation validation)."""

    citation_ratio: float
    has_sufficient_citations: bool
    sentence_count: int
    citation_count: int
    uncited_sentences: List[str]
    citations: Optional[List[CitationDetail]] = None
    phantom_count: Optional[int] = None
    processing_time_ms: int
