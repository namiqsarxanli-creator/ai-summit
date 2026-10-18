"""The venue's big screen during the day (PDF: "canlı top 10, son xal hərəkətləri,
17:30-a geri sayım, birgə şəkillər").

One public, read-only, minimal answer for `GET /api/screen/live`. It carries no
contact data, no user id, no email/phone/QR token, no staff or audit detail and
never a photo that is not consented to by both people AND approved by a
moderator. A person is shown as first name and surname initial (app/core/names).

States, decided by the server:

* `not_configured` - the programme is not set up for the competition: no event,
  no 17:30 close, or the competition ranking is not switched on. `missing` names
  what is absent. Nothing is shown as if the competition were running.
* `open` - before the close: the live top 10 (people with points only), the most
  recent standing awards, the countdown basis (`server_time`, `closes_at`).
* `review` - after the close, until a Game Master publishes: no ranking (the
  results are being checked), the photos stay.
* `published` - a final result is published: the screen shows it
  (`GET /api/final`); nothing is recomputed here.

The top 10 is read from the leaderboard's shared ranking snapshot (one ranking
per change for every reader, leaderboard/service.py), with the same tie-break as
everywhere (`reached_at`). The two other reads are small and indexed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.core.names import short_name
from app.modules.attendees.models import User, UserStatus
from app.modules.competition.final import public as public_final
from app.modules.competition.founder import rule_confirmed
from app.modules.competition.ledger import closes_at
from app.modules.competition.rules import COMPETITION_SOURCES
from app.modules.leaderboard.service import competition_mode, shared_ranking
from app.modules.masterclasses.models import MasterclassEvent
from app.modules.people.pair_photos import feed as photo_feed
from app.modules.points.models import PointsEntryType, PointsLog, PointsSource

TOP: Final = 10
RECENT: Final = 8
DEFAULT_TIMEZONE: Final = "Asia/Baku"


def _top(db: Session) -> list[dict[str, Any]]:
    snapshot = shared_ranking(db)
    return [
        {
            "place": e.rank,
            "name": e.name,
            "points": e.total_points,
            "photo_url": e.photo_url,
        }
        for e in snapshot.entries
        if e.total_points > 0
    ][:TOP]


def _recent(db: Session) -> list[dict[str, Any]]:
    """The newest awards that still stand, of competing people only. A Founder
    bonus is not named while its rule is unconfirmed (as on the final)."""
    reversal = aliased(PointsLog)
    sources = tuple(s for s in COMPETITION_SOURCES if s != PointsSource.FOUNDER or rule_confirmed())
    rows = db.execute(
        select(
            User.first_name,
            User.last_name,
            PointsLog.points,
            PointsLog.source,
            PointsLog.created_at,
        )
        .join(User, User.id == PointsLog.user_id)
        .outerjoin(reversal, reversal.reverses_points_log_id == PointsLog.id)
        .where(
            PointsLog.source.in_(sources),
            PointsLog.entry_type == PointsEntryType.AWARD,
            reversal.id.is_(None),
            User.status == UserStatus.ACTIVE,
            User.competition_excluded_reason.is_(None),
        )
        .order_by(PointsLog.created_at.desc(), PointsLog.id.desc())
        .limit(RECENT)
    ).all()
    return [
        {
            "name": short_name(first, last),
            "points": int(points),
            "category": source.value,
            "at": at.isoformat(),
        }
        for first, last, points, source, at in rows
    ]


def live(db: Session, now: datetime) -> dict[str, Any]:
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id).limit(1)).scalar()
    close = closes_at(db)
    ranking_on = competition_mode(db)
    final = public_final(db, now)
    missing = [
        name
        for name, absent in (
            ("event", event is None),
            ("close", close is None),
            ("competition_mode", not ranking_on),
        )
        if absent
    ]
    if final["status"] == "published":
        state = "published"
    elif missing:
        state = "not_configured"
    elif close is not None and now < close:
        state = "open"
    else:
        state = "review"
    return {
        "state": state,
        "missing": missing,
        "server_time": now.isoformat(),
        "timezone": event.timezone if event is not None else DEFAULT_TIMEZONE,
        "closes_at": close.isoformat() if close is not None else None,
        "top": _top(db) if state == "open" else [],
        "recent": _recent(db) if state == "open" else [],
        "photos": photo_feed(db),
    }
