"""Verification middleware for the OpenAI SDK.

Implementation strategy : we DON'T monkey-patch the OpenAI module
(too invasive, brittle across version bumps). Instead, `with_verification`
returns a thin proxy that delegates `chat.completions.create()` to the
underlying client + post-processes the response with a Wauldo fact-check.

Every other attribute on the wrapped client passes through unchanged
so tools that introspect `client.api_key`, `client.base_url`, etc.
keep working.
"""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass
from typing import Any, Optional

import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_WAULDO_API = "https://api.wauldo.com"
DEFAULT_FACT_CHECK_TIMEOUT = 8.0  # seconds — same default as the lexical engine
SUPPORTED_FACT_CHECK_MODES = ("lexical", "hybrid", "semantic")


class WauldoVerificationError(RuntimeError):
    """Raised only when the caller passed `raise_on_error=True`. The
    default behavior is fail-open : we attach `response.wauldo = None`
    and log a warning so the OpenAI response itself is never broken
    by a downstream Wauldo issue.
    """


@dataclass
class WauldoFactCheck:
    """Verdict block attached to every wrapped completion.

    All fields default to safe sentinels so the consumer can branch
    on `if response.wauldo and response.wauldo.verdict == "SAFE"` even
    when the verdict couldn't be computed.
    """

    verdict: str = "UNVERIFIED"
    support_score: float = 0.0
    halluc_rate: float = 0.0
    claims_count: int = 0
    fact_check_mode: str = "lexical"
    share_url: Optional[str] = None
    error: Optional[str] = None

    @classmethod
    def from_response(cls, payload: Any, mode: str) -> "WauldoFactCheck":
        """Parse the shape returned by `/v1/external-runs` :
        `{task_id, verdict, support_score, halluc_rate, claims_count, claims, share?, ...}`.
        Reads `support_score` directly (Task-style verdict shape) ;
        `share.url` populated when the caller requested `auto_share=True`.
        """
        if not isinstance(payload, dict):
            return cls(error=f"unexpected upstream shape : {type(payload).__name__}")
        verdict = payload.get("verdict") or "UNVERIFIED"
        support = float(payload.get("support_score") or 0.0)
        halluc = float(payload.get("halluc_rate") or 0.0)
        # `claims_count` is authoritative (server-side count) ; fall
        # back to `len(claims)` for older shapes that didn't return it.
        claims = payload.get("claims") or []
        claims_count = payload.get("claims_count")
        if not isinstance(claims_count, int):
            claims_count = len(claims) if isinstance(claims, list) else 0
        share = payload.get("share")
        share_url = (
            share.get("url")
            if isinstance(share, dict) and isinstance(share.get("url"), str)
            else None
        )
        return cls(
            verdict=verdict,
            support_score=support,
            halluc_rate=halluc,
            claims_count=int(claims_count),
            fact_check_mode=mode,
            share_url=share_url,
        )


