"""Pair photos (see photo_models.py for the rules).

The upload follows the profile photo's order of events (attendees/photo.py):
authenticate and check the pair inside a short fenced transaction, decode and
re-encode with nothing held, write the files under a fresh key, then insert the
row in a second short transaction - removing the new files if that fails.
Withdrawing clears the key in the database first and deletes the files after
the commit (unless a backup holds the media lock; then the janitor does it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import DomainError, PhotoStorageUnavailableError
from app.core.images import LARGE, SMALL
from app.core.media import MediaStorageError, MediaStore, get_store, new_key
from app.core.media_guard import delete_photo_files
from app.core.ratelimit import PHOTO_UPLOAD_PER_ATTENDEE, limiter
from app.db.session import mutating_transaction
from app.modules.attendees.models import User
from app.modules.attendees.photo import decode_upload
from app.modules.attendees.profile import lock_attendee
from app.modules.people.connections import connected
from app.modules.people.photo_models import PairPhoto
from app.modules.staff.models import StaffAccount

MAX_PER_PERSON = 20  # live (pending or accepted) photos one person can upload
SCREEN_FEED = 30


class PairPhotoError(DomainError):
    code = "PAIR_PHOTO_REFUSED"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.details = {"reason": reason}


class PairPhotoMissing(DomainError):
    code = "PAIR_PHOTO_NOT_FOUND"
    status_code = 404


def _now() -> datetime:
    return datetime.now(UTC)


def _url(key: str | None, size: int) -> str | None:
    # Never the public avatar route: pair photos are authorised on every request
    # (media/routers.py).
    return None if key is None else f"/api/media/pair/{key}-{size}.jpg"


def _short(user: User) -> str:
    return f"{user.first_name} {user.last_name[:1]}."


def view(row: PairPhoto, viewer_id: int, other: User) -> dict[str, Any]:
    mine = row.uploader_id == viewer_id
    return {
        "id": row.id,
        "status": row.status.lower(),
        "role": "uploader" if mine else "partner",
        "with": _short(other),
        "photo_url": _url(row.photo_key, LARGE),
        "thumb_url": _url(row.photo_key, SMALL),
        "my_screen_consent": (row.uploader_screen_at if mine else row.partner_screen_at)
        is not None,
        "their_screen_consent": (row.partner_screen_at if mine else row.uploader_screen_at)
        is not None,
        "moderation": row.moderation.lower(),
        "on_screen": on_screen(row),
    }


def on_screen(row: PairPhoto) -> bool:
    return (
        row.status == "ACCEPTED"
        and row.photo_key is not None
        and row.uploader_screen_at is not None
        and row.partner_screen_at is not None
        and row.moderation == "APPROVED"
    )


def upload(
    db: Session,
    *,
    raw_session_token: str | None,
    partner_id: int,
    screen: bool,
    data: bytes,
    store: MediaStore | None = None,
) -> dict[str, Any]:
    store = store or get_store()
    with mutating_transaction(db) as tx:
        user = lock_attendee(tx, raw_session_token)
        limiter.check([(PHOTO_UPLOAD_PER_ATTENDEE, str(user.id))])
        _check_pair(tx, user.id, partner_id)
    processed = decode_upload(data)
    key = new_key()
    try:
        store.write(key, processed.renditions)
    except MediaStorageError:
        raise PhotoStorageUnavailableError() from None
    try:
        with mutating_transaction(db) as tx:
            user = lock_attendee(tx, raw_session_token)
            _check_pair(tx, user.id, partner_id)  # still connected, still allowed
            stamp = _now()
            row = PairPhoto(
                uploader_id=user.id,
                partner_id=partner_id,
                photo_key=key,
                uploader_screen_at=stamp if screen else None,
            )
            tx.add(row)
            tx.flush()
            partner = tx.get(User, partner_id)
            assert partner is not None
            return view(row, user.id, partner)
    except BaseException:
        store.delete(key)  # unreferenced: nothing else can point at a fresh key
        raise


def _check_pair(db: Session, uid: int, partner_id: int) -> None:
    if uid == partner_id:
        raise PairPhotoError("self")
    if not connected(db, uid, partner_id):
        raise PairPhotoError("not_connected")
    live = db.execute(
        select(PairPhoto.id).where(
            PairPhoto.uploader_id == uid, PairPhoto.status.in_(("PENDING", "ACCEPTED"))
        )
    ).all()
    if len(live) >= MAX_PER_PERSON:
        raise PairPhotoError("too_many")


def _own(db: Session, uid: int, photo_id: int) -> PairPhoto:
    row = db.execute(
        select(PairPhoto).where(PairPhoto.id == photo_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or uid not in (row.uploader_id, row.partner_id):
        raise PairPhotoMissing("No such photo.")
    return row


def _other(db: Session, row: PairPhoto, uid: int) -> User:
    other = db.get(User, row.partner_id if row.uploader_id == uid else row.uploader_id)
    assert other is not None
    return other


def answer(
    db: Session, user: User, photo_id: int, accept: bool, screen: bool
) -> tuple[dict[str, Any], str | None]:
    """The partner accepts (optionally consenting to the screen) or declines.
    Returns the view and a key whose files must be deleted after the commit."""
    row = _own(db, user.id, photo_id)
    if row.partner_id != user.id:
        raise PairPhotoMissing("No such photo.")
    gone: str | None = None
    if row.status == "PENDING":
        if accept:
            row.status = "ACCEPTED"
            row.accepted_at = _now()
            row.partner_screen_at = _now() if screen else None
        else:
            row.status = "DECLINED"
            gone, row.photo_key = row.photo_key, None
    elif row.status not in ("ACCEPTED", "DECLINED") or (row.status == "ACCEPTED") != accept:
        raise PairPhotoError("already_answered")
    db.flush()
    return view(row, user.id, _other(db, row, user.id)), gone


def set_screen(db: Session, user: User, photo_id: int, consent: bool) -> dict[str, Any]:
    """Each person's own big-screen consent; withdrawing takes it off at once."""
    row = _own(db, user.id, photo_id)
    if row.status not in ("PENDING", "ACCEPTED"):
        raise PairPhotoError("withdrawn")
    stamp = _now() if consent else None
    if row.uploader_id == user.id:
        row.uploader_screen_at = row.uploader_screen_at or stamp if consent else None
    else:
        if row.status != "ACCEPTED":
            raise PairPhotoError("not_accepted")
        row.partner_screen_at = row.partner_screen_at or stamp if consent else None
    db.flush()
    return view(row, user.id, _other(db, row, user.id))


