"""Administration of the masterclass programme (existing ADMIN role only).

Each operation runs through `admin.service.execute` inside the fenced unit of
work, so the change and its `admin_audit` row commit together. An edit never
breaks existing reservations silently - it is refused with
`MASTERCLASS_ADMIN_CONFLICT` and a reason:

* `capacity_below_confirmed` / `waitlist_below_waiting` - a capacity or waitlist
  size below what attendees already hold (a database trigger refuses it too);
* `has_reservations` - moving a masterclass that has (or had) reservations to
  another block or unscheduling it (the composite foreign key refuses it too),
  or deleting it;
* `active_reservations` - unpublishing a masterclass people currently hold;
* `block_overlap` - a block whose time would overlap another block of the event
  (an attendee may hold one choice in each block, so overlapping blocks would
  double-book them);
* `room_taken` - a room already used in that block;
* `wrong_event` - a block or room of another event.

A larger capacity hands the new seats to the waitlist at once, in queue order.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.errors import MasterclassAdminConflictError
from app.modules.admin.service import AdminInvalid, AdminResult, required
from app.modules.attendees.models import User
from app.modules.masterclasses import service as mc_service
from app.modules.masterclasses.models import (
    ACTIVE_STATUSES,
    CONFIRMED,
    WAITLISTED,
    Masterclass,
    MasterclassBlock,
    MasterclassEvent,
    MasterclassReservation,
    MasterclassRoom,
)
from app.modules.masterclasses.schemas import (
    BlockEdit,
    BlockWrite,
    EventEdit,
    EventWrite,
    MasterclassEdit,
    MasterclassWrite,
    RoomEdit,
    RoomWrite,
)
from app.modules.staff.models import StaffAccount

AUDIT_EVENT_CREATE = "masterclass_event.create"
AUDIT_EVENT_UPDATE = "masterclass_event.update"
AUDIT_BLOCK_CREATE = "masterclass_block.create"
AUDIT_BLOCK_UPDATE = "masterclass_block.update"
AUDIT_ROOM_CREATE = "masterclass_room.create"
AUDIT_ROOM_UPDATE = "masterclass_room.update"
AUDIT_MASTERCLASS_CREATE = "masterclass.create"
AUDIT_MASTERCLASS_UPDATE = "masterclass.update"
AUDIT_MASTERCLASS_DELETE = "masterclass.delete"


# --- views ---------------------------------------------------------------------------


def event_view(row: MasterclassEvent) -> dict[str, Any]:
    return {
        "id": row.id,
        "code": row.code,
        "title": row.title,
        "venue": row.venue,
        "timezone": row.timezone,
        "event_date": row.event_date.isoformat(),
        "competition_closes_at": (
            row.competition_closes_at.isoformat() if row.competition_closes_at else None
        ),
    }


def block_view(row: MasterclassBlock) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "code": row.code,
        "title": row.title,
        "display_number": row.display_number,
        "starts_at": row.starts_at.isoformat(),
        "ends_at": row.ends_at.isoformat(),
        "change_deadline": row.change_deadline.isoformat(),
        "sort_order": row.sort_order,
    }


def room_view(row: MasterclassRoom) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "name": row.name,
        "sort_order": row.sort_order,
        "display_number": row.display_number,
    }


def masterclass_view(row: Masterclass) -> dict[str, Any]:
    return {
        "id": row.id,
        "event_id": row.event_id,
        "block_id": row.block_id,
        "room_id": row.room_id,
        "title": row.title,
        "description": row.description,
        "speaker": row.speaker,
        "capacity": row.capacity,
        "waitlist_capacity": row.waitlist_capacity,
        "is_published": row.is_published,
        "sort_order": row.sort_order,
        "standing_capacity": row.standing_capacity,
    }


def _counts(db: Session) -> dict[tuple[int, str], int]:
    return {
        (mc_id, status): int(n)
        for mc_id, status, n in db.execute(
            select(
                MasterclassReservation.masterclass_id, MasterclassReservation.status, func.count()
            ).group_by(MasterclassReservation.masterclass_id, MasterclassReservation.status)
        ).all()
    }


def program_rows(db: Session) -> dict[str, Any]:
    """Everything an administrator manages, with confirmed / waiting counts."""
    counts = _counts(db)
    return {
        "events": [
            event_view(r)
            for r in db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id)).scalars()
        ],
        "blocks": [
            block_view(r)
            for r in db.execute(
                select(MasterclassBlock).order_by(MasterclassBlock.starts_at, MasterclassBlock.id)
            ).scalars()
        ],
        "rooms": [
            room_view(r)
            for r in db.execute(
                select(MasterclassRoom).order_by(MasterclassRoom.sort_order, MasterclassRoom.id)
            ).scalars()
        ],
        "masterclasses": [
            {
                **masterclass_view(r),
                "confirmed_count": counts.get((r.id, CONFIRMED), 0),
                "waitlist_count": counts.get((r.id, WAITLISTED), 0),
            }
            for r in db.execute(
                select(Masterclass).order_by(Masterclass.sort_order, Masterclass.id)
            ).scalars()
        ],
    }


def reservation_rows(db: Session, masterclass_id: int) -> dict[str, Any]:
    """Who holds a seat and who is waiting, in queue order (administrators only:
    names and e-mail addresses; never phone numbers)."""
    mc = required(db, Masterclass, masterclass_id)
    rows = db.execute(
        select(MasterclassReservation, User.first_name, User.last_name, User.email)
        .join(User, User.id == MasterclassReservation.user_id)
        .where(
            MasterclassReservation.masterclass_id == mc.id,
            MasterclassReservation.status.in_(ACTIVE_STATUSES),
        )
        .order_by(
            MasterclassReservation.status,
            MasterclassReservation.queued_at,
            MasterclassReservation.confirmed_at,
            MasterclassReservation.id,
        )
    ).all()

    def person(r: MasterclassReservation, first: str, last: str, email: Any) -> dict[str, Any]:
        return {
            "reservation_id": r.id,
            "user_id": r.user_id,
            "name": f"{first} {last}",
            "email": str(email),
        }

    confirmed = [
        {
            **person(r, f, la, e),
            "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
        }
        for r, f, la, e in rows
        if r.status == CONFIRMED
    ]
    waiting = sorted(
        ((r, f, la, e) for r, f, la, e in rows if r.status == WAITLISTED),
        key=lambda t: (t[0].queued_at, t[0].id),
    )
    return {
        "masterclass": masterclass_view(mc),
        "confirmed_count": len(confirmed),
        "waitlist_count": len(waiting),
        "confirmed": confirmed,
        "waitlist": [
            {
                **person(r, f, la, e),
                "position": i,
                "queued_at": r.queued_at.isoformat() if r.queued_at else None,
            }
            for i, (r, f, la, e) in enumerate(waiting, start=1)
        ],
    }


# --- guards ----------------------------------------------------------------------------


def _has_any_reservation(db: Session, masterclass_id: int) -> bool:
    return bool(
        db.execute(
            select(exists().where(MasterclassReservation.masterclass_id == masterclass_id))
        ).scalar()
    )


def _count(db: Session, masterclass_id: int, status: str) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(MasterclassReservation)
            .where(
                MasterclassReservation.masterclass_id == masterclass_id,
                MasterclassReservation.status == status,
            )
        ).scalar_one()
    )


def _no_overlap(
    db: Session, event_id: int, starts_at: Any, ends_at: Any, own_id: int | None
) -> None:
    clash = db.execute(
        select(MasterclassBlock.id).where(
            MasterclassBlock.event_id == event_id,
            MasterclassBlock.id != (own_id or 0),
            MasterclassBlock.starts_at < ends_at,
            MasterclassBlock.ends_at > starts_at,
        )
    ).first()
    if clash is not None:
        raise MasterclassAdminConflictError(
            "block_overlap", "This block would overlap another block of the event."
        )


def _no_playground_clash(db: Session, block: MasterclassBlock) -> None:
    """A confirmed OR waiting masterclass choice reserves its block's time; a
    block moved onto a time where one of its attendees holds a Playground place
    would double-book that person, so it is refused instead."""
    from app.modules.playground.models import PlaygroundBooking, PlaygroundSlot
    from app.modules.playground.service import ACTIVE as PLAYGROUND_ACTIVE

    clash = db.execute(
        select(MasterclassReservation.id)
        .join(PlaygroundBooking, PlaygroundBooking.user_id == MasterclassReservation.user_id)
        .join(PlaygroundSlot, PlaygroundSlot.id == PlaygroundBooking.slot_id)
        .where(
            MasterclassReservation.block_id == block.id,
            MasterclassReservation.status.in_(ACTIVE_STATUSES),
            PlaygroundBooking.status.in_(PLAYGROUND_ACTIVE),
            PlaygroundSlot.event_id == block.event_id,
            PlaygroundSlot.starts_at < block.ends_at,
            PlaygroundSlot.ends_at > block.starts_at,
        )
        .limit(1)
    ).first()
    if clash is not None:
        raise MasterclassAdminConflictError(
            "playground_overlap",
            "Attendees of this block hold Playground places at the new time.",
        )
    from app.modules.mentors.models import ACTIVE as MENTOR_ACTIVE
    from app.modules.mentors.models import MentorBooking, MentorSession

    mentor = db.execute(
        select(MasterclassReservation.id)
        .join(MentorBooking, MentorBooking.user_id == MasterclassReservation.user_id)
        .join(MentorSession, MentorSession.id == MentorBooking.session_id)
        .where(
            MasterclassReservation.block_id == block.id,
            MasterclassReservation.status.in_(ACTIVE_STATUSES),
            MentorBooking.status.in_(MENTOR_ACTIVE),
            MentorSession.event_id == block.event_id,
            MentorSession.starts_at < block.ends_at,
            MentorSession.ends_at > block.starts_at,
        )
        .limit(1)
    ).first()
    if mentor is not None:
        raise MasterclassAdminConflictError(
            "mentor_overlap",
            "Attendees of this block hold mentor group places at the new time.",
        )


def _check_slot(db: Session, event_id: int, block_id: int | None, room_id: int | None) -> None:
    if block_id is not None and required(db, MasterclassBlock, block_id).event_id != event_id:
        raise MasterclassAdminConflictError("wrong_event", "That block belongs to another event.")
    if room_id is not None and required(db, MasterclassRoom, room_id).event_id != event_id:
        raise MasterclassAdminConflictError("wrong_event", "That room belongs to another event.")


def _lock(db: Session, masterclass_id: int) -> Masterclass:
    row = db.execute(
        select(Masterclass)
        .where(Masterclass.id == masterclass_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        required(db, Masterclass, masterclass_id)  # raises ADMIN_NOT_FOUND
    assert row is not None
    return row


# --- operations ------------------------------------------------------------------------


def create_event(db: Session, actor: StaffAccount, payload: EventWrite) -> AdminResult:
    row = MasterclassEvent(**payload.model_dump())
    db.add(row)
    db.flush()
    view = event_view(row)
    return AUDIT_EVENT_CREATE, "masterclass_event", None, view, view


def edit_event(db: Session, actor: StaffAccount, event_id: int, payload: EventEdit) -> AdminResult:
    row = required(db, MasterclassEvent, event_id)
    before = event_view(row)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is None and key in {"title", "timezone", "event_date"}:
            raise AdminInvalid(f"{key} cannot be empty.")
        setattr(row, key, value)
    db.flush()
    after = event_view(row)
    return AUDIT_EVENT_UPDATE, "masterclass_event", before, after, after


def create_block(db: Session, actor: StaffAccount, payload: BlockWrite) -> AdminResult:
    required(db, MasterclassEvent, payload.event_id)
    _no_overlap(db, payload.event_id, payload.starts_at, payload.ends_at, None)
    row = MasterclassBlock(**payload.model_dump())
    db.add(row)
    db.flush()
    view = block_view(row)
    return AUDIT_BLOCK_CREATE, "masterclass_block", None, view, view


def edit_block(db: Session, actor: StaffAccount, block_id: int, payload: BlockEdit) -> AdminResult:
    row = db.execute(
        select(MasterclassBlock).where(MasterclassBlock.id == block_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        required(db, MasterclassBlock, block_id)
    assert row is not None
    before = block_view(row)
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is None and key not in {"sort_order", "display_number"}:
            raise AdminInvalid(f"{key} cannot be empty.")
        if value is not None or key == "display_number":
            setattr(row, key, value)
    if row.ends_at <= row.starts_at:
        raise AdminInvalid("The block must end after it starts.")
    if row.change_deadline > row.starts_at:
        raise AdminInvalid("The change deadline must not be after the start.")
    _no_overlap(db, row.event_id, row.starts_at, row.ends_at, row.id)
    _no_playground_clash(db, row)
    db.flush()
    after = block_view(row)
    return AUDIT_BLOCK_UPDATE, "masterclass_block", before, after, after


def create_room(db: Session, actor: StaffAccount, payload: RoomWrite) -> AdminResult:
    required(db, MasterclassEvent, payload.event_id)
    row = MasterclassRoom(**payload.model_dump())
    db.add(row)
    db.flush()
    view = room_view(row)
    return AUDIT_ROOM_CREATE, "masterclass_room", None, view, view


def edit_room(db: Session, actor: StaffAccount, room_id: int, payload: RoomEdit) -> AdminResult:
    row = required(db, MasterclassRoom, room_id)
    before = room_view(row)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if value is None and key != "display_number":
            raise AdminInvalid(f"{key} cannot be empty.")
        setattr(row, key, value)
    db.flush()
    after = room_view(row)
    return AUDIT_ROOM_UPDATE, "masterclass_room", before, after, after


def _room_free(db: Session, block_id: int | None, room_id: int | None, own_id: int | None) -> None:
    if block_id is None or room_id is None:
        return
    taken = db.execute(
        select(Masterclass.id).where(
            Masterclass.block_id == block_id,
            Masterclass.room_id == room_id,
            Masterclass.id != (own_id or 0),
        )
    ).first()
    if taken is not None:
        raise MasterclassAdminConflictError(
            "room_taken", "That room is already used in this block."
        )


def create_masterclass(db: Session, actor: StaffAccount, payload: MasterclassWrite) -> AdminResult:
    required(db, MasterclassEvent, payload.event_id)
    _check_slot(db, payload.event_id, payload.block_id, payload.room_id)
    _room_free(db, payload.block_id, payload.room_id, None)
    row = Masterclass(**payload.model_dump())
    db.add(row)
    db.flush()
    view = masterclass_view(row)
    return AUDIT_MASTERCLASS_CREATE, "masterclass", None, view, view


def edit_masterclass(
    db: Session, actor: StaffAccount, masterclass_id: int, payload: MasterclassEdit
) -> AdminResult:
    row = _lock(db, masterclass_id)  # every seat writer of this masterclass waits now
    before = masterclass_view(row)
    changes = payload.model_dump(exclude_unset=True)
    for key in (
        "title",
        "capacity",
        "waitlist_capacity",
        "standing_capacity",
        "is_published",
        "sort_order",
    ):
        if key in changes and changes[key] is None:
            raise AdminInvalid(f"{key} cannot be empty.")

    moved = "block_id" in changes and changes["block_id"] != row.block_id
    if moved and _has_any_reservation(db, row.id):
        raise MasterclassAdminConflictError(
            "has_reservations",
            "This masterclass has reservations, so it cannot move to another block.",
        )
    block_id = changes.get("block_id", row.block_id)
    room_id = changes.get("room_id", row.room_id)
    _check_slot(db, row.event_id, block_id, room_id)
    _room_free(db, block_id, room_id, row.id)

    confirmed = _count(db, row.id, CONFIRMED)
    waiting = _count(db, row.id, WAITLISTED)
    if changes.get("capacity") is not None and changes["capacity"] < confirmed:
        raise MasterclassAdminConflictError(
            "capacity_below_confirmed",
            "The capacity cannot be below the seats already confirmed.",
            confirmed=confirmed,
        )
    if changes.get("waitlist_capacity") is not None and changes["waitlist_capacity"] < waiting:
        raise MasterclassAdminConflictError(
            "waitlist_below_waiting",
            "The waitlist cannot be shorter than the people already waiting.",
            waiting=waiting,
        )
    if changes.get("is_published") is False and confirmed + waiting > 0:
        raise MasterclassAdminConflictError(
            "active_reservations",
            "People hold places in this masterclass, so it cannot be hidden.",
            confirmed=confirmed,
            waiting=waiting,
        )

    for key, value in changes.items():
        setattr(row, key, value)
    db.flush()
    promoted = mc_service.promote_waiting(db, row, mc_service.server_now())
    after = masterclass_view(row)
    return (
        AUDIT_MASTERCLASS_UPDATE,
        "masterclass",
        before,
        after,
        {**after, "promoted_from_waitlist": len(promoted)},
    )


def delete_masterclass(db: Session, actor: StaffAccount, masterclass_id: int) -> AdminResult:
    row = _lock(db, masterclass_id)
    if _has_any_reservation(db, row.id):
        raise MasterclassAdminConflictError(
            "has_reservations",
            "This masterclass has reservations, so it cannot be deleted.",
        )
    before = masterclass_view(row)
    db.delete(row)
    db.flush()
    return (
        AUDIT_MASTERCLASS_DELETE,
        "masterclass",
        before,
        None,
        {"id": masterclass_id, "removed": True},
    )
