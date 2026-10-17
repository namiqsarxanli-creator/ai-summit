"""Meet the Mentor bookings and the host's end-of-session scan (migration 0022).

Explicit policy (PDF "Meet the Mentor", Beyond Now decisions):

* An administrator configures each session (topic, the mentor as announced, the
  time, the group size up to 6, the waiting-list size). Nothing is invented.
* Booking: open sessions only, until the session starts. A free seat makes the
  booking CONFIRMED; when the group is full the attendee joins the FIFO waiting
  list while it has room; otherwise `session_full`.
* An attendee holds at most ONE active (confirmed or waiting) mentor booking at a
  time; cancelling restores the ability to book. A second session after one was
  attended can be booked, but MENTOR points are paid once in total.
* The time of a confirmed OR waiting booking is reserved: it may not overlap a
  confirmed or waiting masterclass choice or an active Playground booking, and
  those modules refuse to overlap it in turn (both directions are checked under
  the shared schedule lock), so a promotion never creates a clash.
* Cancelling is possible until the session starts. A freed confirmed seat goes to
  the first person waiting, in the order they joined.
* The host (a staff account holding the MENTOR_HOST scope for THAT session)
  scans each attendee's QR at the end: from 5 minutes before the end to 10
  minutes after it. A confirmed attendee always has their seat; a waiting one
  present at the end is accepted only while the group stays within its size
  (confirmed + attended). Nobody without a booking is scored. +5 MENTOR, once per
  attendee in total; a repeat scan pays nothing more.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import DomainError, StaffNotAuthorizedError
from app.core.ratelimit import RateLimit, limiter
from app.modules.attendees.models import User
from app.modules.competition.ledger import award
from app.modules.competition.panels import reverse_overlapping_panel_awards
from app.modules.competition.rules import MENTOR_POINTS
from app.modules.competition.scopes import has_scope, require_scope
from app.modules.contact_cards.service import resolve_active_attendee
from app.modules.masterclasses.models import (
    MasterclassBlock,
    MasterclassEvent,
    MasterclassReservation,
)
from app.modules.mentors.models import ACTIVE, SEATED, MentorBooking, MentorSession
from app.modules.playground.models import PlaygroundBooking, PlaygroundSlot
from app.modules.points.models import PointsSource
from app.modules.staff.models import StaffAccount, StaffRole

LIMIT = RateLimit("mentor:attendee", 60, 600)
SCAN_BEFORE_END = timedelta(minutes=5)
SCAN_AFTER_END = timedelta(minutes=10)
SCOPE = "MENTOR_HOST"


class MentorError(DomainError):
    code = "MENTOR_CONFLICT"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.details = {"reason": reason}


class MentorMissing(DomainError):
    code = "MENTOR_NOT_FOUND"
    status_code = 404


def now() -> datetime:
    return datetime.now(UTC)


def session_of(db: Session, session_id: int) -> MentorSession:
    row = db.get(MentorSession, session_id)
    if row is None:
        raise MentorMissing("No such mentor session.")
    return row


def _change(row: MentorBooking, status: str, stamp: datetime) -> None:
    row.status = status
    row.version += 1
    row.updated_at = stamp


def _count(db: Session, session_id: int, statuses: tuple[str, ...]) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(MentorBooking)
            .where(MentorBooking.session_id == session_id, MentorBooking.status.in_(statuses))
        ).scalar_one()
    )


def _latest(db: Session, user_id: int, session_id: int) -> MentorBooking | None:
    return db.execute(
        select(MentorBooking)
        .where(MentorBooking.user_id == user_id, MentorBooking.session_id == session_id)
        .order_by(MentorBooking.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def revision(row: MentorBooking | None) -> str:
    return "0" if row is None else f"{row.id}:{row.version}"


def view(row: MentorBooking) -> dict[str, Any]:
    return {
        "id": row.id,
        "session_id": row.session_id,
        "status": row.status.lower(),
        "version": row.version,
    }


# --- the shared schedule ----------------------------------------------------------------


def overlaps_mentor(
    db: Session, user_id: int, event_id: int, starts_at: datetime, ends_at: datetime
) -> bool:
    """A confirmed, waiting or attended mentor booking of this person at that time."""
    return (
        db.execute(
            select(MentorBooking.id)
            .join(MentorSession, MentorSession.id == MentorBooking.session_id)
            .where(
                MentorBooking.user_id == user_id,
                MentorBooking.status.in_((*ACTIVE, "ATTENDED")),
                MentorSession.event_id == event_id,
                MentorSession.starts_at < ends_at,
                MentorSession.ends_at > starts_at,
            )
            .limit(1)
        ).first()
        is not None
    )


def _check_free(db: Session, user_id: int, session: MentorSession) -> None:
    masterclass = db.execute(
        select(MasterclassReservation.id)
        .join(MasterclassBlock, MasterclassReservation.block_id == MasterclassBlock.id)
        .where(
            MasterclassReservation.user_id == user_id,
            MasterclassBlock.event_id == session.event_id,
            MasterclassReservation.status.in_(("CONFIRMED", "WAITLISTED")),
            MasterclassBlock.starts_at < session.ends_at,
            MasterclassBlock.ends_at > session.starts_at,
        )
        .limit(1)
    ).first()
    if masterclass is not None:
        raise MentorError("masterclass_overlap")
    playground = db.execute(
        select(PlaygroundBooking.id)
        .join(PlaygroundSlot, PlaygroundSlot.id == PlaygroundBooking.slot_id)
        .where(
            PlaygroundBooking.user_id == user_id,
            PlaygroundSlot.event_id == session.event_id,
            PlaygroundBooking.status.in_(("CONFIRMED", "CHECKED_IN", "COMPLETED")),
            PlaygroundSlot.starts_at < session.ends_at,
            PlaygroundSlot.ends_at > session.starts_at,
        )
        .limit(1)
    ).first()
    if playground is not None:
        raise MentorError("playground_overlap")


# --- attendee ---------------------------------------------------------------------------


def book(db: Session, user: User, session_id: int, command: str, expected: str) -> dict[str, Any]:
    """Caller holds: reset fence -> schedule lock -> attendee lock."""
    limiter.check([(LIMIT, str(user.id))])
    previous = db.execute(
        select(MentorBooking).where(MentorBooking.command_id == command)
    ).scalar_one_or_none()
    if previous is not None:
        if previous.user_id != user.id or previous.session_id != session_id:
            raise MentorError("command_conflict")
        return view(previous)
    session = db.execute(
        select(MentorSession).where(MentorSession.id == session_id).with_for_update()
    ).scalar_one_or_none()
    if session is None or not session.is_open:
        raise MentorMissing("No such mentor session.")
    stamp = now()
    old = _latest(db, user.id, session.id)
    if revision(old) != expected:
        raise MentorError("choice_changed")
    if old is not None and old.status in (*ACTIVE, "ATTENDED"):
        return view(old)
    if stamp >= session.starts_at:
        raise MentorError("booking_closed")
    other = db.execute(
        select(MentorBooking.id).where(
            MentorBooking.user_id == user.id, MentorBooking.status.in_(ACTIVE)
        )
    ).first()
    if other is not None:
        raise MentorError("one_active_booking")
    _check_free(db, user.id, session)
    if _count(db, session.id, SEATED) < session.capacity:
        status = "CONFIRMED"
    elif _count(db, session.id, ("WAITLISTED",)) < session.waitlist_capacity:
        status = "WAITLISTED"
    else:
        raise MentorError("session_full")
    row = MentorBooking(
        session_id=session.id,
        user_id=user.id,
        command_id=command,
        status=status,
        created_at=stamp,
        updated_at=stamp,
    )
    db.add(row)
    db.flush()
    return view(row)


def promote_waiting(db: Session, session: MentorSession, stamp: datetime) -> list[int]:
    """Free seats go to the waiting list in the order people joined it."""
    promoted: list[int] = []
    free = session.capacity - _count(db, session.id, SEATED)
    if free <= 0:
        return promoted
    for row in db.execute(
        select(MentorBooking)
        .where(MentorBooking.session_id == session.id, MentorBooking.status == "WAITLISTED")
        .order_by(MentorBooking.created_at, MentorBooking.id)
        .limit(free)
        .with_for_update()
    ).scalars():
        _change(row, "CONFIRMED", stamp)
        promoted.append(row.user_id)
    db.flush()
    return promoted


def cancel(db: Session, user: User, booking_id: int, expected: int) -> dict[str, Any]:
    row = db.execute(
        select(MentorBooking).where(MentorBooking.id == booking_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or row.user_id != user.id:
        raise MentorMissing("No such booking.")
    if row.status == "CANCELLED":
        return view(row)
    if row.version != expected:
        raise MentorError("choice_changed")
    if row.status not in ACTIVE:
        raise MentorError("booking_closed")
    session = db.execute(
        select(MentorSession).where(MentorSession.id == row.session_id).with_for_update()
    ).scalar_one()
    stamp = now()
    if stamp >= session.starts_at:
        raise MentorError("cancellation_closed")
    seat = row.status == "CONFIRMED"
    _change(row, "CANCELLED", stamp)
    db.flush()
    if seat:
        promote_waiting(db, session, stamp)
    return view(row)


def _position(db: Session, row: MentorBooking) -> int:
    return 1 + int(
        db.execute(
            select(func.count())
            .select_from(MentorBooking)
            .where(
                MentorBooking.session_id == row.session_id,
                MentorBooking.status == "WAITLISTED",
                (MentorBooking.created_at < row.created_at)
                | ((MentorBooking.created_at == row.created_at) & (MentorBooking.id < row.id)),
            )
        ).scalar_one()
    )


def programme(db: Session, user_id: int) -> dict[str, Any]:
    """Open sessions (and closed ones the viewer still holds a place in) with the
    seats left and the viewer's own booking, in a fixed number of queries."""
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()
    sessions = list(
        db.execute(
            select(MentorSession)
            .where(
                MentorSession.is_open.is_(True)
                | MentorSession.id.in_(
                    select(MentorBooking.session_id).where(
                        MentorBooking.user_id == user_id, MentorBooking.status != "CANCELLED"
                    )
                )
            )
            .order_by(MentorSession.starts_at, MentorSession.id)
        ).scalars()
    )
    ids = [s.id for s in sessions]
    counts: dict[tuple[int, str], int] = {}
    mine: dict[int, MentorBooking] = {}
    if ids:
        for sid, status, n in db.execute(
            select(MentorBooking.session_id, MentorBooking.status, func.count())
            .where(MentorBooking.session_id.in_(ids))
            .group_by(MentorBooking.session_id, MentorBooking.status)
        ):
            counts[(sid, status)] = int(n)
        for row in db.execute(
            select(MentorBooking)
            .where(MentorBooking.session_id.in_(ids), MentorBooking.user_id == user_id)
            .order_by(MentorBooking.id)
        ).scalars():
            mine[row.session_id] = row  # the latest one wins
    stamp = now()
    out = []
    for s in sessions:
        seated = counts.get((s.id, "CONFIRMED"), 0) + counts.get((s.id, "ATTENDED"), 0)
        waiting = counts.get((s.id, "WAITLISTED"), 0)
        own = mine.get(s.id)
        booking = None
        if own is not None and own.status != "CANCELLED":
            booking = {**view(own), "position": None}
            if own.status == "WAITLISTED":
                booking["position"] = _position(db, own)
        out.append(
            {
                "id": s.id,
                "title": s.title,
                "mentor_name": s.mentor_name,
                "starts_at": s.starts_at.isoformat(),
                "ends_at": s.ends_at.isoformat(),
                "capacity": s.capacity,
                "seats_left": max(0, s.capacity - seated),
                "waitlist_left": max(0, s.waitlist_capacity - waiting),
                "bookable": s.is_open and stamp < s.starts_at,
                "cancellable": stamp < s.starts_at,
                "revision": revision(own),
                "booking": booking,
            }
        )
    return {"timezone": event.timezone if event else "UTC", "sessions": out}


