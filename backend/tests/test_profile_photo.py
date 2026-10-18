"""Profile photo: upload, replace, remove, serve - and what the public sees.

Real PostgreSQL and a throw-away MEDIA_ROOT. Pinned here:

* the upload is checked by CONTENT, bounded in bytes and pixels, re-encoded and
  stored under a server-made random name; nothing the client named survives;
* the database points at a photo only once its files exist; if the commit fails
  the new files are removed and the old photo stands; the old photo's files are
  removed only after a successful commit and only that key's own;
* the media route serves only server-made names, immutably cacheable;
* the leaderboard and the public card carry a photo URL and nothing private,
  and doing so costs no extra queries.
"""

from __future__ import annotations

import io
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import event

from app.core.config import get_settings
from app.core.ratelimit import PHOTO_UPLOAD_PER_ATTENDEE
from app.db.session import engine
from tests.m04_support import (
    assert_fenced_before_any_read,
    cleanup_email,
    execute,
    fetch_one,
    make_client,
)
from tests.profile_support import (
    PHOTO,
    PROFILE,
    attendee_fixture,  # noqa: F401 - fixture
    emails_fixture,  # noqa: F401 - fixture
    error_of,
    jpeg_bytes,
    media_root_fixture,  # noqa: F401 - fixture
    png_bytes,
)

Attendee = Callable[..., tuple[str, TestClient]]
Emails = Callable[[], str]

HEADERS = {"Content-Type": "application/octet-stream"}


def _upload(client: TestClient, data: bytes, **headers: str) -> Any:
    return client.put(PHOTO, content=data, headers={**HEADERS, **headers})


def _key(email: str) -> str | None:
    row = fetch_one("SELECT photo_key FROM users WHERE email = :e", e=email)
    return None if row is None else row[0]


def _files(root: Path) -> set[str]:
    avatars = root / "avatars"
    return {p.name for p in avatars.iterdir()} if avatars.exists() else set()


def _key_of(url: str) -> str:
    return url.rsplit("/", 1)[1].split("-")[0]


# --- upload --------------------------------------------------------------------------------------


