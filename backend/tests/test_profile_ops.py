"""Operations around profile photos: configuration, the janitor CLI, and the
realtime signals that make other open screens refresh.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.cli import main
from app.core.config import Settings
from app.core.media import MediaStore, new_key
from tests.m04_support import execute, fetch_one
from tests.prod_settings import production_settings
from tests.profile_support import (
    CONSENT,
    PHOTO,
    PROFILE,
    attendee_fixture,  # noqa: F401 - fixture
    emails_fixture,  # noqa: F401 - fixture
    jpeg_bytes,
    media_root_fixture,  # noqa: F401 - fixture
    png_bytes,
    profile_body,
)

Attendee = Callable[..., tuple[str, TestClient]]
Emails = Callable[[], str]


# --- configuration -----------------------------------------------------------------------------


def test_the_media_defaults_are_the_documented_ones() -> None:
    settings = production_settings()
    assert settings.media_root == "/data/media"
    assert settings.media_max_upload_bytes == 8 * 1024 * 1024
    assert settings.media_max_pixels == 25_000_000


@pytest.mark.parametrize("bad", ["media", "./media", "../media", "", "   "])
def test_a_relative_media_root_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError, match="MEDIA_ROOT must be an absolute path"):
        production_settings(media_root=bad)


def test_an_absolute_media_root_is_accepted() -> None:
    assert production_settings(media_root="/srv/facemark/media").media_root == "/srv/facemark/media"


@pytest.mark.parametrize(
    "overrides",
    [
        {"media_max_upload_bytes": 1_000},  # too small to hold a photo
        {"media_max_upload_bytes": 200 * 1024 * 1024},  # far past what the proxy allows
        {"media_max_pixels": 10},
        {"media_max_pixels": 5_000_000_000},
    ],
)
def test_media_limits_cannot_be_set_to_nonsense(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{**production_settings().model_dump(), **overrides})


# --- the janitor CLI ---------------------------------------------------------------------------


def _age(path: Path, seconds: float) -> None:
    moment = path.stat().st_mtime - seconds
    os.utime(path, (moment, moment))


def test_purge_orphan_photos_removes_only_old_unreferenced_files(
    emails: Emails,
    attendee: Attendee,
    media_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    email, client = attendee(emails())
    assert (
        client.put(PHOTO, content=png_bytes(), headers={"Content-Type": "x/y"}).status_code == 200
    )
    mine = fetch_one("SELECT photo_key FROM users WHERE email = :e", e=email)[0]

    store = MediaStore(media_root)
    old_orphan, young_orphan = new_key(), new_key()
    store.write(old_orphan, {128: b"\xff\xd8\xff a", 384: b"\xff\xd8\xff b"})
    store.write(young_orphan, {128: b"\xff\xd8\xff a", 384: b"\xff\xd8\xff b"})
    for size in (128, 384):
        _age(store.path_for(old_orphan, size), 7200)
        _age(store.path_for(mine, size), 7200)  # old, but referenced: must stay
    foreign = store.avatars / "README.txt"
    foreign.write_text("not ours")
    _age(foreign, 7200)

    main(["purge-orphan-photos", "--dry-run"])
    assert "would remove orphan photo files:  2" in capsys.readouterr().out
    assert store.exists(old_orphan)  # a dry run deletes nothing

    main(["purge-orphan-photos"])
    out = capsys.readouterr().out
    assert "removed orphan photo files:  2" in out
    assert not store.path_for(old_orphan, 128).exists()
    assert store.exists(young_orphan)  # inside the grace period: it may be an upload in flight
    assert store.exists(mine)  # referenced
    assert foreign.read_text() == "not ours"  # never touched
    assert client.get(PROFILE).json()["photo"] is not None

    main(["purge-orphan-photos", "--grace-minutes", "0"])
    assert not store.exists(young_orphan)
    assert store.exists(mine)


def test_purge_orphan_photos_keeps_pair_photos_that_no_user_row_names(
    emails: Emails,
    attendee: Attendee,
    media_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pair photos (migration 0025) live on the same volume. A file is not an orphan
    just because `users.photo_key` does not name it."""
    first, _ = attendee(emails())
    second, _ = attendee(emails())
    a = fetch_one("SELECT id FROM users WHERE email = :e", e=first)[0]
    b = fetch_one("SELECT id FROM users WHERE email = :e", e=second)[0]
    store = MediaStore(media_root)
    pair, orphan = new_key(), new_key()
    for key in (pair, orphan):
        store.write(key, {128: b"\xff\xd8\xff a", 384: b"\xff\xd8\xff b"})
        for size in (128, 384):
            _age(store.path_for(key, size), 7200)
    execute(
        "INSERT INTO pair_photos (uploader_id, partner_id, photo_key, status, accepted_at) "
        "VALUES (:a, :b, :k, 'ACCEPTED', now())",
        a=a,
        b=b,
        k=pair,
    )
    # A withdrawn photo keeps no key and protects nothing.
    execute(
        "INSERT INTO pair_photos (uploader_id, partner_id, status, withdrawn_at, withdrawn_by_id) "
        "VALUES (:a, :b, 'WITHDRAWN', now(), :a)",
        a=a,
        b=b,
    )
    try:
        main(["purge-orphan-photos"])
        out = capsys.readouterr().out  # counts FILES: two renditions per key
        assert "removed orphan photo files:  2" in out
        assert "kept (referenced):          2" in out
        assert store.exists(pair)  # referenced by a pair photo
        assert not store.path_for(orphan, 384).exists()  # referenced by nothing
    finally:
        execute("DELETE FROM pair_photos WHERE uploader_id = :a", a=a)