def attended(db: Session, user_id: int) -> list[MentorSession]:
    """The mentor groups whose host verified this person (Founder, programme)."""
    return list(
        db.execute(
            select(MentorSession)
            .join(MentorBooking, MentorBooking.session_id == MentorSession.id)
            .where(MentorBooking.user_id == user_id, MentorBooking.status == "ATTENDED")
            .order_by(MentorSession.starts_at)
        ).scalars()
    )


# --- the host ---------------------------------------------------------------------------


def _short(user: User) -> str:
    return f"{user.first_name} {user.last_name[:1]}."


def _host_may(db: Session, staff: StaffAccount, session_id: int) -> bool:
    return staff.role == StaffRole.ADMIN or has_scope(db, staff, SCOPE, session_id)


def host_sessions(db: Session, staff: StaffAccount) -> dict[str, Any]:
    from app.modules.competition.models import StaffScope

    query = select(MentorSession).order_by(MentorSession.starts_at, MentorSession.id)
    if staff.role != StaffRole.ADMIN:
        query = query.join(
            StaffScope,
            (StaffScope.target_id == MentorSession.id)
            & (StaffScope.scope == SCOPE)
            & (StaffScope.staff_account_id == staff.id)
            & StaffScope.revoked_at.is_(None),
        )
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars().first()
    return {
        "timezone": event.timezone if event else "UTC",
        "sessions": [
            {
                "id": s.id,
                "title": s.title,
                "mentor_name": s.mentor_name,
                "starts_at": s.starts_at.isoformat(),
                "ends_at": s.ends_at.isoformat(),
            }
            for s in db.execute(query).scalars()
        ],
    }


