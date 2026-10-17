"""Programme reminders: a durable, deduplicated email outbox (migration 0024).

Channel: email, through the same provider and worker as sign-in codes. Nothing
is ever sent unless BOTH an administrator has switched reminders on
(`event_config.reminders_enabled`, off by default) AND the attendee has opted in
(`users.reminder_consent_at`); withdrawing consent stops every pending reminder
at its next check. An in-page timer is not a reminder: it cannot reach a closed
browser, so none is pretended.

`reminder_deliveries` holds one row per (person, reminder): the 12 November
"see you tomorrow" message (`eve`) and one 10 minutes before each confirmed item
of their programme (`masterclass:<id>`, `playground:<booking>`,
`meeting:<id>`, `mentor:<booking>`). `uq_reminder_deliveries_user_item` is the
dedupe: replanning never creates a second row, and an item whose time moved has
its PENDING row re-timed. Rows are claimed with a lease (`FOR UPDATE SKIP
LOCKED`), retried with bounded exponential backoff, and every attempt carries the
same provider idempotency key, so an ambiguous timeout cannot send twice.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base, IdentityPKMixin

STATUSES = ("PENDING", "SENDING", "SENT", "FAILED", "CANCELLED")


class ReminderDelivery(IdentityPKMixin, Base):
    __tablename__ = "reminder_deliveries"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", name="fk_reminder_deliveries_user_id"), nullable=False
    )
    item_key: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_code: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "item_key", name="uq_reminder_deliveries_user_item"),
        CheckConstraint(
            "status IN ('PENDING', 'SENDING', 'SENT', 'FAILED', 'CANCELLED')",
            name="status_known",
        ),
        CheckConstraint("attempt_count BETWEEN 0 AND 20", name="attempts_bounded"),
        CheckConstraint("(status = 'SENT') = (sent_at IS NOT NULL)", name="sent_has_time"),
        CheckConstraint(
            "(status = 'SENDING') = (lease_until IS NOT NULL AND lease_owner IS NOT NULL)",
            name="lease_when_sending",
        ),
        CheckConstraint("char_length(item_key) BETWEEN 1 AND 60", name="item_key_length"),
        Index("ix_reminder_deliveries_due", "status", "next_attempt_at"),
    )
