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

import base64
import hashlib
import hmac
import json
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


def password_needs_rehash(hash_: str) -> bool:
    """ARCHITECTURE.md §21.1: "verify_and_update rehashes on parameter
    change." Checked only after a successful `verify_secret` — never
    for OTP codes, which are single-use and never re-hashed. If the
    stored hash's parameters no longer match `_hasher`'s (e.g. the
    memory/time cost was tuned), the caller re-hashes the same
    plaintext it already verified and persists the fresh encoding."""
    return _hasher.check_needs_rehash(hash_)


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
    """ARCHITECTURE.md §9.1: "public_token = secrets.token_urlsafe(32)
    # 256 bits of entropy, 43 URL-safe chars". Takes no argument on
    purpose: there is no function from attendee data to token (§9.1),
    so this signature cannot even express deriving one."""
    return secrets.token_urlsafe(32)


def hash_card_token(token: str) -> str:
    """SHA-256 hex of a card token, for places that must key on a
    token without holding a second copy of it — the per-token rate-limit
    bucket (M06), and later `scan_ref`'s `tok` field (§13.3, M10).
    `errors="replace"`: a malformed path token may hold anything."""
    return hashlib.sha256(token.encode("utf-8", errors="replace")).hexdigest()


def generate_session_token() -> str:
    """ARCHITECTURE.md §8.3: "generate secrets.token_urlsafe(32)"."""
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    """ARCHITECTURE.md §8.3: "store only its SHA-256 hash in sessions."
    Not Argon2id: a session token is already 256 bits of CSPRNG output,
    not a human-guessable secret, so a slow hash buys nothing and would
    only add needless latency to every authenticated request."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def canonical_json(payload: dict[str, object]) -> bytes:
    """ARCHITECTURE.md §13.3: "`P` is serialised as UTF-8 JSON with keys
    sorted ascending, no insignificant whitespace, no trailing newline."
    Used for `scan_ref`'s envelope (M10) and its request fingerprint
    (M11) — both sign/hash this exact byte-stable form, never a
    re-serialization of a parsed object."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def b64url_nopad(data: bytes) -> str:
    """Base64url, padding stripped — §13.3's `B` and `sig` encoding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text_: str) -> bytes:
    """Inverse of `b64url_nopad` — restores the padding `rstrip` removed
    before decoding. Raises `binascii.Error`/`ValueError` on malformed
    input; callers decide the domain error (M11: `INVALID_QR`)."""
    padded = text_ + "=" * (-len(text_) % 4)
    return base64.urlsafe_b64decode(padded)


def sign_hmac_sha256(secret: str, message: str) -> str:
    """ARCHITECTURE.md §13.3: "`sig = base64url_nopad(HMAC_SHA256(key =
    SCAN_REF_SECRET, msg = B))`." `message` must be the exact
    transmitted string, never a re-serialization — the caller owns that
    distinction, this function only signs whatever bytes it is given."""
    mac = hmac.new(secret.encode("utf-8"), message.encode("ascii"), hashlib.sha256).digest()
    return b64url_nopad(mac)
