"""Uploading and removing the attendee's own profile photo.

The order of events is what keeps the database and the disk consistent, and it
is the same for every outcome:

1. **Authenticate, inside the reset fence** (a short transaction that writes
   nothing) and count the upload against the attendee's rate limit. An
   unauthenticated request never reaches the decoder.
2. **Decode and re-encode** (`app.core.images`) with no database work and no
   fence held: a slow image never blocks a reset or holds a connection.
3. **Write the new files** under a fresh random key. Nothing references them
   yet, so a crash here leaves harmless orphans (the janitor's).
4. **Swap the key in one transaction**: the attendee row is locked and
   re-authenticated, `photo_key` is pointed at the new key, commit.
   If this fails for any reason the NEW files are removed and the old photo is
   still the attendee's photo.
5. **Only after the commit**, the OLD key's files are removed. The key is unique
   to its owner (a unique index), and it was just replaced, so this can never
   touch another attendee's file; if the removal fails the janitor takes it later.

Removing a photo is steps 4 and 5 without the new key.

This is the one module in `attendees` that opens its own transactions: a
photo change cannot be one transaction (it must not hold the fence or a
connection while it decodes an image), so it is deliberately two short ones.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    PhotoBusyError,
    PhotoInvalidError,
    PhotoStorageUnavailableError,
    PhotoTooLargeError,
)
from app.core.images import ImageRejected, ProcessedImage, process_avatar
from app.core.media import MediaStorageError, MediaStore, get_store, new_key
from app.core.media_guard import delete_photo_files
from app.core.ratelimit import PHOTO_UPLOAD_PER_ATTENDEE, limiter
from app.db.session import mutating_transaction
from app.modules.attendees.profile import lock_attendee
from app.modules.attendees.schemas import PhotoUrls
from app.modules.attendees.service import own_photo_urls


@dataclass(frozen=True)
class PhotoOutcome:
    photo: PhotoUrls | None
    user_id: int
    # False when there was nothing to remove: no signal is worth publishing.
    changed: bool


def decode_upload(data: bytes) -> ProcessedImage:
    """`ImageRejected` becomes the API's own errors; nothing from the file is
    ever put in a message."""
    settings = get_settings()
    try:
        return process_avatar(data, max_pixels=settings.media_max_pixels)
    except ImageRejected as rejected:
        if rejected.reason == "too_many_pixels":
            raise PhotoTooLargeError(
                max_bytes=settings.media_max_upload_bytes, max_pixels=settings.media_max_pixels
            ) from None
        if rejected.reason == "busy":
            raise PhotoBusyError() from None
        raise PhotoInvalidError(rejected.reason) from None


def replace_photo(
    db: Session, *, raw_session_token: str | None, data: bytes, store: MediaStore | None = None
) -> PhotoOutcome:
    store = store or get_store()

    with mutating_transaction(db) as tx:
        attendee = lock_attendee(tx, raw_session_token)
        limiter.check([(PHOTO_UPLOAD_PER_ATTENDEE, str(attendee.id))])
        user_id = attendee.id

    processed = decode_upload(data)

    key = new_key()
    try:
        store.write(key, processed.renditions)
    except MediaStorageError:
        raise PhotoStorageUnavailableError() from None

    try:
        with mutating_transaction(db) as tx:
            user = lock_attendee(tx, raw_session_token)
            old_key = user.photo_key
            user.photo_key = key
            tx.flush()
            photo = own_photo_urls(user.photo_key)
    except BaseException:
        store.delete(key)  # the new files are unreferenced; the old photo stands
        raise

    # After the commit, and unless a backup is running (then the old files stay for
    # the orphan janitor): app/core/media_guard.py explains why.
    delete_photo_files(store, old_key)
    return PhotoOutcome(photo=photo, user_id=user_id, changed=True)


def remove_photo(
    db: Session, *, raw_session_token: str | None, store: MediaStore | None = None
) -> PhotoOutcome:
    store = store or get_store()
    with mutating_transaction(db) as tx:
        user = lock_attendee(tx, raw_session_token)
        limiter.check([(PHOTO_UPLOAD_PER_ATTENDEE, str(user.id))])
        old_key = user.photo_key
        user_id = user.id
        if old_key is not None:
            user.photo_key = None
            tx.flush()
    delete_photo_files(store, old_key)
    return PhotoOutcome(photo=None, user_id=user_id, changed=old_key is not None)
