"""Password hashing — ARCHITECTURE.md §21.1.

"Argon2id via argon2-cffi (t=2, m=64 MiB, p=1, tuned to ~100 ms on
target hardware). Only encoded hashes are stored. Plaintext passwords
are never stored, never logged, and never returned."

M03 scope: hashing only, needed to seed `staff_accounts.password_hash`
without ever writing a plaintext literal. Verification (login, M09) and
token generation land with the features that need them.
"""

from __future__ import annotations

from argon2 import PasswordHasher

# Parameters from ARCHITECTURE.md §21.1: t=2 (time_cost), m=64 MiB
# (memory_cost, in KiB), p=1 (parallelism).
_hasher = PasswordHasher(time_cost=2, memory_cost=64 * 1024, parallelism=1)


def hash_password(password: str) -> str:
    """Returns an Argon2id encoded hash string, never the input itself."""
    return _hasher.hash(password)
