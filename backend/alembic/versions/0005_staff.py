"""staff

Revision ID: 0005_staff
Revises: 0004_catalogue
Create Date: 2026-09-23

staff_accounts, sessions — ARCHITECTURE.md §7.7, §7.8. `sessions`
lives here (not split across attendees/staff) because it depends on
staff_accounts existing; see app/core/auth_session.py's docstring for
why the model itself lives in `core`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_staff"
down_revision: str | None = "0004_catalogue"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "staff_accounts",
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Enum("BOOTH_STAFF", "ADMIN", name="staff_role"), nullable=False),
        sa.Column("booth_id", sa.BigInteger(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("failed_login_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint(
            "(role = 'BOOTH_STAFF' AND booth_id IS NOT NULL) OR "
            "(role = 'ADMIN' AND booth_id IS NULL)",
            name=op.f("ck_staff_role_booth"),
        ),
        sa.ForeignKeyConstraint(
            ["booth_id"], ["booths.id"], name="fk_staff_accounts_booth_id_booths"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_accounts")),
        sa.UniqueConstraint("email", name=op.f("uq_staff_accounts_email")),
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "principal_type", sa.Enum("ATTENDEE", "STAFF", name="principal_type"), nullable=False
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("staff_account_id", sa.BigInteger(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint(
            "(principal_type = 'ATTENDEE' AND user_id IS NOT NULL) OR "
            "(principal_type = 'STAFF' AND staff_account_id IS NOT NULL)",
            name=op.f("ck_sessions_principal_type_matches_reference"),
        ),
        sa.CheckConstraint(
            "num_nonnulls(user_id, staff_account_id) = 1",
            name=op.f("ck_sessions_exactly_one_principal"),
        ),
        sa.ForeignKeyConstraint(
            ["staff_account_id"],
            ["staff_accounts.id"],
            name="fk_sessions_staff_account_id_staff_accounts",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_sessions_user_id_users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("staff_accounts")
    sa.Enum(name="principal_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="staff_role").drop(op.get_bind(), checkfirst=True)