def _extract_text(completion: Any) -> Optional[str]:
    """Pull the assistant message text from a ChatCompletion. Tolerates
    both pydantic models (OpenAI SDK ≥1.0) and plain dicts (mocks /
    older envelopes). Returns None when no text could be found.
    """
    try:
        choices = (
            completion.choices
            if hasattr(completion, "choices")
            else completion.get("choices")
        )
        if not choices:
            return None
        first = choices[0]
        message = (
            first.message
            if hasattr(first, "message")
            else first.get("message")
        )
        if message is None:
            return None
        content = (
            message.content
            if hasattr(message, "content")
            else message.get("content")
        )
        return content if isinstance(content, str) else None
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def _post_external_run(
    *,
    base_url: str,
    api_key: str,
    answer: str,
    prompt: str,
    mode: str,
    timeout: float,
    share: bool,
    model: Optional[str] = None,
    agent_name: Optional[str] = None,
    tenant: Optional[str] = None,
) -> Any:
    """Synchronous POST to `/v1/external-runs` — the unified verify +
    persist + opt-in publish endpoint. Returns the parsed JSON body.

    We use stdlib `urllib` rather than `requests` / `httpx` to avoid
    pulling another runtime dep into the wrapper — the consumer
    already paid for `openai` and we don't want to add `requests` to
    the optional install set.

    The `tenant` header (`X-RapidAPI-User`) is required by the
    upstream auth middleware when `RAPIDAPI_SECRET` is set on the
    server. Self-host deployments without RapidAPI proxying ignore it.
    """
    payload = {
        "prompt": prompt,
        "answer": answer,
        "fact_check_mode": mode,
        "share": share,
    }
    if model is not None:
        payload["model"] = model
    if agent_name is not None:
        payload["agent_name"] = agent_name
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if tenant:
        headers["X-RapidAPI-User"] = tenant
    req = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/v1/external-runs",
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_chunk_text(chunk: Any) -> Optional[str]:
    """Pull the delta content from a streaming `ChatCompletionChunk`.
    Tolerates both pydantic models (OpenAI SDK ≥1.0) and plain dicts.
    Returns None for terminal chunks that only carry `finish_reason`.
    """
    try:
        choices = chunk.choices if hasattr(chunk, "choices") else chunk.get("choices")
        if not choices:
            return None
        first = choices[0]
        delta = first.delta if hasattr(first, "delta") else first.get("delta")
        if delta is None:
            return None
        content = delta.content if hasattr(delta, "content") else delta.get("content")
        return content if isinstance(content, str) else None
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def _verdict_from_buffered(
    buffered_text: str,
    kwargs: dict,
    config: "_Config",
) -> WauldoFactCheck:
    """Shared logic between the sync and async stream wrappers : run
    `/v1/fact-check` on the assembled text and return a verdict block.
    Mirrors the non-streaming path in `_attach_verdict` but operates on
    a string we already have in hand.
    """
    if not buffered_text.strip():
        return WauldoFactCheck(error="no assistant text in stream")

    messages = kwargs.get("messages") or []
    user_prompt = "\n\n".join(
        m.get("content", "")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "user"
    ).strip()

    try:
        payload = _post_external_run(
            base_url=config.base_url,
            api_key=config.api_key,
            answer=buffered_text,
            prompt=user_prompt,
            mode=config.fact_check_mode,
            timeout=config.timeout,
            share=config.auto_share,
            tenant=config.tenant,
        )
        return WauldoFactCheck.from_response(payload, config.fact_check_mode)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        msg = f"wauldo fact-check failed : {exc}"
        if config.raise_on_error:
            raise WauldoVerificationError(msg) from exc
        logger.warning(msg)
        return WauldoFactCheck(error=str(exc))


class _StreamWrapper:
    """Sync stream wrapper. Yields every chunk from the inner stream
    unchanged ; populates `self.wauldo` AFTER the stream is fully
    consumed. Consumer pattern :

        stream = verified.chat.completions.create(stream=True, ...)
        for chunk in stream:
            print(chunk.choices[0].delta.content or "", end="")
        # Iteration finished — wauldo verdict now available.
        print(f"\\n[verdict={stream.wauldo.verdict}]")

    `self.wauldo` stays `None` if the consumer breaks out of the loop
    early : a partial answer would produce a misleading verdict, and
    we'd rather surface the absence than guess.

    Every other attribute access (`stream.close()`, `stream.response`,
    etc.) passes through to the underlying stream so existing tooling
    that relies on those keeps working.
    """

    def __init__(self, inner: Any, kwargs: dict, config: "_Config") -> None:
        # `object.__setattr__` because `__setattr__` would recursively
        # hit `__getattr__` once we add it.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_kwargs", kwargs)
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_buffer", [])
        object.__setattr__(self, "_completed", False)
        object.__setattr__(self, "wauldo", None)

    def __iter__(self):
        for chunk in self._inner:
            text = _extract_chunk_text(chunk)
            if text:
                self._buffer.append(text)
            yield chunk
        # Stream exhausted normally — fact-check the buffered answer.
        # `_completed=True` lets the consumer distinguish an explicit
        # `None` (consumer broke early) from a genuine fact-check
        # outcome attached on `self.wauldo`.
        object.__setattr__(self, "_completed", True)
        verdict = _verdict_from_buffered(
            "".join(self._buffer),
            self._kwargs,
            self._config,
        )
        object.__setattr__(self, "wauldo", verdict)

    def __getattr__(self, name: str) -> Any:
        # Only called when the attribute is NOT found on `self` — so
        # `self.wauldo` (set in `__init__`) takes precedence.
        return getattr(self._inner, name)


