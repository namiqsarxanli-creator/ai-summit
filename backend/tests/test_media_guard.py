"""The backup's consistency lock for profile-photo files (app/core/media_guard.py).

A "backup" here is a second PostgreSQL connection that takes the exclusive media
lock the way deploy/backup/backup.sh does, reads the referenced photo keys (what
the dump would hold) and later lists the files (what the archive would hold).
Real PostgreSQL, a throw-away MEDIA_ROOT, no mocks of the lock.
"""

from __future__ import annotations

import threading
import time
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import cli
from app.core.config import get_settings
from app.core.media import MediaStore
from app.core.media_guard import MEDIA_FILE_LOCK, delete_photo_files, janitor_session
from tests.m04_support import fetch_one
from tests.profile_support import (
    PHOTO,
    attendee_fixture,  # noqa: F401 - fixture
    emails_fixture,  # noqa: F401 - fixture
    jpeg_bytes,
    media_root_fixture,  # noqa: F401 - fixture
    png_bytes,
)

Attendee = Callable[..., tuple[str, TestClient]]
Emails = Callable[[], str]
HEADERS = {"Content-Type": "application/octet-stream"}
REPO_ROOT = Path(__file__).resolve().parents[2]


def _raw() -> psycopg.Connection[Any]:
    url = get_settings().database_url.replace("postgresql+psycopg://", "postgresql://")
    return psycopg.connect(url, autocommit=True)


class Backup:
    """Holds the exclusive session lock, like the backup script's psql session."""

    def __init__(self) -> None:
        self.conn = _raw()

    def take(self, wait: bool = True) -> bool:
        if wait:
            self.conn.execute("SELECT pg_advisory_lock(%s)", (MEDIA_FILE_LOCK,))
            return True
        row = self.conn.execute("SELECT pg_try_advisory_lock(%s)", (MEDIA_FILE_LOCK,)).fetchone()
        return bool(row and row[0])

    def release(self) -> bool:
        row = self.conn.execute("SELECT pg_advisory_unlock(%s)", (MEDIA_FILE_LOCK,)).fetchone()
        return bool(row and row[0])

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def backup() -> Iterator[Backup]:
    b = Backup()
    yield b
    b.close()  # a session's locks end with the session


def _key(email: str) -> str | None:
    row = fetch_one("SELECT photo_key FROM users WHERE email = :e", e=email)
    return None if row is None else row[0]


def _files(root: Path) -> set[str]:
    avatars = root / "avatars"
    return {p.name for p in avatars.iterdir()} if avatars.exists() else set()


def _pair(key: str) -> set[str]:
    return {f"{key}-128.jpg", f"{key}-384.jpg"}


def _upload(client: TestClient, data: bytes) -> Any:
    return client.put(PHOTO, content=data, headers=HEADERS)


# --- the lock key -----------------------------------------------------------------------


def test_the_lock_key_is_pinned_and_shared_with_the_backup_script() -> None:
    assert zlib.crc32(b"facemark:media_files:v1") == MEDIA_FILE_LOCK == 419078677
    script = REPO_ROOT / "deploy" / "backup" / "lib.sh"
    if script.exists():  # the deploy/ tree is not mounted in the development container
        assert f"FM_MEDIA_LOCK_KEY={MEDIA_FILE_LOCK}" in script.read_text(encoding="utf-8")


# --- the guard by itself ---------------------------------------------------------------------


def test_files_are_deleted_when_no_backup_runs(tmp_path: Path) -> None:
    store = MediaStore(tmp_path)
    store.write("a" * 32, {128: b"x", 384: b"y"})
    assert delete_photo_files(store, "a" * 32) is True
    assert _files(tmp_path) == set()
    assert delete_photo_files(store, None) is True  # nothing to delete is not a refusal


def test_files_are_kept_while_a_backup_holds_the_lock_and_deleted_afterwards(
    tmp_path: Path, backup: Backup
) -> None:
    store = MediaStore(tmp_path)
    store.write("b" * 32, {128: b"x", 384: b"y"})
    assert backup.take()

    started = time.monotonic()
    assert delete_photo_files(store, "b" * 32) is False
    assert time.monotonic() - started < 2  # it never waits for the backup
    assert _files(tmp_path) == _pair("b" * 32)

    assert backup.release()
    assert delete_photo_files(store, "b" * 32) is True
    assert _files(tmp_path) == set()


