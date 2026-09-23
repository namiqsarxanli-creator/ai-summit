"""`POST /api/attendees/register` — ARCHITECTURE.md §8.1."""

from __future__ import annotations

from fastapi import APIRouter

from app.db.session import DbSession, mutating_transaction
from app.modules.attendees.schemas import RegisterRequest, RegisterResponse
from app.modules.attendees.service import register_attendee
from app.modules.auth.otp_delivery import deliver_otp

router = APIRouter(prefix="/attendees", tags=["attendees"])


@router.post("/register", response_model=RegisterResponse)
def register(payload: RegisterRequest, db: DbSession) -> RegisterResponse:
    """Returns no session — ARCHITECTURE.md §8.1: "It returns no
    session. The response says only that a code has been sent." Same
    response body regardless of which of the three duplicate-email
    branches ran (§8.1 Step 2)."""
    with mutating_transaction(db) as tx:
        delivery = register_attendee(tx, payload)

    # Delivered only AFTER the transaction above has committed — never
    # signal a code that might not have actually persisted.
    if delivery is not None:
        deliver_otp(delivery)

    return RegisterResponse()