class _AsyncStreamWrapper:
    """Async counterpart of `_StreamWrapper`. Same contract :

        stream = await verified.chat.completions.create(stream=True, ...)
        async for chunk in stream:
            ...
        print(stream.wauldo.verdict)
    """

    def __init__(self, inner: Any, kwargs: dict, config: "_Config") -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_kwargs", kwargs)
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "_buffer", [])
        object.__setattr__(self, "_completed", False)
        object.__setattr__(self, "wauldo", None)

    async def __aiter__(self):
        async for chunk in self._inner:
            text = _extract_chunk_text(chunk)
            if text:
                self._buffer.append(text)
            yield chunk
        object.__setattr__(self, "_completed", True)
        verdict = _verdict_from_buffered(
            "".join(self._buffer),
            self._kwargs,
            self._config,
        )
        object.__setattr__(self, "wauldo", verdict)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _WrappedCompletions:
    """Sync wrapper around `client.chat.completions`."""

    def __init__(
        self,
        inner: Any,
        config: "_Config",
    ) -> None:
        self._inner = inner
        self._config = config

    def create(self, *args: Any, **kwargs: Any) -> Any:
        completion = self._inner.create(*args, **kwargs)
        if kwargs.get("stream"):
            return _StreamWrapper(completion, kwargs, self._config)
        _attach_verdict(completion, kwargs, self._config)
        return completion


class _AsyncWrappedCompletions:
    """Async wrapper around `client.chat.completions`."""

    def __init__(
        self,
        inner: Any,
        config: "_Config",
    ) -> None:
        self._inner = inner
        self._config = config

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        completion = await self._inner.create(*args, **kwargs)
        if kwargs.get("stream"):
            return _AsyncStreamWrapper(completion, kwargs, self._config)
        _attach_verdict(completion, kwargs, self._config)
        return completion


class _WrappedChat:
    def __init__(self, inner: Any, config: "_Config") -> None:
        self.completions = _WrappedCompletions(inner.completions, config)


class _AsyncWrappedChat:
    def __init__(self, inner: Any, config: "_Config") -> None:
        self.completions = _AsyncWrappedCompletions(inner.completions, config)


@dataclass
class _Config:
    api_key: str
    base_url: str
    fact_check_mode: str
    timeout: float
    raise_on_error: bool
    auto_share: bool
    tenant: Optional[str]


class _Proxy:
    """Sync proxy : forward every attribute access to the wrapped client
    EXCEPT `chat`, which we wrap so `chat.completions.create()` triggers
    the post-fact-check.
    """

    def __init__(self, client: Any, config: _Config) -> None:
        object.__setattr__(self, "_inner", client)
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "chat", _WrappedChat(client.chat, config))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _AsyncProxy:
    """Async proxy — same shape, awaits in the wrapper."""

    def __init__(self, client: Any, config: _Config) -> None:
        object.__setattr__(self, "_inner", client)
        object.__setattr__(self, "_config", config)
        object.__setattr__(self, "chat", _AsyncWrappedChat(client.chat, config))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _attach_verdict(completion: Any, kwargs: dict, config: _Config) -> None:
    """Compute the fact-check verdict and stash it as `completion.wauldo`.
    Always succeeds — failures land in `WauldoFactCheck.error`.
    """
    answer = _extract_text(completion)
    if not answer:
        try:
            setattr(
                completion,
                "wauldo",
                WauldoFactCheck(error="no assistant text in response"),
            )
        except (AttributeError, TypeError):
            pass
        return

    # Reconstruct the user prompt for the fact-check context.
    # We concatenate every user-role message — multi-turn conversations
    # produce a longer context, which `/v1/fact-check` accepts.
    messages = kwargs.get("messages") or []
    user_prompt = "\n\n".join(
        m.get("content", "")
        for m in messages
        if isinstance(m, dict) and m.get("role") == "user"
    ).strip()

    try:
        payload = _post_external_run(
            base_url=config.base_url,
            api_key=config.api_key,
            answer=answer,
            prompt=user_prompt,
            mode=config.fact_check_mode,
            timeout=config.timeout,
            share=config.auto_share,
            tenant=config.tenant,
        )
        verdict = WauldoFactCheck.from_response(payload, config.fact_check_mode)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        msg = f"wauldo fact-check failed : {exc}"
        if config.raise_on_error:
            raise WauldoVerificationError(msg) from exc
        logger.warning(msg)
        verdict = WauldoFactCheck(error=str(exc))

    try:
        setattr(completion, "wauldo", verdict)
    except (AttributeError, TypeError):
        # Pydantic models with `frozen=True` block attribute assignment.
        # Best effort : log a warning and move on. The consumer can opt
        # into a model that allows extras when this matters.
        warnings.warn(
            "Could not attach `.wauldo` to the OpenAI completion (frozen model?)."
            " Set `raise_on_error=True` to surface the issue.",
            stacklevel=3,
        )


