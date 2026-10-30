"""Fenced schedule writes. All attendance and awards belong to the scan flow.

Capacity includes unexpired invitations. Every member owns their own seat, so
one member leaving does not cancel the others. Invitation links contain no
contact information, and never support a public directory/telephone search.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DomainError, StaffNotAuthorizedError
from app.core.ratelimit import RateLimit, limiter
from app.modules.attendees.models import User
from app.modules.booths.models import Booth
from app.modules.masterclasses.models import (
    MasterclassBlock,
    MasterclassEvent,
    MasterclassReservation,
)
from app.modules.playground.models import PlaygroundBooking as Booking
from app.modules.playground.models import PlaygroundSlot as Slot
from app.modules.staff.models import StaffAccount, StaffRole

ACTIVE = ("CONFIRMED", "CHECKED_IN", "COMPLETED")
LIMIT = RateLimit("playground:attendee", 60, 600)


class PlaygroundError(DomainError):
    code = "PLAYGROUND_CONFLICT"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.details = {"reason": reason}


class PlaygroundMissing(DomainError):
    code = "PLAYGROUND_NOT_FOUND"
    status_code = 404


def now() -> datetime:
    return datetime.now(UTC)


def change(row: Booking, status: str, stamp: datetime) -> None:
    row.status = status
    row.version += 1
    row.updated_at = stamp


def slot_of(db: Session, slot_id: int) -> Slot:
    row = db.get(Slot, slot_id)
    if row is None:
        raise PlaygroundMissing("No such slot.")
    return row


def expire(db: Session, slot_id: int, stamp: datetime) -> None:
    for b in db.scalars(
        select(Booking).where(
            Booking.slot_id == slot_id, Booking.status == "INVITED", Booking.expires_at <= stamp
        )
    ):
        change(b, "EXPIRED", stamp)
    db.flush()


def held(stamp: datetime) -> Any:
    return or_(
        Booking.status.in_(ACTIVE), and_(Booking.status == "INVITED", Booking.expires_at > stamp)
    )


def count(db: Session, slot_id: int, stamp: datetime) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(Booking).where(Booking.slot_id == slot_id, held(stamp))
        )
        or 0
    )


def latest(db: Session, uid: int, sid: int) -> Booking | None:
    return db.scalar(
        select(Booking)
        .where(Booking.user_id == uid, Booking.slot_id == sid)
        .order_by(Booking.id.desc())
        .limit(1)
    )


def revision(row: Booking | None) -> str:
    return "0" if row is None else f"{row.id}:{row.version}"


def check_member(
    db: Session, user: User, slot: Slot, stamp: datetime, *, prebook: bool = True
) -> None:
    if prebook:
        n = (
            db.scalar(
                select(func.count())
                .select_from(Booking)
                .join(Slot)
                .where(
                    Booking.user_id == user.id,
                    Slot.event_id == slot.event_id,
                    Slot.ends_at > stamp,
                    Booking.status.in_(("CONFIRMED", "CHECKED_IN")),
                )
            )
            or 0
        )
        if n >= 3:
            raise PlaygroundError("active_booking_limit")
    overlap = db.scalar(
        select(Booking.id)
        .join(Slot)
        .where(
            Booking.user_id == user.id,
            Slot.event_id == slot.event_id,
            Slot.id != slot.id,
            Slot.starts_at < slot.ends_at,
            Slot.ends_at > slot.starts_at,
            Booking.status.in_(ACTIVE),
        )
        .limit(1)
    )
    if overlap:
        raise PlaygroundError("schedule_overlap")
    # Waiting masterclass choices are reserved programme time too: a promotion
    # must never create an overlapping Playground booking behind the attendee.
    mc = db.scalar(
        select(MasterclassReservation.id)
        .join(MasterclassBlock, MasterclassReservation.block_id == MasterclassBlock.id)
        .where(
            MasterclassReservation.user_id == user.id,
            MasterclassBlock.event_id == slot.event_id,
            MasterclassReservation.status.in_(("CONFIRMED", "WAITLISTED")),
            MasterclassBlock.starts_at < slot.ends_at,
            MasterclassBlock.ends_at > slot.starts_at,
        )
        .limit(1)
    )
    if mc:
        raise PlaygroundError("masterclass_overlap")
    _no_mentor_overlap(db, user.id, slot.event_id, slot.starts_at, slot.ends_at)


def _no_mentor_overlap(
    db: Session, uid: int, event_id: int, starts_at: datetime, ends_at: datetime
) -> None:
    # A confirmed or waiting mentor group reserves its 20 minutes as well.
    from app.modules.mentors.service import overlaps_mentor

    if overlaps_mentor(db, uid, event_id, starts_at, ends_at):
        raise PlaygroundError("mentor_overlap")


def check_masterclass_overlap(db: Session, uid: int, block: MasterclassBlock) -> None:
    if db.scalar(
        select(Booking.id)
        .join(Slot)
        .where(
            Booking.user_id == uid,
            Slot.event_id == block.event_id,
            Slot.starts_at < block.ends_at,
            Slot.ends_at > block.starts_at,
            Booking.status.in_(ACTIVE),
        )
        .limit(1)
    ):
        raise PlaygroundError("playground_overlap")
    _no_mentor_overlap(db, uid, block.event_id, block.starts_at, block.ends_at)


def view(row: Booking) -> dict[str, Any]:
    return {
        "id": row.id,
        "slot_id": row.slot_id,
        "status": row.status.lower(),
        "version": row.version,
        "expires_at": row.expires_at,
    }


def book(
    db: Session, user: User, slot_id: int, command: str, expected: str, queue: bool
) -> dict[str, Any]:
    limiter.check([(LIMIT, str(user.id))])
    slot = slot_of(db, slot_id)
    stamp = now()
    previous = db.scalar(select(Booking).where(Booking.command_id == command))
    if previous:
        if previous.user_id != user.id or previous.slot_id != slot_id:
            raise PlaygroundError("command_conflict")
        return view(previous)
    old = latest(db, user.id, slot.id)
    if revision(old) != expected:
        raise PlaygroundError("choice_changed")
    if old and old.status in (*ACTIVE, "WAITLISTED"):
        return view(old)
    if stamp >= slot.ends_at:
        raise PlaygroundError("slot_closed")
    if not queue and (slot.number > 34 or stamp >= slot.starts_at - timedelta(minutes=15)):
        raise PlaygroundError("booking_closed")
    expire(db, slot.id, stamp)
    check_member(db, user, slot, stamp, prebook=not queue)
    if not queue and count(db, slot.id, stamp) >= 3:
        raise PlaygroundError("slot_full")
    row = Booking(
        slot_id=slot.id,
        user_id=user.id,
        command_id=command,
        status="WAITLISTED" if queue else "CONFIRMED",
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.flush()
    return view(row)


def cancel(db: Session, user: User, booking_id: int, expected: int) -> dict[str, Any]:
    b = db.get(Booking, booking_id)
    if b is None or (
        b.user_id != user.id and not (b.status == "INVITED" and b.inviter_id == user.id)
    ):
        raise PlaygroundMissing("No such booking.")
    if b.status == "CANCELLED":
        return view(b)
    if b.version != expected:
        raise PlaygroundError("choice_changed")
    slot = slot_of(db, b.slot_id)
    stamp = now()
    if b.status != "WAITLISTED" and stamp >= slot.starts_at - timedelta(minutes=15):
        raise PlaygroundError("cancellation_closed")
    if b.status not in ("CONFIRMED", "WAITLISTED", "INVITED"):
        raise PlaygroundError("booking_closed")
    change(b, "CANCELLED", stamp)
    # Only unaccepted holds are linked to the inviter. Accepted members own
    # independent seats, so cancelling the captain never cancels their places.
    if b.user_id == user.id:
        for pending in db.scalars(
            select(Booking).where(
                Booking.slot_id == slot.id,
                Booking.inviter_id == user.id,
                Booking.status == "INVITED",
            )
        ):
            change(pending, "CANCELLED", stamp)
    db.flush()
    return view(b)


def invitation_token(b: Booking) -> str:
    # Domain-separated HMAC: another token type cannot be replayed as an invite.
    payload = f"playground-invite-v1:{b.id}:{b.command_id}"
    sig = hmac.new(
        get_settings().scan_ref_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()
    return f"{b.id}.{sig}"


def invitation(db: Session, token: str) -> Booking:
    try:
        raw_id, _ = token.split(".", 1)
        bid = int(raw_id)
        if not 0 < bid < 2**63:
            raise ValueError
    except ValueError:
        raise PlaygroundMissing("Invitation unavailable.") from None
    b = db.get(Booking, bid)
    if b is None or not hmac.compare_digest(invitation_token(b), token):
        raise PlaygroundMissing("Invitation unavailable.")
    return b


def invite(db: Session, user: User, slot_id: int, command: str) -> dict[str, Any]:
    limiter.check([(LIMIT, str(user.id))])
    slot = slot_of(db, slot_id)
    stamp = now()
    mine = latest(db, user.id, slot.id)
    if mine is None or mine.status != "CONFIRMED":
        raise PlaygroundError("confirmed_booking_required")
    previous = db.scalar(select(Booking).where(Booking.command_id == command))
    if previous:
        if previous.inviter_id != user.id or previous.slot_id != slot.id:
            raise PlaygroundError("command_conflict")
        return {**view(previous), "token": invitation_token(previous)}
    expiry = min(stamp + timedelta(minutes=30), slot.starts_at - timedelta(minutes=15))
    if expiry <= stamp or slot.number > 34:
        raise PlaygroundError("booking_closed")
    expire(db, slot.id, stamp)
    if count(db, slot.id, stamp) >= 3:
        raise PlaygroundError("slot_full")
    b = Booking(
        slot_id=slot.id,
        inviter_id=user.id,
        command_id=command,
        status="INVITED",
        expires_at=expiry,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(b)
    db.flush()
    return {**view(b), "token": invitation_token(b)}


def accept(db: Session, user: User, token: str) -> dict[str, Any]:
    limiter.check([(LIMIT, str(user.id))])
    b = invitation(db, token)
    stamp = now()
    expire(db, b.slot_id, stamp)
    if b.user_id == user.id and b.status == "CONFIRMED":
        return view(b)
    if b.status != "INVITED" or b.expires_at is None or b.expires_at <= stamp:
        raise PlaygroundError("invitation_expired")
    if b.inviter_id == user.id:
        raise PlaygroundError("self_invitation")
    slot = slot_of(db, b.slot_id)
    old = latest(db, user.id, slot.id)
    if old and old.status in (*ACTIVE, "WAITLISTED"):
        raise PlaygroundError("already_booked")
    check_member(db, user, slot, stamp)
    b.user_id = user.id
    change(b, "CONFIRMED", stamp)
    db.flush()
    return view(b)


def staff_scope(staff: StaffAccount, slot: Slot) -> None:
    if staff.role != StaffRole.ADMIN and staff.booth_id != slot.booth_id:
        raise StaffNotAuthorizedError("This slot belongs to another booth.")


LATE_AFTER = timedelta(minutes=3)


def release_no_shows(db: Session, slot: Slot, stamp: datetime) -> list[int]:
    """More than 3 minutes after the start, a reserved place nobody has claimed
    goes back to the on-site queue (PDF: "3 dəqiqədən çox gecikəndə yer növbəyə
    keçir"). Derived from the clock and idempotent; it only ever runs inside a
    staff admission that commits."""
    if stamp <= slot.starts_at + LATE_AFTER:
        return []
    released = list(
        db.scalars(select(Booking).where(Booking.slot_id == slot.id, Booking.status == "CONFIRMED"))
    )
    for row in released:
        change(row, "NO_SHOW", stamp)
    db.flush()
    return [row.id for row in released]


def admit(db: Session, staff: StaffAccount, bid: int, expected: int) -> dict[str, Any]:
    """Booth staff confirm that a person is at the booth for this session.

    Outcomes (all committed, nothing half-done):
    * a reserved attendee within 3 minutes of the start, or the head of the
      on-site queue while a place is free -> ``checked_in``;
    * a reserved attendee arriving later than that -> ``no_show`` with
      ``admitted: false``: their place (and every other unclaimed one) is
      released to the queue, and they may join the queue again like anyone else.
    Admission is never points: that belongs to the end-of-session scan."""
    b = db.get(Booking, bid)
    if b is None:
        raise PlaygroundMissing("No such booking.")
    slot = slot_of(db, b.slot_id)
    staff_scope(staff, slot)
    stamp = now()
    if b.status == "CHECKED_IN":
        return {**view(b), "admitted": True}
    if b.version != expected:
        raise PlaygroundError("choice_changed")
    if b.status not in ("CONFIRMED", "WAITLISTED"):
        raise PlaygroundError("join_queue_again")
    if not slot.starts_at <= stamp < slot.ends_at:
        raise PlaygroundError("outside_slot")
    expire(db, slot.id, stamp)
    release_no_shows(db, slot, stamp)
    if b.status == "NO_SHOW":  # this reserved attendee was late: released above
        return {**view(b), "admitted": False, "reason": "late_arrival"}
    if b.status == "WAITLISTED":
        first = db.scalar(
            select(Booking.id)
            .where(Booking.slot_id == slot.id, Booking.status == "WAITLISTED")
            .order_by(Booking.created_at, Booking.id)
            .limit(1)
        )
        if b.id != first:
            raise PlaygroundError("queue_order")
        if count(db, slot.id, stamp) >= 3:
            raise PlaygroundError("slot_full")
        assert b.user_id is not None
        user = db.get(User, b.user_id)
        assert user is not None
        check_member(db, user, slot, stamp, prebook=False)
    elif b.status != "CONFIRMED":
        raise PlaygroundError("join_queue_again")
    change(b, "CHECKED_IN", stamp)
    db.flush()
    return {**view(b), "admitted": True}


def prepare(db: Session, event_id: int, booth_id: int) -> dict[str, Any]:
    event = db.get(MasterclassEvent, event_id)
    booth = db.get(Booth, booth_id)
    if event is None or booth is None or not booth.is_active:
        raise PlaygroundMissing("Event or booth unavailable.")
    start = datetime.combine(event.event_date, time(9), ZoneInfo(event.timezone))
    created = 0
    for i in range(36):
        if db.scalar(
            select(Slot.id).where(
                Slot.event_id == event_id, Slot.booth_id == booth_id, Slot.number == i + 1
            )
        ):
            continue
        db.add(
            Slot(
                event_id=event_id,
                booth_id=booth_id,
                number=i + 1,
                starts_at=start + timedelta(minutes=15 * i),
                ends_at=start + timedelta(minutes=15 * (i + 1)),
            )
        )
        created += 1
    db.flush()
    return {"id": event_id, "created": created, "booth_id": booth_id}


def programme(db: Session, uid: int | None = None, booth_id: int | None = None) -> dict[str, Any]:
    stamp = now()
    query = (
        select(Slot, Booth.name, Booth.description, Booth.slug, MasterclassEvent.timezone)
        .join(Booth)
        .join(MasterclassEvent, Slot.event_id == MasterclassEvent.id)
        .where(Booth.is_active)
        .order_by(Slot.starts_at, Slot.booth_id)
    )
    if booth_id is not None:
        query = query.where(Slot.booth_id == booth_id)
    slot_rows = list(db.execute(query))
    ids = [slot.id for slot, _, _, _, _ in slot_rows]
    own_query = (
        select(Booking)
        .where(Booking.slot_id.in_(ids), or_(Booking.user_id == uid, Booking.inviter_id == uid))
        .order_by(Booking.created_at, Booking.id)
    )
    bookings = list(db.scalars(own_query)) if uid else []
    counts = dict(
        db.execute(
            select(Booking.slot_id, func.count())
            .where(Booking.slot_id.in_(ids), held(stamp))
            .group_by(Booking.slot_id)
        ).all()
    )
    waiting = (
        select(
            Booking.slot_id,
            Booking.user_id,
            func.row_number()
            .over(partition_by=Booking.slot_id, order_by=(Booking.created_at, Booking.id))
            .label("position"),
        )
        .where(Booking.slot_id.in_(ids), Booking.status == "WAITLISTED")
        .subquery()
    )
    positions: dict[int, int] = (
        dict(
            db.execute(
                select(waiting.c.slot_id, waiting.c.position).where(waiting.c.user_id == uid)
            ).all()
        )
        if uid
        else {}
    )
    items = []
    for slot, name, description, slug, zone in slot_rows:
        members = [b for b in bookings if b.slot_id == slot.id]
        current = [b for b in members if b.user_id == uid] if uid else []
        own = current[-1] if current else None
        holds = (
            [
                b
                for b in members
                if b.inviter_id == uid
                and b.status == "INVITED"
                and b.expires_at
                and b.expires_at > stamp
            ]
            if uid
            else []
        )
        occupied = counts.get(slot.id, 0)
        items.append(
            {
                "id": slot.id,
                "event_id": slot.event_id,
                "booth_id": slot.booth_id,
                "booth": name,
                # The booth's one-sentence card (PDF: "stend kartında göstərilir").
                "booth_description": description,
                "booth_slug": slug,
                "number": slot.number,
                "starts_at": slot.starts_at,
                "ends_at": slot.ends_at,
                "timezone": zone,
                "seats_left": max(0, 3 - occupied),
                "bookable": slot.number <= 34 and stamp < slot.starts_at - timedelta(minutes=15),
                "queue_open": stamp < slot.ends_at,
                "mine": view(own) if own else None,
                "revision": revision(own),
                "cancellable": bool(
                    own
                    and own.status in ("CONFIRMED", "WAITLISTED")
                    and (
                        own.status == "WAITLISTED" or stamp < slot.starts_at - timedelta(minutes=15)
                    )
                ),
                "invitations": [{**view(b), "token": invitation_token(b)} for b in holds],
                "queue_position": positions.get(slot.id),
            }
        )
    return {"server_time": stamp, "slots": items}


def staff_list(db: Session, staff: StaffAccount, slot_id: int) -> dict[str, Any]:
    slot = slot_of(db, slot_id)
    staff_scope(staff, slot)
    rows = db.execute(
        select(Booking, User.first_name, User.last_name)
        .join(User, Booking.user_id == User.id)
        .where(Booking.slot_id == slot_id)
        .order_by(Booking.created_at, Booking.id)
    ).all()
    return {
        "slot_id": slot_id,
        "bookings": [{**view(b), "name": f"{first} {last}"} for b, first, last in rows],
    }
