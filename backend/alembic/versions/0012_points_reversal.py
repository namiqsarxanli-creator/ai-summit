"""points reversal by an administrator

Revision ID: 0012_points_reversal
Revises: 0011_audit_view
Create Date: 2026-10-04

Two changes to `points_log`, both of which keep the meaning of every row that
already exists:

1. `source_attribution` gains ONE more way to be true: a FEEDBACK **REVERSAL**
   may be made by an administrator (`entered_by_staff_id` set,
   `entered_by_system = false`). Until now a FEEDBACK row had to be
   `entered_by_staff_id IS NULL AND entered_by_system = true` - right for the
   attendee's own reward, impossible for an administrator who cancels it, who
   would otherwise have to be recorded as "the system" (hiding who did it) or
   be refused. The old two forms are unchanged, so every existing row, and every
   AWARD, satisfies the new constraint exactly as it satisfied the old one.
   TASK reversals already fit the existing form (staff set, not system).

2. `note` is bounded (`char_length <= 300`). It is the only place the reason of
   a reversal is kept; the application also limits it, the database makes the
   limit unconditional. Nothing in the repository writes `note` today, so no
   existing row can exceed it (the migration fails loudly rather than silently
   truncating if one does).

Data-neutral: no row is read, written or rewritten. Downgrade restores the
old constraint; it FAILS (by design, nothing is changed) while a FEEDBACK
reversal made by an administrator exists, because the old rule cannot hold
for it - those rows must be dealt with deliberately, never lost silently.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_points_reversal"
down_revision: str | None = "0011_audit_view"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OLD = (
    "(source = 'TASK' AND booth_id IS NOT NULL AND entered_by_staff_id IS NOT NULL "
    "AND entered_by_system = false) OR (source = 'FEEDBACK' AND booth_id IS NOT NULL "
    "AND entered_by_staff_id IS NULL AND entered_by_system = true)"
)
_NEW = (
    _OLD + " OR (source = 'FEEDBACK' AND entry_type = 'REVERSAL' AND booth_id IS NOT NULL "
    "AND entered_by_staff_id IS NOT NULL AND entered_by_system = false)"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_points_log_source_attribution"), "points_log", type_="check")
    op.create_check_constraint(op.f("ck_points_log_source_attribution"), "points_log", _NEW)
    op.create_check_constraint(
        op.f("ck_points_log_note_length"), "points_log", "note IS NULL OR char_length(note) <= 300"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_points_log_note_length"), "points_log", type_="check")
    op.drop_constraint(op.f("ck_points_log_source_attribution"), "points_log", type_="check")
    op.create_check_constraint(op.f("ck_points_log_source_attribution"), "points_log", _OLD)
