"""Scanned attendance: masterclass doors and the end of a Playground session.

MASTERCLASS (12 per block, at most 24). A door volunteer holding the
MASTERCLASS_DOOR scope for THAT masterclass scans the attendee's QR at ENTRY in
the first 10 minutes and at EXIT in the last 10 minutes; both are needed. A
reservation is not attendance. Seats at the door (PDF "Seçim və giriş
qaydaları"):

* up to +5 minutes a seat belongs to its confirmed reservation;
* after +5 an unclaimed confirmed seat is released (reservation CANCELLED,
  reason NO_SHOW) and a free seat goes first to the waitlist in queue order: a
  waitlisted person at the door is seated only while there are more free seats
  than people waiting ahead of them; then to the door queue (people without a
  reservation), only with seats beyond everyone still waiting;
* when every seat is taken, standing is allowed only up to the masterclass's
  configured `standing_capacity` (0 = none); a standing participant earns the
  same, as the PDF says;
* someone holding another masterclass in this block is refused here.

PLAYGROUND (3 per distinct booth, the first 8 booths, at most 24). The booth's
own staff (BOOTH_STAFF of that booth) scans each member when they finish the
session they were admitted to (CHECKED_IN): any time from its start to 5 minutes
after its end. A game can take one minute (owner's decision, 7 Nov 2026), so the
scan is not held back to the last minutes; the confirmed arrival is the guard.
P35-P36 (17:30-18:00) and a ninth booth still complete the session, without points.
A panel already paid for the same time is corrected by a reversal entry.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import DomainError, StaffNotAuthorizedError
from app.modules.attendees.models import User
from app.modules.competition.ledger import award
from app.modules.competition.models import MasterclassAttendance
from app.modules.competition.panels import reverse_overlapping_panel_awards
from app.modules.competition.rules import (
    MASTERCLASS_ENTRY_MINUTES,
    MASTERCLASS_EXIT_MINUTES,
    MASTERCLASS_NO_SHOW_MINUTES,
    MASTERCLASS_POINTS,
    PLAYGROUND_POINTS,
)
from app.modules.competition.scopes import may_work_booth, require_scope
from app.modules.contact_cards.service import resolve_active_attendee
from app.modules.masterclasses.models import (
    CANCELLED,
    CONFIRMED,
    WAITLISTED,
    Masterclass,
    MasterclassBlock,
    MasterclassReservation,
)
from app.modules.playground.models import PlaygroundBooking, PlaygroundSlot
from app.modules.points.models import PointsSource
from app.modules.staff.models import StaffAccount

PLAYGROUND_SCAN_AFTER_END = timedelta(minutes=5)


class AttendanceError(DomainError):
    code = "ATTENDANCE_REFUSED"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.details = {"reason": reason}


# --- masterclass doors -------------------------------------------------------------------


def _count(db: Session, mc: Masterclass, status: str) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(MasterclassReservation)
            .where(
                MasterclassReservation.masterclass_id == mc.id,
                MasterclassReservation.status == status,
            )
        ).scalar_one()
    )


def release_unclaimed(db: Session, mc: Masterclass, now: datetime) -> int:
    """After +5 minutes: confirmed seats nobody has entered with are released."""
    unclaimed = list(
        db.execute(
            select(MasterclassReservation)
            .outerjoin(
                MasterclassAttendance,
                (MasterclassAttendance.masterclass_id == mc.id)
                & (MasterclassAttendance.user_id == MasterclassReservation.user_id),
            )
            .where(
                MasterclassReservation.masterclass_id == mc.id,
                MasterclassReservation.status == CONFIRMED,
                MasterclassAttendance.id.is_(None),
            )
        ).scalars()
    )
    for row in unclaimed:
        row.status = CANCELLED
        row.cancelled_at = now
        row.cancel_reason = "NO_SHOW"
    db.flush()
    return len(unclaimed)


def _waiting_ahead(db: Session, row: MasterclassReservation) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(MasterclassReservation)
            .where(
                MasterclassReservation.masterclass_id == row.masterclass_id,
                MasterclassReservation.status == WAITLISTED,
                (MasterclassReservation.queued_at < row.queued_at)
                | (
                    (MasterclassReservation.queued_at == row.queued_at)
                    & (MasterclassReservation.id < row.id)
                ),
            )
        ).scalar_one()
    )


def door_scan(
    db: Session,
    staff: StaffAccount,
    *,
    masterclass_id: int,
    card_token: str | None = None,
    phase: str,
    now: datetime,
    attendee: User | None = None,
) -> dict[str, Any]:
    """`attendee` instead of `card_token`: a reviewed offline scan whose QR was
    verified when it reached the server (competition/offline.py)."""
    require_scope(db, staff, "MASTERCLASS_DOOR", masterclass_id)
    mc = db.execute(
        select(Masterclass)
        .where(Masterclass.id == masterclass_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if mc is None or mc.block_id is None:
        raise AttendanceError("unknown_masterclass")
    block = db.get(MasterclassBlock, mc.block_id)
    assert block is not None
    user = attendee or resolve_active_attendee(db, card_token or "")  # revoked -> INVALID_QR
    attendance = db.execute(
        select(MasterclassAttendance).where(
            MasterclassAttendance.masterclass_id == mc.id,
            MasterclassAttendance.user_id == user.id,
        )
    ).scalar_one_or_none()
    name = f"{user.first_name} {user.last_name[:1]}."

    if phase == "ENTRY":
        if attendance is not None:  # a repeated scan
            return {
                "phase": "entry",
                "name": name,
                "standing": attendance.standing,
                "already": True,
            }
        if (
            not block.starts_at
            <= now
            < block.starts_at + timedelta(minutes=MASTERCLASS_ENTRY_MINUTES)
        ):
            raise AttendanceError("outside_window")
        mine = db.execute(
            select(MasterclassReservation).where(
                MasterclassReservation.user_id == user.id,
                MasterclassReservation.block_id == block.id,
                MasterclassReservation.status.in_((CONFIRMED, WAITLISTED)),
            )
        ).scalar_one_or_none()
        if mine is not None and mine.masterclass_id != mc.id:
            raise AttendanceError("booked_elsewhere")
        if now >= block.starts_at + timedelta(minutes=MASTERCLASS_NO_SHOW_MINUTES):
            release_unclaimed(db, mc, now)
            if mine is not None:
                db.refresh(mine)
                if mine.status == CANCELLED:  # their own seat was released: late
                    mine = None  # they join the door queue like anyone else
        standing = False
        if mine is not None and mine.status == CONFIRMED:
            pass  # their own seat
        else:
            if now < block.starts_at + timedelta(minutes=MASTERCLASS_NO_SHOW_MINUTES):
                raise AttendanceError("seats_held_until_plus_five")
            free = mc.capacity - _count(db, mc, CONFIRMED)
            waiting = _count(db, mc, WAITLISTED)
            # waitlisted: seated only past those ahead; door queue: only beyond
            # everyone still waiting
            seated = free > (_waiting_ahead(db, mine) if mine is not None else waiting)
            if seated and mine is not None:
                mine.status = CONFIRMED
                mine.confirmed_at = now
            elif seated:
                db.add(
                    MasterclassReservation(
                        masterclass_id=mc.id,
                        block_id=block.id,
                        user_id=user.id,
                        status=CONFIRMED,
                        confirmed_at=now,
                    )
                )
            else:
                standing_now = int(
                    db.execute(
                        select(func.count())
                        .select_from(MasterclassAttendance)
                        .where(
                            MasterclassAttendance.masterclass_id == mc.id,
                            MasterclassAttendance.standing.is_(True),
                        )
                    ).scalar_one()
                )
                # A seat still held for the waitlist is not given away; standing
                # is the only other way in, within the configured capacity.
                if standing_now >= mc.standing_capacity:
                    raise AttendanceError("full")
                standing = True
        db.add(
            MasterclassAttendance(
                masterclass_id=mc.id,
                user_id=user.id,
                standing=standing,
                entry_at=now,
                entry_by_staff_id=staff.id,
            )
        )
        db.flush()
        return {"phase": "entry", "name": name, "standing": standing, "already": False}

    # EXIT
    if attendance is None:
        raise AttendanceError("no_entry")
    if attendance.exit_at is None:
        if not block.ends_at - timedelta(minutes=MASTERCLASS_EXIT_MINUTES) <= now < block.ends_at:
            raise AttendanceError("outside_window")
        attendance.exit_at = now
        attendance.exit_by_staff_id = staff.id
        db.flush()
    result = award(
        db,
        user_id=user.id,
        source=PointsSource.MASTERCLASS,
        unit=f"block:{block.id}",
        points=MASTERCLASS_POINTS,
        occurred_at=attendance.exit_at,
        staff_id=staff.id,
    )
    return {
        "phase": "exit",
        "name": name,
        "standing": attendance.standing,
        "award": {"outcome": result.outcome, "points": result.points},
    }


# --- Playground: the end of a session --------------------------------------------------------


def playground_complete(
    db: Session,
    staff: StaffAccount,
    *,
    slot_id: int,
    card_token: str | None = None,
    now: datetime,
    attendee: User | None = None,
) -> dict[str, Any]:
    slot = db.get(PlaygroundSlot, slot_id)
    if slot is None:
        raise AttendanceError("unknown_slot")
    if not may_work_booth(db, staff, slot.booth_id):
        raise StaffNotAuthorizedError("Only this booth's staff can complete its sessions.")
    user = attendee or resolve_active_attendee(db, card_token or "")
    # Owner, 13 Nov 2026 (event day): the end scan is the booth's confirmation that
    # the person played - admitted, reserved, queued, or a walk-in with no booking.
    booking = db.execute(
        select(PlaygroundBooking)
        .where(
            PlaygroundBooking.slot_id == slot.id,
            PlaygroundBooking.user_id == user.id,
            PlaygroundBooking.status.in_(("CONFIRMED", "WAITLISTED", "CHECKED_IN", "COMPLETED")),
        )
        .with_for_update()
    ).scalar_one_or_none()
    name = f"{user.first_name} {user.last_name[:1]}."
    if booking is None or booking.status != "COMPLETED":
        if not slot.starts_at <= now < slot.ends_at + PLAYGROUND_SCAN_AFTER_END:
            raise AttendanceError("outside_window")
        from app.modules.playground.service import make_room

        if booking is None or booking.status != "CHECKED_IN":
            make_room(db, slot.id, now, keep=None if booking is None else booking.id)
        if booking is None:  # a walk-in
            booking = PlaygroundBooking(
                slot_id=slot.id,
                user_id=user.id,
                command_id=f"walk-in:{uuid4()}",
                status="COMPLETED",
                created_at=now,
                updated_at=now,
            )
            db.add(booking)
        else:
            booking.status = "COMPLETED"
            booking.version += 1
            booking.updated_at = now
        db.flush()
        reverse_overlapping_panel_awards(db, user.id, slot)
    if slot.number > 34:  # 17:30-18:00: walk-in, no points
        return {"name": name, "award": {"outcome": "not_scored", "points": 0}}
    result = award(
        db,
        user_id=user.id,
        source=PointsSource.PLAYGROUND,
        unit=f"booth:{slot.booth_id}",
        points=PLAYGROUND_POINTS,
        occurred_at=booking.updated_at,
        staff_id=staff.id,
        booth_id=slot.booth_id,
    )
    return {"name": name, "award": {"outcome": result.outcome, "points": result.points}}
