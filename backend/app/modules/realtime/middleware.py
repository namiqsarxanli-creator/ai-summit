"""`X-Dataset-Generation` on every response — ARCHITECTURE.md §19.3:
"Every API response carries `X-Dataset-Generation`. The client stores
the value it first saw and compares it on every response. On
mismatch, the client hard-reinitialises."

This is a GLOBAL response header, not scoped to realtime or to
leaderboard/score (which already carry the same value in their JSON
bodies, M13) — a stale response to, say, a feedback submission must be
just as detectable as a stale score snapshot. Implemented once, here,
as pure ASGI middleware in the same style as
`app.core.privacy.CardPrivacyHeadersMiddleware`, added outermost in
`main.py` so the header lands on EVERY response this process sends —
success, a `DomainError`, a validation error, an unhandled exception,
even `/api/health` — not only ones a route handler controls.
"""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.modules.realtime.service import cached_dataset_generation, peek_cached_dataset_generation


class DatasetGenerationHeaderMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        generation = peek_cached_dataset_generation()
        if generation is None:
            generation = await run_in_threadpool(cached_dataset_generation)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Dataset-Generation"] = str(generation)
            await send(message)

        await self.app(scope, receive, send_with_header)


# `/api/realtime/stream`'s own path only. The framework's own SSE
# handling (`fastapi.sse`'s routing support) already sets
# `Cache-Control: no-cache` and `X-Accel-Buffering: no` on that one
# response — see `fastapi/routing.py`'s `_producer_cm` branch — but
# §17.9 locks `Cache-Control: no-cache, no-transform` exactly, and a
# route handler cannot cleanly REPLACE a header the framework already
# set via its own injected `Response` parameter (that would append a
# second `Cache-Control` line, not replace the first — the same
# raw-header-list `.extend()` mechanics `CardPrivacyHeadersMiddleware`
# was written around). Fixed up here, outermost, the same technique.
STREAM_PATH = "/api/realtime/stream"


class RealtimeStreamHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != STREAM_PATH:
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-cache, no-transform"
            await send(message)

        await self.app(scope, receive, send_with_header)
