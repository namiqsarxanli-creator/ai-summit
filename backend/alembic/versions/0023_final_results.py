"""Versioned, immutable final results of the competition

Revision ID: 0023_final_results
Revises: 0022_mentors
Create Date: 2026-10-17

Additive: one new table and one trigger, nothing inserted. The trigger makes a
snapshot immutable in the database itself: payload, version, event and
authorship never change; a status only moves DRAFT -> PUBLISHED | DISCARDED and
PUBLISHED -> SUPERSEDED; a published or superseded snapshot is never deleted.
One published snapshot per event (`uq_final_results_one_published`). Downgrade
refuses while any snapshot exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023_final_results"
down_revision: str | None = "0022_mentors"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "final_results",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default="DRAFT", nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_staff_id", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by_staff_id", sa.BigInteger(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'PUBLISHED', 'SUPERSEDED', 'DISCARDED')",
            name=op.f("ck_final_results_status_known"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_final_results_version_positive")),
        sa.CheckConstraint(
            "(status IN ('PUBLISHED', 'SUPERSEDED')) = (published_at IS NOT NULL) "
            "AND (published_at IS NULL) = (published_by_staff_id IS NULL)",
            name=op.f("ck_final_results_publication_shape"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["masterclass_events.id"], name="fk_final_results_event_id"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_staff_id"], ["staff_accounts.id"], name="fk_final_results_created_by"
        ),
        sa.ForeignKeyConstraint(
            ["published_by_staff_id"], ["staff_accounts.id"], name="fk_final_results_published_by"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_final_results")),
    )
    op.create_index(
        "uq_final_results_event_version", "final_results", ["event_id", "version"], unique=True
    )
    op.create_index(
        "uq_final_results_one_published",
        "final_results",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )
    op.execute("""
CREATE FUNCTION final_results_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN
  IF OLD.status IN ('PUBLISHED', 'SUPERSEDED') THEN
   RAISE EXCEPTION 'a published final result is permanent'
   USING ERRCODE='23514', CONSTRAINT='ck_final_results_immutable';
  END IF;
  RETURN OLD;
 END IF;
 IF NEW.payload IS DISTINCT FROM OLD.payload OR NEW.version <> OLD.version
    OR NEW.event_id <> OLD.event_id OR NEW.created_at <> OLD.created_at
    OR NEW.created_by_staff_id <> OLD.created_by_staff_id
    OR (OLD.published_at IS NOT NULL AND NEW.published_at IS DISTINCT FROM OLD.published_at)
    OR NOT (NEW.status = OLD.status
            OR (OLD.status = 'DRAFT' AND NEW.status IN ('PUBLISHED', 'DISCARDED'))
            OR (OLD.status = 'PUBLISHED' AND NEW.status = 'SUPERSEDED'))
 THEN
  RAISE EXCEPTION 'a final result snapshot cannot be rewritten'
  USING ERRCODE='23514', CONSTRAINT='ck_final_results_immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER final_results_immutable BEFORE UPDATE OR DELETE ON final_results
FOR EACH ROW EXECUTE FUNCTION final_results_guard();
""")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM final_results)")).scalar():
        raise RuntimeError(
            "final_results holds the competition's results; export them and remove them "
            "explicitly before downgrading below 0023_final_results."
        )
    op.drop_table("final_results")
    op.execute("DROP FUNCTION final_results_guard()")
