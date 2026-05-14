"""Unit tests for `wauldo.openai.with_verification`.

We mock both halves :
- the OpenAI client (via a tiny in-memory stub that mimics
  `client.chat.completions.create`) — keeps the test offline.
- the upstream Wauldo `/v1/fact-check` endpoint (via
  `unittest.mock.patch` on `urllib.request.urlopen`).

Coverage :
- happy path : verdict attached to a sync completion
- async path : same shape but awaited
- streaming  : pass-through, no verdict computed
- failure    : wauldo down → fail-open with `error` set, raise-on-error path
- empty answer : verdict marks `error="no assistant text in response"`
- missing api key : raises ValueError at construction time
- bad mode   : raises ValueError at construction time
- detection  : sync vs async client correctly picked up
"""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from wauldo.openai import (
    WauldoFactCheck,
    WauldoVerificationError,
    with_verification,
)


# ───────────────────── stub OpenAI client ─────────────────────


class StubCompletions:
    def __init__(self, response):
        self._response = response

    def create(self, **kwargs):
        return self._response


class AsyncStubCompletions:
    def __init__(self, response):
        self._response = response

    async def create(self, **kwargs):
        return self._response


def make_sync_client(answer="Paris is the capital of France."):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=answer)),
        ],
        id="cmpl-test",
    )
    return SimpleNamespace(
        chat=SimpleNamespace(completions=StubCompletions(response)),
        api_key="sk-stub",
    )


def make_async_client(answer="Paris is the capital of France."):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=answer)),
        ],
        id="cmpl-test",
    )
    return SimpleNamespace(
        chat=SimpleNamespace(completions=AsyncStubCompletions(response)),
        api_key="sk-stub",
    )


# ───────────────── stub /v1/fact-check responder ─────────────────


def fake_urlopen(payload, status=200):
    """Returns a context manager that mimics what `urllib.urlopen`
    yields : an object with `.read()` returning bytes.
    """
    raw = json.dumps(payload).encode("utf-8")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return raw

    def _factory(req, timeout=None):
        return _Resp()

    return _factory


# ─────────────────────────── tests ───────────────────────────


def test_construct_requires_api_key():
    with pytest.raises(ValueError, match="wauldo_api_key is required"):
        with_verification(make_sync_client(), wauldo_api_key="")


def test_construct_validates_mode():
    with pytest.raises(ValueError, match="fact_check_mode"):
        with_verification(
            make_sync_client(),
            wauldo_api_key="tig_live_x",
            fact_check_mode="bogus",
        )


def test_construct_rejects_non_openai_client():
    bad = SimpleNamespace()  # no .chat attr
    with pytest.raises(AttributeError):
        with_verification(bad, wauldo_api_key="tig_live_x")


def test_sync_happy_path_attaches_verdict():
    client = make_sync_client()
    payload = {
        "verdict": "SAFE",
        "support_score": 0.92,
        "halluc_rate": 0.0,
        "claims": [{"text": "Paris is the capital", "supported": True}],
    }
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    with patch("urllib.request.urlopen", new=fake_urlopen(payload)):
        resp = verified.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Capital of France?"}],
        )
    assert resp.wauldo is not None
    assert resp.wauldo.verdict == "SAFE"
    assert resp.wauldo.support_score == 0.92
    assert resp.wauldo.halluc_rate == 0.0
    assert resp.wauldo.claims_count == 1
    assert resp.wauldo.error is None
    # The original ChatCompletion shape is preserved
    assert resp.choices[0].message.content == "Paris is the capital of France."


def test_sync_failure_is_fail_open():
    client = make_sync_client()

    def boom(*a, **kw):
        raise OSError("connection refused")

    verified = with_verification(client, wauldo_api_key="tig_live_x")
    with patch("urllib.request.urlopen", side_effect=boom):
        resp = verified.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "x"}],
        )
    # Verdict attached but populated with the error sentinel ; OpenAI
    # response itself is intact.
    assert isinstance(resp.wauldo, WauldoFactCheck)
    assert resp.wauldo.error is not None
    assert "connection refused" in resp.wauldo.error
    assert resp.wauldo.verdict == "UNVERIFIED"  # safe default


def test_sync_failure_raise_on_error():
    client = make_sync_client()

    def boom(*a, **kw):
        raise OSError("upstream 500")

    verified = with_verification(
        client, wauldo_api_key="tig_live_x", raise_on_error=True
    )
    with patch("urllib.request.urlopen", side_effect=boom):
        with pytest.raises(WauldoVerificationError, match="upstream 500"):
            verified.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "x"}],
            )


