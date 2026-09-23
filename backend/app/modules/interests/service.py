"""Read access to the controlled interest vocabulary (ARCHITECTURE.md
§7.2) — what the registration form offers, and exactly what
`POST /api/attendees/register` will accept (active rows, by slug)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.interests.models import Interest


def list_active_interests(db: Session) -> list[Interest]:
    return list(
        db.execute(
            select(Interest)
            .where(Interest.is_active.is_(True))
            .order_by(Interest.sort_order, Interest.name)
        ).scalars()
    )
