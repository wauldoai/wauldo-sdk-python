"""History API client — Wauldo Funnel #1 audit log.

Read-only access to a tenant's task history (every completed task is
persisted to a tenant-scoped DynamoDB audit log on the server side, and
exposed via /v1/history). Mirrors :class:`MemoryClient` shape so a
caller already familiar with the Memory API has zero ramp-up.

Three formats:

* ``list()`` — paginated JSON, suitable for dashboards.
* ``export(format="csv")`` — single CSV blob with header + footer
  metadata (compliance evidence).
* ``export(format="jsonl")`` — newline-delimited JSON, one entry per
  line + a final ``{"_export": true, ...}`` footer object for log
  pipelines.

Right To Be Forgotten (GDPR Art. 17) is supported via ``delete_task``,
which removes every audit row for a specific task id within the
caller's tenant.

Example:
    >>> from wauldo.history import HistoryClient
    >>> hist = HistoryClient(base_url="https://api.wauldo.com",
    ...                      api_key="tig_live_...", tenant="my-org")
    >>> page = hist.list(verdict="CONFLICT", limit=20)
    >>> for item in page["items"]:
    ...     print(item["task_id"], item["verdict"])
    >>> blob = hist.export(format="csv")
    >>> open("audit.csv", "wb").write(blob)
    >>> hist.delete_task("a69b8612-0c47-43f3-93f2-c00c8a4ac1f8")
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, Optional

#: Max bytes the client will accept from a single response. Protects against
#: hostile or misbehaving servers that try to stream gigabytes. Mirrors
#: the cap used by :mod:`wauldo.memory`.
MAX_RESPONSE_SIZE: int = 64 * 1024 * 1024  # 64 MB — exports can be larger than memory

# Server caps the export auto-pagination at this many rows per call.
# Documented here for client-side awareness only; the server enforces it.
EXPORT_ROW_CAP: int = 10_000


def _bounded_read(resp, limit: int = MAX_RESPONSE_SIZE) -> bytes:
    """Read a response body in chunks, capped at ``limit`` bytes."""
    chunks = []
    total = 0
    while True:
        chunk = resp.read(8192)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"response body too large: >{limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


class HistoryClient:
    """HTTP client for the Wauldo History audit log."""

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

    def _request_raw(
        self,
        method: str,
        path: str,
    ) -> bytes:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url=url, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 204:
                    return b""
                return _bounded_read(resp)
        except urllib.error.HTTPError as e:
            body_text = e.read(MAX_RESPONSE_SIZE).decode("utf-8", errors="replace")
            raise urllib.error.HTTPError(
                e.url, e.code, f"{e.reason}: {body_text}", e.headers, None
            ) from None

    def _request_json(
        self,
        method: str,
        path: str,
    ) -> Any:
        raw = self._request_raw(method, path)
        return json.loads(raw.decode("utf-8")) if raw else None

    @staticmethod
    def _build_qs(params: Dict[str, Any]) -> str:
        parts = []
        for k, v in params.items():
            if v is None:
                continue
            parts.append(f"{k}={urllib.request.quote(str(v), safe='')}")
        return ("?" + "&".join(parts)) if parts else ""

    def list(
        self,
        verdict: Optional[str] = None,
        agent_id: Optional[str] = None,
        from_ms: Optional[int] = None,
        to_ms: Optional[int] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """GET /v1/history — paginated audit log page.

        Returns ``{"items": [...], "next_cursor": str|None, "enabled": bool}``.
        ``enabled=false`` signals the server hasn't wired its DynamoDB
        store (self-host without IAM perm); ``items=[]`` with ``enabled=true``
        means the window is empty for this tenant.

        Pass ``cursor`` from a previous response's ``next_cursor`` to
        paginate. Filters compose with AND.
        """
        qs = self._build_qs({
            "verdict": verdict,
            "agent_id": agent_id,
            "from": from_ms,
            "to": to_ms,
            "limit": limit,
            "cursor": cursor,
        })
        return self._request_json("GET", f"/v1/history{qs}")

    def iter_pages(
        self,
        verdict: Optional[str] = None,
        agent_id: Optional[str] = None,
        from_ms: Optional[int] = None,
        to_ms: Optional[int] = None,
        page_size: int = 50,
    ) -> Iterator[Dict[str, Any]]:
        """Iterate over all pages, yielding the raw page dict each time.

        Convenience helper that handles cursor stitching for callers
        who want to walk the entire window without manually managing
        ``next_cursor``. Stops when the server returns ``next_cursor=None``.
        """
        cursor: Optional[str] = None
        while True:
            page = self.list(
                verdict=verdict,
                agent_id=agent_id,
                from_ms=from_ms,
                to_ms=to_ms,
                limit=page_size,
                cursor=cursor,
            )
            yield page
            cursor = page.get("next_cursor")
            if not cursor:
                break

    def export(
        self,
        format: str = "csv",
        verdict: Optional[str] = None,
        agent_id: Optional[str] = None,
        from_ms: Optional[int] = None,
        to_ms: Optional[int] = None,
    ) -> bytes:
        """GET /v1/history?format=csv|jsonl — single-blob export.

        Returns raw bytes (CSV text or JSONL). The server auto-paginates
        up to ``EXPORT_ROW_CAP`` rows; the response includes
        ``x-wauldo-row-count`` / ``x-wauldo-truncated`` headers (not
        surfaced here) and a body footer line / object signaling
        truncation. Rate-limited per tenant to 5 requests / 60s — a
        ``HTTPError(429)`` is raised on cap.
        """
        if format not in ("csv", "jsonl", "json"):
            raise ValueError(f"unsupported format '{format}' — use csv|jsonl|json")
        qs = self._build_qs({
            "format": format,
            "verdict": verdict,
            "agent_id": agent_id,
            "from": from_ms,
            "to": to_ms,
        })
        return self._request_raw("GET", f"/v1/history{qs}")

    def delete_task(self, task_id: str) -> int:
        """DELETE /v1/history/:task_id — RTBF (GDPR Art. 17).

        Removes every audit row for ``task_id`` within the caller's
        tenant. Idempotent: deleting a non-existent task returns 0.
        Returns the number of rows deleted.
        """
        if not task_id:
            raise ValueError("task_id required")
        resp = self._request_json("DELETE", f"/v1/history/{task_id}")
        if isinstance(resp, dict):
            return int(resp.get("deleted", 0))
        return 0