def _stream_chunk(text=None, finish=None):
    """Build a ChatCompletionChunk-like SimpleNamespace."""
    delta = SimpleNamespace(content=text) if text is not None else SimpleNamespace(content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)],
    )


def make_sync_streaming_client(chunks):
    """Stub OpenAI client whose `chat.completions.create(stream=True)`
    returns a plain Python iterator of pre-built chunks.
    """
    class StreamingCompletions:
        def create(self, **kwargs):
            return iter(chunks)

    return SimpleNamespace(
        chat=SimpleNamespace(completions=StreamingCompletions()),
    )


def make_async_streaming_client(chunks):
    """Async variant — `create()` is a coroutine returning an async iterator."""

    class _AsyncIter:
        def __init__(self, items):
            self._items = list(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    class StreamingCompletions:
        async def create(self, **kwargs):
            return _AsyncIter(chunks)

    return SimpleNamespace(
        chat=SimpleNamespace(completions=StreamingCompletions()),
    )


def test_sync_streaming_buffers_and_attaches_verdict_after_loop():
    """Sprint 6 v2 — verdict computed AFTER the stream completes,
    available on `stream.wauldo`. Chunks pass through unchanged."""
    client = make_sync_streaming_client([
        _stream_chunk("Paris "),
        _stream_chunk("is the "),
        _stream_chunk("capital."),
        _stream_chunk(finish="stop"),
    ])
    payload = {
        "verdict": "SAFE",
        "support_score": 0.9,
        "halluc_rate": 0.0,
        "claims": [{"text": "Paris is the capital", "supported": True}],
    }
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    with patch("urllib.request.urlopen", new=fake_urlopen(payload)):
        stream = verified.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Capital of France?"}],
            stream=True,
        )
        # Verdict is None DURING streaming
        assert stream.wauldo is None
        collected = [chunk for chunk in stream]
    assert len(collected) == 4
    # Verdict populated AFTER the loop exits
    assert stream.wauldo is not None
    assert stream.wauldo.verdict == "SAFE"
    assert stream.wauldo.support_score == 0.9
    assert stream.wauldo.claims_count == 1


def test_sync_streaming_break_early_leaves_wauldo_none():
    """Consumer who breaks mid-stream gets None — a partial answer
    would be misleading to fact-check, so we surface the absence."""
    chunks = [_stream_chunk("partial...")] * 10
    client = make_sync_streaming_client(chunks)
    payload = {"verdict": "SAFE", "support_score": 1.0, "halluc_rate": 0, "claims": []}
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    with patch("urllib.request.urlopen", new=fake_urlopen(payload)) as _fake:
        stream = verified.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "x"}],
            stream=True,
        )
        for i, _chunk in enumerate(stream):
            if i >= 2:
                break  # consumer abandons stream
    assert stream.wauldo is None, "early break must leave wauldo unset"


def test_sync_streaming_empty_content_marks_error():
    """Stream with only finish_reason chunks (no text) → verdict
    block carries `error="no assistant text in stream"`."""
    client = make_sync_streaming_client([_stream_chunk(finish="stop")])
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    stream = verified.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "x"}],
        stream=True,
    )
    list(stream)  # exhaust
    assert stream.wauldo is not None
    assert "no assistant text" in (stream.wauldo.error or "")


def test_sync_streaming_passthrough_attribute_access():
    """Stream wrapper should pass `.close()` etc. to inner stream."""

    class StreamWithClose:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            return iter([_stream_chunk("x"), _stream_chunk(finish="stop")])

        def close(self):
            self.closed = True

    inner = StreamWithClose()

    class StreamingCompletions:
        def create(self, **kwargs):
            return inner

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=StreamingCompletions()),
    )
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    payload = {"verdict": "SAFE", "support_score": 1.0, "halluc_rate": 0, "claims": []}
    with patch("urllib.request.urlopen", new=fake_urlopen(payload)):
        stream = verified.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "x"}],
            stream=True,
        )
        stream.close()
    assert inner.closed, "close() must propagate to the inner stream"