def test_concurrent_deleters_do_not_block_each_other(tmp_path: Path) -> None:
    store = MediaStore(tmp_path)
    keys = [f"{i:032x}" for i in range(8)]
    for key in keys:
        store.write(key, {128: b"x", 384: b"y"})
    results: list[bool] = []
    threads = [
        threading.Thread(target=lambda k=k: results.append(delete_photo_files(store, k)))
        for k in keys
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert results == [True] * 8 and _files(tmp_path) == set()


def test_the_backup_waits_for_a_deletion_that_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G2: a deletion that began before the backup asked finishes first."""
    store = MediaStore(tmp_path)
    store.write("c" * 32, {128: b"x", 384: b"y"})
    inside, proceed = threading.Event(), threading.Event()
    real_delete = MediaStore.delete

    def slow_delete(self: MediaStore, key: str | None) -> None:
        inside.set()
        proceed.wait(10)
        real_delete(self, key)

    monkeypatch.setattr(MediaStore, "delete", slow_delete)
    deleter = threading.Thread(target=lambda: delete_photo_files(store, "c" * 32))
    deleter.start()
    assert inside.wait(10)

    backup = Backup()
    try:
        # the deletion holds the shared lock: the backup cannot have the exclusive one yet
        assert backup.take(wait=False) is False
        proceed.set()
        deleter.join(10)
        assert backup.take(wait=False) is True  # now it can, and the file is already gone
        assert _files(tmp_path) == set()
    finally:
        proceed.set()
        backup.close()


def test_a_crashed_backup_releases_the_lock_with_its_connection(tmp_path: Path) -> None:
    """G4: nothing is left behind to clean up."""
    store = MediaStore(tmp_path)
    store.write("d" * 32, {128: b"x", 384: b"y"})
    b = Backup()
    assert b.take()
    assert delete_photo_files(store, "d" * 32) is False
    b.close()  # what a killed backup does: its connection ends
    deadline = time.monotonic() + 5
    while not delete_photo_files(store, "d" * 32):  # bounded: the server notices at once
        assert time.monotonic() < deadline, "the lock was not released with the connection"
        time.sleep(0.05)
    assert _files(tmp_path) == set()


# --- the real upload / replace / remove routes ---


def test_a_replacement_during_a_backup_keeps_the_old_files_for_the_janitor(
    emails: Emails, attendee: Attendee, media_root: Path, backup: Backup
) -> None:
    email, client = attendee(emails())
    assert _upload(client, png_bytes((300, 300))).status_code == 200
    old = _key(email)
    assert old is not None
    referenced_by_the_dump = {_key(email)}  # what pg_dump would see
    assert backup.take()

    assert _upload(client, jpeg_bytes((300, 300))).status_code == 200  # works as always
    new = _key(email)
    assert new is not None and new != old
    # the dump (taken before) references `old`; the archive (taken now) must still have it
    assert _pair(old) <= _files(media_root)
    assert referenced_by_the_dump == {old}
    assert client.get(f"/api/media/avatars/{new}-128.jpg").status_code == 200

    backup.release()
    # the leftover is a plain orphan: the janitor's job, and it does it
    store = MediaStore(media_root)
    report = store.purge_orphans({new}, grace_seconds=0.0, now=time.time() + 5)
    assert report.removed_files == 2
    assert _files(media_root) == _pair(new)


def test_a_removal_during_a_backup_keeps_the_files_too(
    emails: Emails, attendee: Attendee, media_root: Path, backup: Backup
) -> None:
    email, client = attendee(emails())
    _upload(client, png_bytes())
    key = _key(email)
    assert key is not None and backup.take()

    removed = client.delete(PHOTO)
    assert removed.status_code == 200 and removed.json() == {"photo": None}
    assert _key(email) is None  # the attendee sees the photo gone at once...
    assert _pair(key) <= _files(media_root)  # ...the bytes wait for the janitor


def test_without_the_lock_a_replacement_would_have_broken_the_snapshot(
    emails: Emails,
    attendee: Attendee,
    media_root: Path,
    backup: Backup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: the same sequence with the old, unconditional delete
    leaves the dump pointing at files the archive no longer has."""
    from app.modules.attendees import photo as photo_service

    monkeypatch.setattr(photo_service, "delete_photo_files", lambda s, k: s.delete(k) or True)
    email, client = attendee(emails())
    _upload(client, png_bytes((300, 300)))
    old = _key(email)
    assert old is not None and backup.take()
    _upload(client, jpeg_bytes((300, 300)))
    assert not (_pair(old) & _files(media_root))  # the race the lock exists to prevent


def test_snapshot_invariant_holds_under_continuous_replacement_and_removal(
    emails: Emails, attendee: Attendee, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the exclusive lock is held, every key the database references
    (the dump) is among the files on disk (the archive) - however many photos are
    replaced or removed meanwhile. Many windows, with an attendee changing their
    photo as fast as the server allows."""
    from app.core.ratelimit import RateLimit
    from app.modules.attendees import photo as photo_service

    # the per-attendee upload limit is not what is being tested here
    monkeypatch.setattr(
        photo_service, "PHOTO_UPLOAD_PER_ATTENDEE", RateLimit("photo_upload:test", 100_000, 600)
    )
    email, client = attendee(emails())
    stop = threading.Event()
    errors: list[str] = []

    def churn() -> None:
        i = 0
        while not stop.is_set():
            i += 1
            r = _upload(client, jpeg_bytes((200 + i % 7, 200), (i % 255, 80, 160)))
            r2 = client.delete(PHOTO) if i % 3 == 0 else r
            if r.status_code != 200 or r2.status_code != 200:
                errors.append(f"request failed: {r.status_code}/{r2.status_code}")

    worker = threading.Thread(target=churn)
    worker.start()
    violations: list[tuple[str, set[str]]] = []
    windows = 0
    try:
        deadline = time.monotonic() + 6
        backup = Backup()
        try:
            while time.monotonic() < deadline:
                backup.take()  # blocks while a deletion is in flight (G2)
                dump = _key(email)  # the dump
                time.sleep(0.12)  # the gap between dump and archive
                archive = _files(media_root)  # the archive
                if dump is not None and not _pair(dump) <= archive:
                    violations.append((dump, archive))
                windows += 1
                backup.release()
                time.sleep(0.03)
        finally:
            backup.close()
    finally:
        stop.set()
        worker.join(30)
    assert not errors
    assert windows >= 10, "the test did not exercise enough backup windows"
    assert not violations, f"{len(violations)} of {windows} windows lost a referenced photo"


# --- the janitor ---


def test_the_janitor_refuses_while_a_backup_runs_and_works_otherwise(backup: Backup) -> None:
    with janitor_session() as allowed:
        assert allowed is True
        # while the janitor holds the shared lock a backup cannot start its exclusive one
        assert backup.take(wait=False) is False
    assert backup.take(wait=False) is True
    with janitor_session() as allowed:
        assert allowed is False


def test_the_janitor_command_deletes_nothing_during_a_backup_and_reports_it(
    media_root: Path,
    backup: Backup,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MediaStore(media_root)
    orphan = "e" * 32
    store.write(orphan, {128: b"x", 384: b"y"})
    old = time.time() - 7200
    for path in (media_root / "avatars").iterdir():
        import os

        os.utime(path, (old, old))

    # a dry run is a report only: it needs no lock and removes nothing
    cli.main(["purge-orphan-photos", "--dry-run", "--grace-minutes", "60"])
    assert "would remove orphan photo files:  2" in capsys.readouterr().out
    assert _pair(orphan) <= _files(media_root)

    backup.take()
    with pytest.raises(SystemExit) as refused:
        cli.main(["purge-orphan-photos", "--grace-minutes", "60"])
    assert refused.value.code == 3
    assert "a backup is running" in capsys.readouterr().err
    assert _pair(orphan) <= _files(media_root)

    backup.release()
    cli.main(["purge-orphan-photos", "--grace-minutes", "60"])
    assert "removed orphan photo files:  2" in capsys.readouterr().out
    assert _files(media_root) == set()