def withdraw(db: Session, user: User, photo_id: int) -> tuple[dict[str, Any], str | None]:
    row = _own(db, user.id, photo_id)
    gone: str | None = None
    if row.status != "WITHDRAWN":
        gone, row.photo_key = row.photo_key, None
        row.status = "WITHDRAWN"
        row.withdrawn_at = _now()
        row.withdrawn_by_id = user.id
        row.uploader_screen_at = None
        row.partner_screen_at = None
        db.flush()
    return view(row, user.id, _other(db, row, user.id)), gone


def after_commit_delete(key: str | None, store: MediaStore | None = None) -> None:
    delete_photo_files(store or get_store(), key)


def mine(db: Session, user: User) -> list[dict[str, Any]]:
    rows = db.execute(
        select(PairPhoto)
        .where(
            or_(PairPhoto.uploader_id == user.id, PairPhoto.partner_id == user.id),
            PairPhoto.status.in_(("PENDING", "ACCEPTED")),
        )
        .order_by(PairPhoto.created_at.desc(), PairPhoto.id.desc())
    ).scalars()
    return [view(r, user.id, _other(db, r, user.id)) for r in rows]


def feed(db: Session) -> list[dict[str, Any]]:
    """The big screen: only photos both people consented to AND a moderator
    approved. No names, no ids - just the picture."""
    rows = db.execute(
        select(PairPhoto)
        .where(
            PairPhoto.status == "ACCEPTED",
            PairPhoto.moderation == "APPROVED",
            PairPhoto.photo_key.is_not(None),
            PairPhoto.uploader_screen_at.is_not(None),
            PairPhoto.partner_screen_at.is_not(None),
        )
        .order_by(PairPhoto.moderated_at.desc(), PairPhoto.id.desc())
        .limit(SCREEN_FEED)
    ).scalars()
    return [{"photo_url": _url(r.photo_key, LARGE)} for r in rows]


def moderation_queue(db: Session) -> list[dict[str, Any]]:
    """Accepted photos with both screen consents, waiting for a moderator."""
    rows = db.execute(
        select(PairPhoto)
        .where(
            PairPhoto.status == "ACCEPTED",
            PairPhoto.photo_key.is_not(None),
            PairPhoto.uploader_screen_at.is_not(None),
            PairPhoto.partner_screen_at.is_not(None),
        )
        .order_by(PairPhoto.created_at, PairPhoto.id)
    ).scalars()
    return [
        {"id": r.id, "photo_url": _url(r.photo_key, LARGE), "moderation": r.moderation.lower()}
        for r in rows
    ]


def moderate(db: Session, staff: StaffAccount, photo_id: int, approve: bool) -> dict[str, Any]:
    row = db.execute(
        select(PairPhoto).where(PairPhoto.id == photo_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise PairPhotoMissing("No such photo.")
    row.moderation = "APPROVED" if approve else "REJECTED"
    row.moderated_at = _now()
    row.moderated_by_staff_id = staff.id
    db.flush()
    return {"id": row.id, "moderation": row.moderation.lower(), "on_screen": on_screen(row)}


def referenced_keys(db: Session) -> set[str]:
    """For the media janitor: pair photo files still in use."""
    return {
        k
        for k in db.execute(select(PairPhoto.photo_key).where(PairPhoto.photo_key.is_not(None)))
        .scalars()
        .all()
        if k is not None
    }
