"""Beyond Now masterclass programme and reservations

Revision ID: 0017_masterclasses
Revises: 0016_interest_card
Create Date: 2026-10-16

Additive only: five new tables and two triggers; no existing table, row or
constraint is touched. No data is inserted - not even the event or its blocks:
the programme is entered by an administrator (or the `masterclass-skeleton`
CLI), and no reservation or attendance is invented for anyone.

* `masterclass_events`, `masterclass_blocks`, `masterclass_rooms`,
  `masterclasses` - the programme (app/modules/masterclasses/models.py).
* `masterclass_reservations` - the attendees' choices. One ACTIVE choice per
  attendee per block is a partial unique index; a reservation's block is its
  masterclass's block through a composite foreign key.
* Trigger `trg_masterclass_reservations_capacity` (BEFORE INSERT / UPDATE OF
  status, masterclass_id): locks the masterclass row and refuses a CONFIRMED row
  beyond `capacity` or a WAITLISTED row beyond `waitlist_capacity`
  (SQLSTATE 23514, constraint `ck_masterclass_reservations_capacity` /
  `ck_masterclass_reservations_waitlist_capacity`). Locking the row serialises
  every writer of that masterclass, so two concurrent last-seat inserts cannot
  both pass, whichever code path writes.
* Trigger `trg_masterclasses_capacity_guard` (BEFORE UPDATE OF capacity,
  waitlist_capacity): refuses a capacity below the seats already confirmed, or a
  waitlist size below the people already waiting.

Downgrade drops exactly these triggers, functions and tables.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017_masterclasses"
down_revision: str | None = "0016_interest_card"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


_RESERVATION_CAPACITY_FN = """
CREATE FUNCTION masterclass_reservations_capacity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    seats integer;
    queue integer;
    held integer;
BEGIN
    IF NEW.status NOT IN ('CONFIRMED', 'WAITLISTED') THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.status = NEW.status
            AND OLD.masterclass_id = NEW.masterclass_id THEN
        RETURN NEW;
    END IF;
    -- Serialises every writer of this masterclass: a concurrent insert waits
    -- here until the other transaction ends, then counts what it committed.
    SELECT capacity, waitlist_capacity INTO seats, queue
      FROM masterclasses WHERE id = NEW.masterclass_id FOR UPDATE;
    SELECT count(*) INTO held FROM masterclass_reservations
     WHERE masterclass_id = NEW.masterclass_id AND status = NEW.status AND id <> NEW.id;
    IF NEW.status = 'CONFIRMED' AND held >= seats THEN
        RAISE EXCEPTION 'masterclass is full'
            USING ERRCODE = '23514', CONSTRAINT = 'ck_masterclass_reservations_capacity';
    END IF;
    IF NEW.status = 'WAITLISTED' AND held >= queue THEN
        RAISE EXCEPTION 'masterclass waitlist is full'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'ck_masterclass_reservations_waitlist_capacity';
    END IF;
    RETURN NEW;
