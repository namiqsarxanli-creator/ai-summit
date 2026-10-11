"""Admin CSV exports: attendees, points ledger, sponsor contacts.

Three exports, one engine. What each contains and the rules every export
follows are fixed here, on the server, whatever the client sends.

**What is exported**

* `attendees`        - the internal attendee list: the columns the admin screen
                       shows, for EVERY attendee matching the screen's search
                       and filters (the same `attendee_clauses`).
* `points-log`       - the points ledger, every row matching the screen's
                       filters (the same `points_clauses`). Read-only.
* `sponsor-contacts` - contact details for sponsors. The server itself restricts
                       it to attendees who are ACTIVE **and** whose sponsor
                       consent is true at the export's snapshot; the request
                       has no parameter that can widen it. Columns: first_name,
                       last_name, profession, email, phone, linkedin_url - and
                       nothing else (no id, no card token, no status).

Cursors and page sizes do not exist here: an export is the whole matching
set, bounded only by the limits below.

**Snapshot.** The export runs in ONE `REPEATABLE READ, READ ONLY` transaction
on its own connection. Its first query takes the snapshot; the row count, the
header values and every row come from that single snapshot, so concurrent
registrations, edits, consent changes and new awards can neither duplicate nor
drop a row, and the file is internally consistent. `snapshot_at` (sent as the
`X-Export-Snapshot-At` header and recorded in the audit) is the instant the
snapshot was taken. For sponsors that is what "currently consents" means:
**consent as committed at `snapshot_at`**. A withdrawal committed after that
instant is not in the file - re-export right before using a sponsor list.

**Memory.** Rows are read through a server-side cursor in batches
(`ExportLimits.batch_rows`) and each batch is encoded and handed to the client
before the next is read: memory is bounded by the batch, not by the result.

**Limits.** At most `export_max_rows` rows (checked before the first byte; a
bigger set is refused with 413 and the admin narrows the filters) and
`export_max_seconds` of wall-clock time (checked between batches; a stalled
client is cut off by the database's `idle_in_transaction_session_timeout` and by
a watchdog). One export per administrator at a time.

**Failure is never presented as success.** Everything that can be checked is
checked before the response starts (authority, ticket, limits, snapshot, the
first batch), so those failures are ordinary error responses. A failure after
the response has started cannot change the status line; the stream is then
ABORTED - the connection is dropped without the normal end of a chunked
response - so the browser reports a failed download instead of a short file that
looks complete. The response also carries the exact `X-Export-Rows`, and the
server verifies that exactly that many rows were written.

**Audit.** `export.started` (actor, kind, which filters were used - names only,
snapshot instant, expected rows) and exactly one of `export.finished` /
`export.rejected`. `outcome` is `stream_finished` (the server wrote the whole
response - it says NOTHING about whether the browser received or the user saved
the file), `interrupted` (the stream was closed before the end, e.g. the client
went away) or `failed`. Search text, personal data and CSV content are never
recorded.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import Depends
from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.ratelimit import RateLimit, limiter
from app.db.session import SessionLocal, mutating_transaction
from app.modules.admin import export_status as status
from app.modules.admin import service as admin
from app.modules.admin.csv_export import Kind, encode_header, encode_rows
from app.modules.admin.export_tickets import TTL_SECONDS, tickets
from app.modules.admin.models import AdminAudit
from app.modules.admin.schemas import (
    AttendeeFilters,
    ExportPrepared,
    PointsFilters,
    SponsorExportRequest,
)
from app.modules.attendees.models import User, UserStatus
from app.modules.points.models import PointsLog
from app.modules.points.service import maintenance_fast_path_check
from app.modules.staff.models import StaffAccount

logger = logging.getLogger(__name__)

ExportKind = Literal["attendees", "points-log", "sponsor-contacts"]
SessionFactory = Callable[[], Session]
ExportFilters = AttendeeFilters | PointsFilters | SponsorExportRequest


def export_session_factory() -> SessionFactory:
    """FastAPI dependency: where an export gets its OWN session. It cannot use
    the request's: that one ends with the endpoint function, long before a
    streamed body is finished. (Tests substitute a factory for a scratch database.)"""
    return SessionLocal


ExportSessions = Annotated[SessionFactory, Depends(export_session_factory)]

# The screen asks about a running export about once a second, so this is
# generous - but it is still a limit, and it is separate from the general
# admin read limit so polling cannot starve the rest of the console.
EXPORT_STATUS_PER_ACCOUNT = RateLimit("admin:export-status", 600, 60)
# Preparing exports is cheap but each one can lead to a long read.
EXPORT_PER_ACCOUNT = RateLimit("admin:export", 30, 600)
# After the time limit, how long a stalled stream is left before the watchdog
# releases its resources (a module value so tests can shorten it).
WATCHDOG_GRACE_SECONDS = 5.0


class ExportTooLarge(DomainError):
    code = "EXPORT_TOO_LARGE"
    status_code = 413

    def __init__(self, rows: int, maximum: int) -> None:
        super().__init__(
            f"This export matches {rows} rows; the limit is {maximum}. Narrow the filters."
        )
        self.details = {"rows": rows, "max_rows": maximum}


class ExportBusy(DomainError):
    code = "EXPORT_BUSY"
    status_code = 409

    def __init__(self) -> None:
        super().__init__("An export is already running for your account. Wait for it to finish.")


class ExportTicketInvalid(DomainError):
    code = "EXPORT_TICKET_INVALID"
    status_code = 410

    def __init__(self) -> None:
        super().__init__(
            "This export link has expired or was already used. Start the export again."
        )


class ExportFailed(DomainError):
    """The export could not even begin: it ran out of time or hit a limit while
    producing its first batch. (After the first byte there is no status line
    left to change - see ExportAborted.)"""

    code = "EXPORT_FAILED"
    status_code = 503

    def __init__(self) -> None:
        super().__init__("The export could not be completed. Narrow the filters and try again.")


class ExportAborted(RuntimeError):
    """A stream that must not be allowed to look complete."""

    reason = "error"


class ExportTimeLimit(ExportAborted):
    reason = "time_limit"


class ExportRowLimit(ExportAborted):
    reason = "row_limit"


class ExportInconsistent(ExportAborted):
    reason = "row_count_mismatch"


@dataclass(frozen=True)
class ExportLimits:
    max_rows: int
    max_seconds: float
    batch_rows: int = 500
    statement_timeout_ms: int = 30_000


def limits() -> ExportLimits:
    settings = get_settings()
    return ExportLimits(
        max_rows=settings.export_max_rows, max_seconds=float(settings.export_max_seconds)
    )


# --- what each export contains ------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ExportSpec:
    columns: tuple[tuple[str, Kind], ...]
    statement: Callable[[Any], Select[Any]]
    cells: Callable[[Any], tuple[object, ...]]
    description: str


def _attendees_statement(f: AttendeeFilters) -> Select[Any]:
    stmt = select(
        User.id,
        User.first_name,
        User.last_name,
        User.email,
        User.status,
        User.sponsor_consent,
        User.created_at,
    )
    for clause in admin.attendee_clauses(f):
        stmt = stmt.where(clause)
    return stmt.order_by(User.created_at.desc(), User.id.desc())


def _points_statement(f: PointsFilters) -> Select[Any]:
    stmt = admin.points_statement()
    for clause in admin.points_clauses(f):
        stmt = stmt.where(clause)
    return stmt.order_by(PointsLog.created_at.desc(), PointsLog.id.desc())


def _sponsor_statement(_: SponsorExportRequest) -> Select[Any]:
    # The two conditions are part of the query, not of any parameter.
    return (
        select(
            User.first_name,
            User.last_name,
            User.profession,
            User.email,
            User.phone,
            User.linkedin_url,
        )
        .where(User.status == UserStatus.ACTIVE, User.sponsor_consent.is_(True))
        .order_by(User.last_name, User.first_name, User.id)
    )


SPECS: dict[str, ExportSpec] = {
    "attendees": ExportSpec(
        columns=(
            ("id", Kind.RAW),
            ("first_name", Kind.TEXT),
            ("last_name", Kind.TEXT),
            ("email", Kind.TEXT),
            ("status", Kind.RAW),
            ("sponsor_consent", Kind.RAW),
            ("created_at", Kind.RAW),
        ),
        statement=_attendees_statement,
        cells=lambda r: (
            r.id,
            r.first_name,
            r.last_name,
            str(r.email),
            r.status.value,
            r.sponsor_consent,
            _iso(r.created_at),
        ),
        description="attendees matching the screen's filters",
    ),
    "points-log": ExportSpec(
        columns=(
            ("id", Kind.RAW),
            ("created_at", Kind.RAW),
            ("user_id", Kind.RAW),
            ("attendee_name", Kind.TEXT),
            ("booth_id", Kind.RAW),
            ("booth_name", Kind.TEXT),
            ("source", Kind.RAW),
            ("entry_type", Kind.RAW),
            ("points", Kind.RAW),
            ("task_id", Kind.RAW),
            ("activity", Kind.TEXT),
            ("entered_by_staff_id", Kind.RAW),
            ("staff_name", Kind.TEXT),
            ("entered_by_system", Kind.RAW),
            ("reverses_entry_id", Kind.RAW),
            ("reversed_by_entry_id", Kind.RAW),
            ("reversal_reason", Kind.TEXT),
        ),
        statement=_points_statement,
        cells=lambda r: (
            r.id,
            _iso(r.created_at),
            r.user_id,
            f"{r.first_name} {r.last_name}",
            r.booth_id,
            r.booth_name,
            r.source.value,
            r.entry_type.value,
            r.points,
            r.task_id,
            admin._activity_label(
                r.source.value,
                r.task_id,
                {r.task_id: r.task_title} if r.task_id is not None and r.task_title else {},
            ),
            r.entered_by_staff_id,
            r.staff_name,
            r.entered_by_system,
            r.reverses_points_log_id,
            r.reversed_by_id,
            admin.reversal_reason(r.entry_type.value, r.note),
        ),
        description="ledger rows matching the screen's filters",
    ),
    "sponsor-contacts": ExportSpec(
        columns=(
            ("first_name", Kind.TEXT),
            ("last_name", Kind.TEXT),
            ("profession", Kind.TEXT),
            ("email", Kind.TEXT),
            ("phone", Kind.PHONE),
            ("linkedin_url", Kind.TEXT),
        ),
        statement=_sponsor_statement,
        cells=lambda r: (
            r.first_name,
            r.last_name,
            r.profession,
            str(r.email),
            r.phone,
            r.linkedin_url,
        ),
        description="ACTIVE attendees whose sponsor consent is true at the snapshot",
    ),
}

FILTER_MODELS: dict[str, type[ExportFilters]] = {
    "attendees": AttendeeFilters,
    "points-log": PointsFilters,
    "sponsor-contacts": SponsorExportRequest,
}


def filters_used(filters: ExportFilters) -> list[str]:
    """NAMES of the filters that were set - never their values (a search text is
    personal data and must not reach the audit trail)."""
    return sorted(name for name in type(filters).model_fields if getattr(filters, name) is not None)


# --- one export at a time per administrator ----------------------------------

_active_lock = threading.Lock()
_active: dict[int, float] = {}


def _slot_is_live(started: float | None, limit: ExportLimits) -> bool:
    # a slot older than any export may legitimately run is a leak: not live
    return started is not None and (
        time.monotonic() - started < limit.max_seconds + WATCHDOG_GRACE_SECONDS + 5
    )


def _acquire(actor_id: int, limit: ExportLimits) -> None:
    with _active_lock:
        if _slot_is_live(_active.get(actor_id), limit):
            raise ExportBusy()
        _active[actor_id] = time.monotonic()


def _release(actor_id: int) -> None:
    with _active_lock:
        _active.pop(actor_id, None)


def active_exports() -> int:
    with _active_lock:
        return len(_active)


# --- audit --------------------------------------------------------------------


def _audit(
    factory: SessionFactory, actor_id: int, action: str, export_id: str, details: dict[str, Any]
) -> None:
    """One audit row in its own short transaction, through the reset fence
    like every other write to reset-sensitive tables."""
    session = factory()
    try:
        with mutating_transaction(session) as tx:
            tx.add(
                AdminAudit(
                    staff_account_id=actor_id,
                    action=action,
                    target_type="export",
                    target_id=export_id,
                    before_value=None,
                    after_value=details,
                )
            )
            tx.flush()
    finally:
        session.close()


# --- the stream ---------------------------------------------------------------


def _count(session: Session, stmt: Select[Any]) -> int:
    return int(
        session.execute(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ).scalar_one()
    )


class ExportRun:
    """One running export: owns its database session, its per-admin slot and
    its audit trail, and releases all three exactly once however it ends."""

    def __init__(
        self,
        *,
        factory: SessionFactory,
        actor_id: int,
        kind: str,
        filters: ExportFilters,
        limit: ExportLimits,
        status_ref: str | None = None,
    ) -> None:
        self.factory = factory
        self.status_ref = status_ref
        # True once the first chunk was produced: from then on the HTTP status
        # can no longer change, so a failure is reported as an aborted stream
        # rather than as a refusal.
        self.response_started = False
        self.actor_id = actor_id
        self.kind = kind
        self.filters = filters
        self.limit = limit
        self.spec = SPECS[kind]
        self.export_id = secrets.token_hex(8)
        self.session: Session | None = None
        self.total = 0
        self.rows = 0
        self.bytes = 0
        self.snapshot_at: datetime | None = None
        self._started = time.monotonic()
        self._finished = False
        self._lock = threading.Lock()
        self._watchdog: threading.Timer | None = None

    # -- setup (runs before the HTTP response starts) --
    def open(self) -> None:
        session = self.factory()
        self.session = session
        # The very first statements of the transaction: its isolation and mode,
        # then a query that takes the snapshot. Transaction-local, so nothing
        # leaks into the pooled connection.
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        session.execute(
            text(f"SET LOCAL statement_timeout = {int(self.limit.statement_timeout_ms)}")
        )
        idle_ms = int(self.limit.max_seconds * 1000)
        session.execute(text(f"SET LOCAL idle_in_transaction_session_timeout = {idle_ms}"))
        self.snapshot_at = session.execute(select(func.clock_timestamp())).scalar_one()
        self.total = _count(session, self.spec.statement(self.filters))

    def reject_if_too_large(self) -> None:
        if self.total <= self.limit.max_rows:
            return
        self._record_rejected("row_limit")
        self.close_resources()
        raise ExportTooLarge(self.total, self.limit.max_rows)

    def audit_started(self) -> None:
        assert self.snapshot_at is not None
        _audit(
            self.factory,
            self.actor_id,
            "export.started",
            self.export_id,
            {
                "kind": self.kind,
                "filters_used": filters_used(self.filters),
                "snapshot_at": _iso(self.snapshot_at),
                "rows_expected": self.total,
            },
        )

    def _record_rejected(self, reason: str) -> None:
        try:
            _audit(
                self.factory,
                self.actor_id,
                "export.rejected",
                self.export_id,
                {
                    "kind": self.kind,
                    "filters_used": filters_used(self.filters),
                    "reason": reason,
                    "rows_matched": self.total,
                    "max_rows": self.limit.max_rows,
                },
            )
        except Exception:  # noqa: BLE001 - the refusal must still reach the user
            logger.exception("could not audit a rejected export")

    def arm_watchdog(self) -> None:
        timer = threading.Timer(
            self.limit.max_seconds + WATCHDOG_GRACE_SECONDS, self._abandon, args=("stalled",)
        )
        timer.daemon = True
        self._watchdog = timer
        timer.start()

    # -- the body --
    def chunks(self) -> Iterator[bytes]:
        outcome = "failed"
        reason: str | None = "error"
        error_class: str | None = None
        try:
            assert self.session is not None
            stmt = self.spec.statement(self.filters).execution_options(
                stream_results=True, yield_per=self.limit.batch_rows
            )
            kinds = [kind for _, kind in self.spec.columns]
            header = encode_header([name for name, _ in self.spec.columns])
            result = self.session.execute(stmt)
            first = True
            for batch in result.partitions():
                self._check_budget()
                data = encode_rows((self.spec.cells(row) for row in batch), kinds)
                self.rows += len(batch)
                status.store.progress(self.status_ref, self.actor_id, rows_written=self.rows)
                if first:
                    data, first = header + data, False
                self.bytes += len(data)
                yield data
            if first:  # nothing matched: still a valid, header-only file
                self.bytes += len(header)
                yield header
            if self.rows != self.total:
                raise ExportInconsistent(f"wrote {self.rows} rows, snapshot counted {self.total}")
            outcome, reason = "stream_finished", None
        except GeneratorExit:
            outcome, reason = "interrupted", "closed_before_end"
            raise
        except ExportAborted as err:
            reason, error_class = err.reason, type(err).__name__
            raise
        except BaseException as err:
            reason, error_class = "error", type(err).__name__
            raise
        finally:
            self._finish(outcome, reason, error_class)

    def _check_budget(self) -> None:
        if time.monotonic() - self._started > self.limit.max_seconds:
            raise ExportTimeLimit("the export ran past its time limit")
        if self.rows > self.limit.max_rows:
            raise ExportRowLimit("the export grew past its row limit")

    # -- ending --
    def close_resources(self) -> None:
        """Idempotent. Ends the snapshot transaction, returns the connection,
        stops the watchdog and frees the administrator's slot."""
        if self._watchdog is not None:
            self._watchdog.cancel()
        session, self.session = self.session, None
        if session is not None:
            try:
                session.rollback()  # a read-only transaction: nothing to keep
            except Exception:  # noqa: BLE001
                logger.warning("export %s: rollback failed", self.export_id, exc_info=True)
            finally:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    logger.warning("export %s: close failed", self.export_id, exc_info=True)
        _release(self.actor_id)

    def _finish(self, outcome: str, reason: str | None, error_class: str | None) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self.close_resources()
        if self.response_started:
            status.store.ended(
                self.status_ref, self.actor_id, outcome=outcome, rows_written=self.rows
            )
        try:
            _audit(
                self.factory,
                self.actor_id,
                "export.finished",
                self.export_id,
                {
                    "kind": self.kind,
                    "outcome": outcome,
                    "reason": reason,
                    "error_class": error_class,
                    "rows_written": self.rows,
                    "rows_expected": self.total,
                    "bytes_written": self.bytes,
                    "elapsed_ms": int((time.monotonic() - self._started) * 1000),
                },
            )
        except Exception:  # noqa: BLE001 - nothing left to tell the client
            logger.exception("could not audit the end of export %s", self.export_id)

    def _abandon(self, reason: str) -> None:
        """Watchdog: the stream stopped being driven (a client that neither reads
        nor disconnects). Release everything; the next read, if any, fails."""
        self._finish("failed", reason, None)