def test_auto_share_populates_share_url():
    """Sprint 6 v2bis — `auto_share=True` triggers `/v1/external-runs?share=true`
    and the response carries the public URL on `response.wauldo.share_url`."""
    client = make_sync_client()
    payload = {
        "task_id": "task_x",
        "verdict": "SAFE",
        "support_score": 0.95,
        "halluc_rate": 0.0,
        "claims_count": 2,
        "claims": [],
        "fact_check_mode": "lexical",
        "share": {
            "share_id": "r_aaaaaaaabbbbbbbbccccccccdddddddd",
            "url": "https://wauldo.com/r/r_aaaaaaaabbbbbbbbccccccccdddddddd",
            "expires_at": 1780797026156,
        },
    }
    verified = with_verification(
        client,
        wauldo_api_key="tig_live_x",
        auto_share=True,
        tenant="my_tenant",
    )
    with patch("urllib.request.urlopen", new=fake_urlopen(payload)):
        resp = verified.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Capital of France?"}],
        )
    assert resp.wauldo is not None
    assert resp.wauldo.verdict == "SAFE"
    assert resp.wauldo.share_url == "https://wauldo.com/r/r_aaaaaaaabbbbbbbbccccccccdddddddd"


def test_no_auto_share_leaves_share_url_none():
    """When `auto_share=False` (default), `share_url` stays None even
    if the upstream gratuitously returned a `share` block."""
    client = make_sync_client()
    payload = {
        "verdict": "SAFE",
        "support_score": 0.9,
        "halluc_rate": 0.0,
        "claims_count": 1,
        "claims": [],
        # Note : no `share` field in this payload — the typical case.
    }
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    with patch("urllib.request.urlopen", new=fake_urlopen(payload)):
        resp = verified.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "x"}],
        )
    assert resp.wauldo is not None
    assert resp.wauldo.share_url is None


def test_async_streaming_buffers_and_attaches_verdict():
    """Async stream: same contract, awaited."""
    client = make_async_streaming_client([
        _stream_chunk("Berlin"),
        _stream_chunk(" is..."),
        _stream_chunk(finish="stop"),
    ])
    payload = {
        "verdict": "CONFLICT",
        "support_score": 0.0,
        "halluc_rate": 1.0,
        "claims": [{"text": "Berlin", "supported": False}],
    }
    verified = with_verification(client, wauldo_api_key="tig_live_x")

    async def consume():
        with patch("urllib.request.urlopen", new=fake_urlopen(payload)):
            stream = await verified.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": "Capital of Germany?"}],
                stream=True,
            )
            collected = []
            async for chunk in stream:
                collected.append(chunk)
            return stream, collected

    stream, collected = asyncio.get_event_loop().run_until_complete(consume())
    assert len(collected) == 3
    assert stream.wauldo is not None
    assert stream.wauldo.verdict == "CONFLICT"
    assert stream.wauldo.support_score == 0.0


def test_empty_answer_marks_error():
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=StubCompletions(
                SimpleNamespace(choices=[], id="cmpl-empty")
            )
        ),
    )
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    resp = verified.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "x"}],
    )
    assert resp.wauldo is not None
    assert "no assistant text" in resp.wauldo.error


def test_passthrough_attribute_access():
    """Accessing `client.api_key` (or any non-`chat` attr) on the proxy
    must hit the underlying client unchanged so existing tooling that
    reads `client.api_key` keeps working.
    """
    client = make_sync_client()
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    assert verified.api_key == "sk-stub"


def test_async_detection_returns_async_proxy():
    """Async client → calling `.create()` returns a coroutine."""
    client = make_async_client()
    payload = {
        "verdict": "SAFE",
        "support_score": 0.9,
        "halluc_rate": 0.0,
        "claims": [{"text": "x", "supported": True}],
    }
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    with patch("urllib.request.urlopen", new=fake_urlopen(payload)):
        coro = verified.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "x"}],
        )
        # Must be awaitable (the sync proxy returns the response directly)
        resp = asyncio.get_event_loop().run_until_complete(coro)
    assert resp.wauldo.verdict == "SAFE"


def test_unknown_response_shape_marks_error():
    """When `/v1/fact-check` returns a string (e.g. an error page) the
    verdict block carries an error rather than crashing the wrapper.
    """
    client = make_sync_client()
    verified = with_verification(client, wauldo_api_key="tig_live_x")
    with patch("urllib.request.urlopen", new=fake_urlopen("oops, html error page")):
        resp = verified.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "x"}],
        )
    assert resp.wauldo is not None
    assert "unexpected upstream shape" in (resp.wauldo.error or "")
