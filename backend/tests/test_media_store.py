"""`app.core.media`: where photos live on disk, and what may be removed.

Pure unit tests on a temporary directory. The rules pinned here are the ones
that keep one attendee's cleanup from ever touching another attendee's file and
keep the janitor from taking an upload that is still in flight.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.images import LARGE, SIZES, SMALL
from app.core.media import (
    FILE_PATTERN,
    MediaStorageError,
    MediaStore,
    avatar_url,
    is_key,
    new_key,
)

DATA = {SMALL: b"\xff\xd8\xff small", LARGE: b"\xff\xd8\xff large"}


@pytest.fixture
def store(tmp_path: Path) -> MediaStore:
    return MediaStore(tmp_path / "media")


def _files(store: MediaStore) -> set[str]:
    return {p.name for p in store.avatars.iterdir()} if store.avatars.exists() else set()


def _age(path: Path, seconds: float) -> None:
    moment = path.stat().st_mtime - seconds
    os.utime(path, (moment, moment))


# --- keys and names ------------------------------------------------------------------------------


def test_a_key_is_random_hex_and_never_repeats() -> None:
    keys = {new_key() for _ in range(200)}
    assert len(keys) == 200
    assert all(is_key(k) and len(k) == 32 for k in keys)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "abc",
        "0" * 31,
        "0" * 33,
        "G" * 32,
        "A" * 32,  # upper case is not what the server makes
        "../" + "0" * 29,
        "0" * 31 + "/",
        "0" * 32 + "\n",
        "0" * 16 + "\x00" + "0" * 15,
        123,
    ],
)
def test_only_the_servers_own_key_shape_is_a_key(bad: object) -> None:
    assert not is_key(bad)


def test_the_public_url_is_built_only_from_a_valid_key_and_a_known_size() -> None:
    key = new_key()
    assert avatar_url(key, SMALL) == f"/api/media/avatars/{key}-128.jpg"
    assert avatar_url(key, LARGE) == f"/api/media/avatars/{key}-384.jpg"
    assert avatar_url(None, SMALL) is None
    assert avatar_url("../etc/passwd", SMALL) is None
    assert avatar_url(key, 999) is None


def test_a_request_name_resolves_only_when_it_is_exactly_a_photo_name(store: MediaStore) -> None:
    key = new_key()
    assert store.path_for_file(f"{key}-128.jpg") == store.avatars / f"{key}-128.jpg"
    assert store.path_for_file(f"{key}-384.jpg") == store.avatars / f"{key}-384.jpg"
    for bad in [
        f"{key}-999.jpg",
        f"{key}-128.png",
        f"{key}-128.jpg/",
        f"{key}-128.jpg\n",
        f"{key.upper()}-128.jpg",
        f"../{key}-128.jpg",
        f"..%2f{key}-128.jpg",
        f"{key}-128.jpg.php",
        f"x{key}-128.jpg",
        "secret.txt",
        "..",
        ".",
        "",
        f"{key}-0128.jpg",
        f"{key}-١٢٨.jpg",  # Arabic-Indic digits that int() reads as 128
        f"{key}-128",
    ]:
        assert store.path_for_file(bad) is None, bad
    assert FILE_PATTERN.fullmatch(f"{key}-128.jpg")


def test_paths_are_built_only_from_a_key_and_a_known_size(store: MediaStore) -> None:
    with pytest.raises(ValueError):
        store.path_for("../../x", SMALL)
    with pytest.raises(ValueError):
        store.path_for(new_key(), 500)
    # whatever it builds stays inside the avatars directory
    path = store.path_for(new_key(), SMALL)
    assert path.parent == store.avatars


# --- writing --------------------------------------------------------------------------------------


def test_write_creates_every_rendition_and_leaves_no_temporary_file(store: MediaStore) -> None:
    key = new_key()
    store.write(key, DATA)
    assert _files(store) == {f"{key}-{s}.jpg" for s in SIZES}
    assert store.exists(key)
    assert store.path_for(key, SMALL).read_bytes() == DATA[SMALL]


def test_a_failed_write_leaves_nothing_behind(
    store: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    calls: list[int] = []

    def flaky(src: object, dst: object) -> None:
        calls.append(1)
        if len(calls) == 2:  # the SECOND rendition cannot be put in place
            raise OSError("disk full")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(MediaStorageError):
        store.write(new_key(), DATA)
    assert _files(store) == set()  # not the first rendition, not a temporary file


def test_an_unwritable_root_is_a_storage_error(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    with pytest.raises(MediaStorageError):
        MediaStore(blocker / "media").write(new_key(), DATA)


# --- deleting -------------------------------------------------------------------------------------


def test_delete_removes_that_keys_files_and_only_those(store: MediaStore) -> None:
    mine, theirs = new_key(), new_key()
    store.write(mine, DATA)
    store.write(theirs, DATA)
    store.delete(mine)
    assert _files(store) == {f"{theirs}-{s}.jpg" for s in SIZES}


def test_delete_ignores_anything_that_is_not_a_key(store: MediaStore) -> None:
    key = new_key()
    store.write(key, DATA)
    outside = store.root / "outside.txt"
    outside.write_text("keep")
    for bad in (None, "", "../outside", "*", "0" * 31, key.upper(), f"{key}-128"):
        store.delete(bad)
    assert store.exists(key)
    assert outside.read_text() == "keep"


def test_delete_of_a_missing_photo_is_quiet(store: MediaStore) -> None:
    store.delete(new_key())  # no error, nothing to do


# --- start-up check -----------------------------------------------------------------------


def test_a_writable_store_says_so_and_leaves_no_probe_behind(store: MediaStore) -> None:
    assert store.check_writable() is True
    assert _files(store) == set()


def test_a_missing_or_blocked_volume_is_reported_not_raised(tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    assert MediaStore(blocker / "media").check_writable() is False


def test_the_app_warns_at_start_up_when_the_volume_is_unwritable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.core.config import get_settings
    from tests.m04_support import make_client

    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    monkeypatch.setenv("MEDIA_ROOT", str(blocker / "media"))
    get_settings.cache_clear()
    try:
        with caplog.at_level("WARNING"), make_client():
            pass
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert any("MEDIA_ROOT is not writable" in r.getMessage() for r in caplog.records)


# --- the janitor --------------------------------------------------------------------------


def test_orphans_older_than_the_grace_period_go_and_young_ones_stay(store: MediaStore) -> None:
    old, young = new_key(), new_key()
    store.write(old, DATA)
    store.write(young, DATA)
    for size in SIZES:
        _age(store.path_for(old, size), 7200)

    report = store.purge_orphans([], grace_seconds=3600)
    assert (report.removed_files, report.kept_recent, report.kept_referenced) == (2, 2, 0)
    assert _files(store) == {f"{young}-{s}.jpg" for s in SIZES}


def test_a_referenced_photo_is_never_removed_however_old(store: MediaStore) -> None:
    key = new_key()
    store.write(key, DATA)
    for size in SIZES:
        _age(store.path_for(key, size), 10 * 86400)
    report = store.purge_orphans([key], grace_seconds=60)
    assert (report.removed_files, report.kept_referenced) == (0, 2)
    assert store.exists(key)


def test_an_upload_in_flight_is_protected_by_the_grace_period(store: MediaStore) -> None:
    """The files exist (written) but no row references them yet (not committed)."""
    in_flight = new_key()
    store.write(in_flight, DATA)
    report = store.purge_orphans([], grace_seconds=3600)
    assert report.removed_files == 0
    assert store.exists(in_flight)


def test_stale_temporary_files_are_removed_and_fresh_ones_kept(store: MediaStore) -> None:
    store.avatars.mkdir(parents=True)
    stale = store.avatars / f".tmp-{new_key()}-128-abcd"
    fresh = store.avatars / f".tmp-{new_key()}-384-abcd"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    _age(stale, 7200)
    report = store.purge_orphans([], grace_seconds=3600)
    assert report.removed_temporary == 1
    assert not stale.exists() and fresh.exists()


def test_the_janitor_touches_only_names_it_made(store: MediaStore) -> None:
    store.avatars.mkdir(parents=True)
    foreign = [
        store.avatars / "README.txt",
        store.avatars / f"{new_key()}-128.png",
        store.avatars / f"{new_key()}-777.jpg",
        store.avatars / "notes",
    ]
    for path in foreign:
        path.write_bytes(b"x")
        _age(path, 10 * 86400)
    (store.avatars / "subdir").mkdir()
    store.purge_orphans([], grace_seconds=1)
    assert all(p.exists() for p in foreign)
    assert (store.avatars / "subdir").is_dir()


def test_dry_run_reports_but_deletes_nothing(store: MediaStore) -> None:
    key = new_key()
    store.write(key, DATA)
    for size in SIZES:
        _age(store.path_for(key, size), 7200)
    report = store.purge_orphans([], grace_seconds=3600, dry_run=True)
    assert report.removed_files == 2
    assert store.exists(key)


def test_a_missing_directory_is_an_empty_store(store: MediaStore) -> None:
    report = store.purge_orphans([], grace_seconds=1)
    assert (report.removed_files, report.removed_temporary, report.kept_referenced) == (0, 0, 0)