def test_purge_orphan_photos_works_when_nothing_was_ever_uploaded(
    media_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["purge-orphan-photos"])
    assert "removed orphan photo files:  0" in capsys.readouterr().out


# --- realtime signals --------------------------------------------------------------------------


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, bool]]:
    seen: list[tuple[int, bool]] = []

    def record(user_id: int, *, leaderboard: bool) -> None:
        seen.append((user_id, leaderboard))

    monkeypatch.setattr("app.modules.attendees.routers.publish_profile_signals", record)
    return seen


def test_other_screens_are_told_only_about_real_changes(
    emails: Emails,
    attendee: Attendee,
    media_root: Path,
    signals: list[tuple[int, bool]],
) -> None:
    email, client = attendee(emails())
    uid = fetch_one("SELECT id FROM users WHERE email = :e", e=email)[0]
    profile = client.get(PROFILE).json()

    # a no-op edit: nothing was saved, so nothing is announced
    assert client.put(PROFILE, json=profile_body(profile)).status_code == 200
    assert signals == []

    # interests only: this attendee's own recommendations; the leaderboard shows no interest
    changed = client.put(PROFILE, json=profile_body(profile, interest_slugs=["generative-ai"]))
    assert signals == [(uid, False)]

    # a name is on the leaderboard
    client.put(PROFILE, json=profile_body(changed.json(), first_name="Renamed"))
    assert signals[-1] == (uid, True)

    # consent: personal only, and only when it really changed
    signals.clear()
    client.put(CONSENT, json={"granted": True})
    client.put(CONSENT, json={"granted": True})
    assert signals == [(uid, False)]

    # photo: the leaderboard shows it; a no-op removal announces nothing
    signals.clear()
    assert client.put(PHOTO, content=jpeg_bytes()).status_code == 200
    assert signals == [(uid, True)]
    signals.clear()
    client.delete(PHOTO)
    client.delete(PHOTO)
    assert signals == [(uid, True)]


def test_a_refused_change_announces_nothing(
    emails: Emails,
    attendee: Attendee,
    media_root: Path,
    signals: list[tuple[int, bool]],
) -> None:
    _, client = attendee(emails())
    profile = client.get(PROFILE).json()
    assert client.put(PROFILE, json=profile_body(profile, phone="")).status_code == 422
    assert (
        client.put(PROFILE, json=profile_body(profile, interest_slugs=["nope"])).status_code == 400
    )
    assert client.put(PHOTO, content=b"not an image").status_code == 400
    assert signals == []


def test_the_real_signal_reaches_the_attendees_own_stream_and_the_leaderboard(
    emails: Emails, attendee: Attendee, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the recorder above: the real publisher, on the real bus."""
    from app.modules.realtime import service
    from app.modules.realtime.broker import event_bus

    published: list[tuple[str, str]] = []
    monkeypatch.setattr(
        event_bus, "publish_personal", lambda uid, name, data: published.append(("personal", name))
    )
    monkeypatch.setattr(
        event_bus, "publish_broadcast", lambda name, data: published.append(("broadcast", name))
    )
    service.publish_profile_signals(1, leaderboard=True)
    service.publish_profile_signals(1, leaderboard=False)
    assert published == [
        ("personal", "state_changed"),
        ("broadcast", "leaderboard_changed"),
        ("personal", "state_changed"),
    ]

    # and a failing bus never fails the request that already committed
    def broken(*args: Any) -> None:
        raise RuntimeError("bus down")

    monkeypatch.setattr(event_bus, "publish_personal", broken)
    service.publish_profile_signals(1, leaderboard=True)  # logged, not raised

    _, client = attendee(emails())
    assert client.put(PHOTO, content=png_bytes()).status_code == 200


def test_unused_execute_helper_stays_importable() -> None:
    assert callable(execute)
