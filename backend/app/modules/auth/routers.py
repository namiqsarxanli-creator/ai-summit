"""`POST /api/auth/otp/request` and `POST /api/auth/otp/verify` —
ARCHITECTURE.md §8.2, §8.1 Step 3, §8.3.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.core.errors import OtpInvalidError
from app.db.session import DbSession, mutating_transaction
from app.modules.attendees.service import SESSION_TTL_HOURS, verify_activation_otp
from app.modules.auth.otp_delivery import deliver_otp
from app.modules.auth.schemas import (
    OtpRequestRequest,
    OtpRequestResponse,
    OtpVerifyRequest,
    OtpVerifyResponse,
)
from app.modules.auth.service import request_otp

router = APIRouter(prefix="/auth", tags=["auth"])

# No architecture-mandated name exists for the cookie itself (only its
# attributes are specified, §8.3) — chosen once here so M05 has a
# single, already-established name for logout/session-resolution to
# reference rather than inventing its own.
SESSION_COOKIE_NAME = "session"


@router.post("/otp/request", response_model=OtpRequestResponse)
def otp_request(payload: OtpRequestRequest, db: DbSession) -> OtpRequestResponse:
    with mutating_transaction(db) as tx:
        delivery = request_otp(tx, payload.email)

    if delivery is not None:
        deliver_otp(delivery)

    return OtpRequestResponse()


@router.post("/otp/verify", response_model=OtpVerifyResponse)
def otp_verify(
    payload: OtpVerifyRequest, request: Request, response: Response, db: DbSession
) -> OtpVerifyResponse:
    """ARCHITECTURE.md §8.1 Step 3: consume the challenge, activate the
    attendee and create the session, all in one transaction. A wrong
    code still commits (its `attempt_count` increment must persist,
    §8.2) — only AFTER the transaction exits does a failed outcome
    become the client-visible `OTP_INVALID` (see VerifyOutcome's
    docstring for why this can't be raised from inside the fence)."""
    with mutating_transaction(db) as tx:
        outcome = verify_activation_otp(tx, email=payload.email, code=payload.code)

    if not outcome.ok:
        raise OtpInvalidError(
            "This code is invalid, expired, already used, or has too many failed attempts."
        )

    assert outcome.session_token is not None  # ok=True always carries one

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=outcome.session_token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
        max_age=SESSION_TTL_HOURS * 3600,
    )
    return OtpVerifyResponse()
