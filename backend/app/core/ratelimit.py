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
# Keyed by the submitted (normalised) email, exactly like the OTP
# rows — never by a resolved staff id, so an unknown email is limited
# too and cannot be used to distinguish "no such account" from "wrong
# password" by hammering one bucket differently. This is BESIDES the
# persistent failed_login_count/locked_until brute-force gate (§21.8):
# two independent defenses, mirroring OTP's own attempt-cap +
# rate-limit pair.
STAFF_LOGIN_PER_EMAIL = RateLimit("staff_login:email", 10, 15 * 60)
STAFF_LOGIN_PER_IP = RateLimit("staff_login:ip", 600, 15 * 60)


# Staff passwords. Changing one needs the current password, so a stolen session
# must not be able to guess it: few tries per staff account.
STAFF_PASSWORD_CHANGE_PER_STAFF = RateLimit("staff_password_change:staff", 5, 15 * 60)
# Reset by email: limited per submitted address (known or not, so the limiter is
# no oracle), exactly like the attendee OTP rows, plus the shared-IP ceiling.
STAFF_RESET_REQUEST_PER_EMAIL = RateLimit("staff_reset_request:email", 3, 15 * 60)
STAFF_RESET_REQUEST_PER_IP = RateLimit("staff_reset_request:ip", 600, 15 * 60)
STAFF_RESET_CONFIRM_PER_EMAIL = RateLimit("staff_reset_confirm:email", 10, 15 * 60)
STAFF_RESET_CONFIRM_PER_IP = RateLimit("staff_reset_confirm:ip", 1200, 15 * 60)
# An administrator starting reset emails for other staff.
ADMIN_RESET_START_PER_ADMIN = RateLimit("admin_reset_start:admin", 20, 60 * 60)


# The attendee's own profile (app/modules/attendees/profile.py): per signed-in
# attendee, generous for a person editing a form, tight for a script. Photos cost
# CPU and disk, so they are limited harder (and per IP as a ceiling that a venue
# behind one NAT address will not reach).
PROFILE_UPDATE_PER_ATTENDEE = RateLimit("profile_update:attendee", 60, 10 * 60)
CONSENT_UPDATE_PER_ATTENDEE = RateLimit("consent_update:attendee", 30, 10 * 60)
PHOTO_UPLOAD_PER_ATTENDEE = RateLimit("photo_upload:attendee", 12, 10 * 60)
PHOTO_UPLOAD_PER_IP = RateLimit("photo_upload:ip", 600, 10 * 60)


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
        """Counts the request against the (limit, identity) pairs IN ORDER
        and raises RateLimitedError at the first one that is exceeded;
        the pairs after it are not counted.

        Callers list the shared-IP ceiling FIRST. A client already over
        its own ceiling is then refused before it can spend anyone
        else's per-identity bucket: the submitted email is attacker-chosen
        input, so counting it on a refused request would let one address
        lock any number of other people out of signing in. The pair that
        refused is always counted, so a client cannot dodge it."""
        now = self.clock()
        for limit, identity in checks:
            count, window_end = self.store.hit(
                f"{limit.name}:{identity}", limit.window_seconds, now
            )
            if count > limit.limit:
                raise RateLimitedError(max(1, math.ceil(window_end - now)))

    def reset(self) -> None:
        self.store.reset()


limiter = RateLimiter(InMemoryFixedWindowStore())
