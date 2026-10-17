"""Beyond Now Founder: +10 once, for verified activity in all four zones.

PDF ("Founder: 4 zonanın hamısında fəaliyyət", flow page 3).

PROVISIONAL - NOT CONFIRMED BY THE ORGANISER. The PDFs do not say which
activities qualify. Until the organiser decides, a zone counts when the person
holds at least one LIVE (not reversed) competition award in it:

| Zone           | Qualifying evidence (an unreversed award of)        |
|----------------|------------------------------------------------------|
| Main Stage     | PANEL (entry + exit scanned) or QMETER (verified)     |
| Masterclass    | MASTERCLASS (door entry + exit scanned)               |
| AI Playground  | PLAYGROUND (booth staff scanned the session's end)    |
| Connection Hub | CONNECTION (mutual scan that paid) or MENTOR (host)   |

Profile completion is not a fifth zone. Activity that earned nothing (a ninth
booth, a same-company connection, a session after 17:30) is evidence of
attendance but not of a qualifying, scored activity, so it does not complete a
zone.

The bonus is DERIVED, never entered by hand: `recompute` runs inside the same
transaction as every change of a person's competition awards (an award, a
reversal of an overlapping panel, a better connection replacing a weaker one,
an administrator's reversal). When the evidence is complete and no live Founder
award exists, it pays +10 (dated by the moment the fourth zone was reached, so
the 17:30 close applies to that moment); when a correction removes the evidence,
the live Founder award is reversed by an append-only REVERSAL entry. Lucky Draw
eligibility is exactly: a live Founder award and competition eligibility.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, aliased

from app.modules.points.models import PointsEntryType, PointsLog, PointsSource

ZONES: Final[dict[str, tuple[PointsSource, ...]]] = {
    "main_stage": (PointsSource.PANEL, PointsSource.QMETER),
    "masterclass": (PointsSource.MASTERCLASS,),
    "playground": (PointsSource.PLAYGROUND,),
    "connection_hub": (PointsSource.CONNECTION, PointsSource.MENTOR),
}
_ZONE_OF: Final = {source: zone for zone, sources in ZONES.items() for source in sources}


def _live_awards(user_id: int, sources: tuple[PointsSource, ...]) -> Select[PointsLog]:
    reversal = aliased(PointsLog)
    return (
        select(PointsLog)
        .outerjoin(reversal, reversal.reverses_points_log_id == PointsLog.id)
        .where(
            PointsLog.user_id == user_id,
            PointsLog.source.in_(sources),
            PointsLog.entry_type == PointsEntryType.AWARD,
            PointsLog.points > 0,
            reversal.id.is_(None),
        )
    )


def occurred(row: PointsLog) -> datetime:
    """When the activity behind an award happened: the verified occurrence time
    (migration 0027), or - for rows recorded before it existed - the recording
    time, which is exactly what those rows were evaluated by."""
    return row.occurred_at or row.created_at


def zones(db: Session, user_id: int) -> dict[str, datetime | None]:
    """For each zone, when its first live qualifying activity happened."""
    reached: dict[str, datetime | None] = dict.fromkeys(ZONES)
    for row in db.execute(_live_awards(user_id, tuple(_ZONE_OF))).scalars():
        zone = _ZONE_OF[row.source]
        first = reached[zone]
        if first is None or occurred(row) < first:
            reached[zone] = occurred(row)
    return reached


def live_founder(db: Session, user_id: int) -> PointsLog | None:
    return db.execute(_live_awards(user_id, (PointsSource.FOUNDER,)).limit(1)).scalars().first()


def rule_confirmed() -> bool:
    from app.core.config import get_settings

    return get_settings().founder_rule_confirmed


def recompute(db: Session, user_id: int) -> str:
    """Bring the derived Founder bonus in line with the evidence. The caller holds
    the person's points lock (taken again here; it is re-entrant).

    Gated: while the qualification rule is unconfirmed nothing is awarded or
    reversed here (`gated`); once confirmed, the next change of each person's
    awards brings their bonus in line."""
    from app.modules.competition.ledger import award, lock_attendee_points
    from app.modules.competition.rules import FOUNDER_POINTS

    if not rule_confirmed():
        return "gated"
    lock_attendee_points(db, user_id)
    reached = zones(db, user_id)
    complete = all(reached.values())
    current = live_founder(db, user_id)
    if complete and current is None:
        earlier = int(
            db.execute(
                select(func.count())
                .select_from(PointsLog)
                .where(
                    PointsLog.user_id == user_id,
                    PointsLog.source == PointsSource.FOUNDER,
                    PointsLog.entry_type == PointsEntryType.AWARD,
                )
            ).scalar_one()
        )
        moment = max(t for t in reached.values() if t is not None)
        return award(
            db,
            user_id=user_id,
            source=PointsSource.FOUNDER,
            unit=f"award:{earlier + 1}",  # a re-earned bonus after a correction is a new unit
            points=FOUNDER_POINTS,
            occurred_at=moment,
            note="All four zones completed.",
        ).outcome
    if not complete and current is not None:
        db.add(
            PointsLog(
                user_id=user_id,
                source=PointsSource.FOUNDER,
                entry_type=PointsEntryType.REVERSAL,
                points=-current.points,
                reverses_points_log_id=current.id,
                entered_by_system=True,
                note="A correction removed the evidence of one of the four zones.",
            )
        )
        db.flush()
        return "reversed"
    return "unchanged"


def recompute_all(db: Session, *, apply: bool) -> dict[str, int]:
    """After the organiser confirms the rule: bring EVERY person's bonus in line,
    including people who will receive no further award (the per-award hook would
    never reach them). `apply=False` reports what would change and writes nothing.
    Each person is handled under their own points lock, in the caller's fenced
    transaction; the ledger stays append-only (awards and reversals only)."""
    from app.modules.attendees.models import User, UserStatus

    counts = {"would_award": 0, "would_reverse": 0, "unchanged": 0, "awarded": 0, "reversed": 0}
    candidates = db.execute(
        select(PointsLog.user_id)
        .join(User, User.id == PointsLog.user_id)
        .where(
            PointsLog.source.in_((*_ZONE_OF, PointsSource.FOUNDER)),
            User.status == UserStatus.ACTIVE,
        )
        .distinct()
        .order_by(PointsLog.user_id)
    ).scalars()
    for user_id in list(candidates):
        complete = all(zones(db, user_id).values())
        current = live_founder(db, user_id)
        if complete == (current is not None):
            counts["unchanged"] += 1
            continue
        counts["would_award" if complete else "would_reverse"] += 1
        if apply:
            outcome = recompute(db, user_id)
            if outcome == "awarded":
                counts["awarded"] += 1
            elif outcome == "reversed":
                counts["reversed"] += 1
    return counts
