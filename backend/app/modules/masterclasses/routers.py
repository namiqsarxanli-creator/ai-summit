"""HTTP surface of the masterclass programme.

* `GET  /api/masterclasses` - the published programme with seat counts. Public:
  no attendee data of any kind.
* `GET  /api/attendees/me/masterclasses` - the signed-in attendee's own choices.
* `PUT  /api/attendees/me/masterclasses/{block_id}` - set the choice for a block.
* `DELETE /api/attendees/me/masterclasses/{block_id}` - cancel it.
* `/api/admin/masterclass-*` - programme administration (ADMIN only, audited).

The attendee is always the session's: no request names a user, so nobody can
read or change another person's reservation.
"""

from __future__ import annotations

from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Path
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import CurrentAdmin, CurrentAttendee, SessionToken
from app.core.errors import (
    MasterclassAdminConflictError,
    UnexpectedConstraintViolationError,
    translate_integrity_error,
)
from app.core.schedule import lock_schedule
from app.db.session import DbSession, mutating_transaction
from app.modules.admin import service as admin
from app.modules.admin.routers import _read
from app.modules.masterclasses import admin as mc_admin
from app.modules.masterclasses import service
from app.modules.masterclasses.schemas import (
    BlockEdit,
    BlockWrite,
    CancelRequest,
    ChoiceRequest,
    ChoiceResult,
    EventEdit,
    EventWrite,
    MasterclassEdit,
    MasterclassWrite,
    MyChoicesOut,
    ProgramOut,
    RoomEdit,
    RoomWrite,
)
from app.modules.points.service import maintenance_fast_path_check

router = APIRouter(tags=["masterclasses"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])

BlockId = Annotated[int, Path(ge=1, le=2**53)]
RowId = Annotated[int, Path(ge=1, le=2**53)]


def _reraise(err: IntegrityError) -> NoReturn:
    """Constraint violations by NAME (§7.14): the capacity trigger's names map to
    MASTERCLASS_FULL / MASTERCLASS_WAITLIST_FULL; an unnamed one is re-raised."""
    translated = translate_integrity_error(err)
    if translated is err:
        raise err
    raise translated from err


@router.get("/masterclasses", response_model=ProgramOut)
def get_program(db: DbSession) -> ProgramOut:
    """The published programme by event and block: room, time, speaker,
    description and seat counts. `changes_open` is decided by the server clock."""
    return service.program(db, service.server_now())


@router.get("/attendees/me/masterclasses", response_model=MyChoicesOut)
def get_my_choices(user: CurrentAttendee, db: DbSession) -> MyChoicesOut:
    """The signed-in attendee's current choice per block: confirmed, or waiting
    with the place in the queue. A reservation is not attendance."""
    return service.my_choices(db, user, service.server_now())


@router.put("/attendees/me/masterclasses/{block_id}", response_model=ChoiceResult)
def put_my_choice(
    block_id: BlockId, payload: ChoiceRequest, token: SessionToken, db: DbSession
) -> ChoiceResult:
    """Choose a masterclass for this block, or change to another one (one
    transaction: if the new one cannot be had, the old choice is kept). Sending
    the same choice again changes nothing. Errors: MASTERCLASS_FULL (send
    `join_waitlist` to queue), MASTERCLASS_WAITLIST_FULL, MASTERCLASS_SEAT_WOULD_BE_LOST
    (send `give_up_confirmed_seat`), MASTERCLASS_CHANGES_CLOSED, MASTERCLASS_NOT_FOUND."""
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            lock_schedule(tx, shared=True)
            outcome = service.choose(
                tx, raw_session_token=token, block_id=block_id, request=payload
            )
    except IntegrityError as err:
        _reraise(err)
    return outcome.result


@router.delete("/attendees/me/masterclasses/{block_id}", response_model=ChoiceResult)
def delete_my_choice(
    block_id: BlockId, payload: CancelRequest, token: SessionToken, db: DbSession
) -> ChoiceResult:
    """Cancel this block's choice (a freed seat goes to the first in the queue at
    once). Cancelling when there is nothing to cancel changes nothing."""
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            lock_schedule(tx, shared=True)
            outcome = service.cancel(
                tx,
                raw_session_token=token,
                block_id=block_id,
                expected_revision=payload.expected_revision,
            )
    except IntegrityError as err:
        _reraise(err)
    return outcome.result


# --- administration ------------------------------------------------------------------

_ADMIN_CONSTRAINTS = {
    "uq_masterclasses_block_room": ("room_taken", "That room is already used in this block."),
    "uq_masterclass_rooms_event_name": ("duplicate", "A room with that name already exists."),
    "uq_masterclass_blocks_event_code": ("duplicate", "A block with that code already exists."),
    "uq_masterclass_events_code": ("duplicate", "An event with that code already exists."),
    "ck_masterclasses_capacity_not_below_confirmed": (
        "capacity_below_confirmed",
        "The capacity cannot be below the seats already confirmed.",
    ),
    "ck_masterclasses_waitlist_not_below_waiting": (
        "waitlist_below_waiting",
        "The waitlist cannot be shorter than the people already waiting.",
    ),
    "fk_masterclass_reservations_masterclass_block": (
        "has_reservations",
        "This masterclass has reservations, so it cannot be moved or deleted.",
    ),
}


