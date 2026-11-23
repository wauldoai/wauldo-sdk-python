"""Keep-alive HTTP connections for the synchronous clients.

``urllib.request.urlopen`` opens a new TCP connection, and a new TLS session,
for every request (it even sends ``Connection: close``): every call, and every
poll of ``wait_for_task``, paid the handshake again. ``ConnectionPool.urlopen``
takes the place of ``urlopen(req, timeout=timeout)`` and keeps HTTP/1.1
connections open between calls. The rest is urlopen's: the headers it adds,
the timeout, TLS verification with the default context, the exceptions
(``URLError`` when the request could not be sent, ``HTTPError`` for a status
outside 2xx) and redirects, still followed by urllib's own handlers.

A request still goes through ``urlopen`` when ``http.client`` alone would not
send it the same way: a proxy set in the environment for its scheme and host,
or a scheme other than http/https. An opener installed with
``urllib.request.install_opener()`` is not consulted for pooled requests.
Streams keep using ``urlopen``: one connection per stream, never pooled.
"""

from __future__ import annotations

import http.client
import logging
import os
import selectors
import time
import weakref
from collections import deque
from functools import cache
from types import TracebackType
from urllib.error import URLError
from urllib.request import (
    HTTPHandler,
    OpenerDirector,
    Request,
    build_opener,
    getproxies,
    proxy_bypass,
    urlopen,
)

logger = logging.getLogger("wauldo")

#: An idle connection older than this is closed instead of reused: load
#: balancers commonly drop connections left idle for 60 s.
IDLE_LIMIT_SEC = 50.0

#: Idle connections kept per (scheme, host:port); a connection released
#: beyond that is closed.
MAX_IDLE_PER_HOST = 10

#: How a request fails on a kept-alive connection the server has closed.
_STALE_ERRORS = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError)

#: Methods that may be sent again although the server may have answered.
_IDEMPOTENT = frozenset({"GET", "HEAD"})

_CONNECTION_CLASSES: dict[str, type[http.client.HTTPConnection]] = {
    "http": http.client.HTTPConnection,
}
if hasattr(http.client, "HTTPSConnection"):  # absent without ssl; urlopen reports that
    _CONNECTION_CLASSES["https"] = http.client.HTTPSConnection

_Key = tuple[str, str]  # (scheme, host[:port])
_Idle = dict[_Key, "deque[tuple[http.client.HTTPConnection, float]]"]

# urllib's own request preparation, so Host, User-Agent, Content-Type and
# Content-Length are filled in exactly as urlopen fills them.
_PREPARE = HTTPHandler()
_PREPARE.add_parent(OpenerDirector())