def host_list(db: Session, staff: StaffAccount, session_id: int) -> dict[str, Any]:
    """Short names and statuses of a session's group. Never contact data."""
    session = session_of(db, session_id)
    if not _host_may(db, staff, session.id):
        raise StaffNotAuthorizedError("This account does not host that session.")
    rows = db.execute(
        select(MentorBooking, User)
        .join(User, User.id == MentorBooking.user_id)
        .where(MentorBooking.session_id == session.id, MentorBooking.status != "CANCELLED")
        .order_by(MentorBooking.created_at, MentorBooking.id)
    ).all()
    return {
        "session_id": session.id,
        "capacity": session.capacity,
        "people": [{"name": _short(u), "status": b.status.lower()} for b, u in rows],
    }


def complete(
    db: Session,
    staff: StaffAccount,
    *,
    session_id: int,
    card_token: str | None = None,
    now: datetime,
    attendee: User | None = None,
) -> dict[str, Any]:
    """Caller holds: reset fence -> schedule lock. The scope is re-read here."""
    session = db.execute(
        select(MentorSession).where(MentorSession.id == session_id).with_for_update()
    ).scalar_one_or_none()
    if session is None:
        raise MentorMissing("No such mentor session.")
    require_scope(db, staff, SCOPE, session.id)
    user = attendee or resolve_active_attendee(db, card_token or "")
    booking = db.execute(
        select(MentorBooking)
        .where(
            MentorBooking.session_id == session.id,
            MentorBooking.user_id == user.id,
            MentorBooking.status != "CANCELLED",
        )
        .with_for_update()
    ).scalar_one_or_none()
    if booking is None:
        raise MentorError("not_booked")
    if booking.status != "ATTENDED":
        if not session.ends_at - SCAN_BEFORE_END <= now < session.ends_at + SCAN_AFTER_END:
            raise MentorError("outside_window")
        if booking.status == "WAITLISTED" and _count(db, session.id, SEATED) >= session.capacity:
            raise MentorError("group_full")
        _change(booking, "ATTENDED", now)
        booking.attended_at = now
        booking.attended_by_staff_id = staff.id
        db.flush()
        reverse_overlapping_panel_awards(
            db, user.id, session, note="Verified at a mentor group during this panel."
        )
    assert booking.attended_at is not None
    result = award(
        db,
        user_id=user.id,
        source=PointsSource.MENTOR,
        unit="once",
        points=MENTOR_POINTS,
        occurred_at=booking.attended_at,
        staff_id=staff.id,
        note=f"Mentor session {session.code}",
    )
    return {"name": _short(user), "award": {"outcome": result.outcome, "points": result.points}}


# --- administration ---------------------------------------------------------------------


def admin_view(row: MentorSession) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "code": row.code,
        "title": row.title,
        "mentor_name": row.mentor_name,
        "starts_at": row.starts_at.isoformat(),
        "ends_at": row.ends_at.isoformat(),
        "capacity": row.capacity,
        "waitlist_capacity": row.waitlist_capacity,
        "is_open": row.is_open,
    }
