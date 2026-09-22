"""baseline (empty) — M01 foundation

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-22

Intentionally empty. M01 proves the migration mechanism works
end-to-end (upgrade/downgrade against a real PostgreSQL database)
without creating any business table — ARCHITECTURE.md's entities
(users, points_log, etc.) are M02 scope. The CREATE EXTENSION citext
statement ARCHITECTURE.md §7 requires also belongs to M02's first
entity migration, not here, per IMPLEMENTATION_PLAN.md M02 scope.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
