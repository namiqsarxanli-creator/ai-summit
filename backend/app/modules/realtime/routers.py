"""`GET /api/realtime/stream` — ARCHITECTURE.md §17.

Pure read: no mutation, no reset fence. §19.6 names "leaderboard,
score snapshot, public card, SSE stream" together as the requests that
take no fence — a reader sees either the pre- or post-restore dataset,
never a partial one, because the restore itself is one transaction.

Uses its OWN short-lived sessions throughout (`resolve_attendee_for_stream`
in `realtime/service.py`), never the request-scoped `DbSession`/
`CurrentAttendee` FastAPI dependency: a stream can stay open up to
30 minutes (§17.9), and `get_db`'s `finally: db.close()` only runs when
the WHOLE request ends — for a streaming response, that is when the
stream itself ends. Pinning one pooled connection per open stream for
up to half an hour, for a dependency this route only ever needs for an
instant at a time, is a real resource risk at any scale beyond a demo.

The dependency below IS still a normal (non-streaming) FastAPI
dependency, resolved before the generator function is ever entered —
deliberately, so an invalid session produces a clean domain-error JSON
response (401/403) the same way as any other route, rather than a
partially-started `200 text/event-stream` response that then breaks.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from starlette.concurrency import run_in_threadpool

from app.core.errors import DomainError, RateLimitedError
from app.core.sessions import SESSION_COOKIE_NAME
from app.modules.attendees.models import User
from app.modules.realtime import service as realtime_service
from app.modules.realtime.broker import RealtimeEvent, event_bus
from app.modules.realtime.service import current_dataset_generation, resolve_attendee_for_stream

router = APIRouter(tags=["realtime"])


def _current_attendee_for_stream(request: Request) -> User:
    """Not `CurrentAttendee` — see the module docstring. Same validity
    rule (`resolve_attendee_session`, via `resolve_attendee_for_stream`),
    a short-lived session instead of the pooled request-scoped one.
    Never accepts a client-supplied identity: the only input is the
    HttpOnly session cookie, exactly like every other attendee route."""
    attendee = resolve_attendee_for_stream(request.cookies.get(SESSION_COOKIE_NAME))
    # Each open stream holds a socket and a task on the single worker for up to
    # 30 minutes and re-validates its session every heartbeat, so one account
    # may not open them without bound. Refused BEFORE the response starts: the
    # browser treats a refused EventSource as final and the client falls back to
    # polling. Checked, then subscribed: a race can overshoot by a few, no more.
    if (
        event_bus.subscriber_count(attendee.id) >= realtime_service.MAX_STREAMS_PER_ATTENDEE
        or event_bus.total_subscribers() >= realtime_service.MAX_STREAMS_TOTAL
    ):
        raise RateLimitedError(realtime_service.HEARTBEAT_SECONDS)
    return attendee


CurrentAttendeeForStream = Annotated[User, Depends(_current_attendee_for_stream)]


@router.get("/realtime/stream", response_class=EventSourceResponse)
async def stream(
    request: Request, attendee: CurrentAttendeeForStream
) -> AsyncIterator[ServerSentEvent]:
    # Captured once, reused by every heartbeat-tick re-validation below
    # — a plain string read, not itself an authentication decision.
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    queue = event_bus.subscribe(attendee.id)
    try:
        # Subscriber is registered BEFORE `ready` is sent — the server
        # half of "subscribe first, then snapshot" (§17.4). The CLIENT
        # must wait for `ready` before its own HTTP snapshot fetch;
        # this ordering is what makes that safe. An award committing
        # anywhere between `event_bus.subscribe()` above and the
        # client's snapshot fetch is already queued for delivery by
        # the time `ready` reaches the browser — there is no gap.
        yield ServerSentEvent(
            event="ready",
            data={
                "dataset_generation": await run_in_threadpool(current_dataset_generation),
                "heartbeat_seconds": realtime_service.HEARTBEAT_SECONDS,
            },
        )

        loop = asyncio.get_running_loop()
        # Read via the module object, not a name bound at import time,
        # so tests can shrink these with `monkeypatch.setattr` and have
        # a REAL, still-running stream observe the new value — these
        # scenarios (15 s heartbeat, 30-minute max age) are exercised
        # against real elapsed time, not a mocked clock.
        stream_deadline = loop.time() + realtime_service.MAX_STREAM_AGE_SECONDS
        heartbeat_deadline = loop.time() + realtime_service.HEARTBEAT_SECONDS

        while True:
            now = loop.time()
            if now >= stream_deadline:
                # §17.9: "Streams are closed by the server after 30
                # minutes; the client reconnects transparently."
                return

            timeout = max(0.0, min(heartbeat_deadline, stream_deadline) - now)
            item: RealtimeEvent | None
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                item = None

            if item is not None:
                yield (
                    ServerSentEvent(event=item.event, raw_data=item.raw)
                    if item.raw is not None
                    else ServerSentEvent(event=item.event, data=item.data)
                )
                if item.event == "dataset_reset":
                    # M15 compatibility only — nothing in M14 ever
                    # publishes this event. A reset invalidates the
                    # whole stream; the client hard-reinitialises and
                    # this connection has nothing further to say.
                    return

            if loop.time() >= heartbeat_deadline:
                # §17.9: "Stream re-validates its session on every
                # heartbeat tick... Authorization lifetime is bounded
                # by 15 s, not by socket lifetime." Same check, same
                # function, as the initial connection and every other
                # attendee-authenticated read.
                try:
                    await run_in_threadpool(resolve_attendee_for_stream, raw_token)
                except DomainError:
                    yield ServerSentEvent(event="session_invalid", data={"reason": "revoked"})
                    return
                yield ServerSentEvent(event="heartbeat", data={"t": datetime.now(UTC).isoformat()})
                heartbeat_deadline = loop.time() + realtime_service.HEARTBEAT_SECONDS
    finally:
        event_bus.unsubscribe(attendee.id, queue)
