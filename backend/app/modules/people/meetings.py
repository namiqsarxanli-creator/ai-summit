"""Meeting requests and Match Corner meetings (PDF "Matching: uyğunluq və görüş").

* A person sends at most 10 requests per event day (Asia/Baku day of the event);
  a retried send (same command id) creates nothing. One open request per pair.
* The recipient accepts or declines; either side can cancel; anyone can block -
  a block also withdraws every open request and future meeting between the two.
* An accepted request gets a 15-minute slot between 09:00 and 17:30 (event time)
  at a configured, active Match Corner table: the first slot from the next
  quarter hour on in which the table AND both people are free. The PDF allows
  meetings during masterclass time too, so only meetings are checked against
  each other; the database refuses a double-booked table or person regardless.
* No active table = no Match Corner (the 13 Nov venue has no room for one): an
  accepted request then gets no table or slot; the two find each other at the
  Connection Hub, and the mutual QR scan is what connects them (and earns).
* Each meeting carries 2-3 discussion prompts, chosen from what the pair share.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.modules.attendees.models import User, UserStatus
from app.modules.masterclasses.models import MasterclassEvent
from app.modules.people.compatibility import blocked_ids, compatibility
from app.modules.people.models import (
    MatchTable,
    Meeting,
    MeetingParticipant,
    MeetingRequest,
    UserBlock,
)

DAILY_REQUESTS = 10
SLOT = timedelta(minutes=15)
OPENS = time(9, 0)
CLOSES = time(17, 30)
_MEETING_LOCK = 21461018


class PeopleError(DomainError):
    code = "PEOPLE_CONFLICT"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.details = {"reason": reason}


class PeopleMissing(DomainError):
    code = "PEOPLE_NOT_FOUND"
    status_code = 404


def _event(db: Session) -> MasterclassEvent:
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()
    if event is None:
        raise PeopleError("not_configured")
    return event


def _day_start(event: MasterclassEvent, now: datetime) -> datetime:
    zone = ZoneInfo(event.timezone)
    local = now.astimezone(zone)
    return datetime.combine(local.date(), time(0), zone).astimezone(UTC)


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


def view(row: MeetingRequest) -> dict[str, Any]:
    return {
        "id": row.id,
        "status": row.status.lower(),
        "requester_id": row.requester_id,
        "recipient_id": row.recipient_id,
    }


def send(db: Session, user: User, recipient_id: int, command: str, now: datetime) -> dict[str, Any]:
    previous = db.execute(
        select(MeetingRequest).where(MeetingRequest.command_id == command)
    ).scalar_one_or_none()
    if previous is not None:
        if previous.requester_id != user.id or previous.recipient_id != recipient_id:
            raise PeopleError("command_conflict")
        return view(previous)
    if recipient_id == user.id:
        raise PeopleError("self")
    recipient = db.get(User, recipient_id)
    if recipient is None or recipient.status != UserStatus.ACTIVE:
        raise PeopleMissing("No such attendee.")
    if recipient_id in blocked_ids(db, user.id):
        raise PeopleMissing("No such attendee.")  # a block is never revealed
    low, high = _pair(user.id, recipient_id)
    open_one = db.execute(
        select(MeetingRequest).where(
            MeetingRequest.pair_low_id == low,
            MeetingRequest.pair_high_id == high,
            MeetingRequest.status.in_(("PENDING", "ACCEPTED")),
        )
    ).scalar_one_or_none()
    if open_one is not None:
        raise PeopleError("already_open" if open_one.requester_id == user.id else "they_asked_you")
    sent_today = int(
        db.execute(
            select(func.count())
            .select_from(MeetingRequest)
            .where(
                MeetingRequest.requester_id == user.id,
                MeetingRequest.created_at >= _day_start(_event(db), now),
            )
        ).scalar_one()
    )
    if sent_today >= DAILY_REQUESTS:
        raise PeopleError("daily_limit")
    row = MeetingRequest(
        requester_id=user.id,
        recipient_id=recipient_id,
        pair_low_id=low,
        pair_high_id=high,
        command_id=command,
        created_at=now,
    )
    db.add(row)
    db.flush()
    return view(row)


def _prompts(a: User, b: User) -> list[str]:
    match = compatibility(a, b, 0)
    keys = [f"topic:{t}" for t in match.shared_topics[:2]]
    if match.complement:
        keys.append(f"goal:{match.complement[0]}")
    for generic in ("intro", "takeaway", "next_step"):
        if len(keys) >= 3:
            break
        keys.append(generic)
    return keys[:3]


def _schedule(db: Session, request: MeetingRequest, now: datetime) -> Meeting | None:
    db.execute(text("SELECT pg_advisory_xact_lock(:c, 0)"), {"c": _MEETING_LOCK})
    event = _event(db)
    zone = ZoneInfo(event.timezone)
    first = datetime.combine(event.event_date, OPENS, zone)
    last_start = datetime.combine(event.event_date, CLOSES, zone) - SLOT
    start = max(first, now.astimezone(zone))
    minutes = start.minute % 15
    if minutes or start.second or start.microsecond:  # the next quarter hour
        start = (start + timedelta(minutes=15 - minutes)).replace(second=0, microsecond=0)
    tables = list(
        db.execute(
            select(MatchTable.id)
            .where(MatchTable.event_id == event.id, MatchTable.is_active.is_(True))
            .order_by(MatchTable.id)
        ).scalars()
    )
    if not tables:  # Match Corner is off: accepted without a table
        return None
    people = (request.requester_id, request.recipient_id)
    slot = start
    while slot <= last_start:
        busy_person = db.execute(
            select(MeetingParticipant.id).where(
                MeetingParticipant.user_id.in_(people),
                MeetingParticipant.starts_at == slot,
                MeetingParticipant.active.is_(True),
            )
        ).first()
        if busy_person is None:
            taken = set(
                db.execute(
                    select(Meeting.table_id).where(
                        Meeting.starts_at == slot, Meeting.cancelled_at.is_(None)
                    )
                ).scalars()
            )
            free = next((t for t in tables if t not in taken), None)
            if free is not None:
                a, b = db.get(User, people[0]), db.get(User, people[1])
                assert a is not None and b is not None
                meeting = Meeting(
                    request_id=request.id,
                    table_id=free,
                    starts_at=slot.astimezone(UTC),
                    ends_at=(slot + SLOT).astimezone(UTC),
                    prompts=_prompts(a, b),
                )
                db.add(meeting)
                db.flush()
                for uid in people:
                    db.add(
                        MeetingParticipant(
                            meeting_id=meeting.id, user_id=uid, starts_at=meeting.starts_at
                        )
                    )
                db.flush()
                return meeting
        slot += SLOT
    raise PeopleError("no_free_slot")


def respond(
    db: Session, user: User, request_id: int, accept: bool, now: datetime
) -> dict[str, Any]:
    row = db.execute(
        select(MeetingRequest).where(MeetingRequest.id == request_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or row.recipient_id != user.id:
        raise PeopleMissing("No such request.")
    target = "ACCEPTED" if accept else "DECLINED"
    if row.status == target:  # a repeated answer
        return view(row)
    if row.status != "PENDING":
        raise PeopleError("not_pending")
    if accept:
        _schedule(db, row, now)  # raises (and changes nothing) if no slot is free
    row.status = target
    row.responded_at = now
    db.flush()
    return view(row)


def _cancel_meetings(db: Session, request_ids: list[int], now: datetime) -> None:
    if not request_ids:
        return
    meetings = list(
        db.execute(
            select(Meeting).where(
                Meeting.request_id.in_(request_ids), Meeting.cancelled_at.is_(None)
            )
        ).scalars()
    )
    for m in meetings:
        m.cancelled_at = now
    if meetings:
        db.execute(
            update(MeetingParticipant)
            .where(MeetingParticipant.meeting_id.in_([m.id for m in meetings]))
            .values(active=False)
        )
    db.flush()


def cancel(db: Session, user: User, request_id: int, now: datetime) -> dict[str, Any]:
    row = db.execute(
        select(MeetingRequest).where(MeetingRequest.id == request_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or user.id not in (row.requester_id, row.recipient_id):
        raise PeopleMissing("No such request.")
    if row.status in ("CANCELLED", "DECLINED"):
        return view(row)
    if row.status == "PENDING" and row.requester_id != user.id:
        raise PeopleError("decline_instead")
    _cancel_meetings(db, [row.id], now)
    row.status = "CANCELLED"
    row.responded_at = now
    db.flush()
    return view(row)


def block(db: Session, user: User, other_id: int, now: datetime) -> dict[str, Any]:
    if other_id == user.id or db.get(User, other_id) is None:
        raise PeopleMissing("No such attendee.")
    exists = db.execute(
        select(UserBlock.id).where(
            UserBlock.blocker_id == user.id, UserBlock.blocked_id == other_id
        )
    ).first()
    if exists is None:
        db.add(UserBlock(blocker_id=user.id, blocked_id=other_id))
    low, high = _pair(user.id, other_id)
    open_rows = list(
        db.execute(
            select(MeetingRequest).where(
                MeetingRequest.pair_low_id == low,
                MeetingRequest.pair_high_id == high,
                MeetingRequest.status.in_(("PENDING", "ACCEPTED")),
            )
        ).scalars()
    )
    _cancel_meetings(db, [r.id for r in open_rows], now)
    for r in open_rows:
        r.status = "CANCELLED"
        r.responded_at = now
    db.flush()
    return {"blocked": True}


def mine(db: Session, user: User) -> dict[str, Any]:
    rows = list(
        db.execute(
            select(MeetingRequest)
            .where(
                or_(MeetingRequest.requester_id == user.id, MeetingRequest.recipient_id == user.id)
            )
            .order_by(MeetingRequest.created_at.desc(), MeetingRequest.id.desc())
        ).scalars()
    )
    others = {r.recipient_id if r.requester_id == user.id else r.requester_id for r in rows}
    people = (
        {u.id: u for u in db.execute(select(User).where(User.id.in_(others))).scalars()}
        if others
        else {}
    )
    meetings = (
        {
            m.request_id: (m, label)
            for m, label in db.execute(
                select(Meeting, MatchTable.label)
                .join(MatchTable, MatchTable.id == Meeting.table_id)
                .where(Meeting.request_id.in_([r.id for r in rows]), Meeting.cancelled_at.is_(None))
            ).all()
        }
        if rows
        else {}
    )
    items = []
    for r in rows:
        other = people.get(r.recipient_id if r.requester_id == user.id else r.requester_id)
        if other is None:
            continue
        meeting = meetings.get(r.id)
        items.append(
            {
                **view(r),
                "direction": "outgoing" if r.requester_id == user.id else "incoming",
                "other": {
                    "user_id": other.id,
                    "name": f"{other.first_name} {other.last_name}",
                    "profession": other.profession,
                },
                "meeting": None
                if meeting is None
                else {
                    "table": meeting[1],
                    "starts_at": meeting[0].starts_at,
                    "ends_at": meeting[0].ends_at,
                    "prompts": list(meeting[0].prompts),
                },
            }
        )
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()
    return {"timezone": event.timezone if event else None, "requests": items}
