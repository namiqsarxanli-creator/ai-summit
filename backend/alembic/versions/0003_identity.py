"""identity

Revision ID: 0003_identity
Revises: 0002_extension
Create Date: 2026-09-23

interests, users, user_interests, consent_audit, otp_challenges,
contact_cards — ARCHITECTURE.md §7.1, §7.2, §7.3, §7.6, §8.1, §8.2.

Content verified via `alembic revision --autogenerate` against the
SQLAlchemy models in app/modules/{attendees,interests,contact_cards}
and app/db/all_models, then split into this domain group by hand per
IMPLEMENTATION_PLAN.md M02's "reviewable migrations grouped by domain
(identity, catalogue, staff, ledger, operations)". `interests` is
created here (not in the catalogue group) because `user_interests`
needs it to already exist — its own FK dependency, not its subject
matter, decides its placement.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_identity"
down_revision: str | None = "0002_extension"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interests",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interests")),
        sa.UniqueConstraint("slug", name=op.f("uq_interests_slug")),
    )
    op.create_table(
        "users",
        sa.Column("first_name", sa.Text(), nullable=False),
        sa.Column("last_name", sa.Text(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("profession", sa.Text(), nullable=False),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("sponsor_consent", sa.Boolean(), nullable=False),
        sa.Column("sponsor_consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sponsor_consent_version", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PROVISIONAL", "ACTIVE", name="user_status"),
            server_default="PROVISIONAL",
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint(
            "status = 'PROVISIONAL' OR activated_at IS NOT NULL",
            name=op.f("ck_users_status_requires_activated_at"),
        ),
        sa.CheckConstraint(
            "sponsor_consent = false OR sponsor_consent_at IS NOT NULL",
            name=op.f("ck_users_sponsor_consent_requires_timestamp"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_table(
        "user_interests",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("interest_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["interest_id"], ["interests.id"], name="fk_user_interests_interest_id_interests"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_interests_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "interest_id", name=op.f("pk_user_interests")),
    )
    op.create_index(
        "ix_user_interests_interest_id", "user_interests", ["interest_id"], unique=False
    )
    op.create_table(
        "consent_audit",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("consent_version", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_consent_audit_user_id_users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_audit")),
    )
    op.create_index(
        "ix_consent_audit_user_id_changed_at",
        "consent_audit",
        ["user_id", "changed_at"],
        unique=False,
    )
    op.create_table(
        "otp_challenges",
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("purpose", sa.Enum("ACTIVATION", "LOGIN", name="otp_purpose"), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("request_ip", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_otp_challenges_user_id_users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_otp_challenges")),
    )
    op.create_index(
        "uq_otp_challenges_email_purpose_live",
        "otp_challenges",
        ["email", "purpose"],
        unique=True,
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.create_table(
        "contact_cards",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("public_token", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_contact_cards_user_id_users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_cards")),
        sa.UniqueConstraint("public_token", name=op.f("uq_contact_cards_public_token")),
    )
    op.create_index(
        "uq_contact_card_live_user",
        "contact_cards",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_contact_card_live_user",
        table_name="contact_cards",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_table("contact_cards")
    op.drop_index(
        "uq_otp_challenges_email_purpose_live",
        table_name="otp_challenges",
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.drop_table("otp_challenges")
    op.drop_index("ix_consent_audit_user_id_changed_at", table_name="consent_audit")
    op.drop_table("consent_audit")
    op.drop_index("ix_user_interests_interest_id", table_name="user_interests")
    op.drop_table("user_interests")
    op.drop_table("users")
    op.drop_table("interests")
    # Drop the enum types SQLAlchemy auto-created inline with their
    # owning tables above — table drops do not implicitly drop them.
    sa.Enum(name="otp_purpose").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="user_status").drop(op.get_bind(), checkfirst=True)
