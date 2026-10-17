"""Server-owned, revocable staff scopes (migration 0020).

A scope is checked INSIDE the fenced transaction of every scan, from the
database, never from the client or a cached session: revoking it (or
deactivating the account, or ending the session) stops the very next request,
including one queued on a phone while offline. An ADMIN does not implicitly hold
volunteer scopes for scanning; administrators grant them explicitly.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import StaffNotAuthorizedError
from app.modules.competition.models import StaffScope
from app.modules.staff.models import StaffAccount


def has_scope(db: Session, staff: StaffAccount, scope: str, target_id: int | None) -> bool:
    if not staff.is_active:
        return False
    query = select(StaffScope.id).where(
        StaffScope.staff_account_id == staff.id,
        StaffScope.scope == scope,
        StaffScope.revoked_at.is_(None),
    )
    query = (
        query.where(StaffScope.target_id.is_(None))
        if target_id is None
        else query.where(StaffScope.target_id == target_id)
    )
    return db.execute(query.limit(1)).first() is not None


def require_scope(db: Session, staff: StaffAccount, scope: str, target_id: int | None) -> None:
    if not has_scope(db, staff, scope, target_id):
        raise StaffNotAuthorizedError("This account has no permission for that.")
