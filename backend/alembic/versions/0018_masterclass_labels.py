"""Explicit standard display numbers for localised block and room labels.

Existing free-form names and reservations are untouched; NULL uses the stored name.
"""

import sqlalchemy as sa

from alembic import op

revision = "0018_masterclass_labels"
down_revision = "0017_masterclasses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("masterclass_blocks", "masterclass_rooms"):
        op.add_column(table, sa.Column("display_number", sa.Integer(), nullable=True))
        op.create_check_constraint(
            op.f(f"ck_{table}_display_number_range"),
            table,
            "display_number IS NULL OR display_number BETWEEN 1 AND 10000",
        )


def downgrade() -> None:
    for table in ("masterclass_rooms", "masterclass_blocks"):
        op.drop_constraint(op.f(f"ck_{table}_display_number_range"), table, type_="check")
        op.drop_column(table, "display_number")
