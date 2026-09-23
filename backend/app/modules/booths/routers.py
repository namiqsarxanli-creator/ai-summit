"""`GET /api/booths` and `GET /api/booths/{slug}` — ARCHITECTURE.md §10,
IMPLEMENTATION_PLAN.md M07.

Both unauthenticated: the booth catalogue is public summit information,
like the interest catalogue (`GET /api/interests`). Pure reads — no
reset fence (§19.4).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.errors import BoothNotFoundError
from app.db.session import DbSession
from app.modules.booths.schemas import BoothDetail, BoothOut
from app.modules.booths.service import get_active_booth_by_slug, list_active_booths

router = APIRouter(prefix="/booths", tags=["booths"])


@router.get("", response_model=list[BoothOut])
def list_booths(db: DbSession) -> list[BoothOut]:
    return [
        BoothOut(slug=b.slug, name=b.name, location_label=b.location_label)
        for b in list_active_booths(db)
    ]


@router.get("/{slug}", response_model=BoothDetail)
def get_booth(slug: str, db: DbSession) -> BoothDetail:
    booth = get_active_booth_by_slug(db, slug)
    if booth is None:
        raise BoothNotFoundError("No such booth.")
    return BoothDetail(
        slug=booth.slug,
        name=booth.name,
        description=booth.description,
        location_label=booth.location_label,
    )
