"""staff password reset purpose for the OTP outbox

Revision ID: 0013_staff_password_reset
Revises: 0012_points_reversal
Create Date: 2026-10-04

Adds `STAFF_PASSWORD_RESET` to the `otp_purpose` enum, so a staff member's
password-reset code is a different kind of challenge from an attendee's
activation / login code: both the single-live-challenge index
(`uq_otp_challenges_email_purpose_live`, keyed on email AND purpose) and every
verification query match on purpose, so a code of one kind can never be used as
the other, and issuing one never supersedes the other.

Nothing else changes: the existing challenge and outbox tables, their
constraints and every existing row are untouched.

PostgreSQL rule: a new enum value cannot be USED in the transaction that adds
it, so the ALTER runs in its own autocommit block, before alembic records the
revision.

Downgrade removes the value: it first deletes the (short-lived) staff reset
challenges and their outbox rows, then rebuilds the type without the value.
Attendee challenges and deliveries are kept.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_staff_password_reset"
down_revision: str | None = "0012_points_reversal"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE otp_purpose ADD VALUE IF NOT EXISTS 'STAFF_PASSWORD_RESET'")


def downgrade() -> None:
    # otp_deliveries rows go with their challenge (ON DELETE CASCADE).
    op.execute("DELETE FROM otp_challenges WHERE purpose::text = 'STAFF_PASSWORD_RESET'")
    op.execute("ALTER TYPE otp_purpose RENAME TO otp_purpose_old")
    op.execute("CREATE TYPE otp_purpose AS ENUM ('ACTIVATION', 'LOGIN')")
    for table in ("otp_challenges", "otp_deliveries"):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN purpose TYPE otp_purpose "
            "USING purpose::text::otp_purpose"
        )
    op.execute("DROP TYPE otp_purpose_old")
