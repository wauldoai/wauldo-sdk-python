"""``HttpClient.upload_file`` (POST /v1/upload-file, multipart) and the CLI
``upload`` command that calls it."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Tuple

import pytest

import wauldo.async_transport as async_transport
import wauldo.multipart as multipart
from wauldo import cli
from wauldo.async_client import AsyncHttpClient
from wauldo.exceptions import ValidationError
from wauldo.http_client import HttpClient
from wauldo.mock_client import MockHttpClient

UPLOAD_RESPONSE = {
    "document_id": "doc-1",
    "chunks_count": 3,
    "indexed_at": "2026-11-22T00:00:00Z",
    "content_type": "pdf",
    "trace_id": "tr",
    "quality": {"score": 0.8, "label": "high"},
}


class _Handler(BaseHTTPRequestHandler):
    requests: List[Dict[str, Any]] = []

    def log_message(self, *_a, **_k) -> None:
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        _Handler.requests.append(
            {
                "path": self.path,
                "content_type": self.headers.get("Content-Type", ""),
                "body": self.rfile.read(n),
            }
        )
        payload = json.dumps(UPLOAD_RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def base_url():
    _Handler.requests = []
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "contract.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return str(path)


def test_upload_file_sends_multipart_with_title_and_pii_policy(base_url, pdf):
    resp = HttpClient(base_url=base_url).upload_file(pdf, "Q3 Contract", pii_policy="keep")
    assert resp.document_id == "doc-1"
    assert resp.content_type == "pdf"
    req = _Handler.requests[0]
    assert req["path"] == "/v1/upload-file"
    assert req["content_type"].startswith("multipart/form-data; boundary=")
    body = req["body"]
    assert b'name="file"; filename="contract.pdf"' in body
    assert b"%PDF-1.4 fake" in body
    assert b'name="title"\r\n\r\nQ3 Contract\r\n' in body
    assert b'name="pii_policy"\r\n\r\nkeep\r\n' in body


def test_upload_file_sends_only_the_file_by_default(base_url, pdf):
    HttpClient(base_url=base_url).upload_file(pdf)
    body = _Handler.requests[0]["body"]
    assert b'name="title"' not in body
    assert b'name="pii_policy"' not in body


def test_upload_file_refuses_before_sending(base_url, pdf, tmp_path, monkeypatch):
    client = HttpClient(base_url=base_url)
    with pytest.raises(ValidationError):
        client.upload_file(str(tmp_path / "missing.pdf"))
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(ValidationError):
        client.upload_file(str(empty))
    with pytest.raises(ValidationError):
        client.upload_file(pdf, pii_policy="tag")
    monkeypatch.setattr(multipart, "MAX_UPLOAD_FILE_SIZE", 4)
    with pytest.raises(ValidationError, match="10 MB"):
        client.upload_file(pdf)
    assert _Handler.requests == []


def test_multipart_escapes_the_file_name_and_avoids_the_data():
    data = b"----WauldoBoundary" * 4
    body, content_type = multipart.encode_multipart('a"b\r\n.pdf', data, {})
    boundary = content_type.split("boundary=", 1)[1]
    assert b'filename="a_b__.pdf"' in body
    assert body.endswith(f"--{boundary}--\r\n".encode())
    assert boundary.encode() not in data


def _file_of(tmp_path, name: str, size: int) -> str:
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return str(path)


def test_upload_file_limit_counts_the_whole_request(base_url, tmp_path):
    # The server's 10 MB limit applies to the multipart body, not the file:
    # a file of exactly 10 MB used to pass the check and be refused unread.
    client = HttpClient(base_url=base_url)
    overhead = len(multipart.encode_multipart("big.pdf", b"", {"title": "T"})[0])
    fits = multipart.MAX_UPLOAD_FILE_SIZE - overhead
    client.upload_file(_file_of(tmp_path, "big.pdf", fits), "T")
    assert len(_Handler.requests[0]["body"]) == multipart.MAX_UPLOAD_FILE_SIZE
    with pytest.raises(ValidationError, match="file name and form fields"):
        client.upload_file(_file_of(tmp_path, "big.pdf", fits + 1), "T")
    with pytest.raises(ValidationError, match="10 MB"):
        client.upload_file(_file_of(tmp_path, "big.pdf", multipart.MAX_UPLOAD_FILE_SIZE))
    assert len(_Handler.requests) == 1


class _FakeCall:
    """One request of :class:`_FakeSession`. The upload stays in flight for a
    moment: the window in which another coroutine of the same client sends."""

    status = 200
    headers: Dict[str, str] = {}

    def __init__(self, upload: bool) -> None:
        self.upload = upload

    async def __aenter__(self) -> "_FakeCall":
        if self.upload:
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *_exc: Any) -> bool:
        return False

    async def read(self) -> bytes:
        return json.dumps(UPLOAD_RESPONSE if self.upload else {"ok": True}).encode()


class _FakeSession:
    """Stands in for the aiohttp session: records each request's headers as
    the transport computes them."""

    closed = False

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def request(self, method, url, data=None, headers=None, timeout=None) -> _FakeCall:
        self.sent.append({"url": url, "headers": dict(headers or {}), "body": data})
        return _FakeCall(url.endswith("/v1/upload-file"))


class _ClientError(Exception):
    pass


class _FakeAiohttp:
    ClientError = _ClientError

    @staticmethod
    def ClientTimeout(total: float) -> None:  # noqa: N802 - mirrors aiohttp
        return None


def _async_client(monkeypatch) -> Tuple[AsyncHttpClient, _FakeSession]:
    monkeypatch.setattr(async_transport, "_import_aiohttp", lambda: _FakeAiohttp)
    client = AsyncHttpClient(base_url="http://api.test", api_key="k")
    session = _FakeSession()

    async def get_session() -> _FakeSession:
        return session

    client._transport._get_session = get_session
    return client, session


def test_async_upload_does_not_leak_its_content_type_to_concurrent_requests(monkeypatch, pdf):
    client, session = _async_client(monkeypatch)

    async def both() -> None:
        await asyncio.gather(
            client.upload_file(pdf, "Q3"),
            client._request("POST", "/v1/query", {"query": "q"}),
        )

    asyncio.run(both())
    by_path = {s["url"].rsplit("/", 1)[1]: s["headers"] for s in session.sent}
    assert by_path["query"]["Content-Type"] == "application/json"
    assert by_path["query"]["Authorization"] == "Bearer k"
    assert by_path["upload-file"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert by_path["upload-file"]["Authorization"] == "Bearer k"


def test_async_upload_uses_the_safe_encoder(monkeypatch, tmp_path):
    client, session = _async_client(monkeypatch)
    path = tmp_path / 'été "q3".pdf'
    path.write_bytes(b"%PDF-1.4 fake")
    asyncio.run(client.upload_file(str(path)))
    asyncio.run(client.upload_file(str(path)))
    first, second = session.sent
    boundary = first["headers"]["Content-Type"].split("boundary=", 1)[1]
    assert boundary != second["headers"]["Content-Type"].split("boundary=", 1)[1]
    assert first["body"].endswith(f"--{boundary}--\r\n".encode())
    # Raw UTF-8, as a browser form sends it; the quotes are replaced so the
    # name cannot end its header early.
    assert 'filename="été _q3_.pdf"'.encode() in first["body"]
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    with pytest.raises(ValidationError, match="empty"):
        asyncio.run(client.upload_file(str(empty)))
    big = _file_of(tmp_path, "big.pdf", multipart.MAX_UPLOAD_FILE_SIZE)
    with pytest.raises(ValidationError, match="10 MB"):
        asyncio.run(client.upload_file(big))
    assert len(session.sent) == 2


def test_cli_upload_of_a_pdf_reaches_upload_file(base_url, pdf, monkeypatch, capsys):
    monkeypatch.setenv("WAULDO_API_KEY", "k")
    monkeypatch.setenv("WAULDO_BASE_URL", base_url)
    cli.main(["upload", pdf, "--title", "Q3", "--tags", "a,b"])
    assert "doc-1" in capsys.readouterr().out
    assert b'name="title"\r\n\r\nQ3\r\n' in _Handler.requests[0]["body"]


def test_cli_upload_of_a_pdf_in_mock_mode(pdf, capsys):
    cli.main(["--mock", "upload", pdf])
    assert "mock-doc-001" in capsys.readouterr().out


def test_mock_client_upload_file_matches_the_real_signature():
    resp = MockHttpClient().upload_file("x.pdf", "T", timeout_ms=5, pii_policy="keep")
    assert resp.chunks_count == 3
