"""GET /api/attendees/me/notifications - the attendee's own notifications (service.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Response

from app.core.deps import CurrentAttendee
from app.db.session import DbSession
from app.modules.notifications.service import notifications

router = APIRouter(tags=["notifications"])


def _now() -> datetime:
    return datetime.now(UTC)


@router.get("/attendees/me/notifications")
def my_notifications(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return notifications(db, user.id, _now())
