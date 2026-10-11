"""Aggregate pacing of OTP email sends - the worker's own speed limit.

The per-address and per-IP limits (app/core/ratelimit.py) protect ONE identity.
They do nothing about a crowd: a few hundred attendees registering in the same
minute each stay far below those limits, yet together they ask the email
provider for more requests per second than its plan allows. The provider then
answers 429 and every row burns one of its few attempts.

`SendThrottle` is the missing aggregate limit, owned by the worker process:

* a sliding window - at most `limit` sends may START in any `window_seconds`;
* a pause - when the provider answers 429 (optionally with `Retry-After`),
  nothing is claimed until the pause ends, so the rest of the queue does not
  also run into the limit and lose attempts;
* it only answers "how many may I take now" and "how long until the next
  slot". It never sleeps and never touches the database: the worker asks it
  BEFORE it claims rows and waits (outside every transaction) when the
  answer is zero, so no lease is held and no lock is open while paced.

Accepted limitation (see DEPLOYMENT.md, "OTP email delivery"): the state is in
process memory, exactly like the request rate limiter. A worker restart empties
the window (at most one extra window's worth of sends, and the provider's own
429 + `Retry-After` handling covers the rest), and each worker process enforces
the limit on its own - which is why the supported deployment runs ONE worker.
Sharing the window between workers would need a shared store; no deployment
has shown the need.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

# A provider may ask for a very long wait (a daily quota, for example). Pausing
# the whole worker that long would also hide the moment the provider recovers,
# so a pause is capped and the worker simply asks again after it.
MAX_PAUSE_SECONDS = 300.0
# Used when the provider says 429 without a usable `Retry-After`.
DEFAULT_PAUSE_SECONDS = 10.0


@dataclass(frozen=True)
class ThrottleState:
    """What the status file and the logs may say about the throttle."""

    sent_in_window: int
    limit: int
    paused_for_seconds: float


class SendThrottle:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._starts: deque[float] = deque()
        self._paused_until = 0.0

    # --- internals (call with the lock held) ----------------------------------

    def _drop_old(self, now: float) -> None:
        horizon = now - self.window_seconds
        while self._starts and self._starts[0] <= horizon:
            self._starts.popleft()

    def _available(self, now: float) -> int:
        if now < self._paused_until:
            return 0
        self._drop_old(now)
        return max(0, self.limit - len(self._starts))

    # --- the worker's questions ------------------------------------------------

    def available(self) -> int:
        """How many sends may start right now (0 while paused or full)."""
        with self._lock:
            return self._available(self._clock())

    def reserve(self, count: int) -> None:
        """Records `count` sends starting now. Call it for what was actually
        claimed, not for what was allowed."""
        if count < 1:
            return
        with self._lock:
            now = self._clock()
            self._starts.extend([now] * count)

    def refund(self, count: int = 1) -> None:
        """Gives back slots reserved for rows that never reached the provider
        (a code that was replaced or used while it waited): they cost no request,
        so they must not eat into the provider's allowance."""
        with self._lock:
            for _ in range(min(count, len(self._starts))):
                self._starts.pop()

    def wait_seconds(self) -> float:
        """0 when a send may start now, else how long until one may. A hint for
        the worker's idle wait - never a promise."""
        with self._lock:
            now = self._clock()
            if self._available(now) > 0:
                return 0.0
            until_pause_ends = max(0.0, self._paused_until - now)
            if now < self._paused_until:
                return until_pause_ends
            # window is full: the oldest start leaves it first
            return max(0.0, self._starts[0] + self.window_seconds - now)

    def pause(self, seconds: float | None) -> float:
        """The provider asked us to slow down. Returns the pause actually
        applied (capped; a missing or non-positive hint uses the default).
        A shorter request never shortens a pause already running."""
        wanted = DEFAULT_PAUSE_SECONDS if seconds is None or seconds <= 0 else seconds
        applied = min(float(wanted), MAX_PAUSE_SECONDS)
        with self._lock:
            now = self._clock()
            self._paused_until = max(self._paused_until, now + applied)
            return max(0.0, self._paused_until - now)

    def state(self) -> ThrottleState:
        with self._lock:
            now = self._clock()
            self._drop_old(now)
            return ThrottleState(
                sent_in_window=len(self._starts),
                limit=self.limit,
                paused_for_seconds=max(0.0, self._paused_until - now),
            )
