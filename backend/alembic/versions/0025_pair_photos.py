"""Beyond Now Photo: pair photos with two consents and moderation

Revision ID: 0025_pair_photos
Revises: 0024_reminders
Create Date: 2026-10-17

Additive: one new table, nothing inserted. A withdrawn or declined photo keeps
no file key (`ck_pair_photos_gone_has_no_key`). Downgrade refuses while any row
exists (their files live on the media volume and must be dealt with first).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0025_pair_photos"
down_revision: str | None = "0024_reminders"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pair_photos",
        sa.Column("uploader_id", sa.BigInteger(), nullable=False),
        sa.Column("partner_id", sa.BigInteger(), nullable=False),
        sa.Column("photo_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="PENDING", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_by_id", sa.BigInteger(), nullable=True),
        sa.Column("uploader_screen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("partner_screen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("moderation", sa.Text(), server_default="PENDING", nullable=False),
        sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("moderated_by_staff_id", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint("uploader_id <> partner_id", name=op.f("ck_pair_photos_not_self")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'DECLINED', 'WITHDRAWN')",
            name=op.f("ck_pair_photos_status_known"),
        ),
        sa.CheckConstraint(
            "moderation IN ('PENDING', 'APPROVED', 'REJECTED')",
            name=op.f("ck_pair_photos_moderation_known"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('WITHDRAWN', 'DECLINED') OR photo_key IS NULL",
            name=op.f("ck_pair_photos_gone_has_no_key"),
        ),
        sa.CheckConstraint(
            "(status = 'WITHDRAWN') = (withdrawn_at IS NOT NULL)",
            name=op.f("ck_pair_photos_withdrawn_has_time"),
        ),
        sa.CheckConstraint(
            "photo_key IS NULL OR photo_key ~ '^[0-9a-f]{32}$'",
            name=op.f("ck_pair_photos_key_shape"),
        ),
        sa.ForeignKeyConstraint(["uploader_id"], ["users.id"], name="fk_pair_photos_uploader_id"),
        sa.ForeignKeyConstraint(["partner_id"], ["users.id"], name="fk_pair_photos_partner_id"),
        sa.ForeignKeyConstraint(
            ["withdrawn_by_id"], ["users.id"], name="fk_pair_photos_withdrawn_by_id"
        ),
        sa.ForeignKeyConstraint(
            ["moderated_by_staff_id"], ["staff_accounts.id"], name="fk_pair_photos_moderated_by"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pair_photos")),
        sa.UniqueConstraint("photo_key", name=op.f("uq_pair_photos_photo_key")),
    )
    op.create_index(
        "ix_pair_photos_screen",
        "pair_photos",
        ["moderation", "created_at"],
        postgresql_where=sa.text("status = 'ACCEPTED'"),
    )
    op.create_index("ix_pair_photos_partner", "pair_photos", ["partner_id", "status"])
    op.create_index("ix_pair_photos_uploader", "pair_photos", ["uploader_id", "status"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM pair_photos)")).scalar():
        raise RuntimeError(
            "pair_photos references photo files on the media volume; remove them explicitly "
            "before downgrading below 0025_pair_photos."
        )
    op.drop_index("ix_pair_photos_uploader", table_name="pair_photos")
    op.drop_index("ix_pair_photos_partner", table_name="pair_photos")
    op.drop_index("ix_pair_photos_screen", table_name="pair_photos")
    op.drop_table("pair_photos")