END;
$$
"""

_MASTERCLASS_GUARD_FN = """
CREATE FUNCTION masterclasses_capacity_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.capacity < (SELECT count(*) FROM masterclass_reservations
                        WHERE masterclass_id = NEW.id AND status = 'CONFIRMED') THEN
        RAISE EXCEPTION 'capacity below confirmed seats'
            USING ERRCODE = '23514', CONSTRAINT = 'ck_masterclasses_capacity_not_below_confirmed';
    END IF;
    IF NEW.waitlist_capacity < (SELECT count(*) FROM masterclass_reservations
                                 WHERE masterclass_id = NEW.id AND status = 'WAITLISTED') THEN
        RAISE EXCEPTION 'waitlist below waiting attendees'
            USING ERRCODE = '23514', CONSTRAINT = 'ck_masterclasses_waitlist_not_below_waiting';
    END IF;
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.create_table(
        "masterclass_events",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("venue", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        *_timestamps(),
        _id(),
        sa.CheckConstraint(
            "char_length(code) BETWEEN 1 AND 64", name=op.f("ck_masterclass_events_code_length")
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200",
            name=op.f("ck_masterclass_events_title_length"),
        ),
        sa.CheckConstraint(
            "char_length(timezone) BETWEEN 1 AND 64",
            name=op.f("ck_masterclass_events_timezone_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_masterclass_events")),
        sa.UniqueConstraint("code", name=op.f("uq_masterclass_events_code")),
    )
    op.create_table(
        "masterclass_blocks",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("change_deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        _id(),
        sa.CheckConstraint(
            "ends_at > starts_at", name=op.f("ck_masterclass_blocks_ends_after_start")
        ),
        sa.CheckConstraint(
            "change_deadline <= starts_at", name=op.f("ck_masterclass_blocks_deadline_before_start")
        ),
        sa.CheckConstraint(
            "char_length(code) BETWEEN 1 AND 64", name=op.f("ck_masterclass_blocks_code_length")
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200",
            name=op.f("ck_masterclass_blocks_title_length"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["masterclass_events.id"], name="fk_masterclass_blocks_event_id"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_masterclass_blocks")),
        sa.UniqueConstraint("event_id", "code", name="uq_masterclass_blocks_event_code"),
    )
    op.create_table(
        "masterclass_rooms",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        _id(),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100", name=op.f("ck_masterclass_rooms_name_length")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["masterclass_events.id"], name="fk_masterclass_rooms_event_id"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_masterclass_rooms")),
        sa.UniqueConstraint("event_id", "name", name="uq_masterclass_rooms_event_name"),
    )
    op.create_table(
        "masterclasses",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("block_id", sa.BigInteger(), nullable=True),
        sa.Column("room_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("speaker", sa.Text(), nullable=True),
        sa.Column("capacity", sa.Integer(), server_default="40", nullable=False),
        sa.Column("waitlist_capacity", sa.Integer(), server_default="10", nullable=False),
        sa.Column("is_published", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        _id(),
        sa.CheckConstraint(
            "capacity BETWEEN 1 AND 500", name=op.f("ck_masterclasses_capacity_range")
        ),
        sa.CheckConstraint(
            "waitlist_capacity BETWEEN 0 AND 100",
            name=op.f("ck_masterclasses_waitlist_capacity_range"),
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200", name=op.f("ck_masterclasses_title_length")
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) <= 2000",
            name=op.f("ck_masterclasses_description_length"),
        ),
        sa.CheckConstraint(
            "speaker IS NULL OR char_length(speaker) BETWEEN 1 AND 300",
            name=op.f("ck_masterclasses_speaker_length"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["masterclass_events.id"], name="fk_masterclasses_event_id"
        ),
        sa.ForeignKeyConstraint(
            ["block_id"], ["masterclass_blocks.id"], name="fk_masterclasses_block_id"
        ),
        sa.ForeignKeyConstraint(
            ["room_id"], ["masterclass_rooms.id"], name="fk_masterclasses_room_id"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_masterclasses")),
        sa.UniqueConstraint("id", "block_id", name="uq_masterclasses_id_block"),
    )
    op.create_index(
        "uq_masterclasses_block_room",
        "masterclasses",
        ["block_id", "room_id"],
        unique=True,
        postgresql_where=sa.text("block_id IS NOT NULL AND room_id IS NOT NULL"),
    )
    op.create_index("ix_masterclasses_event_block", "masterclasses", ["event_id", "block_id"])
    op.create_table(
        "masterclass_reservations",
        sa.Column("masterclass_id", sa.BigInteger(), nullable=False),
        sa.Column("block_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        *_timestamps(),
        _id(),
        sa.CheckConstraint(
            "status IN ('CONFIRMED', 'WAITLISTED', 'CANCELLED')",
            name=op.f("ck_masterclass_reservations_status_known"),
        ),
        sa.CheckConstraint(
            "status <> 'WAITLISTED' OR queued_at IS NOT NULL",
            name=op.f("ck_masterclass_reservations_waitlist_has_queue_time"),
        ),
        sa.CheckConstraint(
            "status <> 'CONFIRMED' OR confirmed_at IS NOT NULL",
            name=op.f("ck_masterclass_reservations_confirmed_has_time"),
        ),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name=op.f("ck_masterclass_reservations_cancelled_has_time"),
        ),
        sa.CheckConstraint(
            "cancel_reason IS NULL OR cancel_reason IN ('CANCELLED_BY_ATTENDEE', 'REPLACED')",
            name=op.f("ck_masterclass_reservations_cancel_reason_known"),
        ),
        sa.ForeignKeyConstraint(
            ["masterclass_id", "block_id"],
            ["masterclasses.id", "masterclasses.block_id"],
            name="fk_masterclass_reservations_masterclass_block",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_masterclass_reservations_user_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_masterclass_reservations")),
    )
    op.create_index(
        "uq_masterclass_reservations_one_active_per_block",
        "masterclass_reservations",
        ["user_id", "block_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('CONFIRMED', 'WAITLISTED')"),
    )
    op.create_index(
        "ix_masterclass_reservations_queue",
        "masterclass_reservations",
        ["masterclass_id", "queued_at", "id"],
        postgresql_where=sa.text("status = 'WAITLISTED'"),
    )
    op.create_index(
        "ix_masterclass_reservations_confirmed",
        "masterclass_reservations",
        ["masterclass_id"],
        postgresql_where=sa.text("status = 'CONFIRMED'"),
    )
    op.create_index("ix_masterclass_reservations_user", "masterclass_reservations", ["user_id"])

    op.execute(_RESERVATION_CAPACITY_FN)
    op.execute(
        "CREATE TRIGGER trg_masterclass_reservations_capacity "
        "BEFORE INSERT OR UPDATE OF status, masterclass_id ON masterclass_reservations "
        "FOR EACH ROW EXECUTE FUNCTION masterclass_reservations_capacity()"
    )
    op.execute(_MASTERCLASS_GUARD_FN)
    op.execute(
        "CREATE TRIGGER trg_masterclasses_capacity_guard "
        "BEFORE UPDATE OF capacity, waitlist_capacity ON masterclasses "
        "FOR EACH ROW EXECUTE FUNCTION masterclasses_capacity_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_masterclasses_capacity_guard ON masterclasses")
    op.execute("DROP FUNCTION IF EXISTS masterclasses_capacity_guard()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_masterclass_reservations_capacity ON masterclass_reservations"
    )
    op.execute("DROP FUNCTION IF EXISTS masterclass_reservations_capacity()")
    op.drop_table("masterclass_reservations")
    op.drop_table("masterclasses")
    op.drop_table("masterclass_rooms")
    op.drop_table("masterclass_blocks")
    op.drop_table("masterclass_events")
