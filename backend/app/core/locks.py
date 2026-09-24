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

``RESET_NOTIFY_CHANNEL`` (M15) is a second, unrelated well-known name:
a PostgreSQL ``LISTEN``/``NOTIFY`` channel, not a lock. The reset CLI
runs as its own OS process (``docker compose exec backend python -m
app.cli reset-demo``) — a separate process from the serving Uvicorn
worker, so the in-process ``EventBus`` (``app/modules/realtime/broker.py``)
literally cannot be reached from it. ``LISTEN``/``NOTIFY`` is the
minimal cross-process transport: it needs no new infrastructure (the
same PostgreSQL connection already carries the reset fence and the
restore transaction), and — like the fence — a lost notification never
threatens correctness, because ``dataset_generation`` mismatch remains
the authoritative backstop (ARCHITECTURE.md §19.3) regardless of
whether any given client happens to receive the live push.
"""

from __future__ import annotations

import zlib

# Bump the version suffix (never reuse a retired one) if this key ever
# needs to be deliberately rotated. Doing so is a breaking change for
# any process still running the old value — coordinate a full restart.
_RESET_FENCE_NAMESPACE = b"facemark:reset_fence:v1"

FACEMARK_RESET_FENCE: int = zlib.crc32(_RESET_FENCE_NAMESPACE)

# A plain SQL identifier (LISTEN/NOTIFY channel names are not bigints
# like the advisory lock key above) — valid unquoted, deliberately
# namespaced so it can never collide with a channel name some other
# tool on the same PostgreSQL instance might use.
RESET_NOTIFY_CHANNEL = "facemark_dataset_reset"

# A second, distinct advisory-lock key — an admission mutex, not the
# fence. Two concurrent `reset-demo` invocations both racing to acquire
# FACEMARK_RESET_FENCE would otherwise both eventually succeed (the
# second simply waits out the first's exclusive hold, since ordinary
# mutations are expected to do exactly that against the fence) — which
# would silently run the ENTIRE destructive sequence twice, advancing
# dataset_generation by 2 for what an operator believes is one reset
# (M15 Codex pre-review: "accidental G→G+2 from two operator
# invocations" is an explicit failure mode to prevent). This key is
# acquired with `pg_try_advisory_lock` (non-blocking) as the very first
# thing `run_demo_reset` does: if another reset already holds it, this
# invocation refuses immediately — before touching maintenance_mode or
# anything else — rather than queuing for a second, redundant
# destructive pass. `FACEMARK_RESET_FENCE` itself is untouched by this
# — mutation draining against it (§19.4) works exactly as before.
FACEMARK_RESET_ADMISSION: int = zlib.crc32(b"facemark:reset_admission:v1")
