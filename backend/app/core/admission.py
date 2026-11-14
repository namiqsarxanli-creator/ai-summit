"""Admission control: how many API requests may be inside the application at once.

Why it exists (found by load-testing a production copy): a request takes a
pooled database connection in one worker thread (its session dependency) and
then needs ANOTHER thread for the endpoint. When more requests arrive at once
than the pool has connections - 500 phones all refetching the leaderboard after
one award is enough - every thread ends up waiting for a connection while the
requests that hold the connections wait for a thread. Nothing moves until the
pool's 30 s timeout turns them into errors.

So at most `limit` requests are let in at a time, a number below the pool size;
the rest wait here, in the event loop, holding neither a thread nor a
connection, and are answered 503 SERVER_BUSY (with Retry-After) if they wait too
long. Long-lived routes that bound their own concurrency are not counted: the
live stream (open for up to 30 minutes, capped per attendee and in total) and
the photo upload (its own two-thread limiter). Pure ASGI, outermost, single
worker - the same in-process assumption as the rate limiter.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from starlette.types import ASGIApp, Receive, Scope, Send

ADMISSION_LIMIT = 12
ADMISSION_WAIT_SECONDS = 15.0
EXEMPT_PATHS = frozenset({"/api/realtime/stream", "/api/attendees/me/photo"})
# What the operator's status page shows (app/core/error_log.py's neighbour):
# requests inside now, waiting for a slot now, and turned away since the start.
STATE: dict[str, int] = {"limit": ADMISSION_LIMIT, "inside": 0, "waiting": 0, "turned_away": 0}


class AdmissionMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int = ADMISSION_LIMIT,
        wait_seconds: float = ADMISSION_WAIT_SECONDS,
    ) -> None:
        self.app = app
        self.limit = limit
        self.wait_seconds = wait_seconds
        self._slots: asyncio.Semaphore | None = None  # needs the running loop

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith("/api/") or path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.limit)
        slots = self._slots
        STATE["limit"] = self.limit
        STATE["waiting"] += 1
        try:
            async with asyncio.timeout(self.wait_seconds):
                await slots.acquire()
        except TimeoutError:
            STATE["turned_away"] += 1
            await _busy(send)
            return
        finally:
            STATE["waiting"] -= 1
        STATE["inside"] += 1
        try:
            await self.app(scope, receive, send)
        finally:
            STATE["inside"] -= 1
            slots.release()


async def _busy(send: Send) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "SERVER_BUSY",
                "message": "The server is busy right now. Please try again in a moment.",
                "details": {},
                "correlation_id": str(uuid.uuid4()),
            }
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", b"2"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
