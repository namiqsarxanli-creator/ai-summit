"""One-time download tickets for CSV exports.

The browser must receive the file through a plain download (so a large file
streams to disk instead of being held in page memory), and a plain download is
a GET. A GET that exports personal data must not be triggerable by a link,
a prefetch or a cross-site page, and must not carry the administrator's search
text in a URL that ends up in logs and history. So the export is two steps:

1. `POST /api/admin/exports/<kind>` (JSON body = the filters, same-origin,
   admin session) validates everything and answers with a **ticket**.
2. `GET /api/admin/exports/download?ticket=...` redeems it.

A ticket is 256 random bits, held in server memory (the backend runs exactly
one worker - invariant #17), bound to the administrator who prepared it,
good for `TTL_SECONDS`, and **single use**. The filters live server-side with
the ticket; the URL carries nothing but the opaque token.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.privacy import remember_secret

TTL_SECONDS = 60
MAX_OUTSTANDING = 200
MAX_PER_ADMIN = 5


@dataclass(frozen=True)
class Ticket:
    actor_id: int
    kind: str
    filters: Any  # the validated filter model the export will run with
    expires_at: float


class TicketStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}

    def _prune(self, now: float) -> None:
        for token in [t for t, ticket in self._tickets.items() if ticket.expires_at <= now]:
            del self._tickets[token]

    def issue(self, *, actor_id: int, kind: str, filters: Any) -> str:
        now = self._clock()
        with self._lock:
            self._prune(now)
            mine = [t for t, ticket in self._tickets.items() if ticket.actor_id == actor_id]
            # an administrator who keeps preparing without downloading only
            # ever displaces their own oldest ticket
            while len(mine) >= MAX_PER_ADMIN:
                del self._tickets[mine.pop(0)]
            while len(self._tickets) >= MAX_OUTSTANDING:
                del self._tickets[next(iter(self._tickets))]
            token = secrets.token_urlsafe(32)
            remember_secret(token)  # the log filter hides it even without `ticket=`
            self._tickets[token] = Ticket(actor_id, kind, filters, now + TTL_SECONDS)
            return token

    def peek(self, token: str, *, actor_id: int) -> Ticket | None:
        """The ticket if it is live and belongs to `actor_id`, else None."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            ticket = self._tickets.get(token)
            return ticket if ticket is not None and ticket.actor_id == actor_id else None

    def redeem(self, token: str, *, actor_id: int) -> Ticket | None:
        """Like `peek`, and the ticket can never be redeemed again."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            ticket = self._tickets.get(token)
            if ticket is None or ticket.actor_id != actor_id:
                return None
            del self._tickets[token]
            return ticket

    def clear(self) -> None:
        with self._lock:
            self._tickets.clear()


tickets = TicketStore()