def test_uploading_a_photo_stores_re_encoded_renditions_under_a_random_name(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    email, client = attendee(emails())
    original = png_bytes((500, 420))
    response = _upload(client, original)
    assert response.status_code == 200, response.text
    photo = response.json()["photo"]

    key = _key(email)
    assert key is not None and len(key) == 32
    assert photo == {
        "small": f"/api/media/avatars/{key}-128.jpg",
        "large": f"/api/media/avatars/{key}-384.jpg",
    }
    assert _files(media_root) == {f"{key}-128.jpg", f"{key}-384.jpg"}
    # what is on disk is a JPEG the server made, not the upload
    stored = (media_root / "avatars" / f"{key}-384.jpg").read_bytes()
    assert stored != original and stored[:3] == b"\xff\xd8\xff"
    assert Image.open(io.BytesIO(stored)).size == (384, 384)
    # the profile screen reports the same
    assert client.get(PROFILE).json()["photo"] == photo


def test_the_file_name_the_client_sends_plays_no_part(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    email, client = attendee(emails())
    response = _upload(
        client,
        png_bytes(),
        **{
            "Content-Type": "image/svg+xml",
            "Content-Disposition": 'attachment; filename="../../etc/evil.svg"',
            "X-Filename": "evil.php",
        },
    )
    assert response.status_code == 200  # the CONTENT is a valid PNG; the labels are ignored
    assert all(
        name.startswith(_key(email) or "?") and name.endswith(".jpg") for name in _files(media_root)
    )
    assert "evil" not in " ".join(_files(media_root))


@pytest.mark.parametrize(
    "data",
    [
        b"just some text",
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00;",
        b"\x89PNG\r\n\x1a\n" + b"broken" * 30,
    ],
)
def test_a_file_that_is_not_a_supported_photo_is_refused_and_changes_nothing(
    emails: Emails, attendee: Attendee, media_root: Path, data: bytes
) -> None:
    email, client = attendee(emails())
    # a .png-named, image/png-typed upload of non-image bytes
    response = _upload(client, data, **{"Content-Type": "image/png"})
    assert error_of(response) == (400, "PHOTO_INVALID")
    assert response.json()["error"]["details"]["reason"] in {"unsupported", "corrupt"}
    assert _key(email) is None
    assert _files(media_root) == set()


def test_an_empty_body_is_refused(emails: Emails, attendee: Attendee, media_root: Path) -> None:
    _, client = attendee(emails())
    assert error_of(_upload(client, b"")) == (400, "PHOTO_INVALID")


def test_an_animated_or_tiny_photo_is_refused(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, client = attendee(emails())
    frames = [Image.new("RGB", (120, 120), (i * 50, 0, 0)) for i in range(3)]
    animated = io.BytesIO()
    frames[0].save(animated, "WEBP", save_all=True, append_images=frames[1:], duration=50)
    assert _upload(client, animated.getvalue()).json()["error"]["details"]["reason"] == "animated"
    assert _upload(client, png_bytes((20, 20))).json()["error"]["details"]["reason"] == "too_small"
    assert _files(media_root) == set()


def test_an_upload_over_the_byte_limit_is_refused_before_it_is_decoded(
    emails: Emails,
    attendee: Attendee,
    media_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email, client = attendee(emails())
    monkeypatch.setenv("MEDIA_MAX_UPLOAD_BYTES", "100000")
    get_settings.cache_clear()
    big = b"\xff\xd8\xff" + b"\x00" * 150_000

    response = _upload(client, big)
    assert error_of(response) == (413, "PHOTO_TOO_LARGE")
    assert response.json()["error"]["details"]["max_bytes"] == 100_000
    assert _key(email) is None and _files(media_root) == set()

    # ... also when the client does not say how large it is (chunked)
    def chunks() -> Iterator[bytes]:
        for _ in range(15):
            yield b"\x00" * 10_000

    streamed = client.put(PHOTO, content=chunks(), headers=HEADERS)
    assert error_of(streamed) == (413, "PHOTO_TOO_LARGE")


def test_a_small_file_that_claims_a_huge_canvas_is_refused_by_pixels(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    bomb = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 50_000, 50_000, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )
    email, client = attendee(emails())
    response = _upload(client, bomb)
    assert error_of(response) == (413, "PHOTO_TOO_LARGE")
    assert response.json()["error"]["details"]["max_pixels"] == get_settings().media_max_pixels
    assert _key(email) is None and _files(media_root) == set()


def test_uploading_requires_a_session_and_decodes_nothing_without_one(
    media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[int] = []
    monkeypatch.setattr(
        "app.modules.attendees.photo.process_avatar",
        lambda *a, **k: called.append(1),
    )
    with make_client() as anonymous:
        assert error_of(_upload(anonymous, png_bytes())) == (401, "UNAUTHENTICATED")
        anonymous.cookies.set("session", "x" * 40)  # a made-up cookie
        assert error_of(_upload(anonymous, png_bytes())) == (401, "UNAUTHENTICATED")
    assert called == [] and _files(media_root) == set()


def test_uploads_are_rate_limited_per_attendee(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, client = attendee(emails())
    data = png_bytes((80, 80))
    codes = [_upload(client, data).status_code for _ in range(PHOTO_UPLOAD_PER_ATTENDEE.limit + 1)]
    assert codes[:-1] == [200] * PHOTO_UPLOAD_PER_ATTENDEE.limit
    assert codes[-1] == 429


def test_photo_changes_take_the_reset_fence_first(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, client = attendee(emails())
    assert assert_fenced_before_any_read(lambda: _upload(client, png_bytes())).status_code == 200
    assert assert_fenced_before_any_read(lambda: client.delete(PHOTO)).status_code == 200


# --- replace and remove --------------------------------------------------------------------------


def test_a_new_photo_replaces_the_old_one_and_the_old_files_go_after_the_commit(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    email, client = attendee(emails())
    first = _upload(client, png_bytes((300, 300))).json()["photo"]
    first_key = _key(email)
    second = _upload(client, jpeg_bytes((300, 300))).json()["photo"]
    second_key = _key(email)

    assert first_key != second_key
    assert first["small"] != second["small"]  # a new photo is a new URL: caches refresh
    assert _files(media_root) == {f"{second_key}-128.jpg", f"{second_key}-384.jpg"}


def test_replacing_a_photo_never_touches_another_attendees_files(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    mine_email, mine = attendee(emails())
    theirs_email, theirs = attendee(emails())
    _upload(mine, png_bytes())
    _upload(theirs, jpeg_bytes())
    their_key = _key(theirs_email)
    their_files = {f"{their_key}-128.jpg", f"{their_key}-384.jpg"}

    for _ in range(3):
        assert _upload(mine, png_bytes()).status_code == 200
    assert mine.delete(PHOTO).status_code == 200

    assert _files(media_root) == their_files  # theirs, untouched
    assert _key(mine_email) is None
    assert _key(theirs_email) == their_key
    assert theirs.get(PROFILE).json()["photo"]["small"].endswith(f"{their_key}-128.jpg")


def test_removing_the_photo_clears_the_row_and_the_files_and_is_repeatable(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    email, client = attendee(emails())
    _upload(client, png_bytes())
    removed = client.delete(PHOTO)
    assert removed.status_code == 200 and removed.json() == {"photo": None}
    assert _key(email) is None
    assert _files(media_root) == set()
    assert client.get(PROFILE).json()["photo"] is None
    again = client.delete(PHOTO)  # nothing to remove: still fine
    assert again.status_code == 200 and again.json() == {"photo": None}


def test_photo_changes_do_not_move_the_profile_version(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, client = attendee(emails())
    version = client.get(PROFILE).json()["version"]
    _upload(client, png_bytes())
    client.delete(PHOTO)
    assert client.get(PROFILE).json()["version"] == version


def test_a_removal_needs_a_session(media_root: Path) -> None:
    with make_client() as anonymous:
        assert error_of(anonymous.delete(PHOTO)) == (401, "UNAUTHENTICATED")


# --- failures keep the disk and the database consistent ------------------------------------------


def test_if_the_database_swap_fails_the_new_files_are_removed_and_the_old_photo_stands(
    emails: Emails, attendee: Attendee, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    email, client = attendee(emails())
    _upload(client, png_bytes())
    old_key = _key(email)
    old_files = _files(media_root)

    def explode(key: str | None) -> None:
        raise RuntimeError("database went away at the last moment")

    monkeypatch.setattr("app.modules.attendees.photo.own_photo_urls", explode)
    with make_client(raise_server_exceptions=False) as failing:
        failing.cookies.update(client.cookies)
        response = _upload(failing, jpeg_bytes())
    assert response.status_code == 500
    monkeypatch.undo()

    assert _key(email) == old_key  # the row never pointed at the new photo
    assert _files(media_root) == old_files  # the new files were cleaned up, the old stay
    assert client.get(PROFILE).json()["photo"]["small"].endswith(f"{old_key}-128.jpg")


def test_if_the_session_dies_while_the_image_is_processed_nothing_is_attached(
    emails: Emails, attendee: Attendee, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    email, client = attendee(emails())
    from app.modules.attendees import photo as photo_module

    real = photo_module.decode_upload

    def slow_decode(data: bytes) -> Any:
        result = real(data)
        execute(
            "UPDATE sessions SET revoked_at = now() "
            "WHERE user_id = (SELECT id FROM users WHERE email = :e)",
            e=email,
        )
        return result

    monkeypatch.setattr(photo_module, "decode_upload", slow_decode)
    response = _upload(client, png_bytes())
    assert error_of(response) == (401, "UNAUTHENTICATED")
    assert _key(email) is None
    assert _files(media_root) == set()  # re-authenticated at the swap: new files removed


def test_if_the_store_cannot_be_written_the_attendee_keeps_their_photo(
    emails: Emails,
    attendee: Attendee,
    media_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    email, client = attendee(emails())
    _upload(client, png_bytes())
    old_key = _key(email)

    blocker = tmp_path / "a-file-not-a-directory"
    blocker.write_text("x")
    monkeypatch.setenv("MEDIA_ROOT", str(blocker / "media"))
    get_settings.cache_clear()
    response = _upload(client, jpeg_bytes())
    assert error_of(response) == (503, "PHOTO_STORAGE_UNAVAILABLE")
    assert _key(email) == old_key


def test_a_failed_removal_of_the_old_files_does_not_fail_the_upload(
    emails: Emails, attendee: Attendee, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    email, client = attendee(emails())
    _upload(client, png_bytes())
    old_key = _key(email)

    real_unlink = Path.unlink

    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        if old_key and old_key in self.name:
            raise PermissionError("read-only")
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)
    assert _upload(client, jpeg_bytes()).status_code == 200  # the swap succeeded
    monkeypatch.undo()
    assert _key(email) != old_key
    # the leftover is an orphan: the janitor's, not an error
    assert {f"{old_key}-128.jpg", f"{old_key}-384.jpg"} <= _files(media_root)


# --- serving -------------------------------------------------------------------------------------


def test_the_photo_is_served_as_an_immutable_jpeg_without_a_session(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, client = attendee(emails())
    photo = _upload(client, png_bytes((400, 400))).json()["photo"]
    with make_client() as anonymous:  # the leaderboard shows it to people who are not signed in
        for url, side in ((photo["small"], 128), (photo["large"], 384)):
            response = anonymous.get(url)
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/jpeg"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "immutable" in response.headers["cache-control"]
            assert "max-age=31536000" in response.headers["cache-control"]
            assert Image.open(io.BytesIO(response.content)).size == (side, side)
            assert "set-cookie" not in response.headers
        # a conditional request is answered without the body
        etag = anonymous.get(photo["small"]).headers.get("etag")
        assert etag
        revalidated = anonymous.get(photo["small"], headers={"If-None-Match": etag})
        assert revalidated.status_code == 304
        assert "immutable" in revalidated.headers["cache-control"]  # a 304 keeps it cacheable
        assert revalidated.content == b""
        # a stale validator gets the bytes again
        assert anonymous.get(photo["small"], headers={"If-None-Match": '"nope"'}).status_code == 200


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "..%2f..%2fetc%2fpasswd",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "secret.txt",
        "0" * 32 + "-128.jpg",  # well-formed but nobody's
        "0" * 32 + "-999.jpg",
        "0" * 32 + "-128.png",
        "0" * 32 + "-0128.jpg",
        ("A" * 32) + "-128.jpg",
        "0" * 32 + "-128.jpg%00.png",
        ".tmp-" + "0" * 32 + "-128-abcd",
        "..",
    ],
)
def test_the_media_route_names_only_server_made_files(media_root: Path, name: str) -> None:
    # something that must never be reachable sits next to the avatars
    media_root.mkdir(parents=True, exist_ok=True)
    (media_root / "secret.txt").write_text("top secret")
    (media_root / "avatars").mkdir(exist_ok=True)
    (media_root / "avatars" / "secret.txt").write_text("top secret")
    with make_client() as anonymous:
        response = anonymous.get(f"/api/media/avatars/{name}")
    assert response.status_code in (404, 405)
    assert b"top secret" not in response.content
    assert b"root:" not in response.content


def test_other_methods_are_not_accepted_on_the_media_route(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, client = attendee(emails())
    url = _upload(client, png_bytes()).json()["photo"]["small"]
    for method in ("post", "put", "delete", "patch"):
        assert getattr(client, method)(url).status_code == 405


def test_a_replaced_photos_old_url_stops_resolving(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, client = attendee(emails())
    old = _upload(client, png_bytes()).json()["photo"]["small"]
    _upload(client, jpeg_bytes())
    assert error_of(client.get(old)) == (404, "MEDIA_NOT_FOUND")


# --- the public side: leaderboard, cards, privacy ------------------------------------------------


def _token_of(client: TestClient) -> str:
    return str(client.get("/api/attendees/me/card").json()["card_url"]).rsplit("/c/", 1)[1]


PRIVATE_KEYS = {
    "phone",
    "email",
    "sponsor_consent",
    "sponsor_consent_at",
    "sponsor_consent_version",
    "photo_key",
    "version",
    "profile_version",
    "linkedin_url",
}


def test_the_public_card_carries_the_large_photo_and_nothing_private(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    email, client = attendee(emails(), sponsor_consent=True)
    photo = _upload(client, png_bytes()).json()["photo"]
    with make_client() as anonymous:
        card = anonymous.get(f"/api/cards/{_token_of(client)}")
    assert card.status_code == 200
    body = card.json()
    assert body["photo_url"] == photo["large"]
    assert set(body) == {
        "first_name",
        "last_name",
        "profession",
        "interests",
        "contact",
        "photo_url",
    }
    assert not (PRIVATE_KEYS & set(body))
    assert "photo_key" not in card.text
    # Migration 0021 contact gate: an anonymous viewer gets no contact at all.
    assert body["contact"] is None
    assert email not in card.text


def test_a_card_without_a_photo_says_so(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, client = attendee(emails())
    with make_client() as anonymous:
        assert anonymous.get(f"/api/cards/{_token_of(client)}").json()["photo_url"] is None
    assert client.get("/api/attendees/me/card").json()["card"]["photo_url"] is None


def test_my_own_card_shows_my_photo(emails: Emails, attendee: Attendee, media_root: Path) -> None:
    _, client = attendee(emails())
    photo = _upload(client, png_bytes()).json()["photo"]
    assert client.get("/api/attendees/me/card").json()["card"]["photo_url"] == photo["large"]


def test_the_leaderboard_shows_the_small_photo_or_none_and_nothing_private(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, with_photo = attendee(emails(), first_name="Photo", last_name="Nasir")
    _, without = attendee(emails(), first_name="Plain", last_name="Mammadov")
    photo = _upload(with_photo, png_bytes()).json()["photo"]

    board = without.get("/api/leaderboard?view=all&limit=100")
    assert board.status_code == 200, board.text
    entries = {e["name"]: e for e in board.json()["entries"]}
    assert entries["Photo N."]["photo_url"] == photo["small"]
    assert entries["Plain M."]["photo_url"] is None
    for entry in board.json()["entries"]:
        assert not ((PRIVATE_KEYS | {"first_name", "last_name"}) & set(entry)), entry

    top = without.get("/api/leaderboard").json()  # the default view carries `current_user`
    assert top["current_user"]["name"] == "Plain M."
    assert top["current_user"]["photo_url"] is None
    assert not (PRIVATE_KEYS & set(top["current_user"]))
    assert {e["name"]: e["photo_url"] for e in top["entries"]}.get("Photo N.") in (
        photo["small"],
        None,  # (outside the top N, if the test database holds many attendees)
    )


def test_the_leaderboard_follows_a_removed_photo(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, client = attendee(emails(), first_name="Fleeting")
    _upload(client, png_bytes())
    client.delete(PHOTO)
    me = client.get("/api/leaderboard").json()["current_user"]
    assert me["photo_url"] is None


# --- no N+1 --------------------------------------------------------------------------------------


@pytest.fixture
def crowd() -> Iterator[Callable[[int], None]]:
    """Adds ACTIVE attendees that each have a photo (a row is enough: only the
    key is read by the leaderboard)."""
    made: list[str] = []

    def add(count: int) -> None:
        for _ in range(count):
            email = f"crowd-{uuid.uuid4().hex[:12]}@test.facemark.demo"
            made.append(email)
            execute(
                "INSERT INTO users (first_name, last_name, email, phone, profession, "
                "sponsor_consent, status, activated_at, photo_key) VALUES ('Crowd', 'Member', "
                ":e, '+1', 'Eng', false, 'ACTIVE', now(), :k)",
                e=email,
                k=uuid.uuid4().hex,
            )

    yield add
    for email in made:
        cleanup_email(email)


def _statements(call: Callable[[], Any]) -> list[str]:
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = call()
        assert response.status_code == 200, response.text
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return seen


def test_showing_photos_on_the_leaderboard_costs_no_extra_queries(
    emails: Emails,
    attendee: Attendee,
    media_root: Path,
    crowd: Callable[[int], None],
) -> None:
    _, client = attendee(emails())
    _upload(client, png_bytes())
    crowd(2)
    few = _statements(lambda: client.get("/api/leaderboard?view=all&limit=100"))
    crowd(25)
    many = _statements(lambda: client.get("/api/leaderboard?view=all&limit=100"))
    assert len(many) == len(few), (len(few), len(many))
    assert not any("FROM users WHERE users.photo_key" in s for s in many)


def test_a_public_card_with_a_photo_costs_the_same_queries_as_one_without(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    _, plain = attendee(emails())
    _, shown = attendee(emails())
    _upload(shown, png_bytes())
    plain_token, shown_token = _token_of(plain), _token_of(shown)
    with make_client() as anonymous:
        without = _statements(lambda: anonymous.get(f"/api/cards/{plain_token}"))
        with_photo = _statements(lambda: anonymous.get(f"/api/cards/{shown_token}"))
    assert len(with_photo) == len(without)


def test_uploads_in_parallel_leave_exactly_one_photo(
    emails: Emails, attendee: Attendee, media_root: Path
) -> None:
    """Two devices uploading at once: the row lock serialises the swaps, the
    loser's files are removed by the winner of the next swap, and exactly one
    photo remains."""
    email, first = attendee(emails())
    second = make_client()
    second.cookies.update(first.cookies)
    try:
        barrier = threading.Barrier(2)
        codes: list[int] = []

        def go(client: TestClient, data: bytes) -> None:
            barrier.wait(timeout=10)
            codes.append(_upload(client, data).status_code)

        threads = [
            threading.Thread(target=go, args=(first, png_bytes((300, 300)))),
            threading.Thread(target=go, args=(second, jpeg_bytes((300, 300)))),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert codes == [200, 200]
        key = _key(email)
        assert key is not None
        assert _files(media_root) == {f"{key}-128.jpg", f"{key}-384.jpg"}
    finally:
        second.close()
