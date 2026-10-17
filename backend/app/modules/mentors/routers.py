"""HTTP surface of Meet the Mentor (migration 0022).

Attendee (the session's own person; no request names anyone):
* GET  /api/mentor-sessions                       - open sessions, seats, own booking
* POST /api/mentor-sessions/{id}/book             - book or join the waiting list
* POST /api/mentor-bookings/{id}/cancel           - until the session starts

Staff (MENTOR_HOST scope for THAT session, re-read inside the fence):
* GET  /api/staff/mentor-sessions                 - the sessions this account hosts
* GET  /api/staff/mentor-sessions/{id}            - the group (short names, status)
* POST /api/staff/mentor-sessions/{id}/complete   - end-of-session scan, +5 once

Admin (excluded from the production schema, audited):
* GET/POST /api/admin/mentor-sessions, PATCH /api/admin/mentor-sessions/{id}

Every attendee write takes: reset fence -> schedule lock -> attendee lock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import CurrentAdmin, CurrentAttendee, CurrentStaff, SessionToken
from app.core.ratelimit import RateLimit, limiter
from app.core.schedule import lock_schedule
from app.core.sessions import resolve_staff_session
from app.db.session import DbSession, mutating_transaction
from app.modules.admin import service as admin
from app.modules.admin.routers import _read
from app.modules.admin.service import AdminConflict, AdminInvalid, AdminResult, required
from app.modules.attendees.models import User
from app.modules.attendees.profile import lock_attendee
from app.modules.masterclasses.models import MasterclassEvent
from app.modules.mentors import service
from app.modules.mentors.models import ACTIVE, SEATED, MentorBooking, MentorSession
from app.modules.points.service import maintenance_fast_path_check
from app.modules.staff.models import StaffAccount

router = APIRouter(tags=["mentors"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])

RowId = Annotated[int, Path(ge=1, le=2**53)]
SCAN_PER_STAFF = RateLimit("mentor_scan:staff", 600, 600)
DURATION = timedelta(minutes=20)

AUDIT_SESSION_CREATE = "mentor_session.create"
AUDIT_SESSION_UPDATE = "mentor_session.update"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookRequest(Strict):
    command_id: UUID
    expected_revision: str = Field(pattern=r"^(0|[1-9][0-9]*:[1-9][0-9]*)$", max_length=50)


class VersionRequest(Strict):
    expected_version: int = Field(ge=1, le=2**31 - 1)


class CompleteRequest(Strict):
    card_token: str = Field(min_length=20, max_length=128)


class SessionWrite(Strict):
    event_id: int = Field(ge=1, le=2**53)
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    title: str = Field(min_length=1, max_length=200)
    mentor_name: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: datetime
    capacity: int = Field(default=6, ge=1, le=6)
    waitlist_capacity: int = Field(default=3, ge=0, le=20)
    is_open: bool = True

    @field_validator("starts_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("a time zone offset is required")
        return v


class SessionEdit(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    mentor_name: str | None = Field(default=None, min_length=1, max_length=200)
    starts_at: datetime | None = None
    capacity: int | None = Field(default=None, ge=1, le=6)
    waitlist_capacity: int | None = Field(default=None, ge=0, le=20)
    is_open: bool | None = None

    @field_validator("starts_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("a time zone offset is required")
        return v


def _now() -> datetime:
    return datetime.now(UTC)


# --- attendee ---------------------------------------------------------------------------


def _write(
    db: Session, token: str | None, run: Callable[[Session, User], dict[str, Any]]
) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            lock_schedule(tx)
            user = lock_attendee(tx, token)
            return run(tx, user)
    except IntegrityError as err:
        name = getattr(getattr(err.orig, "diag", None), "constraint_name", None)
        if name == "ck_mentor_capacity":
            raise service.MentorError("session_full") from err
        if name == "uq_mentor_bookings_one_active":
            raise service.MentorError("one_active_booking") from err
        raise


@router.get("/mentor-sessions")
def programme(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return service.programme(db, user.id)


@router.post("/mentor-sessions/{session_id}/book")
def book(
    session_id: RowId, payload: BookRequest, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(
        db,
        token,
        lambda tx, user: service.book(
            tx, user, session_id, str(payload.command_id), payload.expected_revision
        ),
    )


@router.post("/mentor-bookings/{booking_id}/cancel")
def cancel(
    booking_id: RowId, payload: VersionRequest, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _write(
        db, token, lambda tx, user: service.cancel(tx, user, booking_id, payload.expected_version)
    )


# --- the host ---------------------------------------------------------------------------


@router.get("/staff/mentor-sessions")
def host_sessions(staff: CurrentStaff, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return service.host_sessions(db, staff)


@router.get("/staff/mentor-sessions/{session_id}")
def host_list(
    session_id: RowId, staff: CurrentStaff, db: DbSession, response: Response
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return service.host_list(db, staff, session_id)


@router.post("/staff/mentor-sessions/{session_id}/complete")
def complete(
    session_id: RowId, payload: CompleteRequest, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        lock_schedule(tx)
        staff = resolve_staff_session(tx, token)  # inside the fence: revocation applies
        limiter.check([(SCAN_PER_STAFF, str(staff.id))])
        return service.complete(
            tx, staff, session_id=session_id, card_token=payload.card_token, now=_now()
        )


# --- administration ---------------------------------------------------------------------


def _active(tx: Session, session_id: int, statuses: tuple[str, ...]) -> int:
    return int(
        tx.execute(
            select(func.count())
            .select_from(MentorBooking)
            .where(MentorBooking.session_id == session_id, MentorBooking.status.in_(statuses))
        ).scalar_one()
    )


def _create(tx: Session, actor: StaffAccount, payload: SessionWrite) -> AdminResult:
    required(tx, MasterclassEvent, payload.event_id)
    data = payload.model_dump()
    row = MentorSession(**data, ends_at=payload.starts_at + DURATION)
    tx.add(row)
    tx.flush()
    tx.refresh(row)  # times as stored, so every response spells them the same way
    view = service.admin_view(row)
    return AUDIT_SESSION_CREATE, "mentor_session", None, view, view


def _edit(tx: Session, actor: StaffAccount, session_id: int, payload: SessionEdit) -> AdminResult:
    row = tx.execute(
        select(MentorSession).where(MentorSession.id == session_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise admin.AdminNotFound("No such record.")
    before = service.admin_view(row)
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key != "mentor_name":
            raise AdminInvalid(f"{key} cannot be empty.")
    if "starts_at" in changes and changes["starts_at"] != row.starts_at:
        # Moving a session under people who planned their day around it could
        # double-book them; it is refused while anyone holds a place.
        if _active(tx, row.id, (*ACTIVE, "ATTENDED")):
            raise AdminConflict("People hold places in this session; it cannot be moved.")
        row.starts_at = changes["starts_at"]
        row.ends_at = changes["starts_at"] + DURATION
    if "capacity" in changes and changes["capacity"] < _active(tx, row.id, SEATED):
        raise AdminConflict("More people already hold seats than the new group size.")
    if "waitlist_capacity" in changes and changes["waitlist_capacity"] < _active(
        tx, row.id, ("WAITLISTED",)
    ):
        raise AdminConflict("More people are already waiting than the new list size.")
    for key in ("title", "mentor_name", "capacity", "waitlist_capacity", "is_open"):
        if key in changes:
            setattr(row, key, changes[key])
    tx.flush()
    if "capacity" in changes:  # a larger group seats the people waiting, in order
        service.promote_waiting(tx, row, _now())
    tx.refresh(row)
    after = service.admin_view(row)
    return AUDIT_SESSION_UPDATE, "mentor_session", before, after, after


def _admin_write(db: DbSession, token: str | None, op: admin.AdminOperation) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            lock_schedule(tx)
            return admin.execute(tx, token, op)
    except IntegrityError as err:  # a duplicate session code
        raise AdminConflict("A session with that code already exists.") from err


@admin_router.get("/mentor-sessions")
def list_sessions(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    rows = db.execute(
        select(MentorSession).order_by(MentorSession.starts_at, MentorSession.id)
    ).scalars()
    return [service.admin_view(r) for r in rows]


@admin_router.post("/mentor-sessions", status_code=201)
def create_session(payload: SessionWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: _create(tx, actor, payload))


@admin_router.patch("/mentor-sessions/{session_id}")
def edit_session(
    session_id: RowId, payload: SessionEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: _edit(tx, actor, session_id, payload))
