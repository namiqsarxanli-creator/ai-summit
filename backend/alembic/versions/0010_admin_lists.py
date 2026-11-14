"""admin list indexes

Revision ID: 0010_admin_lists
Revises: 0009_task_points
Create Date: 2026-10-04

Indexes for the keyset-paginated admin lists (attendees, points ledger):

* users       (created_at DESC, id DESC)                   - the attendee list
* points_log  (created_at DESC, id DESC)                   - the unfiltered ledger
* points_log  (task_id, created_at DESC, id DESC)          - activity filter
                WHERE task_id IS NOT NULL
* points_log  (entered_by_staff_id, created_at DESC, id DESC) - staff filter
                WHERE entered_by_staff_id IS NOT NULL

The attendee and booth ledger filters are already served by
ix_points_log_user_id_created_at / ix_points_log_booth_id_created_at.

Purely additive and data-neutral: no row is read or written. The indexes are
built with a plain CREATE INDEX (a brief write lock on the table), which at
event scale takes milliseconds; run it before the event, not during a rush.
Old code ignores them.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_admin_lists"
down_revision: str | None = "0009_task_points"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_users_created_at_id", "users", [sa.text("created_at DESC"), sa.text("id DESC")]
    )
    op.create_index(
        "ix_points_log_created_at_id",
        "points_log",
        [sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_points_log_task_id_created_at_id",
        "points_log",
        ["task_id", sa.text("created_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("task_id IS NOT NULL"),
    )
    op.create_index(
        "ix_points_log_staff_id_created_at_id",
        "points_log",
        ["entered_by_staff_id", sa.text("created_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("entered_by_staff_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_points_log_staff_id_created_at_id", table_name="points_log")
    op.drop_index("ix_points_log_task_id_created_at_id", table_name="points_log")
    op.drop_index("ix_points_log_created_at_id", table_name="points_log")
    op.drop_index("ix_users_created_at_id", table_name="users")
