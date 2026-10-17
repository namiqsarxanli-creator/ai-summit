"""Beyond Now Photo: a connected pair, two separate screen consents, moderation,
withdrawal that deletes the files - and no points, ever.

Rig database (disposable) and a throw-away MEDIA_ROOT per test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.modules.staff.models import StaffRole
from tests.api_rig import Rig
from tests.profile_support import jpeg_bytes, media_root_fixture  # noqa: F401 - fixture
from tests.test_masterclasses import Program

URL = "/api/attendees/me/pair-photos"


@pytest.fixture
def pp(rig: Rig, media_root: Path) -> Iterator[Program]:
    p = Program(rig)
    yield p
    for c in p.clients:
        c.close()


def connect(p: Program, a: int, b: int) -> None:
    low, high = sorted((a, b))
    p.rig.sql(
        "INSERT INTO connections (user_low_id, user_high_id, kind, same_company, established_at) "
        "VALUES (:lo, :hi, 'OTHER', false, now())",
        lo=low,
        hi=high,
    )


def files(media_root: Path) -> set[str]:
    folder = media_root / "avatars"
    return {f.name for f in folder.iterdir()} if folder.is_dir() else set()


def test_only_a_connected_pair_can_share_a_photo(pp: Program, media_root: Path) -> None:
    a, b, stranger = pp.users(3)
    ca = pp.client(a)
    r = ca.post(URL, params={"partner_id": b}, content=jpeg_bytes())
    assert r.status_code == 409 and r.json()["error"]["details"]["reason"] == "not_connected"
    assert files(media_root) == set()  # refused before anything was decoded or stored
    assert ca.post(URL, params={"partner_id": a}, content=jpeg_bytes()).status_code == 409
    connect(pp, a, b)
    made = ca.post(URL, params={"partner_id": b, "screen": "true"}, content=jpeg_bytes())
    assert made.status_code == 201, made.text
    assert made.json()["status"] == "pending" and made.json()["with"] == "Guest N."
    assert len(files(media_root)) == 2  # two re-encoded renditions, nothing else
    # The stranger sees nothing of it.
    assert pp.client(stranger).get(URL).json()["photos"] == []
    assert pp.rig.scalar("SELECT count(*) FROM points_log") == 0


def test_the_screen_needs_both_consents_and_a_moderator(pp: Program, media_root: Path) -> None:
    a, b = pp.users(2)
    connect(pp, a, b)
    ca, cb = pp.client(a), pp.client(b)
    pid = ca.post(URL, params={"partner_id": b, "screen": "true"}, content=jpeg_bytes()).json()[
        "id"
    ]
    feed = "/api/pair-photos/feed"
    assert cb.post(f"{URL}/{pid}/answer", json={"accept": True}).json()["status"] == "accepted"
    pp.rig.sign_in_as(pp.rig.staff_id(StaffRole.ADMIN))
    assert pp.rig.client.get("/api/admin/pair-photos").json() == []  # partner has not consented
    on = cb.post(f"{URL}/{pid}/screen", json={"consent": True}).json()
    assert on["my_screen_consent"] and on["their_screen_consent"] and not on["on_screen"]
    assert ca.get(feed).json()["photos"] == []  # not moderated yet
    queue = pp.rig.client.get("/api/admin/pair-photos").json()
    assert [q["id"] for q in queue] == [pid]
    approved = pp.rig.client.post(f"/api/admin/pair-photos/{pid}/moderate", json={"approve": True})
    assert approved.json()["on_screen"] is True
    shown = ca.get(feed)
    assert len(shown.json()["photos"]) == 1 and "Guest" not in shown.text  # pictures only
    # Either person can take it off the screen again, at once.
    ca.post(f"{URL}/{pid}/screen", json={"consent": False})
    assert ca.get(feed).json()["photos"] == []
    assert (
        pp.rig.scalar("SELECT count(*) FROM admin_audit WHERE action = 'pair_photo.moderate'") == 1
    )


def test_withdrawing_deletes_the_files_for_both(pp: Program, media_root: Path) -> None:
    a, b = pp.users(2)
    connect(pp, a, b)
    ca, cb = pp.client(a), pp.client(b)
    pid = ca.post(URL, params={"partner_id": b}, content=jpeg_bytes()).json()["id"]
    cb.post(f"{URL}/{pid}/answer", json={"accept": True, "screen": True})
    assert len(files(media_root)) == 2
    gone = cb.post(f"{URL}/{pid}/withdraw")  # the partner may withdraw too
    assert gone.json()["status"] == "withdrawn" and gone.json()["photo_url"] is None
    assert files(media_root) == set()
    assert ca.get(URL).json()["photos"] == [] and cb.get(URL).json()["photos"] == []
    assert pp.rig.scalar("SELECT photo_key FROM pair_photos WHERE id = :i", i=pid) is None


def test_a_declined_photo_is_deleted_and_cannot_be_reaccepted(
    pp: Program, media_root: Path
) -> None:
    a, b = pp.users(2)
    connect(pp, a, b)
    ca, cb = pp.client(a), pp.client(b)
    pid = ca.post(URL, params={"partner_id": b}, content=jpeg_bytes()).json()["id"]
    assert ca.post(f"{URL}/{pid}/answer", json={"accept": True}).status_code == 404  # not theirs
    assert cb.post(f"{URL}/{pid}/answer", json={"accept": False}).json()["status"] == "declined"
    assert files(media_root) == set()
    again = cb.post(f"{URL}/{pid}/answer", json={"accept": True})
    assert again.status_code == 409 and again.json()["error"]["details"]["reason"] == (
        "already_answered"
    )


def test_a_pair_photo_is_not_a_profile_photo(pp: Program, media_root: Path) -> None:
    a, b = pp.users(2)
    connect(pp, a, b)
    pp.client(a).post(URL, params={"partner_id": b}, content=jpeg_bytes())
    assert pp.rig.scalar("SELECT count(*) FROM users WHERE photo_key IS NOT NULL") == 0


# --- who may see the bytes (independent audit 2026-10-17, finding 1) ---------------------

FEED = "/api/pair-photos/feed"


def _anonymous() -> TestClient:
    return TestClient(app, headers={"Origin": get_settings().app_origin})


def _shared(pp: Program, media_root: Path) -> tuple[int, str, str, TestClient, TestClient]:
    """A pending photo of a connected pair: (id, private url, legacy avatar url, a, b)."""
    a, b = pp.users(2)
    connect(pp, a, b)
    ca, cb = pp.client(a), pp.client(b)
    made = ca.post(URL, params={"partner_id": b, "screen": "true"}, content=jpeg_bytes())
    assert made.status_code == 201, made.text
    key = str(pp.rig.scalar("SELECT photo_key FROM pair_photos WHERE id = :i", i=made.json()["id"]))
    return made.json()["id"], made.json()["photo_url"], f"/api/media/avatars/{key}-384.jpg", ca, cb


def _admin(pp: Program) -> TestClient:
    pp.rig.sign_in_as(pp.rig.staff_id(StaffRole.ADMIN))
    return pp.rig.client


def test_pending_pair_photo_bytes_only_reach_the_pair_and_the_moderator(
    pp: Program, media_root: Path
) -> None:
    pid, url, legacy, ca, cb = _shared(pp, media_root)
    assert not url.startswith("/api/media/avatars/")  # not the public avatar route
    with _anonymous() as anonymous:
        for path in (url, legacy):
            refused = anonymous.get(path)
            assert refused.status_code == 404, path  # pending: nobody else
            assert "public" not in refused.headers.get("cache-control", "")
    outsider = pp.client(pp.users(1)[0])
    assert outsider.get(url).status_code == 404
    for member in (ca, cb):
        mine = member.get(url)
        assert mine.status_code == 200 and mine.headers["content-type"] == "image/jpeg"
        cache = mine.headers["cache-control"]
        assert "no-store" in cache and "public" not in cache and "immutable" not in cache
    assert _admin(pp).get(url).status_code == 200  # the moderator reviews it


def test_the_screen_shows_bytes_only_while_every_condition_holds_now(
    pp: Program, media_root: Path
) -> None:
    pid, url, legacy, ca, cb = _shared(pp, media_root)
    cb.post(f"{URL}/{pid}/answer", json={"accept": True, "screen": True})
    admin = _admin(pp)
    with _anonymous() as anonymous:
        assert anonymous.get(url).status_code == 404  # both consented, not moderated
        admin.post(f"/api/admin/pair-photos/{pid}/moderate", json={"approve": False})
        assert anonymous.get(url).status_code == 404  # rejected
        admin.post(f"/api/admin/pair-photos/{pid}/moderate", json={"approve": True})
        shown = anonymous.get(FEED).json()["photos"][0]["photo_url"]
        first = anonymous.get(shown)
        assert first.status_code == 200
        assert "no-store" in first.headers["cache-control"]
        assert anonymous.get(legacy).status_code == 404  # never via the avatar route
        # One person withdraws their screen consent: the very next request is refused,
        # including a conditional one that would otherwise be a cheap 304.
        cb.post(f"{URL}/{pid}/screen", json={"consent": False})
        assert anonymous.get(shown).status_code == 404
        etag = first.headers.get("etag")
        conditional = anonymous.get(shown, headers={"If-None-Match": etag or '"x"'})
        assert conditional.status_code == 404


def test_a_withdrawal_during_a_backup_revokes_access_although_the_files_stay(
    pp: Program, media_root: Path
) -> None:
    from tests.test_media_guard import Backup

    pid, url, legacy, ca, cb = _shared(pp, media_root)
    lock = Backup()
    try:
        assert lock.take()
        assert cb.post(f"{URL}/{pid}/withdraw").json()["status"] == "withdrawn"
        assert len(files(media_root)) == 2  # kept for the backup's snapshot
        for client in (ca, cb, _admin(pp)):
            assert client.get(url).status_code == 404
        with _anonymous() as anonymous:
            assert anonymous.get(legacy).status_code == 404
    finally:
        lock.release()
        lock.close()


def test_profile_avatars_stay_public_and_immutable(pp: Program, media_root: Path) -> None:
    uid = pp.users(1)[0]
    c = pp.client(uid)
    photo = c.put(
        "/api/attendees/me/photo", content=jpeg_bytes(), headers={"Content-Type": "image/jpeg"}
    )
    assert photo.status_code == 200, photo.text
    small = photo.json()["photo"]["small"]
    with _anonymous() as anonymous:
        served = anonymous.get(small)
        assert served.status_code == 200
        assert served.headers["cache-control"] == "public, max-age=31536000, immutable"
        again = anonymous.get(small, headers={"If-None-Match": served.headers["etag"]})
        assert again.status_code == 304
