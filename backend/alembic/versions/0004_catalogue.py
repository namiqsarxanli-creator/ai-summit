"""catalogue

Revision ID: 0004_catalogue
Revises: 0003_identity
Create Date: 2026-09-23

booths, booth_interests, tasks, task_preset_points — ARCHITECTURE.md
§7.4, §7.5, §7.9, §7.10.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_catalogue"
down_revision: str | None = "0003_identity"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "booths",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location_label", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("allow_repeat_task_points", sa.Boolean(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_booths")),
        sa.UniqueConstraint("slug", name=op.f("uq_booths_slug")),
    )
    op.create_table(
        "booth_interests",
        sa.Column("booth_id", sa.BigInteger(), nullable=False),
        sa.Column("interest_id", sa.BigInteger(), nullable=False),
        sa.Column("weight", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.CheckConstraint(
            "weight > 0 AND weight <= 1", name=op.f("ck_booth_interests_weight_range")
        ),
        sa.ForeignKeyConstraint(
            ["booth_id"], ["booths.id"], name="fk_booth_interests_booth_id_booths"
        ),
        sa.ForeignKeyConstraint(
            ["interest_id"], ["interests.id"], name="fk_booth_interests_interest_id_interests"
        ),
        sa.PrimaryKeyConstraint("booth_id", "interest_id", name=op.f("pk_booth_interests")),
    )
    op.create_table(
        "tasks",
        sa.Column("booth_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.ForeignKeyConstraint(["booth_id"], ["booths.id"], name="fk_tasks_booth_id_booths"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("id", "booth_id", name="uq_tasks_id_booth_id"),
    )
    op.create_table(
        "task_preset_points",
        sa.Column("booth_id", sa.BigInteger(), nullable=True),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint(
            "points > 0 AND points <= 100", name=op.f("ck_task_preset_points_points_range")
        ),
        sa.ForeignKeyConstraint(
            ["booth_id"], ["booths.id"], name="fk_task_preset_points_booth_id_booths"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_preset_points")),
    )
    op.create_index(
        "uq_preset_global_points",
        "task_preset_points",
        ["points"],
        unique=True,
        postgresql_where=sa.text("booth_id IS NULL"),
    )
    op.create_index(
        "uq_task_preset_points_booth_id_points",
        "task_preset_points",
        ["booth_id", "points"],
        unique=True,
        postgresql_where=sa.text("booth_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_task_preset_points_booth_id_points",
        table_name="task_preset_points",
        postgresql_where=sa.text("booth_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_preset_global_points",
        table_name="task_preset_points",
        postgresql_where=sa.text("booth_id IS NULL"),
    )
    op.drop_table("task_preset_points")
    op.drop_table("tasks")
    op.drop_table("booth_interests")
    op.drop_table("booths")
