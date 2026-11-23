"""Async HTTP client for Wauldo REST API (OpenAI-compatible)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, List, Optional, Union

from .async_transport import AsyncHttpTransport
from .exceptions import ValidationError
from .http_streaming import raise_for_stream_error
from .http_types import (
    ChatRequest,
    ChatResponse,
    EmbeddingResponse,
    FactCheckResponse,
    ModelList,
    OrchestratorResponse,
    RagQueryResponse,
    RagUploadResponse,
    UploadFileResponse,
    VerifyCitationResponse,
)
from .multipart import MAX_UPLOAD_FILE_SIZE, encode_text_upload, encode_upload_file
from .pii import check_pii_policy

logger = logging.getLogger("wauldo")


class AsyncHttpClient:
    """Async HTTP client for the Wauldo REST API.

    Covers all OpenAI-compatible endpoints plus RAG and orchestrator.
    Requires aiohttp: pip install wauldo[async]
    """

    def __init__(
        self,
        base_url: str = "http://localhost:3000",
        api_key: Optional[str] = None,
        timeout: int = 120,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        headers: Optional[dict[str, str]] = None,
        on_request: Optional[Any] = None,
        on_response: Optional[Any] = None,
        on_error: Optional[Any] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._extra_headers: dict[str, str] = headers or {}
        self._transport = AsyncHttpTransport(
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            headers_fn=self._headers,
            on_request=on_request,
            on_response=on_response,
            on_error=on_error,
        )

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        h.update(self._extra_headers)
        return h

    async def _request(
        self, method: str, path: str, body: Optional[dict[str, Any]] = None, timeout_ms: Optional[int] = None,
    ) -> bytes:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body else None
        return await self._transport.execute(method, url, data=data, timeout_ms=timeout_ms)

    async def close(self) -> None:
        await self._transport.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def list_models(self) -> ModelList:
        """GET /v1/models"""
        data = await self._request("GET", "/v1/models")
        return ModelList.model_validate_json(data)

    async def chat(
        self, request: ChatRequest, timeout_ms: Optional[int] = None,
    ) -> ChatResponse:
        """POST /v1/chat/completions (non-streaming)."""
        if not request.messages:
            raise ValueError("messages cannot be empty")
        req = request.model_copy(update={"stream": False})
        data = await self._request(
            "POST", "/v1/chat/completions", req.model_dump(exclude_none=True), timeout_ms=timeout_ms,
        )
        return ChatResponse.model_validate_json(data)

    async def chat_simple(
        self, model: str, message: str, timeout_ms: Optional[int] = None, **kwargs: object,
    ) -> str:
        """Single message chat, returns content string."""
        req = ChatRequest.quick(model, message)
        if kwargs:
            req = req.model_copy(update=kwargs)
        resp = await self.chat(req, timeout_ms=timeout_ms)
        return resp.choices[0].message.content or ""

    async def chat_stream(self, request: ChatRequest) -> AsyncIterator[str]:
        """POST /v1/chat/completions -- SSE streaming, yields content chunks."""
        import aiohttp

        req = request.model_copy(update={"stream": True})
        url = f"{self.base_url}/v1/chat/completions"
        data = json.dumps(req.model_dump(exclude_none=True)).encode()

        session = await self._transport._get_session()
        ct = aiohttp.ClientTimeout(total=self.timeout)
        async with session.post(url, data=data, headers=self._headers(), timeout=ct) as resp:
            if resp.status >= 400:
                body = await resp.read()
                raise Exception(f"HTTP {resp.status}: {body.decode(errors='replace')}")
            buf = ""
            async for chunk in resp.content.iter_any():
                buf += chunk.decode(errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload == "[DONE]":
                        return
                    try:
                        parsed = json.loads(payload)
                        raise_for_stream_error(parsed)
                        choices = parsed.get("choices", [])
                        if choices:
                            content = choices[0].get("delta", {}).get("content")
                            if content:
                                yield content
                    except json.JSONDecodeError:
                        logger.warning("Malformed SSE chunk: %s", payload[:100])

    async def embeddings(self, input: Union[str, List[str]], model: str) -> EmbeddingResponse:
        """POST /v1/embeddings"""
        body = {"input": input, "model": model}
        data = await self._request("POST", "/v1/embeddings", body)
        return EmbeddingResponse.model_validate_json(data)

    _MAX_UPLOAD_SIZE = MAX_UPLOAD_FILE_SIZE  # 10 MB, the whole request body

    async def rag_upload(
        self,
        content: str,
        filename: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        pii_policy: Optional[str] = None,
    ) -> RagUploadResponse:
        """POST /v1/upload -- ``pii_policy``: ``"redact"`` (server default) or ``"keep"``.

        Same checks as :meth:`HttpClient.rag_upload`: empty content, or a
        request over 10 MB once encoded as JSON, raises ``ValidationError``
        before anything is sent.
        """
        if not content.strip():
            raise ValidationError("Content cannot be empty", field="content")
        body: dict[str, Any] = {"content": content}
        if filename:
            body["filename"] = filename
        if pii_policy is not None:
            body["pii_policy"] = check_pii_policy(pii_policy)
        data = await self._transport.execute(
            "POST", f"{self.base_url}/v1/upload", data=encode_text_upload(body),
            timeout_ms=timeout_ms,
        )
        return RagUploadResponse.model_validate_json(data)

    async def upload_file(
        self,
        file_path: str,
        title: Optional[str] = None,
        *,
        timeout_ms: Optional[int] = None,
        pii_policy: Optional[str] = None,
    ) -> UploadFileResponse:
        """POST /v1/upload-file -- ``pii_policy``: ``"redact"`` (server default) or ``"keep"``.

        Same encoding and checks as :meth:`HttpClient.upload_file`: a missing
        or empty file, or a request over 10 MB (file name and form fields
        included), raises ``ValidationError`` before anything is sent.
        """
        fields: dict[str, str] = {}
        if title:
            fields["title"] = title
        if pii_policy is not None:
            fields["pii_policy"] = check_pii_policy(pii_policy)
        # No `tags`: the server parsed the field, packed it, and read it back
        # nowhere — only `title` and `source_url` ever reach a chunk. Sending
        # it suggested documents were tagged for later filtering; they were
        # not. `timeout_ms` is keyword-only so dropping the parameter cannot
        # quietly slide a third positional argument into it.

        # Read and encoded off the event loop (up to 10 MB). The multipart
        # Content-Type goes with this request only: other requests of this
        # client keep sending JSON while the upload is in flight.
        body, content_type = await asyncio.to_thread(encode_upload_file, file_path, fields)
        data = await self._transport.execute(
            "POST", f"{self.base_url}/v1/upload-file", data=body,
            timeout_ms=timeout_ms, content_type=content_type,
        )
        return UploadFileResponse.model_validate_json(data)

    async def rag_query(
        self,
        query: str,
        top_k: int = 5,
        timeout_ms: Optional[int] = None,
        debug: bool = False,
        quality_mode: Optional[str] = None,
    ) -> RagQueryResponse:
        """POST /v1/query"""
        if not query.strip():
            raise ValidationError("Query cannot be empty", field="query")
        if not 1 <= top_k <= 100:
            raise ValidationError("top_k must be between 1 and 100", field="top_k")
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if debug:
            body["debug"] = True
        if quality_mode:
            body["quality_mode"] = quality_mode
        data = await self._request("POST", "/v1/query", body, timeout_ms=timeout_ms)
        return RagQueryResponse.model_validate_json(data)

    async def orchestrate(self, prompt: str) -> OrchestratorResponse:
        """POST /v1/orchestrator/execute"""
        data = await self._request("POST", "/v1/orchestrator/execute", {"prompt": prompt})
        return OrchestratorResponse.model_validate_json(data)

    async def orchestrate_parallel(self, prompt: str) -> OrchestratorResponse:
        """POST /v1/orchestrator/parallel"""
        data = await self._request("POST", "/v1/orchestrator/parallel", {"prompt": prompt})
        return OrchestratorResponse.model_validate_json(data)

    async def fact_check(
        self,
        text: str,
        source_context: str,
        mode: str = "lexical",
        query: "str | None" = None,
        relevance_mode: "str | None" = None,
    ) -> FactCheckResponse:
        """POST /v1/fact-check — Standalone hallucination guard.

        Async counterpart of ``HttpClient.fact_check``. ``source_context`` is
        required (the server rejects a missing context with 400). Returns a
        typed verdict (``verified`` / ``weak`` / ``rejected``) and recommended
        ``action`` (``allow`` / ``review`` / ``block``). When ``query`` is
        provided, the response includes a ``relevance`` block decoupled from
        the factual verdict (``relevance_mode``: only ``"fast"`` currently).
        """
        if not text:
            raise ValidationError("text cannot be empty", field="text")
        if not source_context:
            raise ValidationError(
                "source_context is required for verification", field="source_context"
            )
        if mode not in ("lexical", "hybrid", "semantic"):
            raise ValidationError(
                f"mode must be one of ('lexical', 'hybrid', 'semantic'), got {mode!r}",
                field="mode",
            )
        if relevance_mode is not None and query is None:
            raise ValidationError(
                "relevance_mode requires query to be provided", field="relevance_mode"
            )
        body: dict = {"text": text, "source_context": source_context, "mode": mode}
        if query is not None:
            body["query"] = query
        if relevance_mode is not None:
            body["relevance_mode"] = relevance_mode
        data = await self._request("POST", "/v1/fact-check", body)
        return FactCheckResponse.model_validate_json(data)

    async def guard(
        self,
        text: str,
        source_context: str,
        mode: str = "lexical",
        query: "str | None" = None,
        relevance_mode: "str | None" = None,
    ) -> FactCheckResponse:
        """Deprecated alias for :meth:`fact_check`. Kept for back-compat."""
        return await self.fact_check(text, source_context, mode, query, relevance_mode)

    async def verify_citation(
        self,
        text: str,
        sources: "list[dict] | None" = None,
        threshold: "float | None" = None,
    ) -> VerifyCitationResponse:
        """POST /v1/verify — citation validation."""
        body: dict = {"text": text}
        if sources is not None:
            body["sources"] = sources
        if threshold is not None:
            body["threshold"] = threshold
        data = await self._request("POST", "/v1/verify", body)
        return VerifyCitationResponse.model_validate_json(data)

    def conversation(
        self, system: Optional[str] = None, model: str = "default",
    ):
        """Create a stateful Conversation bound to this async client."""
        from .conversation import Conversation
        return Conversation(self, system=system, model=model)  # type: ignore[arg-type]

    async def get_insights(self) -> "InsightsResponse":
        """GET /v1/insights"""
        from .http_types import InsightsResponse
        data = await self._request("GET", "/v1/insights")
        return InsightsResponse.model_validate_json(data)

    async def get_analytics(self, minutes: int = 60) -> "AnalyticsResponse":
        """GET /v1/analytics"""
        from .http_types import AnalyticsResponse
        data = await self._request("GET", f"/v1/analytics?minutes={minutes}")
        return AnalyticsResponse.model_validate_json(data)

    async def get_analytics_traffic(self) -> "TrafficSummary":
        """GET /v1/analytics/traffic — traffic across every account of the
        server. Requires an administrator credential: any other key or token
        gets HTTP 403 (raised as :class:`ServerError` with ``code=403``)."""
        from .http_types import TrafficSummary
        data = await self._request("GET", "/v1/analytics/traffic")
        return TrafficSummary.model_validate_json(data)

    async def rag_ask(self, question: str, text: str, source: str = "document") -> str:
        """Upload text and query in one call. Returns answer string."""
        await self.rag_upload(content=text, filename=source)
        result = await self.rag_query(question)
        return result.answer
