"""In-process async pub/sub broker — ARCHITECTURE.md §17.10.

A dict of `user_id -> set[asyncio.Queue]` (personal channel) plus one
broadcast set. No Redis, no `LISTEN/NOTIFY` (production path, not built
now, §17.10) — the backend runs with exactly one Uvicorn worker
(invariant #17, pinned in `docker-compose.yml`'s `--workers 1`), which
is what makes a purely in-process broker correct: an award handled by
this worker can always reach every stream this same worker is holding.

Every publish is fire-and-forget and thread-safe: routers call
`publish_personal`/`publish_broadcast` from FastAPI's sync threadpool
(every mutating route in this codebase is a plain `def`, not `async
def` — ARCHITECTURE.md §4), while the queues themselves are consumed
by the async SSE generator running on the event loop.
`loop.call_soon_threadsafe` is the bridge — safe to call from any
thread, including the loop's own.

Bounded, nonblocking, drop-oldest (M14 scope: "Dropped/coalesced
invalidation hints are acceptable because snapshots restore
correctness") — a slow or stalled subscriber must never block a
publisher or grow memory without bound (§17.7.2/§17.8: a hint is never
authoritative, so losing one costs only latency, never correctness).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections import defaultdict
from dataclasses import dataclass

# A subscriber that hasn't drained even this many hints is starved
# anyway — coalescing further hints costs it nothing, and this bounds
# memory per-subscriber regardless of how long a slow client stays
# connected.
QUEUE_MAXSIZE = 8


@dataclass(frozen=True)
class RealtimeEvent:
    event: str
    data: dict[str, object]


class EventBus:
    """One instance, module-level (`event_bus` below). `bind_loop` is
    called once at app startup (the `lifespan` hook in `main.py`),
    after which `publish_*` may be called from any thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._personal: dict[int, set[asyncio.Queue[RealtimeEvent]]] = defaultdict(set)
        self._broadcast: set[asyncio.Queue[RealtimeEvent]] = set()
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def unbind_loop(self) -> None:
        """Called on shutdown — a publish after this is a silent
        no-op (§17.7.2's best-effort contract), rather than risking a
        `call_soon_threadsafe` against a loop that has already stopped."""
        self._loop = None

    def subscribe(self, user_id: int) -> asyncio.Queue[RealtimeEvent]:
        """Registers a fresh queue on BOTH the personal channel for
        `user_id` and the broadcast set, in one call — so a caller
        cannot register on one and forget the other, and the two stay
        consistent for a given connection."""
        queue: asyncio.Queue[RealtimeEvent] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        with self._lock:
            self._personal[user_id].add(queue)
            self._broadcast.add(queue)
        return queue

    def unsubscribe(self, user_id: int, queue: asyncio.Queue[RealtimeEvent]) -> None:
        with self._lock:
            subscribers = self._personal.get(user_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    del self._personal[user_id]
            self._broadcast.discard(queue)

    def subscriber_count(self, user_id: int) -> int:
        """Open streams of one attendee; also the per-attendee stream cap's
        input (`realtime.routers`)."""
        with self._lock:
            return len(self._personal.get(user_id, ()))

    def total_subscribers(self) -> int:
        """Open streams in this process; the global stream cap's input."""
        with self._lock:
            return len(self._broadcast)

    def publish_personal(self, user_id: int, event: str, data: dict[str, object]) -> None:
        with self._lock:
            queues = tuple(self._personal.get(user_id, ()))
        self._deliver(queues, RealtimeEvent(event, data))

    def publish_broadcast(self, event: str, data: dict[str, object]) -> None:
        with self._lock:
            queues = tuple(self._broadcast)
        self._deliver(queues, RealtimeEvent(event, data))

    def _deliver(
        self, queues: tuple[asyncio.Queue[RealtimeEvent], ...], item: RealtimeEvent
    ) -> None:
        loop = self._loop
        if loop is None or not queues:
            return
        for queue in queues:
            loop.call_soon_threadsafe(_put_dropping_oldest, queue, item)

    def reset(self) -> None:
        """M15 compatibility (not called anywhere in M14 itself, per
        IMPLEMENTATION_PLAN.md's M14 scope: "expose the broker/stream
        cleanup operations required by M15... M15 will use them
        later"). Drops every subscriber queue and clears both
        channels, so a future demo reset can guarantee no stream keeps
        delivering post-reset events to a pre-reset connection. Each
        open generator notices this independently by receiving
        `dataset_reset` (published as a broadcast BEFORE this is
        called) and closing itself — this method only clears the
        broker's own bookkeeping, it does not reach into and close
        the HTTP connections themselves."""
        with self._lock:
            self._personal.clear()
            self._broadcast.clear()


def _put_dropping_oldest(queue: asyncio.Queue[RealtimeEvent], item: RealtimeEvent) -> None:
    """Runs on the event loop (via `call_soon_threadsafe`), never in
    the publisher's own thread. A full queue drops its OLDEST pending
    hint to make room — never blocks, never grows unbounded, and never
    fails the publish; the newest hint is what carries reconciliation
    forward fastest anyway."""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover - benign race
            queue.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover - defensive, see above
        queue.put_nowait(item)


event_bus = EventBus()
