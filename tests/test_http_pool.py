"""Keep-alive pool: one connection reused, a closed one replaced, a POST never sent twice.

A local HTTP/1.1 server counts the TCP connections it accepts. By default it
answers every request with the path it was asked for.
"""

from __future__ import annotations

import copy
import json
import os
import pickle
import socket
import threading
import urllib.request
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from wauldo import http_pool
from wauldo.agents import AgentsClient
from wauldo.exceptions import ServerError, WauldoError
from wauldo.http_pool import ConnectionPool
from wauldo.http_transport import HttpTransport

Reply = tuple[int, dict[str, str], bytes]


def _json(status: int, body: dict[str, Any]) -> Reply:
    return status, {"Content-Type": "application/json"}, json.dumps(body).encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Server

    def log_message(self, *_args: object) -> None:
        pass

    def setup(self) -> None:
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def _serve(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        srv = self.server
        with srv.lock:
            srv.requests.append((self.command, self.path, dict(self.headers.items())))
            number = len(srv.requests)
            replies = srv.routes.get(self.path)
            reply = replies.pop(0) if replies and len(replies) > 1 else None
            reply = reply or (replies[0] if replies else _json(200, {"path": self.path}))
        if number in srv.drop:  # read the request, close without answering
            self.close_connection = True
            return
        status, headers, payload = reply
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        if srv.close_after_reply:  # like an idle timeout, without saying so
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def do_DELETE(self) -> None:  # noqa: N802
        self._serve()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.connections = 0
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.routes: dict[str, list[Reply]] = {}
        self.drop: set[int] = set()  # request numbers (1-based) left unanswered
        self.close_after_reply = False
        self.closed = threading.Event()

    def shutdown_request(self, request: socket.socket) -> None:
        super().shutdown_request(request)
        self.closed.set()

    def url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.server_address[1]}{path}"

    def sent(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.requests]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Server]:
    for name in list(os.environ):
        if name.lower().endswith("_proxy"):
            monkeypatch.delenv(name)
    # urlopen keeps the opener (and the proxies) of its first call: start afresh.
    monkeypatch.setattr(urllib.request, "_opener", None)
    http_pool._error_opener.cache_clear()
    srv = _Server()
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def transport() -> Iterator[HttpTransport]:
    t = HttpTransport(
        timeout=5,
        max_retries=3,
        retry_backoff=0.01,
        headers_fn=lambda: {"Content-Type": "application/json"},
    )
    yield t
    t._pool.close()


def _path(body: bytes) -> str:
    return str(json.loads(body)["path"])


def test_http_pool_ten_gets_use_one_connection(server: _Server, transport: HttpTransport) -> None:
    for i in range(10):
        assert _path(transport.execute("GET", server.url(f"/v1/tasks/{i}"))) == f"/v1/tasks/{i}"
    assert len(server.requests) == 10
    assert server.connections == 1


def test_http_pool_wait_for_task_polls_on_one_connection(server: _Server) -> None:
    running = _json(200, {"task_id": "t1", "status": "running"})
    server.routes["/v1/tasks/t1"] = [running, running, running, _json(200, {"status": "completed"})]
    server.routes["/v1/tasks/t2"] = [(204, {}, b"")]
    agents = AgentsClient(base_url=server.url(), timeout=5)

    assert agents.wait_for_task("t1", poll_interval_sec=0.01).status == "completed"
    agents.delete_task("t2")  # 204, body never read: the connection is still reusable
    agents.get_task("t1")

    assert server.sent()[-2:] == [("DELETE", "/v1/tasks/t2"), ("GET", "/v1/tasks/t1")]
    assert server.connections == 1
    agents._pool.close()


def test_http_pool_idle_connection_closed_by_server_post_uses_new_one(
    server: _Server, transport: HttpTransport,
) -> None:
    server.close_after_reply = True
    transport.execute("GET", server.url("/a"))
    assert server.closed.wait(5)

    assert _path(transport.execute("POST", server.url("/b"), data=b"{}")) == "/b"
    assert server.sent() == [("GET", "/a"), ("POST", "/b")]
    assert server.connections == 2


