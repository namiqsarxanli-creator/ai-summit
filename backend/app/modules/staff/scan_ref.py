"""`scan_ref` issuance — ARCHITECTURE.md §13.3.

Issuance only: `POST /api/staff/scan/resolve` calls `issue_scan_ref`
after a card token resolves successfully. Verification (checks 1-8,
consumed by the award endpoint) is M11's, and is deliberately not
implemented here (M10's Explicitly Out of Scope: "`scan_ref`
verification ordering (M11 — issuance only here)").

Stateless by design (§13.3: "needs no table, no cleanup job, and no
shared state across workers") — every value in the envelope is either a
constant, an argument, or the server clock; nothing is looked up.
"""

from __future__ import annotations

from datetime import datetime

from app.core.security import b64url_nopad, canonical_json, hash_card_token, sign_hmac_sha256

# §13.3: "`v` | format version, currently `1`."
SCAN_REF_VERSION = 1


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
