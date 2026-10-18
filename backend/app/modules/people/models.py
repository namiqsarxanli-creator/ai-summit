"""People: meeting requests, Match Corner meetings, blocks and connections (0021).

* `user_blocks` - one attendee blocks another; neither then appears in the
  other's suggestions, and no request can be sent between them.
* `meeting_requests` - "Görüşək": PENDING -> ACCEPTED / DECLINED / CANCELLED.
  One open request per pair (either direction) at a time.
* `match_tables` - the Match Corner tables an administrator configures (the PDF
  suggests 4-6; the real count is data, never a constant).
* `meetings` - an accepted request gets one table for one 15-minute slot.
  `uq_meetings_table_slot` makes a table impossible to double-book.
* `meeting_participants` - the two people of a meeting, per slot:
  `uq_meeting_participants_person_slot` makes a person impossible to place in two
  confirmed meetings at once.
* `connection_scans` - one direction of the mutual QR scan.
* `connections` - both directions done: the pair is connected, the contact
  action opens to these two only, and the kind (a top-five recommendation in either direction = MATCH,
  otherwise OTHER) and same-company flag are FROZEN at that moment, so a later
  profile edit never rewrites an earned score.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin


class UserBlock(IdentityPKMixin, Base):
    __tablename__ = "user_blocks"

    blocker_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_user_blocks_blocker_id"), nullable=False
    )
    blocked_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_user_blocks_blocked_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("blocker_id", "blocked_id", name="uq_user_blocks_pair"),
        CheckConstraint("blocker_id <> blocked_id", name="not_self"),
    )


class MeetingRequest(IdentityPKMixin, Base):
    __tablename__ = "meeting_requests"

    requester_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_meeting_requests_requester_id"), nullable=False
    )
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_meeting_requests_recipient_id"), nullable=False
    )
    # The unordered pair, so "one open request per pair" is a plain unique index.
    pair_low_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pair_high_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    # The client's idempotency key: a retried "send" creates nothing new.
    command_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("requester_id <> recipient_id", name="not_self"),
        CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'DECLINED', 'CANCELLED')", name="status_known"
        ),
        CheckConstraint(
            "(status = 'PENDING') = (responded_at IS NULL)", name="responded_unless_pending"
        ),
        CheckConstraint(
            "pair_low_id = LEAST(requester_id, recipient_id) "
            "AND pair_high_id = GREATEST(requester_id, recipient_id)",
            name="pair_matches",
        ),
        # one open request per unordered pair
        Index(
            "uq_meeting_requests_open_pair",
            "pair_low_id",
            "pair_high_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'ACCEPTED')"),
        ),
        Index("ix_meeting_requests_requester_created", "requester_id", "created_at"),
        Index("ix_meeting_requests_recipient_status", "recipient_id", "status"),
    )


class MatchTable(IdentityPKMixin, Base):
    __tablename__ = "match_tables"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("masterclass_events.id", name="fk_match_tables_event_id"), nullable=False
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        UniqueConstraint("event_id", "label", name="uq_match_tables_event_label"),
        CheckConstraint("char_length(label) BETWEEN 1 AND 40", name="label_length"),
    )


class Meeting(IdentityPKMixin, Base):
    __tablename__ = "meetings"

    request_id: Mapped[int] = mapped_column(
        ForeignKey("meeting_requests.id", name="fk_meetings_request_id"),
        nullable=False,
        unique=True,
    )
    table_id: Mapped[int] = mapped_column(
        ForeignKey("match_tables.id", name="fk_meetings_table_id"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prompts: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("ends_at = starts_at + interval '15 minutes'", name="fifteen_minutes"),
        CheckConstraint("cardinality(prompts) BETWEEN 2 AND 3", name="two_or_three_prompts"),
        Index(
            "uq_meetings_table_slot",
            "table_id",
            "starts_at",
            unique=True,
            postgresql_where=text("cancelled_at IS NULL"),
        ),
    )


class MeetingParticipant(IdentityPKMixin, Base):
    __tablename__ = "meeting_participants"

    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", name="fk_meeting_participants_meeting_id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_meeting_participants_user_id"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        UniqueConstraint("meeting_id", "user_id", name="uq_meeting_participants_member"),
        Index(
            "uq_meeting_participants_person_slot",
            "user_id",
            "starts_at",
            unique=True,
            postgresql_where=text("active"),
        ),
    )


class ConnectionScan(IdentityPKMixin, Base):
    __tablename__ = "connection_scans"

    scanner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_connection_scans_scanner_id"), nullable=False
    )
    scanned_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_connection_scans_scanned_id"), nullable=False
    )
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("scanner_id", "scanned_id", name="uq_connection_scans_direction"),
        CheckConstraint("scanner_id <> scanned_id", name="not_self"),
    )


class Connection(IdentityPKMixin, Base):
    __tablename__ = "connections"

    user_low_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_connections_user_low_id"), nullable=False
    )
    user_high_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_connections_user_high_id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    same_company: Mapped[bool] = mapped_column(Boolean, nullable=False)
    established_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_low_id", "user_high_id", name="uq_connections_pair"),
        CheckConstraint("user_low_id < user_high_id", name="ordered_pair"),
        CheckConstraint("kind IN ('MATCH', 'OTHER')", name="kind_known"),
    )
