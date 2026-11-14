"""bind an activation code to the browser that submitted the registration

Revision ID: 0015_registration_intent
Revises: 0014_profile_photo
Create Date: 2026-10-11

Two nullable columns on `otp_challenges`, both additive and ignored by code from
before this revision:

* `registration_intent_hash text NULL` - SHA-256 of a random value that only the
  submitting browser holds (an HttpOnly cookie set by POST /attendees/register).
* `registration_snapshot jsonb NULL` - the profile, interests and sponsor
  consent that submission entered.

Anyone who knows an email can re-submit a pending (PROVISIONAL) registration and
so trigger a fresh activation code to the owner. Without a binding, the owner
entering that code activated whatever the latest submission wrote, including a
sponsor consent the owner never gave. With it, verification applies only the
snapshot whose intent the verifying browser presents; without one, the account is
activated with sponsor consent off.

No row is rewritten. Downgrade drops both columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015_registration_intent"
down_revision: str | None = "0014_profile_photo"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("otp_challenges", sa.Column("registration_intent_hash", sa.Text(), nullable=True))
    op.add_column(
        "otp_challenges",
        sa.Column("registration_snapshot", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("otp_challenges", "registration_snapshot")
    op.drop_column("otp_challenges", "registration_intent_hash")