def test_http_pool_reused_connection_dropped_get_sent_again(
    server: _Server, transport: HttpTransport,
) -> None:
    server.drop = {2}
    transport.execute("GET", server.url("/a"))

    assert _path(transport.execute("GET", server.url("/b"))) == "/b"
    assert server.sent() == [("GET", "/a"), ("GET", "/b"), ("GET", "/b")]
    assert server.connections == 2


def test_http_pool_reused_connection_dropped_post_not_sent_again(
    server: _Server, transport: HttpTransport,
) -> None:
    server.drop = {2}
    transport.execute("GET", server.url("/a"))

    with pytest.raises(WauldoError):
        transport.execute("POST", server.url("/b"), data=b"{}")
    assert server.sent() == [("GET", "/a"), ("POST", "/b")]


def test_http_pool_idle_past_limit_opens_new_connection(
    server: _Server, transport: HttpTransport, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_pool, "IDLE_LIMIT_SEC", 0.0)
    transport.execute("GET", server.url("/a"))
    transport.execute("GET", server.url("/b"))
    assert server.connections == 2


def test_http_pool_env_proxy_goes_through_urlopen(
    server: _Server, transport: HttpTransport, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("http_proxy", server.url())  # the local server plays the proxy

    assert _path(transport.execute("GET", "http://wauldo.invalid/v1/models")) == (
        "http://wauldo.invalid/v1/models"
    )
    method, path, headers = server.requests[0]
    assert path == "http://wauldo.invalid/v1/models"  # absolute form: sent to a proxy
    assert headers["Connection"] == "close"  # urlopen's own request


def test_http_pool_no_proxy_host_stays_pooled(
    server: _Server, transport: HttpTransport, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    transport.execute("GET", server.url("/a"))
    transport.execute("GET", server.url("/b"))
    assert server.connections == 1


def test_http_pool_headers_match_urlopen_without_connection_close(server: _Server) -> None:
    def send(opener: Callable[[urllib.request.Request], Any]) -> None:
        req = urllib.request.Request(
            server.url("/h"),
            data=b'{"a": 1}',
            headers={"Content-Type": "application/json", "Authorization": "Bearer k",
                     "x-rapidapi-user": "t"},
            method="POST",
        )
        with opener(req) as resp:
            resp.read()

    pool = ConnectionPool()
    send(lambda req: urllib.request.urlopen(req, timeout=5))
    send(lambda req: pool.urlopen(req, 5))
    pool.close()

    via_urlopen, via_pool = (headers for _, _, headers in server.requests)
    assert via_urlopen.pop("Connection") == "close"
    assert list(via_pool.items()) == list(via_urlopen.items())


def test_http_pool_http_error_mapped_and_connection_kept(
    server: _Server, transport: HttpTransport,
) -> None:
    server.routes["/missing"] = [_json(404, {"error": {"message": "no such task"}})]
    with pytest.raises(ServerError, match="HTTP 404: no such task"):
        transport.execute("GET", server.url("/missing"))
    transport.execute("GET", server.url("/ok"))
    assert server.connections == 1


def test_http_pool_redirect_followed(server: _Server, transport: HttpTransport) -> None:
    server.routes["/old"] = [(302, {"Location": "/new"}, b"")]
    assert _path(transport.execute("GET", server.url("/old"))) == "/new"


def test_http_pool_threads_get_their_own_responses(
    server: _Server, transport: HttpTransport,
) -> None:
    errors: list[str] = []

    def worker(n: int) -> None:
        for i in range(20):
            path = f"/t{n}/{i}"
            got = _path(transport.execute("GET", server.url(path)))
            if got != path:
                errors.append(f"{path} -> {got}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(server.requests) == 160
    assert server.connections <= 8


def test_http_pool_client_copy_and_pickle_get_empty_pool(server: _Server) -> None:
    agents = AgentsClient(base_url=server.url(), timeout=5)
    agents.get_task("t1")
    # Round-trip of an object this test just built: no untrusted pickle data.
    for clone in (copy.deepcopy(agents), pickle.loads(pickle.dumps(agents))):
        assert clone._pool is not agents._pool
        clone.get_task("t1")
    agents._pool.close()
