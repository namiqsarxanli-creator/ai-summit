"""Playground seats and invitation holds. A booking is not attendance or points."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin


class PlaygroundSlot(IdentityPKMixin, Base):
    __tablename__ = "playground_slots"
    event_id: Mapped[int] = mapped_column(ForeignKey("masterclass_events.id"), nullable=False)
    booth_id: Mapped[int] = mapped_column(ForeignKey("booths.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("event_id", "booth_id", "number", name="uq_playground_slot_number"),
        CheckConstraint("number BETWEEN 1 AND 36", name="number_range"),
        CheckConstraint("ends_at = starts_at + interval '15 minutes'", name="duration"),
        Index("ix_playground_slots_event_start", "event_id", "starts_at"),
    )


class PlaygroundBooking(IdentityPKMixin, Base):
    __tablename__ = "playground_bookings"
    slot_id: Mapped[int] = mapped_column(ForeignKey("playground_slots.id"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    inviter_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # The creation command can be retried without consuming another seat.
    command_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        CheckConstraint(
            "status IN ('CONFIRMED','INVITED','WAITLISTED','CHECKED_IN',"
            "'COMPLETED','CANCELLED','EXPIRED','NO_SHOW')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'INVITED' AND user_id IS NULL AND inviter_id IS NOT NULL "
            "AND expires_at IS NOT NULL) OR (status <> 'INVITED')",
            name="invitation_shape",
        ),
        CheckConstraint(
            "status NOT IN ('CONFIRMED','WAITLISTED','CHECKED_IN',"
            "'COMPLETED') OR user_id IS NOT NULL",
            name="member_required",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        Index(
            "uq_playground_booking_active_user",
            "slot_id",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('CONFIRMED','WAITLISTED','CHECKED_IN','COMPLETED')"),
        ),
        Index("ix_playground_booking_user", "user_id", "status"),
        Index("ix_playground_booking_slot_queue", "slot_id", "status", "created_at", "id"),
    )
