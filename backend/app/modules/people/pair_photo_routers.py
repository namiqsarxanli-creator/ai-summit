"""HTTP surface of pair photos (migration 0025).

Attendee (one's own photos only):
* POST /api/attendees/me/pair-photos?partner_id=&screen=  - the raw image as the body
* GET  /api/attendees/me/pair-photos
* POST /api/attendees/me/pair-photos/{id}/answer    - the partner accepts or declines
* POST /api/attendees/me/pair-photos/{id}/screen    - one's own big-screen consent
* POST /api/attendees/me/pair-photos/{id}/withdraw  - either person; files deleted

Public: GET /api/pair-photos/feed - consented by both AND approved; pictures only.
Admin (excluded from the production schema, audited): the moderation queue and
POST /api/admin/pair-photos/{id}/moderate.
"""

from __future__ import annotations

from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from app.core.config import get_settings
from app.core.deps import ClientIp, CurrentAdmin, CurrentAttendee, SessionToken
from app.core.errors import (
    PhotoBusyError,
    PhotoInvalidError,
    PhotoTooLargeError,
    UnauthenticatedError,
)
from app.core.ratelimit import PHOTO_UPLOAD_PER_IP, limiter
from app.db.session import DbSession, mutating_transaction
from app.modules.admin import service as admin
from app.modules.admin.routers import _read
from app.modules.attendees.profile import lock_attendee
from app.modules.attendees.routers import _PHOTO_MAX_WAITING, _PHOTO_WAIT_SECONDS, _photo_threads
from app.modules.people import pair_photos as service
from app.modules.points.service import maintenance_fast_path_check

router = APIRouter(tags=["people"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])
RowId = Annotated[int, Path(ge=1, le=2**53)]
AUDIT_MODERATE = "pair_photo.moderate"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Answer(Strict):
    accept: bool
    screen: bool = False


class Consent(Strict):
    consent: bool


class Moderation(Strict):
    approve: bool


@router.post("/attendees/me/pair-photos", status_code=201)
async def upload(
    request: Request,
    token: SessionToken,
    db: DbSession,
    ip: ClientIp,
    partner_id: Annotated[int, Query(ge=1, le=2**53)],
    screen: bool = False,
) -> dict[str, Any]:
    """Same limits and pipeline as the profile photo: a hard byte cap before the
    body is read, decoding in a bounded worker thread, re-encoded output only."""
    limiter.check([(PHOTO_UPLOAD_PER_IP, ip)])
    if not token:
        raise UnauthenticatedError("Please sign in.")
    settings = get_settings()
    cap = settings.media_max_upload_bytes

    def too_large() -> PhotoTooLargeError:
        return PhotoTooLargeError(max_bytes=cap, max_pixels=settings.media_max_pixels)

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > cap:
        raise too_large()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
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

    def run() -> dict[str, Any]:
        maintenance_fast_path_check(db)
        return service.upload(
            db, raw_session_token=token, partner_id=partner_id, screen=screen, data=body
        )

    try:
        return await anyio.to_thread.run_sync(run, limiter=runner)
    finally:
        gate.release()


@router.get("/attendees/me/pair-photos")
def mine(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"photos": service.mine(db, user)}


@router.post("/attendees/me/pair-photos/{photo_id}/answer")
def answer(photo_id: RowId, payload: Answer, token: SessionToken, db: DbSession) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        user = lock_attendee(tx, token)
        result, gone = service.answer(tx, user, photo_id, payload.accept, payload.screen)
    service.after_commit_delete(gone)
    return result


@router.post("/attendees/me/pair-photos/{photo_id}/screen")
def screen(photo_id: RowId, payload: Consent, token: SessionToken, db: DbSession) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        user = lock_attendee(tx, token)
        return service.set_screen(tx, user, photo_id, payload.consent)


@router.post("/attendees/me/pair-photos/{photo_id}/withdraw")
def withdraw(photo_id: RowId, token: SessionToken, db: DbSession) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        user = lock_attendee(tx, token)
        result, gone = service.withdraw(tx, user, photo_id)
    service.after_commit_delete(gone)
    return result


@router.get("/pair-photos/feed")
def feed(db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return {"photos": service.feed(db)}


@admin_router.get("/pair-photos")
def queue(admin_user: CurrentAdmin, db: DbSession) -> list[dict[str, Any]]:
    _read(admin_user)
    return service.moderation_queue(db)


@admin_router.post("/pair-photos/{photo_id}/moderate")
def moderate(
    photo_id: RowId, payload: Moderation, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    def run(session: Any, actor: Any) -> admin.AdminResult:
        out = service.moderate(session, actor, photo_id, payload.approve)
        return AUDIT_MODERATE, "pair_photo", None, out, out

    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        return admin.execute(tx, token, run)
