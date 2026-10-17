"""Meet the Mentor: 20-minute groups at the Connection Hub (migration 0022).

PDF ("Connection Hub-da 3 görüş küncü", "Meet the Mentor"): groups of 6 with a
speaker or expert, sessions at 13:10, 13:35 and 15:35, registered in the app; at
the end the host scans each attendee's QR: +5, once in total.

* `mentor_sessions` - configured by an administrator: the topic, the mentor's
  name as announced, the time and the group size. Nothing is seeded; no mentor
  is invented. The 20 minutes and the size limit are database checks.
* `mentor_bookings` - an attendee's place: CONFIRMED (a seat), WAITLISTED (FIFO,
  up to the session's `waitlist_capacity`), CANCELLED, ATTENDED (scanned by the
  host). An attendee holds at most ONE active (confirmed or waitlisted) booking
  at a time (`uq_mentor_bookings_one_active`); a waiting place reserves the time
  as well, so a promotion can never create an overlap. A trigger keeps
  CONFIRMED + ATTENDED within the group size even for direct SQL writers.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
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

STATUSES = ("CONFIRMED", "WAITLISTED", "CANCELLED", "ATTENDED")
ACTIVE = ("CONFIRMED", "WAITLISTED")
SEATED = ("CONFIRMED", "ATTENDED")


class MentorSession(IdentityPKMixin, Base):
    __tablename__ = "mentor_sessions"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("masterclass_events.id", name="fk_mentor_sessions_event_id"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    mentor_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="6")
    waitlist_capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    # Closed sessions are hidden from attendees and take no new bookings.
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        UniqueConstraint("event_id", "code", name="uq_mentor_sessions_event_code"),
        CheckConstraint("ends_at = starts_at + interval '20 minutes'", name="duration"),
        CheckConstraint("capacity BETWEEN 1 AND 6", name="capacity_range"),
        CheckConstraint("waitlist_capacity BETWEEN 0 AND 20", name="waitlist_range"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint(
            "mentor_name IS NULL OR char_length(mentor_name) BETWEEN 1 AND 200",
            name="mentor_name_length",
        ),
    )


class MentorBooking(IdentityPKMixin, Base):
    __tablename__ = "mentor_bookings"

    session_id: Mapped[int] = mapped_column(
        ForeignKey("mentor_sessions.id", name="fk_mentor_bookings_session_id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_mentor_bookings_user_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # The client's idempotency key: a retried "book" creates nothing new.
    command_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    attended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attended_by_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_mentor_bookings_attended_by"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('CONFIRMED', 'WAITLISTED', 'CANCELLED', 'ATTENDED')", name="status_known"
        ),
        CheckConstraint(
            "(status = 'ATTENDED') = (attended_at IS NOT NULL) "
            "AND (attended_at IS NULL) = (attended_by_staff_id IS NULL)",
            name="attendance_shape",
        ),
        CheckConstraint("version > 0", name="version_positive"),
        Index(
            "uq_mentor_bookings_one_active",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('CONFIRMED', 'WAITLISTED')"),
        ),
        Index(
            "uq_mentor_bookings_session_user",
            "session_id",
            "user_id",
            unique=True,
            postgresql_where=text("status <> 'CANCELLED'"),
        ),
        Index("ix_mentor_bookings_session_queue", "session_id", "status", "created_at", "id"),
    )
