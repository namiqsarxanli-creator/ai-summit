"""The attendee's own chronological programme (Beyond Now, phase F).

One list, in event time order, of what the person has actually got: masterclass
choices (a waiting place included - it reserves the time), Playground places
(booked, admitted, completed, or the on-site queue), confirmed Match Corner
meetings and mentor groups. Each item carries its real status. `conflict` marks
items whose time overlaps another live item - normally impossible, because every
booking path refuses an overlap under the shared schedule lock, so a conflict
here is a signal for the organisers (Match Corner meetings, which the PDF allows
during masterclass time, are the expected exception).

Read-only, a fixed number of queries, no contact data (the other person of a
meeting is shown as first name and surname initial).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.modules.attendees.models import User
from app.modules.booths.models import Booth
from app.modules.competition.models import MasterclassAttendance
from app.modules.masterclasses.models import (
    Masterclass,
    MasterclassBlock,
    MasterclassEvent,
    MasterclassReservation,
    MasterclassRoom,
)
from app.modules.mentors.models import MentorBooking, MentorSession
from app.modules.people.models import MatchTable, Meeting, MeetingParticipant
from app.modules.playground.models import PlaygroundBooking, PlaygroundSlot

PLAYGROUND_SHOWN = ("CONFIRMED", "WAITLISTED", "CHECKED_IN", "COMPLETED")
# A completed or attended item is history, not a live claim on the time.
_LIVE = {"confirmed", "waitlisted", "checked_in", "entered", "scheduled"}


def _short(first: str, last: str) -> str:
    return f"{first} {last[:1]}." if last else first


def _item(
    kind: str, ref: int, title: str, place: str | None, start: datetime, end: datetime, status: str
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": ref,
        "title": title,
        "place": place,
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "status": status,
        "_start": start,
        "_end": end,
    }


def programme(db: Session, user_id: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    attended = {
        mc_id: exit_at
        for mc_id, exit_at in db.execute(
            select(MasterclassAttendance.masterclass_id, MasterclassAttendance.exit_at).where(
                MasterclassAttendance.user_id == user_id
            )
        )
    }
    for res, mc, block, room in db.execute(
        select(MasterclassReservation, Masterclass, MasterclassBlock, MasterclassRoom)
        .join(Masterclass, Masterclass.id == MasterclassReservation.masterclass_id)
        .join(MasterclassBlock, MasterclassBlock.id == MasterclassReservation.block_id)
        .outerjoin(MasterclassRoom, MasterclassRoom.id == Masterclass.room_id)
        .where(
            MasterclassReservation.user_id == user_id,
            MasterclassReservation.status.in_(("CONFIRMED", "WAITLISTED")),
        )
    ):
        status = res.status.lower()
        if mc.id in attended:
            status = "attended" if attended[mc.id] is not None else "entered"
        items.append(
            _item(
                "masterclass",
                mc.id,
                mc.title,
                room.name if room else None,
                block.starts_at,
                block.ends_at,
                status,
            )
        )
    # Attendance without a reservation (standing, the door queue) is shown too.
    shown = {i["id"] for i in items if i["kind"] == "masterclass"}
    for att, mc, block in db.execute(
        select(MasterclassAttendance, Masterclass, MasterclassBlock)
        .join(Masterclass, Masterclass.id == MasterclassAttendance.masterclass_id)
        .join(MasterclassBlock, MasterclassBlock.id == Masterclass.block_id)
        .where(MasterclassAttendance.user_id == user_id)
    ):
        if mc.id not in shown:
            status = "attended" if att.exit_at is not None else "entered"
            items.append(
                _item("masterclass", mc.id, mc.title, None, block.starts_at, block.ends_at, status)
            )

    for booking, slot, booth in db.execute(
        select(PlaygroundBooking, PlaygroundSlot, Booth)
        .join(PlaygroundSlot, PlaygroundSlot.id == PlaygroundBooking.slot_id)
        .join(Booth, Booth.id == PlaygroundSlot.booth_id)
        .where(
            PlaygroundBooking.user_id == user_id,
            PlaygroundBooking.status.in_(PLAYGROUND_SHOWN),
        )
    ):
        items.append(
            _item(
                "playground",
                booking.id,
                booth.name,
                booth.location_label,
                slot.starts_at,
                slot.ends_at,
                booking.status.lower(),
            )
        )

    me = aliased(MeetingParticipant)
    other = aliased(MeetingParticipant)
    for meeting, table, first, last in db.execute(
        select(Meeting, MatchTable, User.first_name, User.last_name)
        .join(me, (me.meeting_id == Meeting.id) & (me.user_id == user_id))
        .join(other, (other.meeting_id == Meeting.id) & (other.user_id != user_id))
        .join(User, User.id == other.user_id)
        .join(MatchTable, MatchTable.id == Meeting.table_id)
        .where(Meeting.cancelled_at.is_(None), me.active.is_(True))
    ):
        items.append(
            _item(
                "meeting",
                meeting.id,
                _short(first, last),
                table.label,
                meeting.starts_at,
                meeting.ends_at,
                "scheduled",
            )
        )

    for mentor_booking, session in db.execute(
        select(MentorBooking, MentorSession)
        .join(MentorSession, MentorSession.id == MentorBooking.session_id)
        .where(MentorBooking.user_id == user_id, MentorBooking.status != "CANCELLED")
    ):
        items.append(
            _item(
                "mentor",
                mentor_booking.id,
                session.title,
                session.mentor_name,
                session.starts_at,
                session.ends_at,
                mentor_booking.status.lower(),
            )
        )

    items.sort(key=lambda i: (i["_start"], i["_end"], i["kind"], i["id"]))
    live = [i for i in items if i["status"] in _LIVE]
    for a in live:
        a["conflict"] = any(
            b is not a and b["_start"] < a["_end"] and b["_end"] > a["_start"] for b in live
        )
    for i in items:
        i.setdefault("conflict", False)
        del i["_start"], i["_end"]
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()
    return {"timezone": event.timezone if event else "UTC", "items": items}
