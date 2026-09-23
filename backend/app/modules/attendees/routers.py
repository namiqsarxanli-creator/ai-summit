"""`POST /api/attendees/register` (§8.1) and `GET /api/attendees/me`
(M05)."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import ClientIp, CurrentAttendee
from app.core.ratelimit import REGISTER_PER_IP, limiter
from app.db.session import DbSession, mutating_transaction
from app.modules.attendees.schemas import AttendeeMe, RegisterRequest, RegisterResponse
from app.modules.attendees.service import register_attendee
from app.modules.auth.otp_delivery import deliver_otp

router = APIRouter(prefix="/attendees", tags=["attendees"])


@router.post("/register", response_model=RegisterResponse)
def register(payload: RegisterRequest, db: DbSession, ip: ClientIp) -> RegisterResponse:
    """Returns no session — ARCHITECTURE.md §8.1: "It returns no
    session. The response says only that a code has been sent." Same
    response body regardless of which of the three duplicate-email
    branches ran (§8.1 Step 2).

    Only a shared-IP ceiling is counted here (§21.7): the identity
    protection is UNIQUE(email) plus the ACTIVE branch."""
    limiter.check([(REGISTER_PER_IP, ip)])

    with mutating_transaction(db) as tx:
        delivery = register_attendee(tx, payload)

    # Delivered only AFTER the transaction above has committed — never
    # signal a code that might not have actually persisted.
    if delivery is not None:
        deliver_otp(delivery)

    return RegisterResponse()


@router.get("/me", response_model=AttendeeMe)
def me(attendee: CurrentAttendee) -> AttendeeMe:
    """The attendee identity comes only from the resolved server-side
    session — there is no user id anywhere in the request. Read-only, so
    no reset fence (§19.4)."""
    return AttendeeMe(
        first_name=attendee.first_name,
        last_name=attendee.last_name,
        email=attendee.email,
        profession=attendee.profession,
    )
