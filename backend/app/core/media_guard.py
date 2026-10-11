"""Who may delete profile-photo files, and when - the backup's consistency lock.

The problem this solves
-----------------------
A backup is a PostgreSQL dump plus an archive of the photo files. A photo file
is deleted AFTER the row that referenced it has stopped doing so (a replacement
or a removal), so at any instant every key the database references has its
files on disk. But the dump and the archive are two reads, not one: a photo
replaced or removed between them would leave the dump pointing at files the
archive no longer has. Detecting that afterwards does not make the backup
consistent; stopping it from happening does.

The mechanism: ONE PostgreSQL advisory lock, `MEDIA_FILE_LOCK`
-------------------------------------------------------------
* The BACKUP takes it EXCLUSIVELY (session level) before it starts the dump and
  keeps it until the archive is finished (deploy/backup/backup.sh).
* Every code path that deletes photo files takes it SHARED, without waiting
  (`pg_try_advisory_xact_lock_shared`) - the application after a replacement or
  removal (`delete_photo_files`), and the orphan janitor for its whole run
  (`janitor_session`).
* If the shared lock is not available because a backup holds the exclusive one,
  the deleter does NOT delete and does NOT wait: it leaves the files where they
  are. They are then unreferenced, i.e. exactly the orphans the janitor
  (`purge-orphan-photos`) already exists to collect. Nothing is lost, no request
  is slowed, and nothing breaks - at worst a replaced photo's old file lives a
  little longer on disk.

Guarantees (what the backup may rely on)
----------------------------------------
G1. From the moment the backup holds the lock until it releases it, no
    cooperating process unlinks a photo file. The dump is taken after the lock
    is held and the archive before it is released, so every key the dump
    references that existed before the backup is in the archive.
G2. A deletion that began before the backup took the lock is finished first
    (the backup's lock request waits for in-flight shared holders, bounded by
    `MEDIA_LOCK_WAIT`); and its row change was committed before the dump, so
    the dump does not reference the deleted key.
G3. Uploads need no coordination: a new file is written before its row commits
    and is never modified. A file the archive has but the dump does not
    reference is an orphan - harmless.
G4. Crash behaviour. The lock belongs to a database session: if the backup
    dies, its connection closes and PostgreSQL releases the lock (nothing to
    clean up, nothing stuck). If the application dies between a row change and
    the unlink, the files are orphans, as before. If a deleter skipped because
    of the backup, the orphans wait for the next janitor run.

What it cannot cover (the backup detects these and calls itself DEGRADED):
files that were already missing before the backup, and deletions by something
that does not take this lock (a manual `rm`, an old application version still
running during a rolling deploy, a damaged volume).
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.media import MediaStore
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

# Fixed by name like every other advisory key (app/core/locks.py explains why
# zlib.crc32 and not hash()). deploy/backup/lib.sh holds the same number;
# tests/test_media_guard.py fails if the two ever differ.
MEDIA_FILE_LOCK: int = zlib.crc32(b"facemark:media_files:v1")

_TRY_SHARED = text("SELECT pg_try_advisory_xact_lock_shared(:key)")


def delete_photo_files(store: MediaStore, key: str | None) -> bool:
    """Deletes ONE photo's files unless a backup is running.

    True: the files are gone (or there was nothing to delete). False: a backup
    holds the media lock or the lock database is unavailable, so files were
    kept for the orphan janitor. Never waits for a conflicting advisory lock.

    It uses its own short session: the caller's transaction (already committed
    by the time this runs) is not touched, and the lock lives exactly as long as
    the unlink - the transaction ends right after it."""
    if key is None:
        return True
    session = SessionLocal()
    try:
        if not session.execute(_TRY_SHARED, {"key": MEDIA_FILE_LOCK}).scalar_one():
            logger.info("photo files kept for the orphan janitor: a backup holds the media lock")
            return False
        store.delete(key)
        return True
    except SQLAlchemyError:
        # The row change has committed. Failure of best-effort cleanup must not
        # turn that successful update into a misleading HTTP error.
        logger.warning("photo cleanup deferred: media lock database unavailable")
        return False
    finally:
        session.rollback()  # ends the transaction, which releases the shared lock
        session.close()


@contextmanager
def janitor_session() -> Iterator[bool]:
    """Holds the shared media lock for as long as the `with` body runs (a
    transaction that stays open and does nothing else). Yields False when a
    backup holds the lock - the janitor must then not delete anything."""
    session = SessionLocal()
    try:
        yield bool(session.execute(_TRY_SHARED, {"key": MEDIA_FILE_LOCK}).scalar_one())
    finally:
        session.rollback()
        session.close()
