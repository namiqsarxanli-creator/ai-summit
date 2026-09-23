"""`POST /api/staff/scan/resolve` — the staff consumer of the card token
(ARCHITECTURE.md §9.3, IMPLEMENTATION_PLAN.md M06).

M06 builds the resolution itself: a staff session is required and the
response is exactly three identity fields. `scan_ref` issuance is M10;
staff login is M09 — until then a staff session can only exist if one
was created some other way (the tests plant one).

A pure read, so no reset fence (§19.4). POST only because §13.1 defines
it as one, which also puts it under the Origin assertion (§8.3.1).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentStaff
from app.core.ratelimit import SCAN_RESOLVE_PER_STAFF, limiter
from app.db.session import DbSession
from app.modules.contact_cards.schemas import CardIdentity
from app.modules.contact_cards.service import resolve_card_identity
from app.modules.staff.schemas import ScanResolveRequest

router = APIRouter(prefix="/staff", tags=["staff"])


@router.post("/scan/resolve", response_model=CardIdentity)
def scan_resolve(payload: ScanResolveRequest, staff: CurrentStaff, db: DbSession) -> CardIdentity:
    """§12.3: booth staff and admins may resolve a card. §21.7: 120/min
    per staff account."""
    limiter.check([(SCAN_RESOLVE_PER_STAFF, str(staff.id))])
    return resolve_card_identity(db, payload.card_token)
