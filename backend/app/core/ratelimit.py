"""Rate limiting — ARCHITECTURE.md §21.7.

"Limit the identity being attacked, not the network path." Each
endpoint pairs a per-identity primary limit (which does the security
work) with a shared-IP ceiling set high enough that a venue full of
attendees behind one NAT address never reaches it (§24.26).

"Demo implementation is an in-process fixed-window counter (consistent
with the single-worker invariant, §17.10); production moves to a
shared store." `RateLimitStore` is that seam: only the store changes.

Checks run before the reset fence: they read and write no application
data, so a reset cannot invalidate them (§19.4), and a throttled request
should never hold the fence at all.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.core.errors import RateLimitedError


@dataclass(frozen=True)
class RateLimit:
    name: str
    limit: int
    window_seconds: int


# §21.7's table, verbatim. Only endpoints that exist so far are wired
# up; staff login attaches its own rows in M09.
OTP_REQUEST_PER_EMAIL = RateLimit("otp_request:email", 3, 15 * 60)
OTP_REQUEST_PER_IP = RateLimit("otp_request:ip", 600, 15 * 60)
OTP_VERIFY_PER_EMAIL = RateLimit("otp_verify:email", 10, 15 * 60)
OTP_VERIFY_PER_IP = RateLimit("otp_verify:ip", 1200, 15 * 60)
# Registration's identity protection is UNIQUE(email) + the ACTIVE
# branch (§8.1); only the blunt per-IP ceiling is a counter.
REGISTER_PER_IP = RateLimit("register:ip", 600, 60 * 60)
# "Card reads are now limited per token rather than per IP: that is the
# resource actually worth protecting" (§21.7).
CARD_READ_PER_TOKEN = RateLimit("card_read:token", 30, 60)
CARD_READ_PER_IP = RateLimit("card_read:ip", 1200, 60)
# Per staff account; §21.7 sets no shared-IP ceiling for this row.
SCAN_RESOLVE_PER_STAFF = RateLimit("scan_resolve:staff", 120, 60)


class RateLimitStore(Protocol):
    def hit(self, key: str, window_seconds: int, now: float) -> tuple[int, float]:
        """Counts one request against `key`'s current window; returns
        (count so far in that window, time the window ends)."""
        ...

    def reset(self) -> None: ...


class InMemoryFixedWindowStore:
    """Thread-safe (FastAPI runs sync endpoints in a threadpool), and
    bounded: windows that have ended are pruned at most once a minute."""

    _PRUNE_INTERVAL_SECONDS = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, int, int], int] = {}
        self._last_prune = 0.0

    def hit(self, key: str, window_seconds: int, now: float) -> tuple[int, float]:
        window = int(now // window_seconds)
        bucket = (key, window_seconds, window)
        with self._lock:
            if now - self._last_prune >= self._PRUNE_INTERVAL_SECONDS:
                self._counts = {b: c for b, c in self._counts.items() if (b[2] + 1) * b[1] > now}
                self._last_prune = now
            count = self._counts.get(bucket, 0) + 1
            self._counts[bucket] = count
        return count, float((window + 1) * window_seconds)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._last_prune = 0.0


class RateLimiter:
    def __init__(self, store: RateLimitStore, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    def check(self, checks: Sequence[tuple[RateLimit, str]]) -> None:
        """Counts the request against every (limit, identity) pair and
        raises RateLimitedError if ANY is exceeded. All pairs are
        counted, so a request cannot dodge one bucket by tripping
        another first."""
        now = self.clock()
        retry_after: int | None = None
        for limit, identity in checks:
            count, window_end = self.store.hit(
                f"{limit.name}:{identity}", limit.window_seconds, now
            )
            if count > limit.limit:
                wait = max(1, math.ceil(window_end - now))
                retry_after = wait if retry_after is None else max(retry_after, wait)
        if retry_after is not None:
            raise RateLimitedError(retry_after)

    def reset(self) -> None:
        self.store.reset()


limiter = RateLimiter(InMemoryFixedWindowStore())
