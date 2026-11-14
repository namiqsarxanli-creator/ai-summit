"""What happened to an export download - for the admin screen, not for the log.

The CSV itself is fetched by the BROWSER (a hidden frame pointed at the
one-time download link), so the page never sees that response: a 410, a 409, a
size refusal or a start-up error would otherwise vanish inside the frame while
the screen says the download started. This store lets the page ask the server
what became of the download it started.

* One entry per prepared export, identified by an unguessable `ref` that is NOT
  the download ticket (the ticket grants the file; the ref grants only a view of
  this status).
* Bound to the administrator who prepared it: anyone else gets "not found".
* Holds outcomes and counts only - never a row, a column, a filter value or the
  ticket (only its SHA-256, kept to attribute a refused download to its entry).
* In memory (the backend runs exactly one worker - invariant #17), bounded, and
  forgotten `RETENTION_SECONDS` after its last change.

State, as the server knows it:

    ready        prepared; the browser has not asked for the file yet
    starting     the download request is being served (snapshot, first batch)
    streaming    the response has started; `rows_written` grows as it is sent
    finished     the server wrote the whole response (it cannot know the user
                 received or saved the file)
    interrupted  the response was closed before its end (the client went away)
    failed       the response started and then broke; the stream was aborted
    rejected     the download was refused before any byte (`code` says why)
    expired      prepared, never requested, and the ticket has timed out

`retry` tells the page what a retry needs: `same_link` (the ticket is still
valid - e.g. another export was running), `new_export` (prepare again), or
`none` (nothing a retry can fix from here).
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

State = Literal[
    "ready", "starting", "streaming", "finished", "interrupted", "failed", "rejected", "expired"
]
Retry = Literal["none", "same_link", "new_export"]

RETENTION_SECONDS = 15 * 60
MAX_ENTRIES = 400
MAX_PER_ADMIN = 30

# Error codes the page maps to text (see frontend lib/errors.ts).
CODE_TICKET_INVALID = "EXPORT_TICKET_INVALID"
CODE_BUSY = "EXPORT_BUSY"
CODE_TOO_LARGE = "EXPORT_TOO_LARGE"
CODE_FAILED = "EXPORT_FAILED"
CODE_ABORTED = "EXPORT_ABORTED"
CODE_INTERNAL = "INTERNAL_ERROR"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class ExportEntry:
    ref: str
    actor_id: int
    kind: str
    state: State
    ticket_deadline: float  # monotonic instant after which an unused ticket is dead
    updated_at: float
    code: str | None = None
    retry: Retry = "none"
    rows_expected: int | None = None
    rows_written: int | None = None


@dataclass(frozen=True)
class ExportStatusView:
    ref: str
    kind: str
    state: State
    code: str | None
    retry: Retry
    rows_expected: int | None
    rows_written: int | None


class StatusStore:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, ExportEntry] = {}
        self._by_ticket: dict[str, str] = {}  # sha256(ticket) -> ref

    # -- bookkeeping -----------------------------------------------------------
    def _prune(self, now: float) -> None:
        stale = [r for r, e in self._entries.items() if now - e.updated_at > RETENTION_SECONDS]
        for ref in stale:
            self._drop(ref)

    def _drop(self, ref: str) -> None:
        self._entries.pop(ref, None)
        for digest in [d for d, r in self._by_ticket.items() if r == ref]:
            del self._by_ticket[digest]

    def create(self, *, actor_id: int, kind: str, ticket: str, ticket_ttl: float) -> str:
        now = self._clock()
        with self._lock:
            self._prune(now)
            mine = [r for r, e in self._entries.items() if e.actor_id == actor_id]
            while len(mine) >= MAX_PER_ADMIN:
                self._drop(mine.pop(0))
            while len(self._entries) >= MAX_ENTRIES:
                self._drop(next(iter(self._entries)))
            ref = secrets.token_urlsafe(16)
            self._entries[ref] = ExportEntry(
                ref=ref,
                actor_id=actor_id,
                kind=kind,
                state="ready",
                ticket_deadline=now + ticket_ttl,
                updated_at=now,
            )
            self._by_ticket[_digest(ticket)] = ref
            return ref

    def ref_for_ticket(self, ticket: str) -> str | None:
        with self._lock:
            return self._by_ticket.get(_digest(ticket))

    # -- reading ---------------------------------------------------------------
    def view(self, ref: str, *, actor_id: int) -> ExportStatusView | None:
        """The status, or None if there is no such export FOR THIS administrator
        (another admin's, an unknown and a forgotten ref are indistinguishable)."""
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(ref)
            if entry is None or entry.actor_id != actor_id:
                return None
            state, code, retry = entry.state, entry.code, entry.retry
            if (
                state in ("ready", "rejected")
                and retry != "new_export"
                and now > entry.ticket_deadline
            ):
                # the ticket timed out: whatever the page was waiting for, it is over
                state, code, retry = "expired", CODE_TICKET_INVALID, "new_export"
            return ExportStatusView(
                ref=entry.ref,
                kind=entry.kind,
                state=state,
                code=code,
                retry=retry,
                rows_expected=entry.rows_expected,
                rows_written=entry.rows_written,
            )

    # -- writing (each requires the entry to belong to the acting administrator) ----
    def _entry(self, ref: str | None, actor_id: int) -> ExportEntry | None:
        entry = self._entries.get(ref) if ref else None
        return entry if entry is not None and entry.actor_id == actor_id else None

    def _set(self, entry: ExportEntry, state: State, **fields: object) -> None:
        entry.state = state
        entry.updated_at = self._clock()
        for name, value in fields.items():
            setattr(entry, name, value)

    @staticmethod
    def _can_start(entry: ExportEntry) -> bool:
        """A download attempt may (re)start only before anything was sent, or
        after a refusal that left the ticket valid."""
        return entry.state == "ready" or (entry.state == "rejected" and entry.retry == "same_link")

    @classmethod
    def _can_refuse(cls, entry: ExportEntry) -> bool:
        """A refusal is recorded while the attempt is still before its first byte."""
        return cls._can_start(entry) or entry.state == "starting"

    def refused(self, ref: str | None, actor_id: int, *, code: str, retry: Retry) -> None:
        """The download was refused before the first byte."""
        with self._lock:
            entry = self._entry(ref, actor_id)
            if entry is not None and self._can_refuse(entry):
                self._set(entry, "rejected", code=code, retry=retry)

    def starting(self, ref: str | None, actor_id: int) -> None:
        with self._lock:
            entry = self._entry(ref, actor_id)
            if entry is not None and self._can_start(entry):
                self._set(entry, "starting", code=None, retry="none")

    def streaming(self, ref: str | None, actor_id: int, *, rows_expected: int) -> None:
        with self._lock:
            entry = self._entry(ref, actor_id)
            if entry is not None and entry.state in ("starting", "ready", "rejected"):
                self._set(entry, "streaming", rows_expected=rows_expected, rows_written=0)

    def progress(self, ref: str | None, actor_id: int, *, rows_written: int) -> None:
        with self._lock:
            entry = self._entry(ref, actor_id)
            if entry is not None and entry.state == "streaming":
                entry.rows_written = rows_written  # not a state change: no `updated_at` churn

    def ended(self, ref: str | None, actor_id: int, *, outcome: str, rows_written: int) -> None:
        """The stream ended: `stream_finished`, `interrupted` or `failed`."""
        with self._lock:
            entry = self._entry(ref, actor_id)
            if entry is None or entry.state not in ("starting", "streaming"):
                return
            if outcome == "stream_finished":
                self._set(entry, "finished", rows_written=rows_written, code=None, retry="none")
            elif outcome == "interrupted":
                self._set(
                    entry,
                    "interrupted",
                    rows_written=rows_written,
                    code=CODE_ABORTED,
                    retry="new_export",
                )
            else:
                self._set(
                    entry,
                    "failed",
                    rows_written=rows_written,
                    code=CODE_ABORTED,
                    retry="new_export",
                )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._by_ticket.clear()


store = StatusStore()
