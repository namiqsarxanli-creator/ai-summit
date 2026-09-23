"""extension

Revision ID: 0002_extension
Revises: 0001_baseline
Create Date: 2026-09-23

ARCHITECTURE.md §7: "The first migration must CREATE EXTENSION IF NOT
EXISTS citext; — the citext columns below depend on it, and a
migration that assumes the extension is present will fail on a fresh
database." (users.email, staff_accounts.email, otp_challenges.email
are all CITEXT — case-insensitive equality/uniqueness — in the
migrations that follow.)

PostgreSQL native ENUM types (user_status, otp_purpose, staff_role,
principal_type, points_source, points_entry_type) are NOT pre-created
here: each is used by exactly one table, so SQLAlchemy/Alembic creates
it automatically and safely as part of that table's own CREATE TABLE
statement (verified against the autogenerate output this migration
set is derived from).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_extension"
down_revision: str | None = "0001_baseline"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")


def downgrade() -> None:
    # Deliberately NOT dropping the extension: DROP EXTENSION citext
    # would fail while any citext column still exists (users.email
    # etc., dropped by later migrations' downgrades), and even after
    # they're gone, dropping a shared database-level extension from an
    # application migration is broader-reaching than this migration's
    # own change. citext is inert with no columns using it.
    pass
