"""Points a Game Master adds by hand: the other half of a correction.

A scan can be missed although the person really took part (a volunteer forgot, the
wrong masterclass was chosen). The owner's decision (7 Nov 2026): the Game Master,
one person on the day, may then add the points, with a reason.

Everything the ledger guards still holds: each category's own maximum, eligibility
and the once-only key, here of the request itself, so a retry adds nothing. Beyond
Now Founder is never written by hand; it follows from the evidence, and a manual
award counts as evidence of its zone. The activity happened before the close even
if the desk records it after, so a manual award is dated inside the competition.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.orm import Session

from app.modules.admin.models import AdminAudit
from app.modules.attendees.models import User
from app.modules.competition.ledger import AwardResult, award, closes_at
from app.modules.competition.panels import AttendeeMatchError
from app.modules.points.models import PointsSource
from app.modules.staff.models import StaffAccount

AUDIT_MANUAL = "competition.manual_points"

MANUAL_SOURCES: Final = (
    PointsSource.PROFILE,
    PointsSource.PANEL,
    PointsSource.QMETER,
    PointsSource.MASTERCLASS,
    PointsSource.PLAYGROUND,
    PointsSource.CONNECTION,
)


def add_points(
    db: Session,
    staff: StaffAccount,
    *,
    user_id: int,
    source: PointsSource,
    points: int,
    reason: str,
    command_id: str,
    now: datetime,
) -> tuple[dict[str, Any], AwardResult]:
    user = db.get(User, user_id)
    if user is None:
        raise AttendeeMatchError("No such attendee.")
    close = closes_at(db)
    occurred = now if close is None else min(now, close - timedelta(seconds=1))
    result = award(
        db,
        user_id=user_id,
        source=source,
        unit=f"manual:{command_id}",
        points=points,
        occurred_at=occurred,
        staff_id=staff.id,
        note=reason,
    )
    if result.paid:
        db.add(
            AdminAudit(
                staff_account_id=staff.id,
                action=AUDIT_MANUAL,
                target_type="points_log",
                target_id=str(result.entry_id),
                before_value=None,
                after_value={
                    "id": result.entry_id,
                    "user_id": user_id,
                    "source": source.value,
                    "points": result.points,
                    "reason": reason,
                },
            )
        )
        db.flush()
    name = f"{user.first_name} {user.last_name}"
    return {"name": name, "outcome": result.outcome, "points": result.points}, result
