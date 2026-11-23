"""``rag_upload`` (POST /v1/upload, JSON): the 10 MB limit counts the bytes
of the request as sent, not the characters of the text."""

from __future__ import annotations

import asyncio
import json
from typing import Any, List, Optional

import pytest

import wauldo.async_transport as async_transport
from wauldo.async_client import AsyncHttpClient
from wauldo.exceptions import ValidationError
from wauldo.http_client import HttpClient
from wauldo.multipart import MAX_UPLOAD_FILE_SIZE

RESPONSE = json.dumps({"document_id": "doc-1", "chunks_count": 1}).encode()
# `{"content": ""}`: what the body adds around the text.
OVERHEAD = len(json.dumps({"content": ""}))


def _content_of_encoded_size(size: int) -> str:
    """A text whose upload body is exactly ``size`` bytes, a million of its
    characters non-ASCII (2 bytes each in UTF-8)."""
    accents = "é" * 1_000_000
    return accents + "a" * (size - OVERHEAD - 2 * len(accents))


class _Recorder:
    def __init__(self) -> None:
        self.sent: List[bytes] = []

    def execute(self, method: str, url: str, data: Optional[bytes] = None, **_kw: Any) -> bytes:
        assert method == "POST" and url.endswith("/v1/upload")
        self.sent.append(data or b"")
        return RESPONSE

    async def execute_async(
        self, method: str, url: str, data: Optional[bytes] = None, **kw: Any
    ) -> bytes:
        return self.execute(method, url, data, **kw)


def _sync_client() -> "tuple[HttpClient, _Recorder]":
    client = HttpClient(base_url="http://api.test")
    rec = _Recorder()
    client._transport.execute = rec.execute  # type: ignore[method-assign]
    return client, rec


def _async_client(monkeypatch) -> "tuple[AsyncHttpClient, _Recorder]":
    monkeypatch.setattr(async_transport, "_import_aiohttp", lambda: object())
    client = AsyncHttpClient(base_url="http://api.test")
    rec = _Recorder()
    client._transport.execute = rec.execute_async  # type: ignore[method-assign]
    return client, rec


def test_rag_upload_limit_counts_encoded_bytes_at_the_boundary():
    client, rec = _sync_client()
    fits = _content_of_encoded_size(MAX_UPLOAD_FILE_SIZE)
    assert client.rag_upload(fits).document_id == "doc-1"
    assert len(rec.sent[0]) == MAX_UPLOAD_FILE_SIZE
    assert json.loads(rec.sent[0].decode("utf-8")) == {"content": fits}
    assert "é".encode("utf-8") in rec.sent[0], "sent as UTF-8, not escaped"
    with pytest.raises(ValidationError, match="10 MB limit once encoded"):
        client.rag_upload(fits + "a")
    assert len(rec.sent) == 1


def test_rag_upload_refuses_non_ascii_text_under_the_limit_in_characters():
    # 4 M CJK characters: 4 M characters, 12 MB of UTF-8. The server refuses
    # the body unread; the client now says so before sending.
    client, rec = _sync_client()
    text = "中" * 4_000_000
    assert len(text) < MAX_UPLOAD_FILE_SIZE
    with pytest.raises(ValidationError) as exc:
        client.rag_upload(text)
    assert exc.value.field == "content"
    assert rec.sent == []


def test_async_rag_upload_limit_counts_encoded_bytes_at_the_boundary(monkeypatch):
    client, rec = _async_client(monkeypatch)
    fits = _content_of_encoded_size(MAX_UPLOAD_FILE_SIZE)
    assert asyncio.run(client.rag_upload(fits)).chunks_count == 1
    assert len(rec.sent[0]) == MAX_UPLOAD_FILE_SIZE
    # The file name is part of the body too.
    with pytest.raises(ValidationError, match="10 MB limit once encoded"):
        asyncio.run(client.rag_upload(fits, filename="f.txt"))
    with pytest.raises(ValidationError, match="10 MB limit once encoded"):
        asyncio.run(client.rag_upload("中" * 4_000_000))
    assert len(rec.sent) == 1
