"""operations

Revision ID: 0007_operations
Revises: 0006_ledger
Create Date: 2026-09-23

event_config, admin_audit — ARCHITECTURE.md §7.13 and
IMPLEMENTATION_PLAN.md clarification C4. The final migration in the
M02 schema; nothing else depends on these two.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_operations"
down_revision: str | None = "0006_ledger"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_config",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("feedback_points", sa.Integer(), server_default="5", nullable=False),
        sa.Column(
            "allow_repeat_task_points_default", sa.Boolean(), server_default="false", nullable=False
        ),
        sa.Column("leaderboard_top_n", sa.Integer(), server_default="10", nullable=False),
        sa.Column("dataset_generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("maintenance_mode", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by_staff_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "feedback_points BETWEEN 1 AND 100", name=op.f("ck_event_config_feedback_points_range")
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_event_config_singleton")),
        sa.CheckConstraint(
            "leaderboard_top_n BETWEEN 1 AND 100",
            name=op.f("ck_event_config_leaderboard_top_n_range"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_staff_id"],
            ["staff_accounts.id"],
            name="fk_event_config_updated_by_staff_id_staff_accounts",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_config")),
    )
    op.create_table(
        "admin_audit",
        sa.Column("staff_account_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("before_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["staff_account_id"],
            ["staff_accounts.id"],
            name="fk_admin_audit_staff_account_id_staff_accounts",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_audit")),
    )
    op.create_index(
        "ix_admin_audit_created_at",
        "admin_audit",
        [sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_staff_account_id", "admin_audit", ["staff_account_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_admin_audit_staff_account_id", table_name="admin_audit")
    op.drop_index("ix_admin_audit_created_at", table_name="admin_audit")
    op.drop_table("admin_audit")
    op.drop_table("event_config")
