"""`GET /api/attendees/me/recommendations` — ARCHITECTURE.md §11,
IMPLEMENTATION_PLAN.md M07.

Placed under `attendees` in the URL (as the locked API contract names
it) but implemented in `matching`, mirroring M06's
`/api/attendees/me/card` under `contact_cards`. The attendee comes only
from the resolved session (`CurrentAttendee`, §5) — never a
client-supplied id — and, being read-only, is resolved outside any
mutating transaction and takes no reset fence (§19.4).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.deps import CurrentAttendee
from app.db.session import DbSession
from app.modules.matching.schemas import Recommendation
from app.modules.matching.service import recommendations_for

router = APIRouter(tags=["matching"])


@router.get("/attendees/me/recommendations", response_model=list[Recommendation])
def get_my_recommendations(attendee: CurrentAttendee, db: DbSession) -> list[Recommendation]:
    return recommendations_for(db, attendee)
