"""`POST /api/attendees/register` (§8.1), `GET /api/attendees/me` (M05),
`GET /api/attendees/me/profile` (M08), and the attendee's own profile edits:
`PUT /me/profile`, `PUT /me/sponsor-consent`, `PUT`/`DELETE /me/photo`
(see `profile.py` and `photo.py`)."""

from __future__ import annotations

import anyio
from fastapi import APIRouter, Request, Response

from app.core.config import get_settings
from app.core.deps import ClientIp, CurrentAttendee, SessionToken
from app.core.errors import (
    PhotoBusyError,
    PhotoInvalidError,
    PhotoTooLargeError,
    UnauthenticatedError,
)
from app.core.ratelimit import (
    OTP_REQUEST_PER_EMAIL,
    PHOTO_UPLOAD_PER_IP,
    REGISTER_PER_IP,
    limiter,
)
from app.core.sessions import generate_registration_intent, set_registration_intent_cookie
from app.db.session import DbSession, mutating_transaction
from app.modules.attendees.photo import PhotoOutcome, remove_photo, replace_photo
from app.modules.attendees.profile import set_sponsor_consent, update_profile
from app.modules.attendees.schemas import (
    AttendeeMe,
    AttendeeProfile,
    ConsentState,
    ConsentUpdate,
    PhotoResponse,
    ProfileUpdate,
    RegisterRequest,
    RegisterResponse,
)
from app.modules.attendees.service import (
    attendee_profile,
    hash_registration_intent,
    register_attendee,
)
from app.modules.auth.otp_delivery import deliver_otp
from app.modules.points.service import maintenance_fast_path_check
from app.modules.realtime.service import publish_profile_signals

router = APIRouter(prefix="/attendees", tags=["attendees"])


@router.post("/register", response_model=RegisterResponse)
def register(
    payload: RegisterRequest, response: Response, db: DbSession, ip: ClientIp
) -> RegisterResponse:
    """Returns no session — ARCHITECTURE.md §8.1: "It returns no
    session. The response says only that a code has been sent." Same
    response body (and the same intent cookie) regardless of which of the
    three duplicate-email branches ran (§8.1 Step 2).

    Every branch issues a code to an existing or new address, so the
    per-email issuance limit is shared with POST /auth/otp/request (§8.2:
    "per-email and per-IP rate limit on issuance"); the shared-IP ceiling
    is counted first so a client over it spends no one's email bucket."""
    email = payload.email.strip().lower()
    limiter.check([(REGISTER_PER_IP, ip), (OTP_REQUEST_PER_EMAIL, email)])
    maintenance_fast_path_check(db)

    intent = generate_registration_intent()
    with mutating_transaction(db) as tx:
        delivery = register_attendee(tx, payload, hash_registration_intent(intent))

    # Delivered only AFTER the transaction above has committed — never
    # signal a code that might not have actually persisted.
    if delivery is not None:
        deliver_otp(delivery)

    set_registration_intent_cookie(response, intent)
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


@router.get("/me/profile", response_model=AttendeeProfile)
def my_profile(attendee: CurrentAttendee, db: DbSession) -> AttendeeProfile:
    """M08's Profile surface: "registration data, selected interests,
    consent state (private view only)". Same session-only identity rule
    as `/me` — read-only, no reset fence (§19.4)."""
    return attendee_profile(db, attendee)


@router.put("/me/profile", response_model=AttendeeProfile)
def update_my_profile(
    payload: ProfileUpdate, token: SessionToken, db: DbSession
) -> AttendeeProfile:
    """The attendee's own details and interests. The edited person is the
    session's (no id in the request) and e-mail is not editable. A stale
    `expected_version` is a 409 PROFILE_CONFLICT carrying the current profile;
    an edit that changes nothing writes nothing."""
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        outcome = update_profile(tx, raw_session_token=token, payload=payload)
    if outcome.changed:
        publish_profile_signals(outcome.user_id, leaderboard=outcome.details_changed)
    return outcome.profile


