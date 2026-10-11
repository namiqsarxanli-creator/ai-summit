"""`GET /api/media/avatars/{name}` - the profile photos, and nothing else.

What makes this safe to serve without authentication (a leaderboard row and a
public card show the photo to people who are not signed in):

* the only thing a request chooses is `name`, and `MediaStore.path_for_file`
  accepts exactly `<32 lowercase hex>-<128|384>.jpg` - anything else, a
  traversal attempt included, is a 404 before the file system is touched, so no
  other path can be named;
* the bytes are the ones `app.core.images` produced (a re-encoded JPEG with no
  metadata), served as `image/jpeg` with `nosniff`;
* the key is 128 random bits and changes with every new photo, so the URL is
  immutable: it is cached for a year, and a new photo is simply a new URL.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from app.core.errors import MediaNotFoundError
from app.core.media import get_store

router = APIRouter(prefix="/media", tags=["media"])

AVATAR_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "X-Content-Type-Options": "nosniff",
    "Content-Disposition": "inline",
}


@router.get("/avatars/{name}", response_class=FileResponse)
def avatar(name: str, request: Request) -> Response:
    path = get_store().path_for_file(name)
    if path is None or not path.is_file():
        raise MediaNotFoundError()
    # The name IS the identity of the bytes (a new photo is a new name), so it is
    # also the validator: a revalidation is answered without reading the file.
    etag = f'"{path.stem}"'
    headers = {**AVATAR_HEADERS, "ETag": etag}
    if etag in (v.strip() for v in request.headers.get("if-none-match", "").split(",")):
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/jpeg", headers=headers)
