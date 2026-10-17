"""Offline scans waiting for review, with replay protection

Revision ID: 0026_offline_scans
Revises: 0025_pair_photos
Create Date: 2026-10-17

Additive: one new table, nothing inserted. `uq_offline_scans_replay` makes a
replayed submission (same staff account, same client scan id) a no-op. Downgrade
refuses while any row exists (it is evidence for awarded or refused points).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026_offline_scans"
down_revision: str | None = "0025_pair_photos"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "offline_scans",
        sa.Column("staff_account_id", sa.BigInteger(), nullable=False),
        sa.Column("client_scan_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("device_scanned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reviewed_by_staff_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("outcome", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('MASTERCLASS_DOOR', 'PLAYGROUND_COMPLETE', 'MENTOR_COMPLETE')",
            name=op.f("ck_offline_scans_kind_known"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_REVIEW', 'APPLIED', 'REJECTED', 'FAILED')",
            name=op.f("ck_offline_scans_status_known"),
        ),
        sa.CheckConstraint(
            "phase IS NULL OR phase IN ('ENTRY', 'EXIT')", name=op.f("ck_offline_scans_phase_known")
        ),
        sa.CheckConstraint(
            "(kind = 'MASTERCLASS_DOOR') = (phase IS NOT NULL)",
            name=op.f("ck_offline_scans_phase_for_doors"),
        ),
        sa.CheckConstraint(
            "char_length(client_scan_id) BETWEEN 8 AND 64",
            name=op.f("ck_offline_scans_client_scan_id_length"),
        ),
        sa.CheckConstraint(
            "review_reason IS NULL OR char_length(review_reason) BETWEEN 3 AND 300",
            name=op.f("ck_offline_scans_review_reason_length"),
        ),
        sa.ForeignKeyConstraint(
            ["staff_account_id"], ["staff_accounts.id"], name="fk_offline_scans_staff_account_id"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_offline_scans_user_id"),
        sa.ForeignKeyConstraint(
            ["reviewed_by_staff_id"], ["staff_accounts.id"], name="fk_offline_scans_reviewed_by"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_offline_scans")),
        sa.UniqueConstraint("staff_account_id", "client_scan_id", name="uq_offline_scans_replay"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM offline_scans)")).scalar():
        raise RuntimeError(
            "offline_scans holds the evidence of reviewed scans; export it and clear it "
            "explicitly before downgrading below 0026_offline_scans."
        )
    op.drop_table("offline_scans")
