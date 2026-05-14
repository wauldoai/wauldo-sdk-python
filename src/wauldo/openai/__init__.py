"""Drop-in OpenAI SDK middleware that adds a verified `.wauldo` namespace
to every `chat.completions.create()` response.

Install :

    pip install 'wauldo[openai]'

Usage :

    from openai import OpenAI
    from wauldo.openai import with_verification

    client = OpenAI()              # any OpenAI-compatible client
    verified = with_verification(  # wraps it
        client,
        wauldo_api_key="tig_live_...",
    )

    response = verified.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Capital of France?"}],
    )
    # Standard OpenAI ChatCompletion, plus :
    # response.wauldo.verdict          == "SAFE" | "UNVERIFIED" | "CONFLICT" | ...
    # response.wauldo.support_score    == 0.94
    # response.wauldo.halluc_rate      == 0.0
    # response.wauldo.claims_count     == 7
    # response.wauldo.fact_check_mode  == "lexical" (default) | "hybrid" | "semantic"
    # response.wauldo.error            == None when the verdict was attached cleanly

The middleware NEVER raises on a verification failure — Wauldo down,
timeout, or unexpected response shape result in `response.wauldo = None`
plus a logged warning, so the application keeps serving the OpenAI
output un-augmented.

Streaming is supported : the wrapper buffers every chunk's delta
content, yields chunks unchanged to the consumer, and computes the
verdict ONCE the stream is fully consumed. The verdict is then
available on the stream object itself :

    stream = verified.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "Capital of France?"}],
        stream=True,
    )
    for chunk in stream:
        print(chunk.choices[0].delta.content or "", end="")
    # Loop done → verdict ready
    print(f"\\n[{stream.wauldo.verdict}, {stream.wauldo.support_score:.2f}]")

If the consumer breaks out of the loop early, `stream.wauldo` stays
`None` — a partial answer would produce a misleading verdict. Async
clients (`AsyncOpenAI`) work the same way via `async for`.
"""

from .middleware import (
    WauldoFactCheck,
    WauldoVerificationError,
    with_verification,
)

__all__ = [
    "with_verification",
    "WauldoFactCheck",
    "WauldoVerificationError",
]