def _admin_write(db: Session, token: str | None, operation: admin.AdminOperation) -> dict[str, Any]:
    maintenance_fast_path_check(db)
    try:
        with mutating_transaction(db) as tx:
            lock_schedule(tx)
            return admin.execute(tx, token, operation)
    except IntegrityError as err:
        name = getattr(getattr(err.orig, "diag", None), "constraint_name", None)
        if name in _ADMIN_CONSTRAINTS:
            reason, message = _ADMIN_CONSTRAINTS[name]
            raise MasterclassAdminConflictError(reason, message) from err
        sqlstate = getattr(err.orig, "sqlstate", None)
        if sqlstate in {"23503", "23514", "23502"}:
            raise admin.AdminInvalid("That change violates a data constraint.") from err
        raise UnexpectedConstraintViolationError("unexpected constraint violation") from err


@admin_router.get("/masterclass-program")
def admin_program(admin_user: CurrentAdmin, db: DbSession) -> dict[str, Any]:
    _read(admin_user)
    return mc_admin.program_rows(db)


@admin_router.get("/masterclasses/{masterclass_id}/reservations")
def admin_reservations(
    masterclass_id: RowId, admin_user: CurrentAdmin, db: DbSession
) -> dict[str, Any]:
    """Confirmed seats and the waitlist in queue order (names and e-mail)."""
    _read(admin_user)
    return mc_admin.reservation_rows(db, masterclass_id)


def _skeleton(tx: Session, actor: Any) -> admin.AdminResult:
    """The confirmed frame of the programme (masterclasses/skeleton.py): the event
    on its date in Asia/Baku, its two blocks and five rooms - never a masterclass,
    a speaker or a close. Adds only what is missing; re-running changes nothing."""
    from sqlalchemy import select

    from app.modules.masterclasses.models import MasterclassEvent
    from app.modules.masterclasses.skeleton import EVENT_CODE, create_program_skeleton

    codes = set(tx.execute(select(MasterclassEvent.code)).scalars())
    if codes and EVENT_CODE not in codes:  # never a second event beside one entered by hand
        raise admin.AdminConflict("An event already exists; edit it instead.")
    report = create_program_skeleton(tx)
    after = {
        "event_created": report.event_created,
        "blocks_created": report.blocks_created,
        "rooms_created": report.rooms_created,
    }
    return "masterclass.skeleton", "masterclass_event", None, after, after


@admin_router.post("/masterclass-skeleton")
def create_skeleton(token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(db, token, _skeleton)


@admin_router.post("/masterclass-events", status_code=201)
def create_event(payload: EventWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: mc_admin.create_event(tx, actor, payload))


@admin_router.patch("/masterclass-events/{event_id}")
def edit_event(
    event_id: RowId, payload: EventEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _admin_write(
        db, token, lambda tx, actor: mc_admin.edit_event(tx, actor, event_id, payload)
    )


@admin_router.post("/masterclass-blocks", status_code=201)
def create_block(payload: BlockWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: mc_admin.create_block(tx, actor, payload))


@admin_router.patch("/masterclass-blocks/{block_id}")
def edit_block(
    block_id: RowId, payload: BlockEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _admin_write(
        db, token, lambda tx, actor: mc_admin.edit_block(tx, actor, block_id, payload)
    )


@admin_router.post("/masterclass-rooms", status_code=201)
def create_room(payload: RoomWrite, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(db, token, lambda tx, actor: mc_admin.create_room(tx, actor, payload))


@admin_router.patch("/masterclass-rooms/{room_id}")
def edit_room(
    room_id: RowId, payload: RoomEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _admin_write(
        db, token, lambda tx, actor: mc_admin.edit_room(tx, actor, room_id, payload)
    )


@admin_router.post("/masterclasses", status_code=201)
def create_masterclass(
    payload: MasterclassWrite, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _admin_write(
        db, token, lambda tx, actor: mc_admin.create_masterclass(tx, actor, payload)
    )


@admin_router.patch("/masterclasses/{masterclass_id}")
def edit_masterclass(
    masterclass_id: RowId, payload: MasterclassEdit, token: SessionToken, db: DbSession
) -> dict[str, Any]:
    return _admin_write(
        db, token, lambda tx, actor: mc_admin.edit_masterclass(tx, actor, masterclass_id, payload)
    )


@admin_router.delete("/masterclasses/{masterclass_id}")
def delete_masterclass(masterclass_id: RowId, token: SessionToken, db: DbSession) -> dict[str, Any]:
    return _admin_write(
        db, token, lambda tx, actor: mc_admin.delete_masterclass(tx, actor, masterclass_id)
    )
