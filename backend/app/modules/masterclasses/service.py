"""Masterclass reservations: the attendee's choices, seats and the waitlist.

Every change runs inside the fenced unit of work the router opens
(`mutating_transaction`) and follows one locking order, so concurrent requests
cannot deadlock and always end in a defined state:

0. the shared-mode event-schedule advisory lock (router), excluding programme,
   Playground and mentor edits while permitting independent masterclass choices;
1. the attendee's own `users` row (`FOR UPDATE`, via `lock_attendee`) - two
   devices of one attendee are applied one after the other, never interleaved;
2. the masterclass rows involved, in ascending id order (`FOR UPDATE`) - every
   writer of a masterclass's seats and queue waits here; the capacity trigger
   takes the same lock, so even a writer that skipped this module is held to the
   capacity.

Only after both are held is anything read that a decision depends on (the
attendee's current choice, the seat counts, the queue). Rules:

* One active choice per block (also a partial unique index).
* A seat is given only while one is free AND nobody is queueing - a newcomer can
  never overtake the waitlist.
* The queue is ordered by `queued_at` (taken after the lock is held), then `id`.
* When a seat frees (a cancellation, a change of mind, a larger capacity) the
  first in the queue gets it in the same transaction.
* Changing to another masterclass is one transaction: if the new choice cannot
  be had, nothing changes and the old choice is kept. Giving up a CONFIRMED seat
  for a waitlist place needs the attendee's explicit `give_up_confirmed_seat`.
* Sending the same choice again changes nothing (`unchanged`), so a retry or a
  double tap cannot create a second reservation.
* After the block's change deadline (server clock) nothing an attendee sends
  changes anything.

A reservation is a reservation: nothing here records attendance or points.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import (
    MasterclassChangesClosedError,
    MasterclassChoiceConflictError,
    MasterclassFullError,
    MasterclassNotFoundError,
    MasterclassSeatWouldBeLostError,
    MasterclassWaitlistFullError,
)
from app.core.ratelimit import RateLimit, limiter
from app.modules.attendees.models import User
from app.modules.attendees.profile import lock_attendee
from app.modules.masterclasses.models import (
    ACTIVE_STATUSES,
    CANCELLED,
    CONFIRMED,
    WAITLISTED,
    Masterclass,
    MasterclassBlock,
    MasterclassEvent,
    MasterclassReservation,
    MasterclassRoom,
)
from app.modules.masterclasses.schemas import (
    Availability,
    BlockOut,
    ChoiceOut,
    ChoiceRequest,
    ChoiceResult,
    EventOut,
    MasterclassOut,
    MyChoicesOut,
    Outcome,
    ProgramOut,
    UnscheduledOut,
)

# A person choosing and changing their mind a few times is normal; a script is not.
CHOICE_PER_ATTENDEE = RateLimit("masterclass_choice:attendee", 30, 10 * 60)

REPLACED = "REPLACED"
CANCELLED_BY_ATTENDEE = "CANCELLED_BY_ATTENDEE"


def server_now() -> datetime:
    """The one clock every rule uses (tests replace it). Never the browser's."""
    return datetime.now(UTC)


@dataclass
class ChangeOutcome:
    result: ChoiceResult
    # Attendees whose own state changed in this transaction (for refresh signals).
    affected_user_ids: set[int] = field(default_factory=set)


# --- counts and the queue ------------------------------------------------------------


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


def _seat_counts(db: Session, masterclass_id: int) -> tuple[int, int]:
    """Read both capacities in one round trip while the masterclass is locked.

    Cancelled history contributes to neither count. Keep this a database read,
    not a cache: cancellations and queue promotions must be visible immediately.
    """
    confirmed, waiting = db.execute(
        select(
            func.count().filter(MasterclassReservation.status == CONFIRMED),
            func.count().filter(MasterclassReservation.status == WAITLISTED),
        ).where(MasterclassReservation.masterclass_id == masterclass_id)
    ).one()
    return int(confirmed), int(waiting)


