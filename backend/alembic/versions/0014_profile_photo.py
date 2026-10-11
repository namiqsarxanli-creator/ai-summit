"""attendee profile editing: version and photo

Revision ID: 0014_profile_photo
Revises: 0013_staff_password_reset
Create Date: 2026-10-04

Two columns on `users`, both additive and both harmless to code from before this
revision (which never reads or writes them):

* `profile_version integer NOT NULL DEFAULT 1` - optimistic concurrency for the
  attendee's own profile edits. The client sends the version it loaded; the
  server accepts the edit only if it is still current and then increments it, so
  two devices editing the same profile can no longer silently overwrite each
  other. A constant default is a metadata-only change in PostgreSQL 11+: no table
  rewrite, every existing row reads as version 1.
* `photo_key text NULL` - the server-made random name of the attendee's profile
  photo (32 lowercase hex characters, enforced by a CHECK), NULL = no photo. A
  partial unique index guarantees that no two attendees ever share a key, which is
  what makes "delete my old photo's files" unable to touch anyone else's.

No row is rewritten. Downgrade drops both columns; photo files already written
stay on disk as orphans until `purge-orphan-photos` removes them.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014_profile_photo"
down_revision: str | None = "0013_staff_password_reset"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("profile_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column("users", sa.Column("photo_key", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_users_photo_key_format"),
        "users",
        "photo_key IS NULL OR photo_key ~ '^[0-9a-f]{32}$'",
    )
    op.create_index(
        "uq_users_photo_key",
        "users",
        ["photo_key"],
        unique=True,
        postgresql_where=sa.text("photo_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_users_photo_key", table_name="users")
    op.drop_constraint(op.f("ck_users_photo_key_format"), "users", type_="check")
    op.drop_column("users", "photo_key")
    op.drop_column("users", "profile_version")
