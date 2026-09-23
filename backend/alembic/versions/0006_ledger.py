"""ledger

Revision ID: 0006_ledger
Revises: 0005_staff
Create Date: 2026-09-23

points_log, idempotency_records, feedback — ARCHITECTURE.md §7.11,
§7.12, §7.12.1. The points ledger: PointsLog is the sole source of
truth (invariant #1); IdempotencyRecord has no status column and no
IN_PROGRESS state, by design.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_ledger"
down_revision: str | None = "0005_staff"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "points_log",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("booth_id", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.Enum("TASK", "FEEDBACK", name="points_source"), nullable=False),
        sa.Column(
            "entry_type",
            sa.Enum("AWARD", "REVERSAL", name="points_entry_type"),
            server_default="AWARD",
            nullable=False,
        ),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("reverses_points_log_id", sa.Integer(), nullable=True),
        sa.Column("entered_by_staff_id", sa.BigInteger(), nullable=True),
        sa.Column("entered_by_system", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint(
            "(entry_type = 'AWARD' AND points > 0 AND reverses_points_log_id IS NULL) OR "
            "(entry_type = 'REVERSAL' AND points < 0 AND reverses_points_log_id IS NOT NULL)",
            name=op.f("ck_points_log_entry_type_consistency"),
        ),
        sa.CheckConstraint(
            "(source = 'TASK' AND booth_id IS NOT NULL AND entered_by_staff_id IS NOT NULL "
            "AND entered_by_system = false) OR (source = 'FEEDBACK' AND booth_id IS NOT NULL "
            "AND entered_by_staff_id IS NULL AND entered_by_system = true)",
            name=op.f("ck_points_log_source_attribution"),
        ),
        sa.CheckConstraint("points <> 0", name=op.f("ck_points_log_points_nonzero")),
        sa.ForeignKeyConstraint(["booth_id"], ["booths.id"], name="fk_points_log_booth_id_booths"),
        sa.ForeignKeyConstraint(
            ["entered_by_staff_id"],
            ["staff_accounts.id"],
            name="fk_points_log_entered_by_staff_id_staff_accounts",
        ),
        sa.ForeignKeyConstraint(
            ["reverses_points_log_id"],
            ["points_log.id"],
            name="fk_points_log_reverses_points_log_id_points_log",
        ),
        sa.ForeignKeyConstraint(
            ["task_id", "booth_id"], ["tasks.id", "tasks.booth_id"], name="fk_points_task_booth"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_points_log_user_id_users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_points_log")),
        sa.UniqueConstraint("dedupe_key", name="uq_points_dedupe_key"),
        sa.UniqueConstraint("reverses_points_log_id", name="uq_points_reverses"),
    )
    op.create_index(
        "ix_points_log_booth_id_created_at",
        "points_log",
        ["booth_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index("ix_points_log_user_id", "points_log", ["user_id"], unique=False)
    op.create_index(
        "ix_points_log_user_id_created_at",
        "points_log",
        ["user_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "uq_points_feedback_user_booth",
        "points_log",
        ["user_id", "booth_id"],
        unique=True,
        postgresql_where=sa.text("source = 'FEEDBACK' AND entry_type = 'AWARD'"),
    )
    op.create_table(
        "idempotency_records",
        sa.Column("staff_account_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.UUID(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("points_log_id", sa.BigInteger(), nullable=True),
        sa.Column("response_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["points_log_id"],
            ["points_log.id"],
            name="fk_idempotency_records_points_log_id_points_log",
        ),
        sa.ForeignKeyConstraint(
            ["staff_account_id"],
            ["staff_accounts.id"],
            name="fk_idempotency_records_staff_account_id_staff_accounts",
        ),
        sa.PrimaryKeyConstraint(
            "staff_account_id", "idempotency_key", name=op.f("pk_idempotency_records")
        ),
    )
    op.create_table(
        "feedback",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("booth_id", sa.BigInteger(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name=op.f("ck_feedback_rating_range")),
        sa.ForeignKeyConstraint(["booth_id"], ["booths.id"], name="fk_feedback_booth_id_booths"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_feedback_user_id_users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    op.create_index(
        "ix_feedback_booth_id_created_at",
        "feedback",
        ["booth_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_feedback_user_id_booth_id", "feedback", ["user_id", "booth_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_user_id_booth_id", table_name="feedback")
    op.drop_index("ix_feedback_booth_id_created_at", table_name="feedback")
    op.drop_table("feedback")
    op.drop_table("idempotency_records")
    op.drop_index(
        "uq_points_feedback_user_booth",
        table_name="points_log",
        postgresql_where=sa.text("source = 'FEEDBACK' AND entry_type = 'AWARD'"),
    )
    op.drop_index("ix_points_log_user_id_created_at", table_name="points_log")
    op.drop_index("ix_points_log_user_id", table_name="points_log")
    op.drop_index("ix_points_log_booth_id_created_at", table_name="points_log")
    op.drop_table("points_log")
    sa.Enum(name="points_entry_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="points_source").drop(op.get_bind(), checkfirst=True)
