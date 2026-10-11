"""task points

Revision ID: 0009_task_points
Revises: 0008_otp_outbox
Create Date: 2026-10-03

Activities carry their own award rule:

* tasks.points               - the fixed value of one award (NULL on activities
                               that predate the rule; they cannot be awarded)
* tasks.allow_repeat_points  - NULL inherits booth -> event default
* uq_points_task_user_once   - structural once-per-(attendee, activity) backstop

Purely additive. No points_log row is modified: existing awards keep
task_id NULL and are never attached to an activity. Old application code
ignores the new columns; new code needs this revision for awards (the
read paths it shares with old code do not touch them).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_task_points"
down_revision: str | None = "0008_otp_outbox"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("points", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("allow_repeat_points", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        op.f("ck_tasks_points_range"),
        "tasks",
        "points IS NULL OR (points > 0 AND points <= 100)",
    )
    op.create_index(
        "uq_points_task_user_once",
        "points_log",
        ["user_id", "task_id"],
        unique=True,
        postgresql_where=sa.text(
            "source = 'TASK' AND entry_type = 'AWARD' "
            "AND task_id IS NOT NULL AND dedupe_key IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_points_task_user_once",
        table_name="points_log",
        postgresql_where=sa.text(
            "source = 'TASK' AND entry_type = 'AWARD' "
            "AND task_id IS NOT NULL AND dedupe_key IS NOT NULL"
        ),
    )
    op.drop_constraint(op.f("ck_tasks_points_range"), "tasks", type_="check")
    op.drop_column("tasks", "allow_repeat_points")
    op.drop_column("tasks", "points")
