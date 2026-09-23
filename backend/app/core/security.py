"""Password/OTP hashing and secret generation — ARCHITECTURE.md §21.1,
§8.2, §8.3, §21.2.

"Argon2id via argon2-cffi (t=2, m=64 MiB, p=1, tuned to ~100 ms on
target hardware). Only encoded hashes are stored. Plaintext passwords
are never stored, never logged, and never returned."

The same Argon2id hasher/verifier is used for staff passwords (M03) and
attendee OTP codes (M04, §8.2: "Argon2id hash only") — both are
low-entropy, human-typed secrets that need a slow, memory-hard hash.
Session and contact-card tokens are different: they are already
high-entropy CSPRNG output, so §8.3 specifies a fast SHA-256 hash for
those instead of Argon2id.
"""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

# Parameters from ARCHITECTURE.md §21.1: t=2 (time_cost), m=64 MiB
# (memory_cost, in KiB), p=1 (parallelism).
_hasher = PasswordHasher(time_cost=2, memory_cost=64 * 1024, parallelism=1)


def hash_password(password: str) -> str:
    """Returns an Argon2id encoded hash string, never the input itself."""
    return _hasher.hash(password)


def hash_otp_code(code: str) -> str:
    """ARCHITECTURE.md §8.2: "Argon2id hash only. A 6-digit code has a
    10^6 space; storing it in plaintext makes a database read an
    account takeover." Same hasher as `hash_password` — both are
    low-entropy secrets a human types in."""
    return _hasher.hash(code)


def verify_secret(hash_: str, candidate: str) -> bool:
    """Constant-time-safe verification for an Argon2id hash produced by
    either `hash_password` or `hash_otp_code`. Never raises on a
    mismatch — a wrong guess is an expected, routine outcome here, not
    an exceptional one."""
    try:
        _hasher.verify(hash_, candidate)
    except (VerifyMismatchError, VerificationError):
        return False
    return True


def generate_otp_code() -> str:
    """ARCHITECTURE.md §8.2: "6 digits, generated with
    secrets.randbelow(1_000_000), zero-padded." """
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_card_token() -> str:
    """ARCHITECTURE.md §21.2: "256-bit CSPRNG, not derived from any
    attendee attribute." Hex-encoded (64 chars) — the same format
    M03's fixed demo tokens use (app/seed/demo_fixtures.py)."""
    return secrets.token_hex(32)


def generate_session_token() -> str:
    """ARCHITECTURE.md §8.3: "generate secrets.token_urlsafe(32)"."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """ARCHITECTURE.md §8.3: "store only its SHA-256 hash in sessions."
    Not Argon2id: a session token is already 256 bits of CSPRNG output,
    not a human-guessable secret, so a slow hash buys nothing and would
    only add needless latency to every authenticated request."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
