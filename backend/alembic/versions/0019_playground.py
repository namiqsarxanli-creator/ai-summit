"""Playground slots, invitation holds and individual seats; no data seeding."""

import sqlalchemy as sa

from alembic import op

revision = "0019_playground"
down_revision = "0018_masterclass_labels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "playground_slots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "event_id", sa.BigInteger(), sa.ForeignKey("masterclass_events.id"), nullable=False
        ),
        sa.Column("booth_id", sa.BigInteger(), sa.ForeignKey("booths.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "booth_id", "number", name="uq_playground_slot_number"),
        sa.CheckConstraint(
            "number BETWEEN 1 AND 36", name=op.f("ck_playground_slots_number_range")
        ),
        sa.CheckConstraint(
            "ends_at = starts_at + interval '15 minutes'", name=op.f("ck_playground_slots_duration")
        ),
    )
    op.create_index(
        "ix_playground_slots_event_start", "playground_slots", ["event_id", "starts_at"]
    )
    op.create_table(
        "playground_bookings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("slot_id", sa.BigInteger(), sa.ForeignKey("playground_slots.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("inviter_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("command_id", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "status IN ('CONFIRMED','INVITED','WAITLISTED','CHECKED_IN',"
            "'COMPLETED','CANCELLED','EXPIRED','NO_SHOW')",
            name=op.f("ck_playground_bookings_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'INVITED' AND user_id IS NULL AND inviter_id IS NOT NULL "
            "AND expires_at IS NOT NULL) OR (status <> 'INVITED')",
            name=op.f("ck_playground_bookings_invitation_shape"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('CONFIRMED','WAITLISTED','CHECKED_IN',"
            "'COMPLETED') OR user_id IS NOT NULL",
            name=op.f("ck_playground_bookings_member_required"),
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_playground_bookings_version_positive")),
    )
    op.create_index(
        "uq_playground_booking_active_user",
        "playground_bookings",
        ["slot_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('CONFIRMED','WAITLISTED','CHECKED_IN','COMPLETED')"),
    )
    op.create_index("ix_playground_booking_user", "playground_bookings", ["user_id", "status"])
    op.create_index(
        "ix_playground_booking_slot_queue",
        "playground_bookings",
        ["slot_id", "status", "created_at", "id"],
    )
    # Locks the slot even for SQL writers outside the application service. It
    # counts by STATUS only, never by the database clock: the application (whose
    # clock tests can control) marks lapsed invitations EXPIRED under the same
    # lock before every write, so both sides always agree on what a seat is.
    op.execute("""
CREATE FUNCTION playground_capacity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM id FROM playground_slots WHERE id=NEW.slot_id FOR UPDATE;
 IF NEW.status IN ('CONFIRMED','INVITED','CHECKED_IN','COMPLETED') AND
   (SELECT count(*) FROM playground_bookings
    WHERE slot_id=NEW.slot_id AND id<>COALESCE(NEW.id,0)
    AND status IN ('CONFIRMED','INVITED','CHECKED_IN','COMPLETED')) >= 3
 THEN RAISE EXCEPTION 'slot full'
 USING ERRCODE='23514', CONSTRAINT='ck_playground_capacity'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER playground_capacity BEFORE INSERT OR UPDATE ON playground_bookings
FOR EACH ROW EXECUTE FUNCTION playground_capacity_guard();
""")


def downgrade() -> None:
    # RESTRICT is deliberate: no silent cascading removal of operational history.
    op.drop_table("playground_bookings")
    op.execute("DROP FUNCTION playground_capacity_guard()")
    op.drop_table("playground_slots")
