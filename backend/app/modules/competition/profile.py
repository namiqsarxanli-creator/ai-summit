"""PROFILE: 5 points, once, for a verified attendee with a complete card.

Complete (decision of 16 Oct 2026, competition/rules.py): the account is ACTIVE
(e-mail ownership proven - name, phone and profession are already required to
register) and the interest card answers all four questions: a sector, at least
one topic, at least one "looking for" and at least one "offering". Company and
LinkedIn stay optional, so students and independent people are not excluded.

Qualification is frozen at the first moment it is met: editing the card later
never takes the points back (a correction would be an explicit reversal).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.modules.attendees.models import User, UserStatus
from app.modules.competition.ledger import AwardResult, award
from app.modules.competition.rules import PROFILE_POINTS
from app.modules.points.models import PointsSource


def profile_complete(user: User) -> bool:
    return (
        user.status == UserStatus.ACTIVE
        and user.card_sector is not None
        and len(user.card_topics) >= 1
        and len(user.card_seeking) >= 1
        and len(user.card_offering) >= 1
    )


def maybe_award_profile(db: Session, user: User, now: datetime) -> AwardResult | None:
    """Called after a profile save and after activation, inside their fenced
    transaction. Returns None when the profile is not complete yet."""
    if not profile_complete(user):
        return None
    return award(
        db,
        user_id=user.id,
        source=PointsSource.PROFILE,
        unit="profile",
        points=PROFILE_POINTS,
        occurred_at=now,
    )
