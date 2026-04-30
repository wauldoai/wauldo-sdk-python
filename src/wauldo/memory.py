"""Memory API client — Wauldo Deploy long-term memory.

Tenant-scoped key-value store with namespaces and lexical search. Talks
to the /v1/memory endpoints. Standalone like AgentsClient — no coupling
to HttpClient.

Example:
    >>> from wauldo.memory import MemoryClient
    >>> mem = MemoryClient(base_url="http://localhost:3000", api_key="...")
    >>> mem.set("support", "ticket-123", "Customer asked about pricing",
    ...         tags=["urgent", "sales"])
    >>> results = mem.search("support", "pricing", tags=["urgent"])
    >>> print(results["results"][0]["entry"]["value"])
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

#: Max bytes the client will accept from a single response. Protects against
#: hostile or misbehaving servers that try to stream gigabytes.
MAX_RESPONSE_SIZE: int = 10 * 1024 * 1024  # 10 MB


def _bounded_read(resp, limit: int = MAX_RESPONSE_SIZE) -> bytes:
    """Read a urllib response body in chunks, capped at ``limit`` bytes.

    Raises ``ValueError`` when the body exceeds the cap.
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


class MemoryClient:
    """HTTP client for the Wauldo Memory API."""

    def __init__(
        self,
        base_url: str = "http://localhost:3000",
        api_key: Optional[str] = None,
        tenant: Optional[str] = None,
        timeout: float = 60.0,
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
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as e:
            body_text = e.read(MAX_RESPONSE_SIZE).decode("utf-8", errors="replace")
            raise urllib.error.HTTPError(
                e.url, e.code, f"{e.reason}: {body_text}", e.headers, None
            ) from None

    # ── CRUD ─────────────────────────────────────────────────────────

    def set(
        self,
        namespace: str,
        key: str,
        value: str,
        tags: Optional[List[str]] = None,
        embedding: Optional[List[float]] = None,
    ) -> Dict[str, Any]:
        """POST /v1/memory/:namespace — upsert an entry by key.

        Same key + same namespace replaces the existing entry while
        preserving its ``id`` and ``created_at``.
        """
        body: Dict[str, Any] = {"key": key, "value": value}
        if tags:
            body["tags"] = tags
        if embedding is not None:
            body["embedding"] = embedding
        return self._request("POST", f"/v1/memory/{namespace}", body)

    def get(self, namespace: str, key: str) -> Dict[str, Any]:
        """GET /v1/memory/:namespace/:key"""
        return self._request("GET", f"/v1/memory/{namespace}/{key}")

    def delete(self, namespace: str, key: str) -> None:
        """DELETE /v1/memory/:namespace/:key — returns None on success."""
        self._request("DELETE", f"/v1/memory/{namespace}/{key}")

    def list(
        self,
        namespace: str,
        limit: int = 20,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """GET /v1/memory/:namespace — paginated list, most-recent first."""
        return self._request(
            "GET", f"/v1/memory/{namespace}?limit={limit}&offset={offset}"
        )

    def search(
        self,
        namespace: str,
        query: str = "",
        tags: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """POST /v1/memory/:namespace/search — lexical search.

        Either ``query`` or ``tags`` must be non-empty. When both are
        provided, ``tags`` filters (AND) and ``query`` ranks. Response
        shape: ``{"results": [...], "total_matched": N, "mode": "lexical"}``.
        """
        if not query and not tags:
            raise ValueError("search requires query or tags (or both)")
        body: Dict[str, Any] = {"query": query}
        if tags:
            body["tags"] = tags
        if limit is not None:
            body["limit"] = limit
        return self._request("POST", f"/v1/memory/{namespace}/search", body)

    # ── Namespace sugar ──────────────────────────────────────────────
    #
    # Convenience properties that bind a namespace to the underlying
    # client so callers can write ``mem.short_term.set("k", "v")``
    # instead of ``mem.set("short_term", "k", "v")``. Pure syntactic
    # sugar — the original ``set/get/...`` API is unchanged.

    @property
    def short_term(self) -> "NamespacedMemory":
        """Sugar for namespace ``short_term`` (session/transient state)."""
        return NamespacedMemory(self, "short_term")

    @property
    def long_term(self) -> "NamespacedMemory":
        """Sugar for namespace ``long_term`` (durable user/agent facts)."""
        return NamespacedMemory(self, "long_term")

    @property
    def entity(self) -> "NamespacedMemory":
        """Sugar for namespace ``entity`` (per-entity profiles/state)."""
        return NamespacedMemory(self, "entity")

    @property
    def contextual(self) -> "NamespacedMemory":
        """Sugar for namespace ``contextual`` (per-context attachments)."""
        return NamespacedMemory(self, "contextual")


class NamespacedMemory:
    """Namespace-bound view over a :class:`MemoryClient`.

    Returned by :attr:`MemoryClient.short_term` and friends. Every method
    here forwards to the parent client with ``self.namespace`` prefilled.
    Construct manually for an arbitrary namespace if needed::

        mem = MemoryClient(...)
        custom = NamespacedMemory(mem, "my_ns")
        custom.set("k", "v")
    """

    __slots__ = ("_client", "namespace")

    def __init__(self, client: MemoryClient, namespace: str) -> None:
        self._client = client
        self.namespace = namespace

    def set(
        self,
        key: str,
        value: str,
        tags: Optional[List[str]] = None,
        embedding: Optional[List[float]] = None,
    ) -> Dict[str, Any]:
        """See :meth:`MemoryClient.set` — namespace prefilled."""
        return self._client.set(self.namespace, key, value, tags=tags, embedding=embedding)

    def get(self, key: str) -> Dict[str, Any]:
        """See :meth:`MemoryClient.get` — namespace prefilled."""
        return self._client.get(self.namespace, key)

    def delete(self, key: str) -> None:
        """See :meth:`MemoryClient.delete` — namespace prefilled."""
        self._client.delete(self.namespace, key)

    def list(self, limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        """See :meth:`MemoryClient.list` — namespace prefilled."""
        return self._client.list(self.namespace, limit=limit, offset=offset)

    def search(
        self,
        query: str = "",
        tags: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """See :meth:`MemoryClient.search` — namespace prefilled."""
        return self._client.search(self.namespace, query=query, tags=tags, limit=limit)
