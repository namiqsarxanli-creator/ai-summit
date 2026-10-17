"""One worker call for authenticated, materialized attendee reads.

The request's session remains dependency-injected. Authentication is checked on
every call; neither identity nor private responses are cached. The callback must
finish its reads and construct the response before returning, because its pooled
connection is released here, on success and failure, before the async route
serializes the response. Mutations must continue using the reset-fenced path.
"""

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.sessions import resolve_attendee_session
from app.modules.attendees.models import User


def attendee_read[T](
    db: Session,
    token: str | None,
    read: Callable[[Session, User], T],
    *,
    identity_only: bool = False,
) -> T:
    try:
        attendee = resolve_attendee_session(db, token, identity_only=identity_only)
        return read(db, attendee)
    finally:
        db.close()
