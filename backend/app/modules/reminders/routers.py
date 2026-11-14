"""The attendee's own reminder choice (migration 0024).

* GET /api/attendees/me/reminders - is the channel available, have I opted in
* PUT /api/attendees/me/reminders - opt in or out (fenced; withdrawing cancels
  every pending reminder at once)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from app.core.deps import CurrentAttendee, SessionToken
from app.db.session import DbSession, mutating_transaction
from app.modules.attendees.profile import lock_attendee
from app.modules.points.service import maintenance_fast_path_check
from app.modules.reminders import service

router = APIRouter(tags=["reminders"])


class ReminderChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _view(db: Any, consent: datetime | None) -> dict[str, Any]:
    return {"channel": "email", "available": service.enabled(db), "enabled": consent is not None}


@router.get("/attendees/me/reminders")
def my_reminders(user: CurrentAttendee, db: DbSession, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return _view(db, user.reminder_consent_at)


@router.put("/attendees/me/reminders")
def set_reminders(payload: ReminderChoice, token: SessionToken, db: DbSession) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    with mutating_transaction(db) as tx:
        user = lock_attendee(tx, token)
        if payload.enabled and user.reminder_consent_at is None:
            user.reminder_consent_at = datetime.now(UTC)
        elif not payload.enabled and user.reminder_consent_at is not None:
            user.reminder_consent_at = None
            service.cancel_for(tx, user.id, "consent_withdrawn")
        tx.flush()
        return _view(tx, user.reminder_consent_at)
