"""admin audit view indexes

Revision ID: 0011_audit_view
Revises: 0010_admin_lists
Create Date: 2026-10-04

Indexes for the keyset-paginated, filterable audit view (`GET /admin/audit`):
the list is ordered `(created_at DESC, id DESC)` and each filter combines with
that order, so every filter gets an index that ends in it:

* admin_audit (created_at DESC, id DESC)                       - the unfiltered trail
* admin_audit (staff_account_id, created_at DESC, id DESC)     - acting staff account
* admin_audit (action, created_at DESC, id DESC)               - one kind of action
* admin_audit (target_type, target_id, created_at DESC, id DESC)
                                                               - one target type (its
                                                                 leading column), or one
                                                                 target (an export's events)

Two of 0007's indexes are replaced because the new ones cover them completely
(`created_at DESC` is the leading part of the first; `staff_account_id` is the
leading part of the second, and the foreign key is still served by it).

Data-neutral: no row is read or written, only indexes change. Built with a plain
CREATE INDEX (a brief write lock): the table holds an event's worth of audit
rows, so it takes milliseconds. Old code never reads `admin_audit` through these
queries and ignores the indexes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_audit_view"
down_revision: str | None = "0010_admin_lists"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_NEWEST_FIRST = (sa.text("created_at DESC"), sa.text("id DESC"))


def upgrade() -> None:
    op.create_index("ix_admin_audit_created_at_id", "admin_audit", list(_NEWEST_FIRST))
    op.create_index(
        "ix_admin_audit_actor_created_at_id",
        "admin_audit",
        ["staff_account_id", *_NEWEST_FIRST],
    )
    op.create_index(
        "ix_admin_audit_action_created_at_id", "admin_audit", ["action", *_NEWEST_FIRST]
    )
    op.create_index(
        "ix_admin_audit_target_created_at_id",
        "admin_audit",
        ["target_type", "target_id", *_NEWEST_FIRST],
    )
    op.drop_index("ix_admin_audit_created_at", table_name="admin_audit")
    op.drop_index("ix_admin_audit_staff_account_id", table_name="admin_audit")


def downgrade() -> None:
    op.create_index("ix_admin_audit_staff_account_id", "admin_audit", ["staff_account_id"])
    op.create_index("ix_admin_audit_created_at", "admin_audit", [sa.text("created_at DESC")])
    op.drop_index("ix_admin_audit_target_created_at_id", table_name="admin_audit")
    op.drop_index("ix_admin_audit_action_created_at_id", table_name="admin_audit")
    op.drop_index("ix_admin_audit_actor_created_at_id", table_name="admin_audit")
    op.drop_index("ix_admin_audit_created_at_id", table_name="admin_audit")
