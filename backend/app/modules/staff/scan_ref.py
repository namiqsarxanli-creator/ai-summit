"""`scan_ref` issuance and verification — ARCHITECTURE.md §13.3.

`POST /api/staff/scan/resolve` (M10) calls `issue_scan_ref` after a
card token resolves successfully. `POST /api/staff/points/award` (M11)
calls the `verify_*` functions below, in the exact order and stage
split §13.3/§13.4/§14.3 specify:

- Checks 1-4 and 7 are pure crypto/clock arithmetic — no database, no
  authenticated identity — and run PRE-FENCE (`verify_scan_ref_signature`,
  `verify_token_binding`).
- Checks 5-6 compare against the authenticated staff identity, so they
  run INSIDE the fence, after B1 resolves it (`verify_staff_and_session_binding`).
- Check 8 (expiry) is also pure, but is evaluated at B3a — AFTER the
  completed-idempotency lookup, and only for a genuinely new award
  (`verify_not_expired`). It is deliberately a separate function so
  nothing can call it from the wrong stage by accident.

Stateless by design (§13.3: "needs no table, no cleanup job, and no
shared state across workers") — every value in the envelope is either a
constant, an argument, or the server clock; nothing is looked up.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.errors import InvalidQrError, ScanExpiredError, StaffNotAuthorizedError
from app.core.security import (
    b64url_decode,
    b64url_nopad,
    canonical_json,
    hash_card_token,
    sign_hmac_sha256,
)

# §13.3: "`v` | format version, currently `1`."
SCAN_REF_VERSION = 1

# §13.3 check 4: "A clock-skew allowance of 5 s is permitted; beyond
# that the envelope claims to have been issued in the future."
FUTURE_IAT_SKEW = timedelta(seconds=5)

# §13.3 check 8 / §13.4 B3a: "now - iat > 120 s -> SCAN_EXPIRED."
SCAN_REF_TTL = timedelta(seconds=120)


def issue_scan_ref(
    *, secret: str, staff_id: int, session_id: int, card_token: str, now: datetime
) -> str:
    """Builds `scan_ref = B + "." + sig` exactly as §13.3 specifies:
    canonical JSON of `{v, sid, ses, tok, iat}` -> base64url unpadded
    (`B`) -> HMAC-SHA256 over `B` itself, keyed by `SCAN_REF_SECRET` ->
    base64url unpadded (`sig`). `tok` is `sha256(card_token)`, never the
    token itself (§13.3: "the envelope is not a second copy of the QR
    secret and is safe in logs"). `iat` is the server clock, never
    client-influenced."""
    payload: dict[str, object] = {
        "v": SCAN_REF_VERSION,
        "sid": staff_id,
        "ses": session_id,
        "tok": hash_card_token(card_token),
        "iat": int(now.timestamp()),
    }
    body = b64url_nopad(canonical_json(payload))
    signature = sign_hmac_sha256(secret, body)
    return f"{body}.{signature}"


@dataclass(frozen=True)
class ScanRefPayload:
    """The decoded, signature-verified envelope contents — checks 1-4
    already passed by the time this exists. `body` is `B`, the exact
    transmitted string, kept alongside the parsed fields because the
    HMAC covers `B` itself, never a re-serialization (§13.3)."""

    sid: int
    ses: int
    tok: str
    iat: int
    body: str


def verify_scan_ref_signature(*, secret: str, scan_ref: str, now: datetime) -> ScanRefPayload:
    """§13.3 checks 1-4, verbatim and in order. Pure — no database, no
    authenticated identity — safe to run pre-fence (§13.4 stage A).

    1. Split on the final `.`; malformed shape -> INVALID_QR.
    2. Recompute the HMAC over `B` and compare with `hmac.compare_digest`
       (constant time); mismatch -> INVALID_QR.
    3. Parse `P`. `v != 1` -> INVALID_QR.
    4. Future-timestamp rejection: `iat > now + 5s` -> INVALID_QR.
       Treated as tampering, not expiry — never bypassed, never the
       120s expiry check (that is `verify_not_expired`, check 8, B3a).

    Every failure here is `INVALID_QR`: a `scan_ref` that fails any of
    these checks is indistinguishable, from the client's perspective,
    from a malformed or unknown card token (§9.4's enumeration-resistance
    principle extended to the envelope itself)."""
    body, sep, sig = scan_ref.rpartition(".")
    if not sep or not body or not sig:
        raise InvalidQrError()

    expected_sig = sign_hmac_sha256(secret, body)
    if not hmac.compare_digest(expected_sig, sig):
        raise InvalidQrError()

    try:
        parsed: Any = json.loads(b64url_decode(body))
    except Exception as err:
        raise InvalidQrError() from err
    if not isinstance(parsed, dict):
        raise InvalidQrError()
    if parsed.get("v") != SCAN_REF_VERSION:
        raise InvalidQrError()

    try:
        sid = int(parsed["sid"])
        ses = int(parsed["ses"])
        tok = str(parsed["tok"])
        iat = int(parsed["iat"])
    except (KeyError, TypeError, ValueError) as err:
        raise InvalidQrError() from err

    if datetime.fromtimestamp(iat, tz=UTC) > now + FUTURE_IAT_SKEW:
        raise InvalidQrError()

    return ScanRefPayload(sid=sid, ses=ses, tok=tok, iat=iat, body=body)


def verify_token_binding(payload: ScanRefPayload, card_token: str) -> None:
    """§13.3 check 7: `sha256(request.card_token) == tok`. Pure —
    compares two values already in hand — safe pre-fence (A4)."""
    if payload.tok != hash_card_token(card_token):
        raise InvalidQrError()


def verify_staff_and_session_binding(
    payload: ScanRefPayload, *, staff_id: int, session_id: int
) -> None:
    """§13.3 checks 5-6. Both compare against the AUTHENTICATED
    identity, so this must run only after B1 resolves it, inside the
    fence — never before. Session binding (check 6) is what makes a
    revoked session invalidate every outstanding envelope for free."""
    if payload.sid != staff_id:
        raise StaffNotAuthorizedError("This scan was made by a different staff account.")
    if payload.ses != session_id:
        raise StaffNotAuthorizedError("This scan is no longer valid for your current session.")


def verify_not_expired(payload: ScanRefPayload, *, now: datetime) -> None:
    """§13.3 check 8, §13.4 B3a: `now - iat > 120s -> SCAN_EXPIRED`.
    Deliberately last, and deliberately a separate function: the
    caller must reach the completed-idempotency lookup (B3) FIRST and
    call this only when B3 found no record — a replay of a completed
    award must never be rejected for staleness (§13.4)."""
    if now - datetime.fromtimestamp(payload.iat, tz=UTC) > SCAN_REF_TTL:
        raise ScanExpiredError()
