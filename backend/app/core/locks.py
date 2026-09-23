"""The project's PostgreSQL advisory lock key(s).

ARCHITECTURE.md §19.4 requires "a single well-known bigint constant,
defined once in app.core.locks" for the reset fence, and
IMPLEMENTATION_PLAN.md's adversarial-verification instructions require
it to be deterministic across processes — explicitly NOT Python's
built-in ``hash()``, which is salted with a random per-process seed for
``str``/``bytes`` inputs (PEP 456) unless ``PYTHONHASHSEED`` is pinned.
Two different backend workers (or the same worker restarted) must agree
on this value, or the fence protects nothing.

``zlib.crc32`` is used instead: it is part of the standard library, has
no random seed, and produces the same 32-bit unsigned integer for the
same input on every process, every platform, every Python version. That
fits PostgreSQL's ``bigint``-typed advisory lock functions with room to
spare (crc32's range is 0..2**32-1; bigint is signed 64-bit).

The value is also pinned as a literal in ``tests/test_locks.py`` so an
accidental change to the namespace string below — which would silently
change the numeric key and defeat every advisory lock call site — is
caught by the test suite, not discovered in production.
"""

from __future__ import annotations

import zlib

# Bump the version suffix (never reuse a retired one) if this key ever
# needs to be deliberately rotated. Doing so is a breaking change for
# any process still running the old value — coordinate a full restart.
_RESET_FENCE_NAMESPACE = b"facemark:reset_fence:v1"

FACEMARK_RESET_FENCE: int = zlib.crc32(_RESET_FENCE_NAMESPACE)