@router.put("/me/sponsor-consent", response_model=ConsentState)
def update_my_sponsor_consent(
    payload: ConsentUpdate, token: SessionToken, db: DbSession
) -> ConsentState:
    """Grant or withdraw sponsor consent, separately from the profile form. The
    value, its audit row and its timestamp are one transaction; repeating the
    current value writes nothing (`changed: false`)."""
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        outcome = set_sponsor_consent(tx, raw_session_token=token, granted=payload.granted)
    if outcome.state.changed:
        publish_profile_signals(outcome.user_id, leaderboard=False)
    return outcome.state


# Photo uploads get their OWN worker-thread limiter instead of the process-wide
# default one that every sync endpoint, the per-request middleware and the live
# streams' session checks share. Waiting for a decode slot then costs an idle
# coroutine, not a shared thread: a burst of uploads cannot stall the rest of the
# API. Created on first use because a CapacityLimiter needs a running event loop.
_PHOTO_THREADS = 2
_PHOTO_MAX_WAITING = 8
_PHOTO_WAIT_SECONDS = 30.0
_photo_limiters: tuple[anyio.CapacityLimiter, anyio.CapacityLimiter] | None = None


def _photo_threads() -> tuple[anyio.CapacityLimiter, anyio.CapacityLimiter]:
    """(admission gate, thread runner). The gate is acquired with a timeout in
    the coroutine; the runner is the thread pool the decode then runs in. Two
    limiters because a thread already running cannot be abandoned midway: only
    the wait for a slot is cancellable, and a finished upload is never reported
    as busy."""
    global _photo_limiters
    if _photo_limiters is None:
        _photo_limiters = (
            anyio.CapacityLimiter(_PHOTO_THREADS),
            anyio.CapacityLimiter(_PHOTO_THREADS),
        )
    return _photo_limiters


def _upload_photo(db: DbSession, token: str, data: bytes) -> PhotoOutcome:
    maintenance_fast_path_check(db)
    return replace_photo(db, raw_session_token=token, data=data)


@router.put("/me/photo", response_model=PhotoResponse)
async def upload_my_photo(
    request: Request, token: SessionToken, db: DbSession, ip: ClientIp
) -> PhotoResponse:
    """The raw image as the request body (no multipart parser, no filename, no
    declared type to trust). Everything cheap is checked BEFORE the body is read
    and the body is read with a hard byte cap; decoding then runs in a worker
    thread, so the event loop (live score streams) is never blocked by an image."""
    limiter.check([(PHOTO_UPLOAD_PER_IP, ip)])
    if not token:
        raise UnauthenticatedError("Please sign in.")
    settings = get_settings()
    limit = settings.media_max_upload_bytes

    def too_large() -> PhotoTooLargeError:
        return PhotoTooLargeError(max_bytes=limit, max_pixels=settings.media_max_pixels)

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise too_large()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise too_large()
        chunks.append(chunk)
    if total == 0:
        raise PhotoInvalidError("empty")

    gate, runner = _photo_threads()
    if gate.statistics().tasks_waiting >= _PHOTO_MAX_WAITING:
        raise PhotoBusyError()
    body = b"".join(chunks)
    del chunks
    admitted = False
    with anyio.move_on_after(_PHOTO_WAIT_SECONDS):
        await gate.acquire()
        admitted = True
    if not admitted:
        raise PhotoBusyError()
    try:
        outcome = await anyio.to_thread.run_sync(_upload_photo, db, token, body, limiter=runner)
    finally:
        gate.release()
    publish_profile_signals(outcome.user_id, leaderboard=True)
    return PhotoResponse(photo=outcome.photo)


@router.delete("/me/photo", response_model=PhotoResponse)
def delete_my_photo(token: SessionToken, db: DbSession) -> PhotoResponse:
    maintenance_fast_path_check(db)
    outcome = remove_photo(db, raw_session_token=token)
    if outcome.changed:
        publish_profile_signals(outcome.user_id, leaderboard=True)
    return PhotoResponse(photo=None)
