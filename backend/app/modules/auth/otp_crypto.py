"""Authenticated encryption of OTP codes while they wait in the outbox.

The code must exist in readable form somewhere between "challenge created"
and "email sent" - the worker is a different process and has to send it -
but it must not sit in the database as plaintext, where a database dump or
read-only SQL access would be an account takeover for the next ten minutes
(ARCHITECTURE.md 8.2). So it is stored as AES-256-GCM ciphertext under a key
that lives only in the environment (OTP_OUTBOX_ENCRYPTION_KEY), never in the
database or the repository.

* The ciphertext is bound (AAD) to the row's idempotency key, address and
  purpose: a ciphertext copied onto another row fails authentication.
* A fresh random 96-bit nonce per message.
* `key_id` is a short fingerprint stored beside the ciphertext so a rotation
  can use the previous key (OTP_OUTBOX_ENCRYPTION_KEY_PREVIOUS) for rows
  queued before it, without ever trying keys blindly.
* This is separate from the Argon2id hash on `otp_challenges`, which is what
  verification uses and is unchanged.

Errors never carry the code, the key or the ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings, decode_outbox_key, get_settings

_VERSION = b"\x01"
_NONCE_BYTES = 12
_AAD_PREFIX = b"facemark/otp-outbox/v1"


class OtpCryptoError(Exception):
    """The payload cannot be produced or read. Deliberately message-light."""


def key_fingerprint(raw_key: bytes) -> str:
    return hashlib.sha256(b"facemark/otp-outbox/key-id" + raw_key).hexdigest()[:12]


def _keyring(settings: Settings) -> dict[str, bytes]:
    ring: dict[str, bytes] = {}
    for value in (
        settings.otp_outbox_encryption_key,
        settings.otp_outbox_encryption_key_previous,
    ):
        if value:
            raw = decode_outbox_key(value)
            ring[key_fingerprint(raw)] = raw
    return ring


def _aad(idempotency_key: str, email: str, purpose: str) -> bytes:
    return b"|".join(
        (_AAD_PREFIX, idempotency_key.encode(), email.lower().encode(), purpose.encode())
    )


def encrypt_code(
    code: str, *, idempotency_key: str, email: str, purpose: str, settings: Settings | None = None
) -> tuple[bytes, str]:
    """Returns (ciphertext, key_id) under the CURRENT key."""
    settings = settings or get_settings()
    if not settings.otp_outbox_encryption_key:
        raise OtpCryptoError("no encryption key is configured")
    raw = decode_outbox_key(settings.otp_outbox_encryption_key)
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(raw).encrypt(nonce, code.encode(), _aad(idempotency_key, email, purpose))
    return _VERSION + nonce + sealed, key_fingerprint(raw)


def decrypt_code(
    ciphertext: bytes,
    key_id: str | None,
    *,
    idempotency_key: str,
    email: str,
    purpose: str,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    raw = _keyring(settings).get(key_id or "")
    if raw is None:
        raise OtpCryptoError("the key this payload was encrypted with is not configured")
    if len(ciphertext) <= 1 + _NONCE_BYTES or ciphertext[:1] != _VERSION:
        raise OtpCryptoError("unrecognised payload format")
    nonce = ciphertext[1 : 1 + _NONCE_BYTES]
    try:
        plain = AESGCM(raw).decrypt(
            nonce, ciphertext[1 + _NONCE_BYTES :], _aad(idempotency_key, email, purpose)
        )
    except InvalidTag:
        raise OtpCryptoError("payload failed authentication") from None
    return plain.decode()


def generate_key() -> str:
    """A fresh key in the format OTP_OUTBOX_ENCRYPTION_KEY expects."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


if __name__ == "__main__":
    # `python -m app.modules.auth.otp_crypto` prints a new key and needs no
    # configuration at all - useful before the environment file exists.
    print(generate_key())