@dataclass
class ExportStream:
    """A started export: headers are known, the first chunk is already read."""

    run: ExportRun
    first: bytes
    rest: Iterator[bytes]

    @property
    def headers(self) -> dict[str, str]:
        assert self.run.snapshot_at is not None
        stamp = self.run.snapshot_at.astimezone(UTC).strftime("%Y%m%d-%H%M%S")
        return {
            # a fixed pattern: no search text, no names, nothing personal
            "Content-Disposition": f'attachment; filename="facemark-{self.run.kind}-{stamp}Z.csv"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Export-Rows": str(self.run.total),
            "X-Export-Snapshot-At": _iso(self.run.snapshot_at),
        }

    def chunks(self) -> Generator[bytes, None, None]:
        yield self.first
        yield from self.rest


def start_export(
    factory: SessionFactory,
    *,
    actor_id: int,
    kind: str,
    filters: ExportFilters,
    limit: ExportLimits | None = None,
    status_ref: str | None = None,
) -> ExportStream:
    """Everything that can fail before the first byte, then the first batch.
    Raises (an ordinary error response) if any of it fails; on success the
    returned stream owns every resource it opened."""
    limit = limit or limits()
    _acquire(actor_id, limit)
    run = ExportRun(
        factory=factory,
        actor_id=actor_id,
        kind=kind,
        filters=filters,
        limit=limit,
        status_ref=status_ref,
    )
    try:
        run.open()
        run.reject_if_too_large()
        run.audit_started()
        run.arm_watchdog()
        body = run.chunks()
        first = next(body)  # opens the cursor and reads/encodes the first batch
        run.response_started = True
        status.store.streaming(status_ref, actor_id, rows_expected=run.total)
    except ExportAborted as err:
        run.close_resources()
        raise ExportFailed() from err
    except BaseException:
        run.close_resources()
        raise
    return ExportStream(run=run, first=first, rest=body)


