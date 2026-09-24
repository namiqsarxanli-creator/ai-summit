"""Request dependencies — ARCHITECTURE.md §5 ("current_attendee /
current_staff / current_admin"). `current_staff` (any active staff
member, booth staff or admin) arrived with the scan-resolve endpoint
(M06); `current_booth_staff` and `current_admin` narrow it by role
(M09, IMPLEMENTATION_PLAN.md: "admin/staff distinction comes from
staff_accounts.role — one role source", §7.8).

`CurrentAttendee` is for READ-ONLY routes. Resolving the attendee is an
authoritative, reset-sensitive read, and a FastAPI dependency runs
before the handler opens its transaction — so a MUTATING route must not
use it. It extracts the cookie instead (pure work, allowed before the
fence, §19.4) and calls `resolve_attendee_session` inside its own
`mutating_transaction`. `CurrentStaff` carries the same restriction.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.core.config import get_settings
from app.core.errors import StaffNotAuthorizedError
from app.core.proxy import resolve_client_ip
from app.core.sessions import (
    SESSION_COOKIE_NAME,
    resolve_attendee_session,
    resolve_staff_session,
    resolve_staff_session_id,
)
from app.db.session import DbSession
from app.modules.attendees.models import User
from app.modules.staff.models import StaffAccount, StaffRole


def session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE_NAME)


def current_attendee(request: Request, db: DbSession) -> User:
    return resolve_attendee_session(db, session_token(request))


def current_staff(request: Request, db: DbSession) -> StaffAccount:
    return resolve_staff_session(db, session_token(request))


def current_staff_session_id(request: Request, db: DbSession) -> int:
    """M10: the `sessions.id` `scan_ref` binds to (§13.3 `ses`) — kept
    as its own dependency rather than folded into `current_staff` so
    routes that don't issue a `scan_ref` never resolve it."""
    return resolve_staff_session_id(db, session_token(request))


def current_booth_staff(staff: CurrentStaff) -> StaffAccount:
    """§12.2 layer 4: "Depends(current_booth_staff) rejects attendee
    sessions, admin-only accounts, inactive accounts, and expired
    sessions before the handler body runs." The first three of those
    come from `current_staff` (via `resolve_staff_session`); this adds
    only the role narrowing. Every `BOOTH_STAFF` row has a non-null
    `booth_id` by the §7.7 CHECK constraint, so a route depending on
    this never needs to re-check that itself."""
    if staff.role != StaffRole.BOOTH_STAFF:
        raise StaffNotAuthorizedError("This action requires a booth staff account.")
    return staff


def current_admin(staff: CurrentStaff) -> StaffAccount:
    """The admin counterpart — built now (M09) as the plan's own
    "Dependencies" line names it, even though no M09 route uses it yet;
    M16's admin console is its first consumer, the same forward-looking
    pattern M02 used for the reset fence before M04 had a mutation to
    protect."""
    if staff.role != StaffRole.ADMIN:
        raise StaffNotAuthorizedError("This action requires an admin account.")
    return staff


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
CurrentStaff = Annotated[StaffAccount, Depends(current_staff)]
CurrentStaffSessionId = Annotated[int, Depends(current_staff_session_id)]
CurrentBoothStaff = Annotated[StaffAccount, Depends(current_booth_staff)]
CurrentAdmin = Annotated[StaffAccount, Depends(current_admin)]
ClientIp = Annotated[str, Depends(client_ip)]
