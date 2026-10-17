"""attendee company and the Beyond Now interest card

Revision ID: 0016_interest_card
Revises: 0015_registration_intent
Create Date: 2026-10-15

Five columns on `users`, all additive and ignored by code from before this
revision:

* `company text NULL` - the company, separate from `profession` (the role).
  Existing attendees get NULL: nothing is invented for them.
* `card_sector text NULL` and `card_topics` / `card_seeking` / `card_offering`
  `text[] NOT NULL DEFAULT '{}'` - the interest card as stable identifiers
  (app/modules/attendees/interest_card.py). Existing attendees get an empty card.
  The array defaults are constants, so PostgreSQL 11+ adds them without
  rewriting the table.
* CHECKs for the card's maximums (3 topics, 2 sought, 2 offered) and the company
  length, so they hold whoever writes.

`interests` and `user_interests` (booth recommendations and booth weights) are
not touched: no existing selection is converted, renamed or removed. Downgrade
drops exactly these columns and constraints.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_interest_card"
down_revision: str | None = "0015_registration_intent"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_ARRAYS = ("card_topics", "card_seeking", "card_offering")
_CHECKS = {
    "card_topics_max": "cardinality(card_topics) <= 3",
    "card_seeking_max": "cardinality(card_seeking) <= 2",
    "card_offering_max": "cardinality(card_offering) <= 2",
    "company_length": "company IS NULL OR char_length(company) BETWEEN 1 AND 200",
}


def upgrade() -> None:
    op.add_column("users", sa.Column("company", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("card_sector", sa.Text(), nullable=True))
    for column in _ARRAYS:
        op.add_column(
            "users",
            sa.Column(
                column,
                postgresql.ARRAY(sa.Text()),
                server_default=sa.text("'{}'::text[]"),
                nullable=False,
            ),
        )
    for name, condition in _CHECKS.items():
        op.create_check_constraint(op.f(f"ck_users_{name}"), "users", condition)


def downgrade() -> None:
    for name in _CHECKS:
        op.drop_constraint(op.f(f"ck_users_{name}"), "users", type_="check")
    for column in reversed(_ARRAYS):
        op.drop_column("users", column)
    op.drop_column("users", "card_sector")
    op.drop_column("users", "company")