def _lock_masterclasses(db: Session, ids: set[int]) -> dict[int, Masterclass]:
    rows = db.execute(
        select(Masterclass)
        .where(Masterclass.id.in_(sorted(ids)))
        .order_by(Masterclass.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars()
    return {row.id: row for row in rows}


def promote_waiting(db: Session, masterclass: Masterclass, now: datetime) -> list[int]:
    """Fill free seats from the head of the queue. The caller holds the
    masterclass row lock. Returns the promoted attendees' ids."""
    free = masterclass.capacity - _count(db, masterclass.id, CONFIRMED)
    if free <= 0:
        return []
    heads = (
        db.execute(
            select(MasterclassReservation)
            .where(
                MasterclassReservation.masterclass_id == masterclass.id,
                MasterclassReservation.status == WAITLISTED,
            )
            .order_by(MasterclassReservation.queued_at, MasterclassReservation.id)
            .limit(free)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    for row in heads:
        row.status = CONFIRMED
        row.confirmed_at = now
    db.flush()
    return [row.user_id for row in heads]


def _queue_position(db: Session, row: MasterclassReservation) -> int:
    ahead = db.execute(
        select(func.count())
        .select_from(MasterclassReservation)
        .where(
            MasterclassReservation.masterclass_id == row.masterclass_id,
            MasterclassReservation.status == WAITLISTED,
            or_(
                MasterclassReservation.queued_at < row.queued_at,
                and_(
                    MasterclassReservation.queued_at == row.queued_at,
                    MasterclassReservation.id < row.id,
                ),
            ),
        )
    ).scalar_one()
    return int(ahead) + 1


def _availability(confirmed: int, waiting: int, mc: Masterclass) -> Availability:
    if confirmed < mc.capacity and waiting == 0:
        return "available"
    if waiting < mc.waitlist_capacity:
        return "waitlist"
    return "full"


# --- reading --------------------------------------------------------------------------


def program(db: Session, now: datetime) -> ProgramOut:
    """The published programme with seat counts - no attendee data at all."""
    counts: dict[tuple[int, str], int] = {
        (mc_id, status): int(n)
        for mc_id, status, n in db.execute(
            select(
                MasterclassReservation.masterclass_id,
                MasterclassReservation.status,
                func.count(),
            )
            .where(MasterclassReservation.status.in_(ACTIVE_STATUSES))
            .group_by(MasterclassReservation.masterclass_id, MasterclassReservation.status)
        ).all()
    }
    rooms = {r.id: r for r in db.execute(select(MasterclassRoom)).scalars()}
    published = (
        db.execute(
            select(Masterclass)
            .where(Masterclass.is_published.is_(True))
            .order_by(Masterclass.sort_order, Masterclass.id)
        )
        .scalars()
        .all()
    )
    events = []
    for event in db.execute(
        select(MasterclassEvent).order_by(MasterclassEvent.event_date, MasterclassEvent.id)
    ).scalars():
        blocks = []
        for block in db.execute(
            select(MasterclassBlock)
            .where(MasterclassBlock.event_id == event.id)
            .order_by(MasterclassBlock.starts_at, MasterclassBlock.sort_order, MasterclassBlock.id)
        ).scalars():
            sessions = []
            for mc in (m for m in published if m.block_id == block.id):
                confirmed = counts.get((mc.id, CONFIRMED), 0)
                waiting = counts.get((mc.id, WAITLISTED), 0)
                sessions.append(
                    MasterclassOut(
                        id=mc.id,
                        title=mc.title,
                        description=mc.description,
                        speaker=mc.speaker,
                        room=rooms[mc.room_id].name if mc.room_id in rooms else None,
                        room_display_number=rooms[mc.room_id].display_number
                        if mc.room_id in rooms
                        else None,
                        capacity=mc.capacity,
                        waitlist_capacity=mc.waitlist_capacity,
                        confirmed_count=confirmed,
                        waitlist_count=waiting,
                        seats_left=max(mc.capacity - confirmed, 0),
                        availability=_availability(confirmed, waiting, mc),
                    )
                )
            blocks.append(
                BlockOut(
                    id=block.id,
                    code=block.code,
                    display_number=block.display_number,
                    title=block.title,
                    starts_at=block.starts_at,
                    ends_at=block.ends_at,
                    change_deadline=block.change_deadline,
                    changes_open=now < block.change_deadline,
                    masterclasses=sessions,
                )
            )
        events.append(
            EventOut(
                code=event.code,
                title=event.title,
                venue=event.venue,
                timezone=event.timezone,
                event_date=event.event_date,
                blocks=blocks,
                unscheduled=[
                    UnscheduledOut(
                        id=m.id, title=m.title, description=m.description, speaker=m.speaker
                    )
                    for m in published
                    if m.event_id == event.id and m.block_id is None
                ],
            )
        )
    return ProgramOut(server_time=now, events=events)


def my_choices(db: Session, user: User, now: datetime) -> MyChoicesOut:
    # Choice and revision come from ONE statement. Two independent reads could
    # pair an old waitlisted choice with a new confirmed revision during promotion.
    latest = (
        select(func.max(MasterclassReservation.id).label("latest_id"))
        .where(MasterclassReservation.user_id == user.id)
        .group_by(MasterclassReservation.block_id)
        .subquery()
    )
    rows = db.execute(
        select(MasterclassReservation, MasterclassBlock.change_deadline)
        .join(latest, MasterclassReservation.id == latest.c.latest_id)
        .join(MasterclassBlock, MasterclassBlock.id == MasterclassReservation.block_id)
        .order_by(MasterclassBlock.starts_at, MasterclassBlock.id)
        .execution_options(populate_existing=True)
    ).all()
    return MyChoicesOut(
        server_time=now,
        revisions={row.block_id: f"{row.id}:{row.status}" for row, _ in rows},
        choices=[
            ChoiceOut(
                block_id=row.block_id,
                masterclass_id=row.masterclass_id,
                status="confirmed" if row.status == CONFIRMED else "waitlisted",
                waitlist_position=_queue_position(db, row) if row.status == WAITLISTED else None,
                change_deadline=deadline,
                changes_open=now < deadline,
            )
            for row, deadline in sorted(rows, key=lambda pair: pair[1])
            if row.status in ACTIVE_STATUSES
        ],
    )


# --- changing ---------------------------------------------------------------------------


def _open_block(db: Session, block_id: int, now: datetime) -> MasterclassBlock:
    block = db.get(MasterclassBlock, block_id)
    if block is None:
        raise MasterclassNotFoundError("No such masterclass block.")
    if not now < block.change_deadline:
        raise MasterclassChangesClosedError("Choices for this block can no longer be changed.")
    return block


def _active_choice(db: Session, user_id: int, block_id: int) -> MasterclassReservation | None:
    """Read WITHOUT a row lock: locking it before the masterclass row would
    invert the order a promotion uses (masterclass, then its queue rows) and
    could deadlock. Only this attendee (serialised by the users-row lock) or a
    promotion (under the masterclass lock) can change it, so callers re-read it
    once the masterclass lock is held."""
    return db.execute(
        select(MasterclassReservation)
        .where(
            MasterclassReservation.user_id == user_id,
            MasterclassReservation.block_id == block_id,
            MasterclassReservation.status.in_(ACTIVE_STATUSES),
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def choice_revision(db: Session, user_id: int, block_id: int) -> str:
    """Includes cancelled history, so stale creates cannot resurrect a choice.
    Read under the attendee lock and, for an active choice, its masterclass lock.
    Promotions change WAITLISTED to CONFIRMED and invalidate the old revision too.
    """
    latest = db.execute(
        select(MasterclassReservation.id, MasterclassReservation.status)
        .where(
            MasterclassReservation.user_id == user_id, MasterclassReservation.block_id == block_id
        )
        .order_by(MasterclassReservation.id.desc())
        .limit(1)
    ).first()
    return "0" if latest is None else f"{latest.id}:{latest.status}"


def _check_revision(db: Session, user: User, block_id: int, expected: str) -> None:
    if choice_revision(db, user.id, block_id) != expected:
        raise MasterclassChoiceConflictError(
            "Your choice changed. Refresh and review it before trying again."
        )


def _end(row: MasterclassReservation, reason: str, now: datetime) -> None:
    row.status = CANCELLED
    row.cancelled_at = now
    row.cancel_reason = reason


def _result(db: Session, user: User, outcome: Outcome, affected: set[int]) -> ChangeOutcome:
    return ChangeOutcome(
        ChoiceResult(outcome=outcome, choices=my_choices(db, user, server_now())), affected
    )


def choose(
    db: Session, *, raw_session_token: str | None, block_id: int, request: ChoiceRequest
) -> ChangeOutcome:
    user = lock_attendee(db, raw_session_token)
    limiter.check([(CHOICE_PER_ATTENDEE, str(user.id))])
    now = server_now()
    block = _open_block(db, block_id, now)

    from app.modules.playground.service import check_masterclass_overlap

    check_masterclass_overlap(db, user.id, block)
    target = db.get(Masterclass, request.masterclass_id)
    if target is None or not target.is_published or target.block_id != block_id:
        raise MasterclassNotFoundError("No such masterclass in this block.")

    current = _active_choice(db, user.id, block_id)
    if current is not None and current.masterclass_id == target.id:
        return _result(db, user, "unchanged", set())

    locked = _lock_masterclasses(
        db, {target.id} | ({current.masterclass_id} if current is not None else set())
    )
    target = locked[target.id]
    if not target.is_published or target.block_id != block_id:  # changed while we waited
        raise MasterclassNotFoundError("No such masterclass in this block.")
    current = _active_choice(db, user.id, block_id)  # fresh, now that its masterclass is locked
    _check_revision(db, user, block_id, request.expected_revision)
    confirmed, waiting = _seat_counts(db, target.id)

    if confirmed < target.capacity and waiting == 0:
        new_status = CONFIRMED
    else:
        if waiting >= target.waitlist_capacity:
            raise MasterclassWaitlistFullError("There is no place left in this masterclass.")
        if not request.join_waitlist:
            raise MasterclassFullError(waitlist_open=True)
        if (
            current is not None
            and current.status == CONFIRMED
            and not request.give_up_confirmed_seat
        ):
            raise MasterclassSeatWouldBeLostError(
                "Joining this waitlist would give up your confirmed seat."
            )
        new_status = WAITLISTED

    affected = {user.id}
    stamp = server_now()  # after the locks: the queue order is the order of arrival here
    if current is not None:
        held_seat = current.status == CONFIRMED
        _end(current, REPLACED, stamp)
        db.flush()
        if held_seat:
            affected.update(promote_waiting(db, locked[current.masterclass_id], stamp))
    db.add(
        MasterclassReservation(
            masterclass_id=target.id,
            block_id=block_id,
            user_id=user.id,
            status=new_status,
            queued_at=stamp if new_status == WAITLISTED else None,
            confirmed_at=stamp if new_status == CONFIRMED else None,
        )
    )
    db.flush()
    outcome: Outcome = "confirmed" if new_status == CONFIRMED else "waitlisted"
    return _result(db, user, outcome, affected)


def cancel(
    db: Session, *, raw_session_token: str | None, block_id: int, expected_revision: str
) -> ChangeOutcome:
    user = lock_attendee(db, raw_session_token)
    limiter.check([(CHOICE_PER_ATTENDEE, str(user.id))])
    now = server_now()
    _open_block(db, block_id, now)

    current = _active_choice(db, user.id, block_id)
    if current is None:  # nothing to cancel: a repeated cancel is not an error
        return _result(db, user, "unchanged", set())
    locked = _lock_masterclasses(db, {current.masterclass_id})
    current = _active_choice(db, user.id, block_id)
    if current is None:
        return _result(db, user, "unchanged", set())
    _check_revision(db, user, block_id, expected_revision)
    held_seat = current.status == CONFIRMED
    stamp = server_now()
    _end(current, CANCELLED_BY_ATTENDEE, stamp)
    db.flush()
    affected = {user.id}
    if held_seat:
        affected.update(promote_waiting(db, locked[current.masterclass_id], stamp))
    return _result(db, user, "cancelled", affected)
