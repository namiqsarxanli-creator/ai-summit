"""Playground sessions: no 3-person cap on the event day (owner, 13 Nov 2026).

Session times got mixed up at the booths and more than three people played in one
15-minute session. The booth decides who plays; the database guard now stops only at
50 places per session (a safety net, not a rule). Advance bookings keep their own
limit of 3 in the application (playground/service.py `book`).

Replaces the function only; the trigger stays. Downgrade restores the cap of 3.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_playground_places"
down_revision: str | None = "0027_review_evidence"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_GUARD = """
CREATE OR REPLACE FUNCTION playground_capacity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 PERFORM id FROM playground_slots WHERE id=NEW.slot_id FOR UPDATE;
 IF NEW.status IN ('CONFIRMED','INVITED','CHECKED_IN','COMPLETED') AND
   (SELECT count(*) FROM playground_bookings
    WHERE slot_id=NEW.slot_id AND id<>COALESCE(NEW.id,0)
    AND status IN ('CONFIRMED','INVITED','CHECKED_IN','COMPLETED')) >= {cap}
 THEN RAISE EXCEPTION 'slot full'
 USING ERRCODE='23514', CONSTRAINT='ck_playground_capacity'; END IF;
 RETURN NEW;
END $$;
"""


def upgrade() -> None:
    op.execute(_GUARD.format(cap=50))


def downgrade() -> None:
    op.execute(_GUARD.format(cap=3))
