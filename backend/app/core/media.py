"""Where profile photos live on disk, and nothing else.

One directory (MEDIA_ROOT, a named Docker volume in the Compose files so photos
survive a container rebuild), one file per rendition:

    <MEDIA_ROOT>/avatars/<key>-<size>.jpg        key = 32 lowercase hex characters

The KEY is made by the server (128 random bits) and stored on the owner's row
(`users.photo_key`, unique). A photo's file name therefore says nothing about the
upload (no original name, no extension taken from it, no user id), cannot be
guessed, and CHANGES whenever the photo changes - which is what makes the URL
safe to cache for a year: a new photo is a new URL.

Safety rules kept here, in one place:

* every path is built from a key that matches `^[0-9a-f]{32}$` and a size from
  the fixed list - never from a request - so no input can name another
  directory, and deleting a photo can only ever delete THAT key's own files;
* a file is written under a temporary name in the same directory, flushed, and
  renamed into place (atomic): nobody reads half a photo;
* a failure while writing removes what it had written;
* `purge_orphans` is the janitor for what a crash, a failed database write or a
  deleted account can leave behind: files of keys no row references, once they
  are older than a grace period (so an upload in flight is never taken).
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.core.images import SIZES

logger = logging.getLogger(__name__)

KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")
# Exactly the names this module makes: the sizes are spelled out, so there is no
# second spelling of a name (`-0128`, other digit scripts) and no other size.
FILE_PATTERN = re.compile(
    r"^([0-9a-f]{32})-(" + "|".join(str(size) for size in SIZES) + r")\.jpg$", re.ASCII
)
_TEMP_PREFIX = ".tmp-"


class MediaStorageError(Exception):
    """The store could not be written (full disk, missing or read-only volume).
    The message never contains a path from a request."""


@dataclass(frozen=True)
class OrphanReport:
    removed_files: int
    removed_temporary: int
    kept_referenced: int
    kept_recent: int


def new_key() -> str:
    return secrets.token_hex(16)


def is_key(value: object) -> bool:
    return isinstance(value, str) and KEY_PATTERN.fullmatch(value) is not None


def avatar_url(key: str | None, size: int) -> str | None:
    """The public URL of a rendition, or None when there is no photo. Relative:
    the browser asks the same origin (and the CSP's `img-src 'self'` allows it)."""
    if key is None or size not in SIZES or not is_key(key):
        return None
    return f"/api/media/avatars/{key}-{size}.jpg"


class MediaStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.avatars = root / "avatars"

    # --- paths: the only place a file name is built ----------------------------

    def path_for(self, key: str, size: int) -> Path:
        if not is_key(key):
            raise ValueError("not a photo key")
        if size not in SIZES:
            raise ValueError("not a photo size")
        return self.avatars / f"{key}-{size}.jpg"

    def path_for_file(self, name: str) -> Path | None:
        """The file a public URL's last segment names, or None. Anything that is
        not exactly `<key>-<size>.jpg` is not a photo."""
        match = FILE_PATTERN.fullmatch(name)
        if match is None or int(match.group(2)) not in SIZES:
            return None
        return self.path_for(match.group(1), int(match.group(2)))

    # --- writing and deleting --------------------------------------------------

    def write(self, key: str, renditions: dict[int, bytes]) -> None:
        """All renditions of one photo, or none of them (best effort on failure)."""
        paths = {size: self.path_for(key, size) for size in renditions}
        written: list[Path] = []
        temporary: list[Path] = []
        try:
            self.avatars.mkdir(parents=True, exist_ok=True)
            for size, data in renditions.items():
                final = paths[size]
                temp = self.avatars / f"{_TEMP_PREFIX}{key}-{size}-{secrets.token_hex(4)}"
                temporary.append(temp)
                with open(temp, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, final)
                temporary.remove(temp)
                written.append(final)
        except OSError as err:
            for leftover in (*temporary, *written):
                with contextlib.suppress(OSError):
                    leftover.unlink()
            # A stable token for log-based alerting (DEPLOYMENT.md, Monitoring). The
            # reason is the errno NAME (ENOSPC, EROFS, EACCES...) - never a path.
            logger.error(
                "MEDIA_WRITE_FAILED: a profile photo could not be stored (%s)",
                errno.errorcode.get(err.errno or 0, type(err).__name__),
            )
            raise MediaStorageError("the photo could not be stored") from err

    def delete(self, key: str | None) -> None:
        """Removes ONE key's files. Never raises: a leftover is the janitor's."""
        if key is None or not is_key(key):
            return
        for size in SIZES:
            try:
                self.path_for(key, size).unlink(missing_ok=True)
            except OSError:
                logger.warning("a profile photo file could not be removed")

    def check_writable(self) -> bool:
        """Whether a file can be created in the store right now (a missing or
        read-only volume says no). Writes and removes one empty probe file."""
        try:
            self.avatars.mkdir(parents=True, exist_ok=True)
            probe = self.avatars / f"{_TEMP_PREFIX}probe-{secrets.token_hex(4)}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError:
            return False
        return True

    def exists(self, key: str) -> bool:
        return all(self.path_for(key, size).is_file() for size in SIZES)

    # --- the janitor -----------------------------------------------------------

    def purge_orphans(
        self,
        referenced: Iterable[str],
        *,
        grace_seconds: float = 3600.0,
        now: float | None = None,
        dry_run: bool = False,
    ) -> OrphanReport:
        """Deletes the files of photos no row references (and stale temporary
        files), but only those older than `grace_seconds`: an upload writes its
        files BEFORE its row commits, so a young file may belong to one in flight."""
        keep = set(referenced)
        clock = time.time() if now is None else now
        removed = removed_temp = kept_ref = kept_recent = 0
        if not self.avatars.is_dir():
            return OrphanReport(0, 0, 0, 0)
        for entry in sorted(self.avatars.iterdir()):
            if not entry.is_file():
                continue
            try:
                age = clock - entry.stat().st_mtime
            except OSError:
                continue
            if entry.name.startswith(_TEMP_PREFIX):
                if age > grace_seconds:
                    removed_temp += _remove(entry, dry_run)
                else:
                    kept_recent += 1
                continue
            match = FILE_PATTERN.fullmatch(entry.name)
            if match is None:
                continue  # not ours: never touched
            if match.group(1) in keep:
                kept_ref += 1
            elif age > grace_seconds:
                removed += _remove(entry, dry_run)
            else:
                kept_recent += 1
        return OrphanReport(removed, removed_temp, kept_ref, kept_recent)


def _remove(path: Path, dry_run: bool = False) -> int:
    if dry_run:
        return 1
    try:
        path.unlink()
    except OSError:
        return 0
    return 1


def get_store() -> MediaStore:
    """The store for the configured MEDIA_ROOT (read per call, so a test can
    point it at a temporary directory)."""
    return MediaStore(Path(get_settings().media_root))