def with_verification(
    client: Any,
    *,
    wauldo_api_key: str,
    wauldo_base_url: str = DEFAULT_WAULDO_API,
    fact_check_mode: str = "lexical",
    timeout: float = DEFAULT_FACT_CHECK_TIMEOUT,
    raise_on_error: bool = False,
    auto_share: bool = False,
    tenant: Optional[str] = None,
) -> Any:
    """Wrap an OpenAI (or OpenAI-compatible) client so every
    `chat.completions.create()` call returns a completion with a
    `.wauldo` attribute carrying the verdict — and optionally a
    public share URL.

    Detects async clients (`openai.AsyncOpenAI`) automatically by
    checking whether `client.chat.completions.create` is a coroutine
    function.

    Args:
        client: An OpenAI / AsyncOpenAI / OpenAI-compat client. The
            only requirement is that it exposes `client.chat.completions.create`.
        wauldo_api_key: A `tig_live_...` token. Required.
        wauldo_base_url: Override the upstream Wauldo API base —
            defaults to `https://api.wauldo.com`. Useful for staging
            / self-host.
        fact_check_mode: `"lexical"` (default, ~1s), `"hybrid"`
            (lexical + embeddings, ~3-5s) or `"semantic"`
            (embeddings + LLM-judge, ~5-15s).
        timeout: Hard timeout on the verify round-trip in seconds.
            On timeout, the consumer gets `response.wauldo.error` set.
        raise_on_error: Default False (fail-open). Set True to bubble
            verification failures as `WauldoVerificationError` exceptions
            instead of attaching them as `response.wauldo.error`.
        auto_share: When True, every verified run is also published as
            a public share. The URL lands on `response.wauldo.share_url`
            (or `stream.wauldo.share_url` for streaming responses) so
            it can be dropped into Slack / a bug report / a PR comment
            without a follow-up call. Honours the per-tenant cap +
            30-day TTL (free tier) — see `/docs#shareable-runs`.
        tenant: RapidAPI tenant id. Required when the upstream Wauldo
            instance has `RAPIDAPI_SECRET` set (production deployments).
            Self-host installs without RapidAPI proxying can leave it
            unset.
    """
    if not wauldo_api_key:
        raise ValueError("wauldo_api_key is required")
    if fact_check_mode not in SUPPORTED_FACT_CHECK_MODES:
        raise ValueError(
            f"fact_check_mode must be one of {SUPPORTED_FACT_CHECK_MODES}, got {fact_check_mode!r}"
        )

    config = _Config(
        api_key=wauldo_api_key,
        base_url=wauldo_base_url,
        fact_check_mode=fact_check_mode,
        timeout=timeout,
        raise_on_error=raise_on_error,
        auto_share=auto_share,
        tenant=tenant,
    )

    # Detect async by inspecting the create method on the inner client.
    # Doing this here keeps the proxies symmetric and avoids an explicit
    # `is_async=` flag on the public API.
    import inspect

    create = getattr(getattr(client.chat, "completions", None), "create", None)
    if create is None:
        raise TypeError(
            "with_verification expected `client.chat.completions.create` to exist; "
            "is this an OpenAI-compatible client?"
        )
    if inspect.iscoroutinefunction(create):
        return _AsyncProxy(client, config)
    return _Proxy(client, config)
