"""M02 acceptance: the advisory lock key is deterministic.

IMPLEMENTATION_PLAN.md / ARCHITECTURE.md §19.4: the reset fence key must
be "a stable documented integer/key strategy suitable for PostgreSQL" —
explicitly NOT Python's built-in `hash()`, which is randomised per
process for str/bytes inputs unless PYTHONHASHSEED is pinned.
"""

from __future__ import annotations

import zlib

from app.core.locks import _RESET_FENCE_NAMESPACE, FACEMARK_RESET_FENCE


def test_reset_fence_is_a_pinned_literal() -> None:
    """The exact numeric value is hardcoded here. If the namespace
    string in app/core/locks.py ever changes, this test fails loudly —
    an accidental edit there would otherwise silently change every
    advisory lock call site's key with no visible symptom until two
    processes disagreed about it at runtime.
    """
    assert FACEMARK_RESET_FENCE == 3952853003


def test_reset_fence_is_reproducible_from_its_own_definition() -> None:
    """Recomputes the value independently (crc32 of the documented
    namespace) and asserts it matches the module constant — proving
    the constant is derived deterministically, not hand-typed and
    liable to drift from its own documented derivation.
    """
    assert zlib.crc32(_RESET_FENCE_NAMESPACE) == FACEMARK_RESET_FENCE


def test_reset_fence_is_not_derived_from_python_hash() -> None:
    """crc32 is unrelated to (and does not coincidentally equal)
    Python's str hash() for the same bytes — guards against a future
    edit accidentally reintroducing hash() and this test still passing
    by coincidence.
    """
    assert (hash(_RESET_FENCE_NAMESPACE) & 0xFFFFFFFF) != FACEMARK_RESET_FENCE


def test_reset_fence_fits_postgresql_bigint() -> None:
    """pg_advisory_xact_lock_shared(bigint) — crc32's range (0 .. 2**32-1)
    must fit within signed 64-bit, with no overflow or sign-conversion
    surprise at the psycopg boundary."""
    PG_BIGINT_MAX = 2**63 - 1
    assert 0 <= FACEMARK_RESET_FENCE <= PG_BIGINT_MAX
