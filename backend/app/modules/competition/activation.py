"""Activating the Beyond Now competition on a database that already has data.

Nothing here runs by itself. The operator steps (OPERATIONS.md, "Beyond Now
competition"):

1. `python -m app.cli competition-report` - READ-ONLY: what the ranking would
   show, legacy points that would stop counting, people who would be excluded,
   complete profiles that have not earned PROFILE yet, the configured close.
2. Configure: the event's `competition_closes_at`, panels, scopes, eligibility.
3. `python -m app.cli competition-backfill-profile --confirm` - pays PROFILE to
   existing complete profiles (the ledger refuses duplicates; re-runnable).
4. Turn `event_config.competition_mode` on in the admin panel (audited).

Rollback of step 4 is turning the flag off: the legacy ranking returns exactly,
because no legacy row was changed. Competition rows stay as history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.admin.models import EventConfig
from app.modules.attendees.models import User, UserStatus
from app.modules.competition.ledger import closes_at
from app.modules.competition.profile import maybe_award_profile, profile_complete
from app.modules.competition.rules import COMPETITION_SOURCES
from app.modules.points.models import PointsLog, PointsSource


@dataclass(frozen=True)
class Report:
    competition_mode: bool
    closes_at: datetime | None
    active_attendees: int
    excluded_attendees: int
    legacy_points_total: int
    legacy_rows: int
    competition_points_total: int
    profile_backfill_candidates: int


def report(db: Session) -> Report:
    mode = bool(
        db.execute(select(EventConfig.competition_mode).where(EventConfig.id == 1)).scalar()
    )
    active = int(
        db.execute(
            select(func.count()).select_from(User).where(User.status == UserStatus.ACTIVE)
        ).scalar_one()
    )
    excluded = int(
        db.execute(
            select(func.count())
            .select_from(User)
            .where(User.status == UserStatus.ACTIVE, User.competition_excluded_reason.is_not(None))
        ).scalar_one()
    )
    legacy = db.execute(
        select(func.coalesce(func.sum(PointsLog.points), 0), func.count()).where(
            PointsLog.source.not_in(COMPETITION_SOURCES)
        )
    ).one()
    competition_total = int(
        db.execute(
            select(func.coalesce(func.sum(PointsLog.points), 0)).where(
                PointsLog.source.in_(COMPETITION_SOURCES)
            )
        ).scalar_one()
    )
    return Report(
        competition_mode=mode,
        closes_at=closes_at(db),
        active_attendees=active,
        excluded_attendees=excluded,
        legacy_points_total=int(legacy[0]),
        legacy_rows=int(legacy[1]),
        competition_points_total=competition_total,
        profile_backfill_candidates=len(_backfill_candidates(db)),
    )


def _backfill_candidates(db: Session) -> list[User]:
    paid = set(
        db.execute(
            select(PointsLog.user_id).where(PointsLog.source == PointsSource.PROFILE)
        ).scalars()
    )
    return [
        u
        for u in db.execute(select(User).where(User.status == UserStatus.ACTIVE)).scalars()
        if u.id not in paid and u.competition_excluded_reason is None and profile_complete(u)
    ]


def backfill_profile(db: Session, now: datetime | None = None) -> int:
    """Pay PROFILE to every complete profile not paid yet. Inside a fenced
    transaction; the ledger's unique key makes a re-run pay nobody twice."""
    stamp = now or datetime.now(UTC)
    paid = 0
    for user in _backfill_candidates(db):
        result = maybe_award_profile(db, user, stamp)
        if result is not None and result.paid:
            paid += 1
    return paid