# --- the two steps the API exposes ---------------------------------------------


def prepare(
    db: Session, *, actor: StaffAccount, kind: ExportKind, filters: ExportFilters
) -> ExportPrepared:
    """Step 1: validate and count (for an early, readable refusal), then hand
    out a one-time download ticket. Nothing leaves the server yet, so nothing
    is audited."""
    limiter.check([(EXPORT_PER_ACCOUNT, str(actor.id))])
    maintenance_fast_path_check(db)
    limit = limits()
    rows = _count(db, SPECS[kind].statement(filters))
    db.rollback()
    if rows > limit.max_rows:
        raise ExportTooLarge(rows, limit.max_rows)
    token = tickets.issue(actor_id=actor.id, kind=kind, filters=filters)
    ref = status.store.create(actor_id=actor.id, kind=kind, ticket=token, ticket_ttl=TTL_SECONDS)
    return ExportPrepared(
        download_url=f"/api/admin/exports/download?ticket={token}",
        ref=ref,
        status_url=f"/api/admin/exports/status/{ref}",
        kind=kind,
        rows=rows,
        max_rows=limit.max_rows,
        expires_in_seconds=TTL_SECONDS,
    )


def open_download(
    db: Session, factory: SessionFactory, *, actor: StaffAccount, token: str
) -> ExportStream:
    """Step 2: redeem the ticket and start streaming.

    Every refusal is also recorded in the export's status (see export_status.py)
    - the browser fetches this response inside a frame, so the status is the only
    way the admin screen learns why a download did not start - together with
    whether the same link can simply be tried again."""
    ref = status.store.ref_for_ticket(token)
    refuse = status.store.refused
    try:
        maintenance_fast_path_check(db)
    except DomainError as err:
        refuse(ref, actor.id, code=err.code, retry="same_link")  # the ticket is untouched
        raise
    limit = limits()
    ticket = tickets.peek(token, actor_id=actor.id)
    if ticket is None:
        refuse(ref, actor.id, code=status.CODE_TICKET_INVALID, retry="new_export")
        raise ExportTicketInvalid()
    # busy is checked before the ticket is spent: waiting for the running export
    # to end and trying the same link again must still work
    with _active_lock:
        busy = _slot_is_live(_active.get(actor.id), limit)
    if busy:
        refuse(ref, actor.id, code=status.CODE_BUSY, retry="same_link")
        raise ExportBusy()
    if tickets.redeem(token, actor_id=actor.id) is None:  # lost a race with a second click
        refuse(ref, actor.id, code=status.CODE_TICKET_INVALID, retry="new_export")
        raise ExportTicketInvalid()
    status.store.starting(ref, actor.id)
    try:
        return start_export(
            factory,
            actor_id=actor.id,
            kind=ticket.kind,
            filters=ticket.filters,
            limit=limit,
            status_ref=ref,
        )
    except ExportTooLarge:
        refuse(ref, actor.id, code=status.CODE_TOO_LARGE, retry="new_export")
        raise
    except ExportBusy:  # another export slipped in after the check: the ticket is spent now
        refuse(ref, actor.id, code=status.CODE_BUSY, retry="new_export")
        raise
    except ExportFailed:
        refuse(ref, actor.id, code=status.CODE_FAILED, retry="new_export")
        raise
    except DomainError as err:
        refuse(ref, actor.id, code=err.code, retry="new_export")
        raise
    except BaseException:
        refuse(ref, actor.id, code=status.CODE_INTERNAL, retry="new_export")
        raise


def export_status(*, actor: StaffAccount, ref: str) -> status.ExportStatusView:
    """What became of one export's download, for the administrator who prepared
    it. Anyone else - and an unknown or forgotten ref - gets "not found"."""
    view = status.store.view(ref, actor_id=actor.id)
    if view is None:
        raise admin.AdminNotFound("No such export.")
    return view
