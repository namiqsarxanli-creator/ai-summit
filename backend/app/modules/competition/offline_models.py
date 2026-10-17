"""Scans a staff phone made offline and sent later (migration 0026).

Decision of 16 Oct 2026: scans made offline BEFORE 17:30 and synchronised
afterwards may be accepted after verification. A phone's clock is not proof.

`offline_scans` keeps every such submission with the device's claimed time,
the moment the server received it, and who sent it. The server re-checks the
sender's session and scope and the attendee's QR when it arrives. If the scan's
window is still open by the SERVER clock it is simply applied like any live scan
(APPLIED). Otherwise it waits for a Game Master's review (PENDING_REVIEW): only
an explicit approval applies it - evaluated at the reviewed time, which must be
before the close and before the moment the server received it - and only then
can it earn anything. A rejection is recorded with its reason. Replays are
idempotent per (staff account, client scan id).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin

KINDS = ("MASTERCLASS_DOOR", "PLAYGROUND_COMPLETE", "MENTOR_COMPLETE")


class OfflineScan(IdentityPKMixin, Base):
    __tablename__ = "offline_scans"

    staff_account_id: Mapped[int] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_offline_scans_staff_account_id"), nullable=False
    )
    client_scan_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_offline_scans_user_id"), nullable=False
    )
    # The card the QR named when the scan reached the server (migration 0027): a
    # stable row id, never the QR secret, so a review can check the card is still
    # live. NULL for rows submitted before 0027 - their review must re-verify the
    # card explicitly (offline.approve).
    card_id: Mapped[int | None] = mapped_column(
        ForeignKey("contact_cards.id", name="fk_offline_scans_card_id"), nullable=True
    )
    # What the phone CLAIMED. Kept as received; it is never the evidence itself.
    device_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The time the reviewer verified and applied the scan at (migration 0027).
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_by_staff_id: Mapped[int | None] = mapped_column(
        ForeignKey("staff_accounts.id", name="fk_offline_scans_reviewed_by"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("staff_account_id", "client_scan_id", name="uq_offline_scans_replay"),
        CheckConstraint(
            "kind IN ('MASTERCLASS_DOOR', 'PLAYGROUND_COMPLETE', 'MENTOR_COMPLETE')",
            name="kind_known",
        ),
        CheckConstraint(
            "status IN ('PENDING_REVIEW', 'APPLIED', 'REJECTED', 'FAILED')", name="status_known"
        ),
        CheckConstraint("phase IS NULL OR phase IN ('ENTRY', 'EXIT')", name="phase_known"),
        CheckConstraint(
            "(kind = 'MASTERCLASS_DOOR') = (phase IS NOT NULL)", name="phase_for_doors"
        ),
        CheckConstraint(
            "char_length(client_scan_id) BETWEEN 8 AND 64", name="client_scan_id_length"
        ),
        CheckConstraint(
            "review_reason IS NULL OR char_length(review_reason) BETWEEN 3 AND 300",
            name="review_reason_length",
        ),
    )
