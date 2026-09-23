"""Request dependencies — ARCHITECTURE.md §5 ("current_attendee /
current_staff / current_admin"). Staff/admin resolution arrives with
staff login (M09).

`CurrentAttendee` is for READ-ONLY routes. Resolving the attendee is an
authoritative, reset-sensitive read, and a FastAPI dependency runs
before the handler opens its transaction — so a MUTATING route must not
use it. It extracts the cookie instead (pure work, allowed before the
fence, §19.4) and calls `resolve_attendee_session` inside its own
`mutating_transaction`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import get_settings
from app.core.proxy import resolve_client_ip
from app.core.sessions import SESSION_COOKIE_NAME, resolve_attendee_session
from app.db.session import DbSession
from app.modules.attendees.models import User


def session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE_NAME)


def current_attendee(request: Request, db: DbSession) -> User:
    return resolve_attendee_session(db, session_token(request))


def client_ip(request: Request) -> str:
    """§21.7.1: the peer, unless the peer is a configured trusted proxy
    — never a client-written X-Forwarded-For entry."""
    return resolve_client_ip(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        get_settings().trusted_proxies,
    )


SessionToken = Annotated[str | None, Depends(session_token)]
CurrentAttendee = Annotated[User, Depends(current_attendee)]
ClientIp = Annotated[str, Depends(client_ip)]
