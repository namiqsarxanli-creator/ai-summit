"""The Beyond Now masterclass programme and its reservations (migration 0017).

Programme (managed by an administrator, nothing hard-coded in a client):

* `masterclass_events` - the event: title, venue, the IANA time zone every time
  is shown in, and the day.
* `masterclass_blocks` - a time block of an event (start, end, and the moment
  after which an attendee can no longer change a choice in it).
* `masterclass_rooms` - the rooms of an event.
* `masterclasses` - one session: title, description, speaker, capacity and
  waitlist size. `block_id` / `room_id` are nullable on purpose: a masterclass
  whose slot is not decided yet (the LOGO masterclass) exists without one and
  cannot be booked until an administrator schedules it.

Reservations (`masterclass_reservations`) are the attendee's choices. A row is
CONFIRMED (holds a seat), WAITLISTED (in the queue) or CANCELLED (history - holds
nothing). The invariants are held by the database, not only by the service:

* one ACTIVE (CONFIRMED or WAITLISTED) choice per attendee per block - a partial
  unique index (`uq_masterclass_reservations_one_active_per_block`);
* a reservation's block is its masterclass's block - a composite foreign key onto
  `masterclasses (id, block_id)`;
* at most `capacity` CONFIRMED and at most `waitlist_capacity` WAITLISTED rows per
  masterclass - a trigger that locks the masterclass row and counts
  (`ck_masterclass_reservations_capacity` / `..._waitlist_capacity`), and a
  trigger on `masterclasses` refusing a capacity below what is already held.

A reservation is not attendance: nothing here records presence, scans or points.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin

CONFIRMED: Final = "CONFIRMED"
WAITLISTED: Final = "WAITLISTED"
CANCELLED: Final = "CANCELLED"
ACTIVE_STATUSES: Final = (CONFIRMED, WAITLISTED)

# Bounds held by CHECKs too (and by the admin schemas, for a clear 422 first).
MAX_CAPACITY: Final = 500
MAX_WAITLIST: Final = 100


class MasterclassEvent(IdentityPKMixin, Base):
    __tablename__ = "masterclass_events"

    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    venue: Mapped[str | None] = mapped_column(Text)
    # Every time of this event is SHOWN in this zone; all times are stored as
    # timestamptz, so the zone never changes what a moment is.
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Migration 0020: activity at or after this moment earns no competition
    # points (17:30 Asia/Baku). NULL = the competition is not scheduled yet.
    competition_closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("char_length(code) BETWEEN 1 AND 64", name="code_length"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint("char_length(timezone) BETWEEN 1 AND 64", name="timezone_length"),
    )


class MasterclassBlock(IdentityPKMixin, Base):
    display_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __tablename__ = "masterclass_blocks"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("masterclass_events.id", name="fk_masterclass_blocks_event_id"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Changes are accepted strictly BEFORE this moment (server clock).
    change_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "code", name="uq_masterclass_blocks_event_code"),
        CheckConstraint(
            "display_number IS NULL OR display_number BETWEEN 1 AND 10000",
            name="display_number_range",
        ),
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        CheckConstraint("change_deadline <= starts_at", name="deadline_before_start"),
        CheckConstraint("char_length(code) BETWEEN 1 AND 64", name="code_length"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
    )


class MasterclassRoom(IdentityPKMixin, Base):
    display_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __tablename__ = "masterclass_rooms"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("masterclass_events.id", name="fk_masterclass_rooms_event_id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        UniqueConstraint("event_id", "name", name="uq_masterclass_rooms_event_name"),
        CheckConstraint(
            "display_number IS NULL OR display_number BETWEEN 1 AND 10000",
            name="display_number_range",
        ),
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
    )


class Masterclass(IdentityPKMixin, Base):
    __tablename__ = "masterclasses"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("masterclass_events.id", name="fk_masterclasses_event_id"), nullable=False
    )
    block_id: Mapped[int | None] = mapped_column(
        ForeignKey("masterclass_blocks.id", name="fk_masterclasses_block_id"), nullable=True
    )
    room_id: Mapped[int | None] = mapped_column(
        ForeignKey("masterclass_rooms.id", name="fk_masterclasses_room_id"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    speaker: Mapped[str | None] = mapped_column(Text)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="40")
    waitlist_capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="10")
    # Migration 0020: people a door volunteer may admit standing once every seat
    # is taken (PDF: "Zal imkan verirsə ... ayaq üstə"). 0 = no standing at all;
    # never an invented unlimited capacity.
    standing_capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Hidden from attendees until published; an unpublished one cannot be booked.
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # Target of the reservations' composite FK: (id, block_id) is unique
        # because id alone is.
        UniqueConstraint("id", "block_id", name="uq_masterclasses_id_block"),
        CheckConstraint(f"capacity BETWEEN 1 AND {MAX_CAPACITY}", name="capacity_range"),
        CheckConstraint(
            f"waitlist_capacity BETWEEN 0 AND {MAX_WAITLIST}", name="waitlist_capacity_range"
        ),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint(
            "description IS NULL OR char_length(description) <= 2000", name="description_length"
        ),
        CheckConstraint(
            "speaker IS NULL OR char_length(speaker) BETWEEN 1 AND 300", name="speaker_length"
        ),
        # A room hosts one masterclass per block.
        Index(
            "uq_masterclasses_block_room",
            "block_id",
            "room_id",
            unique=True,
            postgresql_where=text("block_id IS NOT NULL AND room_id IS NOT NULL"),
        ),
        Index("ix_masterclasses_event_block", "event_id", "block_id"),
    )


class MasterclassReservation(IdentityPKMixin, Base):
    __tablename__ = "masterclass_reservations"

    masterclass_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_masterclass_reservations_user_id_users"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # The waitlist order: when the attendee joined the queue, then `id`.
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Why it ended: the attendee cancelled, or replaced it with another choice.
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["masterclass_id", "block_id"],
            ["masterclasses.id", "masterclasses.block_id"],
            # No ON UPDATE: a masterclass that has reservations cannot move to
            # another block (the service refuses first; this refuses regardless).
            name="fk_masterclass_reservations_masterclass_block",
        ),
        CheckConstraint("status IN ('CONFIRMED', 'WAITLISTED', 'CANCELLED')", name="status_known"),
        CheckConstraint(
            "status <> 'WAITLISTED' OR queued_at IS NOT NULL", name="waitlist_has_queue_time"
        ),
        CheckConstraint(
            "status <> 'CONFIRMED' OR confirmed_at IS NOT NULL", name="confirmed_has_time"
        ),
        CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)", name="cancelled_has_time"
        ),
        CheckConstraint(
            "cancel_reason IS NULL OR cancel_reason IN "
            "('CANCELLED_BY_ATTENDEE', 'REPLACED', 'NO_SHOW')",
            name="cancel_reason_known",
        ),
        Index(
            "uq_masterclass_reservations_one_active_per_block",
            "user_id",
            "block_id",
            unique=True,
            postgresql_where=text("status IN ('CONFIRMED', 'WAITLISTED')"),
        ),
        Index(
            "ix_masterclass_reservations_queue",
            "masterclass_id",
            "queued_at",
            "id",
            postgresql_where=text("status = 'WAITLISTED'"),
        ),
        Index(
            "ix_masterclass_reservations_confirmed",
            "masterclass_id",
            postgresql_where=text("status = 'CONFIRMED'"),
        ),
        Index("ix_masterclass_reservations_user", "user_id"),
    )
