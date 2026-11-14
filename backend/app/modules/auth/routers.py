"""`POST /api/auth/otp/request`, `POST /api/auth/otp/verify`,
`POST /api/auth/logout` — ARCHITECTURE.md §8.1-§8.3, §21.7.

Ordering in every handler: rate-limit checks (in-memory, touch no
application data) -> `mutating_transaction` (the reset fence is its
first statement) -> every authoritative read and write -> commit ->
post-commit side effects (OTP delivery, cookie). §19.4 permits only
dataset-independent work before the fence; nothing here reads the
database outside it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Response

from app.core.deps import ClientIp, SessionToken
from app.core.errors import EmailNotRegisteredError, OtpInvalidError
from app.core.ratelimit import (
    OTP_REQUEST_PER_EMAIL,
    OTP_REQUEST_PER_IP,
    OTP_VERIFY_PER_EMAIL,
    OTP_VERIFY_PER_IP,
    limiter,
)
from app.core.sessions import (
    REGISTRATION_INTENT_COOKIE_NAME,
    clear_registration_intent_cookie,
    clear_session_cookie,
    revoke_session,
    set_session_cookie,
)
from app.db.session import DbSession, mutating_transaction
from app.modules.attendees.service import verify_otp
from app.modules.auth.otp_delivery import deliver_otp
from app.modules.auth.schemas import (
    OtpRequestRequest,
    OtpRequestResponse,
    OtpVerifyRequest,
    OtpVerifyResponse,
)
from app.modules.auth.service import request_otp
from app.modules.points.service import maintenance_fast_path_check

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/otp/request", response_model=OtpRequestResponse)
def otp_request(payload: OtpRequestRequest, db: DbSession, ip: ClientIp) -> OtpRequestResponse:
    """Returning-attendee login step 1 (and activation re-send). An address with
    no account is told so (EMAIL_NOT_REGISTERED, owner's decision 10 Nov 2026), so
    the attendee goes to registration instead of waiting for a code. The per-email
    and per-IP limits apply first, either way."""
    email = payload.email.strip().lower()
    limiter.check([(OTP_REQUEST_PER_IP, ip), (OTP_REQUEST_PER_EMAIL, email)])
    maintenance_fast_path_check(db)

    with mutating_transaction(db) as tx:
        delivery = request_otp(tx, email, payload.language)

    if delivery is None:
        raise EmailNotRegisteredError("There is no account with this email yet. Register first.")
    deliver_otp(delivery)
    return OtpRequestResponse()


@router.post("/otp/verify", response_model=OtpVerifyResponse)
def otp_verify(
    payload: OtpVerifyRequest,
    response: Response,
    db: DbSession,
    ip: ClientIp,
    registration_intent: Annotated[
        str | None, Cookie(alias=REGISTRATION_INTENT_COOKIE_NAME, max_length=200)
    ] = None,
) -> OtpVerifyResponse:
    """Completes activation (PROVISIONAL, §8.1 Step 3) or login
    (ACTIVE, M05) — the server picks which from the attendee's state.
    A wrong code still commits its `attempt_count` increment (§8.2), so
    failure becomes the client-visible OTP_INVALID only AFTER the
    transaction has committed."""
    email = payload.email.strip().lower()
    limiter.check([(OTP_VERIFY_PER_IP, ip), (OTP_VERIFY_PER_EMAIL, email)])
    maintenance_fast_path_check(db)

    with mutating_transaction(db) as tx:
        outcome = verify_otp(
            tx,
            email=email,
            code=payload.code,
            remember_device=payload.remember_device,
            registration_intent=registration_intent,
        )

    if not outcome.ok:
        raise OtpInvalidError(
            "This code is invalid, expired, already used, or has too many failed attempts."
        )

    assert outcome.session_token is not None  # ok=True always carries one
    set_session_cookie(response, outcome.session_token, remember_device=payload.remember_device)
    if registration_intent is not None:
        clear_registration_intent_cookie(response)
    return OtpVerifyResponse()


@router.post("/logout", status_code=204)
def logout(response: Response, db: DbSession, token: SessionToken) -> None:
    """§8.3: "Logout revokes server-side (revoked_at), so the token is
    dead immediately." Idempotent: with no live session there is
    nothing to revoke, and the cookie is cleared either way. The token
    is taken from the cookie before the fence (extraction is pure work,
    §19.4); the session row is only read and revoked inside it."""
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        revoke_session(tx, token)
    clear_session_cookie(response)