class ConnectionPool:
    """Idle connections, reused per (scheme, host:port), safe across threads.

    A connection leaves the pool while a request uses it, so two threads never
    share one. Idle ones wait in a deque, whose appends and pops are atomic.
    """

    def __init__(self) -> None:
        self._idle: _Idle = {}
        self._pid = os.getpid()
        self._finalizer = weakref.finalize(self, _close_idle, self._idle)

    def __reduce__(self) -> tuple[type[ConnectionPool], tuple[()]]:
        # Copies and pickles of a client get an empty pool: sockets do not travel.
        return (ConnectionPool, ())

    def close(self) -> None:
        """Close the idle connections (done anyway when the pool is collected)."""
        self._finalizer()

    def urlopen(
        self, req: Request, timeout: float,
    ) -> _PooledResponse | http.client.HTTPResponse:
        """Same as ``urllib.request.urlopen(req, timeout=timeout)``, kept alive."""
        if not _poolable(req):
            return urlopen(req, timeout=timeout)  # type: ignore[no-any-return]
        headers = _headers(req)
        req.timeout = timeout  # set by OpenerDirector.open(); the redirect handler reads it
        key = (req.type, req.host)
        conn, resp = self._exchange(key, req, headers, timeout)
        pooled = _PooledResponse(self, key, conn, resp)
        if 200 <= resp.status < 300:
            return pooled
        # What urllib's HTTPErrorProcessor does: follow a redirect, else raise HTTPError.
        return _error_opener().error(  # type: ignore[no-any-return]
            "http", req, pooled, resp.status, resp.reason, resp.headers,
        )

    def _exchange(
        self, key: _Key, req: Request, headers: dict[str, str], timeout: float,
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        """Send ``req`` and read the status line, raising what urlopen raises.

        On a reused connection the server closed meanwhile, the request is sent
        once more on a new connection, only if it could not be sent or if the
        method is idempotent: a POST the server may have answered is never
        sent twice.
        """
        method = req.get_method()
        conn, reused = self._acquire(key, timeout)
        while True:
            sent = False
            try:
                conn.request(
                    method, req.selector, req.data, headers,
                    encode_chunked=req.has_header("Transfer-encoding"),
                )
                sent = True
                return conn, conn.getresponse()
            except BaseException as err:
                conn.close()
                retry = not sent or method in _IDEMPOTENT
                if reused and retry and isinstance(err, _STALE_ERRORS):
                    logger.debug("Kept-alive connection to %s closed (%r), new one", key[1], err)
                    conn, reused = _CONNECTION_CLASSES[key[0]](key[1], timeout=timeout), False
                    continue
                if not sent and isinstance(err, OSError):
                    raise URLError(err) from err
                raise

    def _acquire(self, key: _Key, timeout: float) -> tuple[http.client.HTTPConnection, bool]:
        """An idle connection still fit for use, else a new one (connects on send)."""
        if self._pid != os.getpid():  # forked: the idle sockets belong to the parent too
            _close_idle(self._idle)
            self._pid = os.getpid()
        idle = self._idle.get(key)
        while idle:
            try:
                conn, idle_since = idle.pop()
            except IndexError:  # another thread took the last one
                break
            if time.monotonic() - idle_since < IDLE_LIMIT_SEC and not _dropped(conn):
                conn.timeout = timeout
                conn.sock.settimeout(timeout)
                return conn, True
            conn.close()
        return _CONNECTION_CLASSES[key[0]](key[1], timeout=timeout), False

    def _release(self, key: _Key, conn: http.client.HTTPConnection) -> None:
        """Take back a connection whose response was read to the end."""
        idle = self._idle.setdefault(key, deque())
        if conn.sock is None or not self._finalizer.alive or len(idle) >= MAX_IDLE_PER_HOST:
            conn.close()
            return
        idle.append((conn, time.monotonic()))


class _PooledResponse:
    """A response on a pooled connection.

    The connection goes back to the pool once the body is read to the end.
    Closed before that, the connection is closed too: the unread bytes would
    be taken for the next response.
    """

    def __init__(
        self,
        pool: ConnectionPool,
        key: _Key,
        conn: http.client.HTTPConnection,
        resp: http.client.HTTPResponse,
    ) -> None:
        self._pool = pool
        self._key = key
        self._conn: http.client.HTTPConnection | None = conn
        self._resp = resp
        self.status = resp.status
        self.reason = resp.reason
        self.headers = resp.headers

    def read(self, amt: int | None = None) -> bytes:
        data = self._resp.read(amt)
        if self._resp.isclosed():  # http.client closes it at the end of the body
            self.close()
        return data

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        complete = self._resp.isclosed() or self._resp.length == 0
        self._resp.close()
        if complete:
            self._pool._release(self._key, conn)
        else:
            conn.close()

    def __enter__(self) -> _PooledResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _poolable(req: Request) -> bool:
    """Whether ``http.client`` alone sends ``req`` as urlopen would.

    urlopen goes through a proxy when one is set for the scheme and the host
    is not excluded: the test urllib's ProxyHandler makes.
    """
    if req.type not in _CONNECTION_CLASSES:
        return False
    if req.type not in getproxies():
        return True
    return bool(req.host and proxy_bypass(req.host))


def _headers(req: Request) -> dict[str, str]:
    """The headers urlopen sends for ``req``, without its ``Connection: close``."""
    _PREPARE.http_request(req)
    headers = dict(req.unredirected_hdrs)
    headers.update({k: v for k, v in req.headers.items() if k not in headers})
    return {name.title(): value for name, value in headers.items()}


def _dropped(conn: http.client.HTTPConnection) -> bool:
    """Whether the server closed an idle connection: readable with no request out."""
    if conn.sock is None:
        return True
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(conn.sock, selectors.EVENT_READ)
            return bool(selector.select(timeout=0))
    except (OSError, ValueError):
        return True


@cache
def _error_opener() -> OpenerDirector:
    """urllib's default handlers, for what urlopen does with a non-2xx status."""
    return build_opener()


def _close_idle(idle: _Idle) -> None:
    for connections in list(idle.values()):
        while connections:
            try:
                conn, _ = connections.pop()
            except IndexError:
                break
            conn.close()
