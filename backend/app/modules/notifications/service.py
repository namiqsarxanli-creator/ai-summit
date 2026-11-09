"""The attendee's notifications (FACEMARK, 9 Nov 2026): one list in the app of what the
emails remind about and what other people did that needs an answer.

Nothing is stored: every item is derived on read from the rows that already exist,
so a notification can never disagree with the real state (a cancelled booking's
reminder simply disappears, an answered request stops asking). Whether an item was
seen is the device's own business (the app keeps the time it last opened the list).

Kinds (each with a stable `id`, the time it became relevant `at`, an in-app `href`
and the `params` its sentence needs):

* `programme_eve`   - from 18:00 the evening before the event: the day's sessions
* `programme_soon`  - an own session (masterclass, Playground, meeting, mentor
                      group) starting within the hour, until it ends
* `grand_final_open` - the Grand Final code is open now
* `final_published` - the final result is out
* `scan_back`       - someone scanned my QR and I have not scanned theirs yet
* `connected`       - a connection with someone
* `meeting_request` - someone asks to meet me (still unanswered)
* `meeting_answer`  - my request was accepted or declined
* `pair_photo`      - someone shared a photo of us two, waiting for my answer
* `pair_photo_accepted` - my photo was accepted
* `points`          - points written to (or taken back from) my ledger

People I blocked, and people who are no longer active, never appear.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.modules.attendees.models import User, UserStatus
from app.modules.competition.final_models import FinalResult
from app.modules.competition.models import Panel
from app.modules.competition.programme import programme
from app.modules.competition.rules import GRAND_FINAL_CODE
from app.modules.masterclasses.models import MasterclassEvent
from app.modules.people.models import Connection, ConnectionScan, MeetingRequest, UserBlock
from app.modules.people.photo_models import PairPhoto
from app.modules.points.models import PointsEntryType, PointsLog, PointsSource

SOON: Final = timedelta(hours=1)
EVE_AT: Final = time(18, 0)  # the evening before, as the reminder email
LIMIT: Final = 60
_OWN = {"confirmed", "scheduled", "checked_in"}  # a place of one's own, not a waiting one
_HISTORY = {PointsSource.TASK, PointsSource.FEEDBACK}  # not the competition


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def _name(u: User) -> str:
    return f"{u.first_name} {u.last_name[:1]}." if u.last_name else u.first_name


def _item(kind: str, ref: object, at: datetime, href: str, **params: Any) -> dict[str, Any]:
    return {
        "id": f"{kind}:{ref}",
        "kind": kind,
        "at": _iso(at),
        "href": href,
        "params": params,
        "_at": at,
    }


def _people(db: Session, me: int, ids: set[int]) -> dict[int, User]:
    if not ids:
        return {}
    blocked = set(
        db.execute(select(UserBlock.blocked_id).where(UserBlock.blocker_id == me)).scalars()
    )
    rows = db.execute(
        select(User).where(User.id.in_(ids - blocked), User.status == UserStatus.ACTIVE)
    ).scalars()
    return {u.id: u for u in rows}


def _programme(db: Session, me: int, now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()
    day = programme(db, me)
    own = [i for i in day["items"] if i["status"] in _OWN]
    if event is not None:
        zone = ZoneInfo(event.timezone)
        event_day: date = event.event_date
        eve = datetime.combine(event_day - timedelta(days=1), EVE_AT, zone)
        over = datetime.combine(event_day + timedelta(days=1), time(0), zone)
        if eve <= now < over:
            out.append(
                _item("programme_eve", event_day.isoformat(), eve, "/programme", count=len(own))
            )
    for i in own:
        start, end = datetime.fromisoformat(i["starts_at"]), datetime.fromisoformat(i["ends_at"])
        if start - SOON <= now < end:
            out.append(
                _item(
                    "programme_soon",
                    f"{i['kind']}:{i['id']}",
                    start - SOON,
                    "/programme",
                    item=i["kind"],
                    title=i["title"],
                    place=i["place"],
                    starts_at=_iso(start),
                )
            )
    return out


def _competition(db: Session, me: int, now: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    gf = db.execute(select(Panel).where(Panel.code == GRAND_FINAL_CODE)).scalar_one_or_none()
    if gf is not None and gf.starts_at <= now < gf.ends_at:
        out.append(
            _item(
                "grand_final_open",
                _iso(gf.starts_at),
                gf.starts_at,
                "/points",
                ends_at=_iso(gf.ends_at),
            )
        )
    final = db.execute(
        select(FinalResult)
        .where(FinalResult.status == "PUBLISHED")
        .order_by(FinalResult.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if final is not None and final.published_at is not None:
        out.append(_item("final_published", final.version, final.published_at, "/points"))
    entries = db.execute(
        select(PointsLog)
        .where(PointsLog.user_id == me, PointsLog.source.not_in(_HISTORY))
        .order_by(PointsLog.id.desc())
        .limit(30)
    ).scalars()
    for e in entries:
        out.append(
            _item(
                "points",
                e.id,
                e.created_at,
                "/points",
                source=e.source.value,
                points=e.points,
                reversal=e.entry_type == PointsEntryType.REVERSAL,
            )
        )
    return out


def _social(db: Session, me: int) -> list[dict[str, Any]]:
    scans = list(
        db.execute(
            select(ConnectionScan).where(
                or_(ConnectionScan.scanner_id == me, ConnectionScan.scanned_id == me)
            )
        ).scalars()
    )
    mine = {s.scanned_id for s in scans if s.scanner_id == me}
    waiting = [s for s in scans if s.scanned_id == me and s.scanner_id not in mine]
    connections = list(
        db.execute(
            select(Connection).where(
                or_(Connection.user_low_id == me, Connection.user_high_id == me)
            )
        ).scalars()
    )
    asks = list(
        db.execute(
            select(MeetingRequest).where(
                or_(
                    and_(MeetingRequest.recipient_id == me, MeetingRequest.status == "PENDING"),
                    and_(
                        MeetingRequest.requester_id == me,
                        MeetingRequest.status.in_(("ACCEPTED", "DECLINED")),
                    ),
                )
            )
        ).scalars()
    )
    photos = list(
        db.execute(
            select(PairPhoto).where(
                or_(
                    and_(
                        PairPhoto.partner_id == me,
                        PairPhoto.status == "PENDING",
                        PairPhoto.photo_key.is_not(None),
                    ),
                    and_(PairPhoto.uploader_id == me, PairPhoto.status == "ACCEPTED"),
                )
            )
        ).scalars()
    )
    ids = (
        {s.scanner_id for s in waiting}
        | {c.user_high_id if c.user_low_id == me else c.user_low_id for c in connections}
        | {r.requester_id if r.recipient_id == me else r.recipient_id for r in asks}
        | {p.uploader_id if p.partner_id == me else p.partner_id for p in photos}
    )
    who = _people(db, me, ids)
    out: list[dict[str, Any]] = []
    for s in waiting:
        if s.scanner_id in who:
            out.append(
                _item("scan_back", s.id, s.scanned_at, "/my-card", name=_name(who[s.scanner_id]))
            )
    for c in connections:
        other = c.user_high_id if c.user_low_id == me else c.user_low_id
        if other in who:
            out.append(
                _item("connected", c.id, c.established_at, "/people", name=_name(who[other]))
            )
    for r in asks:
        if r.recipient_id == me and r.requester_id in who:
            out.append(
                _item(
                    "meeting_request",
                    r.id,
                    r.created_at,
                    "/people",
                    name=_name(who[r.requester_id]),
                )
            )
        elif r.requester_id == me and r.recipient_id in who and r.responded_at is not None:
            out.append(
                _item(
                    "meeting_answer",
                    r.id,
                    r.responded_at,
                    "/people",
                    name=_name(who[r.recipient_id]),
                    accepted=r.status == "ACCEPTED",
                )
            )
    for p in photos:
        if p.partner_id == me and p.uploader_id in who:
            out.append(
                _item("pair_photo", p.id, p.created_at, "/people", name=_name(who[p.uploader_id]))
            )
        elif p.uploader_id == me and p.partner_id in who and p.accepted_at is not None:
            out.append(
                _item(
                    "pair_photo_accepted",
                    p.id,
                    p.accepted_at,
                    "/people",
                    name=_name(who[p.partner_id]),
                )
            )
    return out


def notifications(db: Session, me: int, now: datetime) -> dict[str, Any]:
    items = _programme(db, me, now) + _competition(db, me, now) + _social(db, me)
    items.sort(key=lambda i: (i["_at"], i["id"]), reverse=True)
    for i in items:
        del i["_at"]
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()
    return {
        "server_time": _iso(now),
        "timezone": event.timezone if event else "UTC",
        "items": items[:LIMIT],
    }
