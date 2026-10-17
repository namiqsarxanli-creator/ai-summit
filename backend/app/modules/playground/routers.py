from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Path, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import CurrentAttendee, CurrentStaff, SessionToken
from app.core.ratelimit import limiter
from app.core.schedule import lock_schedule
from app.core.sessions import resolve_staff_session
from app.db.session import DbSession, mutating_transaction
from app.modules.admin import service as admin
from app.modules.attendees.models import User
from app.modules.attendees.profile import lock_attendee
from app.modules.playground import service
from app.modules.playground.schemas import (
    BookRequest,
    InviteRequest,
    PrepareRequest,
    TokenRequest,
    VersionRequest,
)
from app.modules.points.service import maintenance_fast_path_check

router = APIRouter(tags=["playground"])
# Administration lives on its own router so it is excluded from the production
# OpenAPI schema like every other /api/admin route (ARCHITECTURE.md §18).
admin_router = APIRouter(tags=["admin"])
RowId = Annotated[int, Path(ge=1, le=2**53)]


@router.get("/playground")
def programme(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return service.programme(db, user.id)


def write(
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
        if name == "ck_playground_capacity":
            raise service.PlaygroundError("slot_full") from err
        raise


@router.post("/playground/slots/{slot_id}/book")
def book(
    slot_id: RowId, payload: BookRequest, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return write(
        db,
        token,
        lambda tx, user: service.book(
            tx, user, slot_id, str(payload.command_id), payload.expected_revision, payload.queue
        ),
    )


@router.post("/playground/bookings/{booking_id}/cancel")
def cancel(
    booking_id: RowId, payload: VersionRequest, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return write(
        db, token, lambda tx, user: service.cancel(tx, user, booking_id, payload.expected_version)
    )


@router.post("/playground/slots/{slot_id}/invite")
def invite(
    slot_id: RowId, payload: InviteRequest, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return write(
        db, token, lambda tx, user: service.invite(tx, user, slot_id, str(payload.command_id))
    )


@router.post("/playground/invitations/accept")
def accept(payload: TokenRequest, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return write(db, token, lambda tx, user: service.accept(tx, user, payload.token))


@router.get("/staff/playground/slots/{slot_id}")
def staff_list(
    slot_id: RowId, staff: CurrentStaff, db: DbSession, response: Response
) -> dict[str, Any]:
    """Names of the people booked or queueing for a slot of the staff member's
    own booth (an administrator sees every booth). Never contact data."""
    response.headers["Cache-Control"] = "no-store"
    return service.staff_list(db, staff, slot_id)


@router.post("/staff/playground/bookings/{booking_id}/admit")
def admit(
    booking_id: RowId, payload: VersionRequest, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        lock_schedule(tx)
        staff = resolve_staff_session(tx, token)
        return service.admit(tx, staff, booking_id, payload.expected_version)


@admin_router.post("/admin/playground/prepare")
def prepare(payload: PrepareRequest, token: SessionToken, db: DbSession) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        lock_schedule(tx)
        return admin.execute(
            tx,
            token,
            lambda session, actor: (
                "playground.prepare",
                "event",
                None,
                {"event_id": payload.event_id, "booth_id": payload.booth_id},
                service.prepare(session, payload.event_id, payload.booth_id),
            ),
        )


@router.post("/playground/invitations/inspect")
def inspect_invite(payload: TokenRequest, token: SessionToken, db: DbSession) -> dict[str, Any]:
    def inspect(tx: Session, user: User) -> dict[str, Any]:
        limiter.check([(service.LIMIT, str(user.id))])
        b = service.invitation(tx, payload.token)
        if b.status != "INVITED" or b.expires_at is None or b.expires_at <= service.now():
            raise service.PlaygroundError("invitation_expired")
        slot = service.slot_of(tx, b.slot_id)
        return {
            "slot_id": slot.id,
            "starts_at": slot.starts_at,
            "ends_at": slot.ends_at,
            "booth_id": slot.booth_id,
            "expires_at": b.expires_at,
        }

    return write(db, token, inspect)


@router.get("/staff/playground")
def staff_programme(staff: CurrentStaff, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return service.programme(db, booth_id=staff.booth_id)
