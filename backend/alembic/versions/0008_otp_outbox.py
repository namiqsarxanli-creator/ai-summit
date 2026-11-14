"""otp outbox

Revision ID: 0008_otp_outbox
Revises: 0007_operations
Create Date: 2026-10-03

otp_deliveries - the durable outbox for OTP emails. Purely additive: one new
table that references otp_challenges; nothing existing is altered, so code
from before this revision keeps working against a database at it (it simply
never reads or writes the table). Downgrading drops the table and any
unsent/unfinished delivery records in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_otp_outbox"
down_revision: str | None = "0007_operations"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "otp_deliveries",
        sa.Column("challenge_id", sa.BigInteger(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column(
            "purpose",
            postgresql.ENUM("ACTIVATION", "LOGIN", name="otp_purpose", create_type=False),
            nullable=False,
        ),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("payload_key_id", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
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
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'accepted', 'failed', 'expired')",
            name=op.f("ck_otp_deliveries_status_valid"),
        ),
        sa.CheckConstraint(
            "language IN ('az', 'en', 'ru')", name=op.f("ck_otp_deliveries_language_valid")
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'processing')) = (payload_ciphertext IS NOT NULL)",
            name=op.f("ck_otp_deliveries_payload_only_while_live"),
        ),
        sa.CheckConstraint(
            "(status = 'processing') = (locked_until IS NOT NULL)",
            name=op.f("ck_otp_deliveries_lease_iff_processing"),
        ),
        sa.CheckConstraint(
            "(locked_until IS NULL) = (locked_by IS NULL)",
            name=op.f("ck_otp_deliveries_lease_complete"),
        ),
        sa.CheckConstraint(
            "(status = 'accepted') = (accepted_at IS NOT NULL)",
            name=op.f("ck_otp_deliveries_accepted_has_timestamp"),
        ),
        sa.CheckConstraint(
            "(status IN ('accepted', 'failed', 'expired')) = (finished_at IS NOT NULL)",
            name=op.f("ck_otp_deliveries_finished_has_timestamp"),
        ),
        sa.ForeignKeyConstraint(
            ["challenge_id"],
            ["otp_challenges.id"],
            name="fk_otp_deliveries_challenge_id_otp_challenges",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_otp_deliveries")),
        sa.UniqueConstraint("challenge_id", name=op.f("uq_otp_deliveries_challenge_id")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_otp_deliveries_idempotency_key")),
    )
    op.create_index(
        "ix_otp_deliveries_pending_due",
        "otp_deliveries",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_otp_deliveries_processing_lease",
        "otp_deliveries",
        ["locked_until"],
        unique=False,
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_otp_deliveries_status_id",
        "otp_deliveries",
        ["status", sa.literal_column("id DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_otp_deliveries_status_id", table_name="otp_deliveries")
    op.drop_index(
        "ix_otp_deliveries_processing_lease",
        table_name="otp_deliveries",
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.drop_index(
        "ix_otp_deliveries_pending_due",
        table_name="otp_deliveries",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("otp_deliveries")
