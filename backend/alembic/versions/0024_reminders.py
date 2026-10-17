"""Programme reminders: consent, the administrator's switch and a durable outbox

Revision ID: 0024_reminders
Revises: 0023_final_results
Create Date: 2026-10-17

Additive. `event_config.reminders_enabled` defaults to FALSE and
`users.reminder_consent_at` to NULL, so after the upgrade nobody receives
anything until an administrator switches reminders on AND each attendee opts in.
One new table, `reminder_deliveries`, deduplicated per (person, item). Downgrade
refuses while any reminder row exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0024_reminders"
down_revision: str | None = "0023_final_results"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_config",
        sa.Column("reminders_enabled", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column("users", sa.Column("reminder_consent_at", sa.DateTime(timezone=True)))
    op.create_table(
        "reminder_deliveries",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("item_key", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENDING', 'SENT', 'FAILED', 'CANCELLED')",
            name=op.f("ck_reminder_deliveries_status_known"),
        ),
        sa.CheckConstraint(
            "attempt_count BETWEEN 0 AND 20", name=op.f("ck_reminder_deliveries_attempts_bounded")
        ),
        sa.CheckConstraint(
            "(status = 'SENT') = (sent_at IS NOT NULL)",
            name=op.f("ck_reminder_deliveries_sent_has_time"),
        ),
        sa.CheckConstraint(
            "(status = 'SENDING') = (lease_until IS NOT NULL AND lease_owner IS NOT NULL)",
            name=op.f("ck_reminder_deliveries_lease_when_sending"),
        ),
        sa.CheckConstraint(
            "char_length(item_key) BETWEEN 1 AND 60",
            name=op.f("ck_reminder_deliveries_item_key_length"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_reminder_deliveries_user_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminder_deliveries")),
        sa.UniqueConstraint("user_id", "item_key", name="uq_reminder_deliveries_user_item"),
    )
    op.create_index(
        "ix_reminder_deliveries_due", "reminder_deliveries", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM reminder_deliveries)")).scalar():
        raise RuntimeError(
            "reminder_deliveries holds delivery history; clear it explicitly before "
            "downgrading below 0024_reminders."
        )
    op.drop_index("ix_reminder_deliveries_due", table_name="reminder_deliveries")
    op.drop_table("reminder_deliveries")
    op.drop_column("users", "reminder_consent_at")
    op.drop_column("event_config", "reminders_enabled")
