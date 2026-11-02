"""The one writer of competition points. Every award goes through `award`.

Inside the caller's fenced transaction it:

1. takes a per-attendee transaction lock, so two scans of one person (two
   doors, a retry, a lost response) are applied one after the other;
2. pays a unit ONCE - the unit's `dedupe_key` is unique in `points_log`, and a
   repeat returns the existing entry (`already`), never a second one;
3. refuses an ineligible person (not ACTIVE, or excluded by the organiser),
   activity at or after the competition close, and anything beyond the
   category's own maximum (and, for Playground, beyond the first 8 booths) -
   without a configured close it pays nothing either (`not_configured`) -
   such evidence is still kept by the caller, it just earns nothing.

It never edits or deletes a ledger row; a correction is a REVERSAL entry
(points/reversal.py). Every paid award of a zone category re-derives the
Founder bonus in the same transaction (competition/founder.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, aliased

from app.modules.attendees.models import User, UserStatus
from app.modules.competition.rules import CATEGORIES, PLAYGROUND_SCORING_BOOTHS
from app.modules.masterclasses.models import MasterclassEvent
from app.modules.points.models import PointsEntryType, PointsLog, PointsSource

_USER_LOCK_CLASS = 21461017

# "not_configured": no competition close is set, so nothing can be earned YET -
# never reported as "closed" (the close has passed).
Outcome = Literal[
    "awarded", "already", "ineligible", "not_configured", "closed", "category_full", "not_scored"
]


@dataclass(frozen=True)
class AwardResult:
    outcome: Outcome
    points: int
    entry_id: int | None = None

    @property
    def paid(self) -> bool:
        return self.outcome == "awarded"


def lock_attendee_points(db: Session, user_id: int) -> None:
    db.execute(text("SELECT pg_advisory_xact_lock(:c, :u)"), {"c": _USER_LOCK_CLASS, "u": user_id})


def dedupe_key(source: PointsSource, user_id: int, unit: str) -> str:
    return f"COMP:{source.value}:{user_id}:{unit}"


def eligible(user: User | None) -> bool:
    return (
        user is not None
        and user.status == UserStatus.ACTIVE
        and user.competition_excluded_reason is None
    )


def closes_at(db: Session) -> datetime | None:
    """The competition close (17:30 Asia/Baku) of the configured event."""
    return db.execute(
        select(func.min(MasterclassEvent.competition_closes_at)).where(
            MasterclassEvent.competition_closes_at.is_not(None)
        )
    ).scalar_one_or_none()


def _unreversed_awards(db: Session, user_id: int, source: PointsSource) -> list[PointsLog]:
    reversal = aliased(PointsLog)
    return list(
        db.execute(
            select(PointsLog)
            .outerjoin(reversal, reversal.reverses_points_log_id == PointsLog.id)
            .where(
                PointsLog.user_id == user_id,
                PointsLog.source == source,
                PointsLog.entry_type == PointsEntryType.AWARD,
                reversal.id.is_(None),
            )
        ).scalars()
    )


def category_total(db: Session, user_id: int, source: PointsSource) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.sum(PointsLog.points), 0)).where(
                PointsLog.user_id == user_id, PointsLog.source == source
            )
        ).scalar_one()
    )


def award(
    db: Session,
    *,
    user_id: int,
    source: PointsSource,
    unit: str,
    points: int,
    occurred_at: datetime,
    staff_id: int | None = None,
    booth_id: int | None = None,
    note: str | None = None,
) -> AwardResult:
    """Pay `points` for one verified unit of participation, at most once."""
    lock_attendee_points(db, user_id)
    key = dedupe_key(source, user_id, unit)
    existing = db.execute(select(PointsLog).where(PointsLog.dedupe_key == key)).scalar_one_or_none()
    if existing is not None:
        return AwardResult("already", existing.points, existing.id)
    if not eligible(db.get(User, user_id)):
        return AwardResult("ineligible", 0)
    close = closes_at(db)
    if close is None:
        return AwardResult("not_configured", 0)
    if occurred_at >= close:
        return AwardResult("closed", 0)
    if source == PointsSource.PLAYGROUND:
        booths = {row.booth_id for row in _unreversed_awards(db, user_id, source)}
        if len(booths) >= PLAYGROUND_SCORING_BOOTHS:
            return AwardResult("not_scored", 0)
    if source not in CATEGORIES:  # e.g. MENTOR, no longer a competition category
        return AwardResult("not_scored", 0)
    if category_total(db, user_id, source) + points > CATEGORIES[source].maximum:
        return AwardResult("category_full", 0)
    entry = PointsLog(
        user_id=user_id,
        booth_id=booth_id,
        source=source,
        entry_type=PointsEntryType.AWARD,
        points=points,
        entered_by_staff_id=staff_id,
        entered_by_system=staff_id is None,
        dedupe_key=key,
        note=note,
        occurred_at=occurred_at,
    )
    db.add(entry)
    db.flush()
    if source != PointsSource.FOUNDER:
        from app.modules.competition.founder import recompute

        recompute(db, user_id)
    return AwardResult("awarded", points, entry.id)
