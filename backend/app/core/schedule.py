"""Shared schedule write lock: fence -> schedule -> attendee -> resource.

Exclusive by default, to keep cross-module overlap checks atomic. Masterclass
attendee choices alone may share it: their attendee and sorted masterclass row
locks serialize conflicting choices, and waitlisted choices already reserve the
block's time. Promotions therefore cannot introduce a new time conflict.

Programme edits, Playground and mentor writes retain the exclusive side, so they
cannot interleave with those choices. Never upgrade a shared lock to exclusive
inside a transaction. Both modes are transaction-scoped and bounded by the
existing lock timeout; no lock state survives commit or rollback.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


def lock_schedule(db: Session, *, shared: bool = False) -> None:
    statement = (
        "SELECT pg_advisory_xact_lock_shared(21461016, 1)"
        if shared
        else "SELECT pg_advisory_xact_lock(21461016, 1)"
    )
    db.execute(text(statement))
