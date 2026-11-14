"""Review evidence: card of an offline scan, verified times, occurrence time of an award

Revision ID: 0027_review_evidence
Revises: 0026_offline_scans
Create Date: 2026-10-17

Additive, nullable, no existing row rewritten:

* `offline_scans.card_id` - the contact card (a stable row id, never the QR
  secret) the scan named when it reached the server. Rows from before this
  revision keep NULL: they are NOT treated as verified; approving one requires
  the reviewer to re-scan the attendee's current card (competition/offline.py).
* `offline_scans.verified_at` - the time the reviewer verified; the phone's
  claimed `device_scanned_at` is kept beside it.
* `points_log.occurred_at` - when the scored activity happened, as verified,
  separately from `created_at` (when it was recorded). NULL on historical rows,
  which are read as `created_at` - exactly what they were evaluated by before.
  The ledger stays append-only: nothing is updated.

Downgrade drops the three columns; it refuses while any value is set (that is
evidence a downgrade would silently lose).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027_review_evidence"
down_revision: str | None = "0026_offline_scans"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("offline_scans", sa.Column("card_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_offline_scans_card_id", "offline_scans", "contact_cards", ["card_id"], ["id"]
    )
    op.add_column(
        "offline_scans", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("points_log", sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    held = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM offline_scans WHERE card_id IS NOT NULL "
            "OR verified_at IS NOT NULL) OR EXISTS (SELECT 1 FROM points_log "
            "WHERE occurred_at IS NOT NULL)"
        )
    ).scalar()
    if held:
        raise RuntimeError(
            "card evidence / verified times are recorded; downgrading below "
            "0027_review_evidence would lose them. Export them first."
        )
    op.drop_column("points_log", "occurred_at")
    op.drop_column("offline_scans", "verified_at")
    op.drop_constraint("fk_offline_scans_card_id", "offline_scans", type_="foreignkey")
    op.drop_column("offline_scans", "card_id")
