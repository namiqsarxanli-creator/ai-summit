"""Photo bytes: public profile avatars, and revocable pair photos.

`GET /api/media/avatars/{name}` - profile photos. What makes this safe to serve
without authentication (a leaderboard row and a public card show the photo to
people who are not signed in):

* the only thing a request chooses is `name`, and `MediaStore.path_for_file`
  accepts exactly `<32 lowercase hex>-<128|384>.jpg` - anything else, a
  traversal attempt included, is a 404 before the file system is touched, so no
  other path can be named;
* the key must be someone's CURRENT `users.photo_key`. The media volume also
  holds pair photos (same naming) and files whose deletion a running backup has
  deferred; neither is ever a public avatar, whatever URL was handed out before;
* the bytes are the ones `app.core.images` produced (a re-encoded JPEG with no
  metadata), served as `image/jpeg` with `nosniff`;
* the key is 128 random bits and changes with every new photo, so the URL is
  immutable: it is cached for a year, and a new photo is simply a new URL.

`GET /api/media/pair/{name}` - pair photos (migration 0025). These are
revocable, so EVERY request is authorised against the row as it is now: the two
people of the photo, an administrator (moderation), or - for the big screen -
anyone while the photo is accepted, both people's screen consent is given and a
moderator approved it. Nothing else, including a withdrawn photo whose files a
backup is still holding. `private, no-store` and no validator: a revoked photo
is not answered from a cache or with a 304. Copies a viewer already saved
cannot be recalled; that is said in the product, not pretended here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select

from app.core.deps import SessionToken
from app.core.errors import DomainError, MediaNotFoundError
from app.core.media import get_store
from app.core.sessions import resolve_attendee_session, resolve_staff_session
from app.db.session import DbSession
from app.modules.attendees.models import User
from app.modules.people.pair_photos import on_screen
from app.modules.people.photo_models import PairPhoto
from app.modules.staff.models import StaffRole

router = APIRouter(prefix="/media", tags=["media"])

AVATAR_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    "Content-Disposition": "inline",
}
PAIR_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "X-Content-Type-Options": "nosniff",
    "Content-Disposition": "inline",
    "Vary": "Cookie",
}


@router.get("/avatars/{name}", response_class=FileResponse)
def avatar(name: str, request: Request, db: DbSession) -> Response:
    path = get_store().path_for_file(name)
    if path is None:
        raise MediaNotFoundError()
    key = path.stem.rsplit("-", 1)[0]
    # Checked BEFORE the 304 below: a revalidation is an access too.
    if db.execute(select(User.id).where(User.photo_key == key).limit(1)).first() is None:
        raise MediaNotFoundError()
    if not path.is_file():
        raise MediaNotFoundError()
    # The name IS the identity of the bytes (a new photo is a new name), so it is
    # also the validator: a revalidation is answered without reading the file.
    etag = f'"{path.stem}"'
    headers = {**AVATAR_HEADERS, "ETag": etag}
    if etag in (v.strip() for v in request.headers.get("if-none-match", "").split(",")):
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/jpeg", headers=headers)


def _may_view(db: DbSession, row: PairPhoto, session: str | None) -> bool:
    if on_screen(row):
        return True
    if not session:
        return False
    try:
        viewer = resolve_attendee_session(db, session)
        if viewer.id in (row.uploader_id, row.partner_id):
            return True
    except DomainError:
        pass
    try:
        return resolve_staff_session(db, session).role == StaffRole.ADMIN
    except DomainError:
        return False


@router.get("/pair/{name}", response_class=FileResponse)
def pair_photo(name: str, db: DbSession, session: SessionToken) -> Response:
    path = get_store().path_for_file(name)
    if path is None:
        raise MediaNotFoundError()
    key = path.stem.rsplit("-", 1)[0]
    row = db.execute(
        select(PairPhoto).where(
            PairPhoto.photo_key == key, PairPhoto.status.in_(("PENDING", "ACCEPTED"))
        )
    ).scalar_one_or_none()
    # One answer for "no such photo" and "not yours": nothing to learn from it.
    if row is None or not _may_view(db, row, session) or not path.is_file():
        raise MediaNotFoundError()
    return FileResponse(path, media_type="image/jpeg", headers=PAIR_HEADERS)
