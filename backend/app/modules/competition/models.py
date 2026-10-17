"""Verified participation for the Beyond Now competition (migration 0020).

Points themselves live ONLY in `points_log` (the single source of truth); these
tables hold the evidence an award is derived from, so an award is never made from
a reservation, a phone clock or a client claim.

* `staff_scopes` - what a staff account (typically a VOLUNTEER) may do, granted
  and revoked by an administrator: scan a masterclass door, verify Qmeter
  questions (Game Master), host a mentor group. A scope names its target; a
  revoked scope stops working at once, including for requests still in flight
  (every scan re-reads the scope inside its fenced transaction).
* `panels` - the Main Stage panels with their times. `qr_version` is part of the
  signed screen QR, so rotating it revokes every QR shown before.
* `panel_scans` - an attendee's ENTRY / EXIT scan of a panel's screen QR, once
  per phase.
* `masterclass_attendance` - a door volunteer's ENTRY / EXIT scan of an
  attendee at one masterclass, once per attendee and masterclass.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

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

SCOPES: Final = ("MASTERCLASS_DOOR", "GAME_MASTER", "MENTOR_HOST")
PHASES: Final = ("ENTRY", "EXIT")


class StaffScope(IdentityPKMixin, Base):
    __tablename__ = "staff_scopes"

    staff_account_id: Mapped[int] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_staff_scopes_staff_account_id"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    # The masterclass a door volunteer works, the mentor group a host runs; NULL
    # for an event-wide scope (Game Master).
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    granted_by_staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_staff_scopes_granted_by"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "scope IN ('MASTERCLASS_DOOR', 'GAME_MASTER', 'MENTOR_HOST')", name="scope_known"
        ),
        CheckConstraint(
            "(scope = 'GAME_MASTER') = (target_id IS NULL)", name="target_matches_scope"
        ),
        # One live grant per (staff, scope, target) - and per (staff, Game Master).
        Index(
            "uq_staff_scopes_live_target",
            "staff_account_id",
            "scope",
            "target_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND target_id IS NOT NULL"),
        ),
        Index(
            "uq_staff_scopes_live_event",
            "staff_account_id",
            "scope",
            unique=True,
            postgresql_where=text("revoked_at IS NULL AND target_id IS NULL"),
        ),
    )


class Panel(IdentityPKMixin, Base):
    __tablename__ = "panels"

    event_id: Mapped[int] = mapped_column(
        ForeignKey("masterclass_events.id", name="fk_panels_event_id"), nullable=False
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    qr_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    __table_args__ = (
        UniqueConstraint("event_id", "code", name="uq_panels_event_code"),
        CheckConstraint("ends_at > starts_at", name="ends_after_start"),
        CheckConstraint("qr_version >= 1", name="qr_version_positive"),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
    )


class PanelScan(IdentityPKMixin, Base):
    __tablename__ = "panel_scans"

    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", name="fk_panel_scans_panel_id"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_panel_scans_user_id"), nullable=False
    )
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("panel_id", "user_id", "phase", name="uq_panel_scans_once"),
        CheckConstraint("phase IN ('ENTRY', 'EXIT')", name="phase_known"),
    )


class MasterclassAttendance(IdentityPKMixin, Base):
    __tablename__ = "masterclass_attendance"

    masterclass_id: Mapped[int] = mapped_column(
        ForeignKey("masterclasses.id", name="fk_masterclass_attendance_masterclass_id"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_masterclass_attendance_user_id"), nullable=False
    )
    # Admitted without a seat, within the room's configured standing capacity.
    standing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_by_staff_id: Mapped[int] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_masterclass_attendance_entry_by"), nullable=False
    )
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_by_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_masterclass_attendance_exit_by"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("masterclass_id", "user_id", name="uq_masterclass_attendance_once"),
        CheckConstraint("(exit_at IS NULL) = (exit_by_staff_id IS NULL)", name="exit_complete"),
        CheckConstraint("exit_at IS NULL OR exit_at >= entry_at", name="exit_after_entry"),
    )
