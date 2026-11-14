"""Meet the Mentor: configured 20-minute groups and their bookings

Revision ID: 0022_mentors
Revises: 0021_people
Create Date: 2026-10-17

Additive: two new tables and one trigger, no existing row or constraint touched,
nothing inserted (no mentor or session is invented). Database invariants: a
session lasts exactly 20 minutes with a group of 1-6; one active booking per
attendee (`uq_mentor_bookings_one_active`); one live booking per attendee and
session; CONFIRMED + ATTENDED never exceed the group size, even for SQL writers
outside the application (`ck_mentor_capacity`, raised by the trigger).
Downgrade refuses while any booking exists, then drops exactly these objects.
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0022_mentors"
down_revision: str | None = "0021_people"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def _id() -> sa.Column[Any]:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "mentor_sessions",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("mentor_name", sa.Text(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capacity", sa.Integer(), server_default="6", nullable=False),
        sa.Column("waitlist_capacity", sa.Integer(), server_default="3", nullable=False),
        sa.Column("is_open", sa.Boolean(), server_default="true", nullable=False),
        _id(),
        sa.CheckConstraint(
            "ends_at = starts_at + interval '20 minutes'", name=op.f("ck_mentor_sessions_duration")
        ),
        sa.CheckConstraint(
            "capacity BETWEEN 1 AND 6", name=op.f("ck_mentor_sessions_capacity_range")
        ),
        sa.CheckConstraint(
            "waitlist_capacity BETWEEN 0 AND 20", name=op.f("ck_mentor_sessions_waitlist_range")
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200", name=op.f("ck_mentor_sessions_title_length")
        ),
        sa.CheckConstraint(
            "mentor_name IS NULL OR char_length(mentor_name) BETWEEN 1 AND 200",
            name=op.f("ck_mentor_sessions_mentor_name_length"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["masterclass_events.id"], name="fk_mentor_sessions_event_id"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_sessions")),
        sa.UniqueConstraint("event_id", "code", name="uq_mentor_sessions_event_code"),
    )
    op.create_table(
        "mentor_bookings",
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("command_id", sa.Text(), nullable=False),
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
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("attended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attended_by_staff_id", sa.BigInteger(), nullable=True),
        _id(),
        sa.CheckConstraint(
            "status IN ('CONFIRMED', 'WAITLISTED', 'CANCELLED', 'ATTENDED')",
            name=op.f("ck_mentor_bookings_status_known"),
        ),
        sa.CheckConstraint(
            "(status = 'ATTENDED') = (attended_at IS NOT NULL) "
            "AND (attended_at IS NULL) = (attended_by_staff_id IS NULL)",
            name=op.f("ck_mentor_bookings_attendance_shape"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_mentor_bookings_version_positive")),
        sa.ForeignKeyConstraint(
            ["session_id"], ["mentor_sessions.id"], name="fk_mentor_bookings_session_id"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_mentor_bookings_user_id"),
        sa.ForeignKeyConstraint(
            ["attended_by_staff_id"], ["staff_accounts.id"], name="fk_mentor_bookings_attended_by"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mentor_bookings")),
        sa.UniqueConstraint("command_id", name=op.f("uq_mentor_bookings_command_id")),
    )
    op.create_index(
        "uq_mentor_bookings_one_active",
        "mentor_bookings",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('CONFIRMED', 'WAITLISTED')"),
    )
    op.create_index(
        "uq_mentor_bookings_session_user",
        "mentor_bookings",
        ["session_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'CANCELLED'"),
    )
    op.create_index(
        "ix_mentor_bookings_session_queue",
        "mentor_bookings",
        ["session_id", "status", "created_at", "id"],
    )
    # Locks the session row even for SQL writers outside the application and
    # keeps the seated group within its configured size.
    op.execute("""
CREATE FUNCTION mentor_capacity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE size integer;
BEGIN
 SELECT capacity INTO size FROM mentor_sessions WHERE id=NEW.session_id FOR UPDATE;
 IF NEW.status IN ('CONFIRMED','ATTENDED') AND
   (SELECT count(*) FROM mentor_bookings
    WHERE session_id=NEW.session_id AND id<>COALESCE(NEW.id,0)
    AND status IN ('CONFIRMED','ATTENDED')) >= size
 THEN RAISE EXCEPTION 'mentor group full'
 USING ERRCODE='23514', CONSTRAINT='ck_mentor_capacity'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER mentor_capacity BEFORE INSERT OR UPDATE ON mentor_bookings
FOR EACH ROW EXECUTE FUNCTION mentor_capacity_guard();
""")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM mentor_bookings)")).scalar():
        raise RuntimeError(
            "mentor_bookings holds attendee history; export it and clear it explicitly "
            "before downgrading below 0022_mentors."
        )
    op.drop_index("ix_mentor_bookings_session_queue", table_name="mentor_bookings")
    op.drop_index("uq_mentor_bookings_session_user", table_name="mentor_bookings")
    op.drop_index("uq_mentor_bookings_one_active", table_name="mentor_bookings")
    op.drop_table("mentor_bookings")
    op.execute("DROP FUNCTION mentor_capacity_guard()")
    op.drop_table("mentor_sessions")
