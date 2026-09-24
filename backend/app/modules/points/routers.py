"""`POST /api/staff/points/award` — ARCHITECTURE.md §12.2, §13.1 step 7,
§13.4, §14.3.

Ordering mirrors every other mutating handler in this codebase (see
`staff/routers.py`): pure pre-fence work (A1-A6) happens here, directly
in the handler, before `mutating_transaction` is ever opened; every
reset-sensitive read and write (B1-B11) happens inside it, in
`points/service.py`. Not rate-limited (ARCHITECTURE.md §21.7 catalogue:
"a busy booth is normal traffic, and idempotency plus dedupe_key
already prevent the abuse a limit would target").
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.deps import SessionToken
from app.db.session import DbSession, mutating_transaction
from app.modules.points.schemas import AwardResponse, AwardTaskPointsRequest
from app.modules.points.service import (
    complete_award,
    compute_award_fingerprint,
    maintenance_fast_path_check,
)
from app.modules.staff.scan_ref import verify_scan_ref_signature, verify_token_binding

router = APIRouter(prefix="/staff/points", tags=["points"])


@router.post("/award", response_model=AwardResponse)
def award(payload: AwardTaskPointsRequest, token: SessionToken, db: DbSession) -> AwardResponse:
    """A1 (pydantic already validated `payload`) through B12.

    A2: `token` is the raw session cookie — a string, not yet an
    authenticated identity (§14.3). A3/A4: `scan_ref` signature and
    token binding are pure crypto, checked before anything else. A5:
    the request fingerprint. A6: the advisory maintenance fast-path.
    None of this touches a reset-sensitive read — B1 (inside the fence,
    in `complete_award`) is where authentication actually happens.
    """
    now = datetime.now(UTC)

    scan_payload = verify_scan_ref_signature(
        secret=get_settings().scan_ref_secret, scan_ref=payload.scan_ref, now=now
    )
    verify_token_binding(scan_payload, payload.card_token)
    fingerprint = compute_award_fingerprint(card_token=payload.card_token, points=payload.points)
    maintenance_fast_path_check(db)

    with mutating_transaction(db) as tx:
        return complete_award(
            tx,
            raw_session_token=token,
            scan_payload=scan_payload,
            request=payload,
            fingerprint=fingerprint,
            now=now,
        )
