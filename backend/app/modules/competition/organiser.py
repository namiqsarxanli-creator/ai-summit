"""The FACEMARK organiser workspace (`/facemark`): who may open it, and
which of its sections this account may use.

FACEMARK is the event's organiser. Its workspace gathers tools that already
exist - scanning desks, Qmeter, points corrections, the final result, the venue
screens - and grants NOTHING by itself: every read and every action inside it
goes to its own endpoint, which checks the same role or scope it always did
(`require_scope`, `CurrentAdmin`, `final.require_reviewer`). This module only
answers, for the screen, what those checks would say, so that the screen shows
the actions this account is allowed to take and no others.

An organiser is an administrator, or a staff account holding a live
GAME_MASTER scope (the event-wide organiser scope). Booth staff and door or
mentor volunteers keep their own `/staff` tools and are refused here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import StaffNotAuthorizedError
from app.modules.competition.models import StaffScope
from app.modules.competition.scopes import has_scope
from app.modules.staff.models import StaffAccount, StaffRole

TITLE = "FACEMARK — Organiser Panel"


def _live_scopes(db: Session, staff: StaffAccount) -> set[str]:
    return set(
        db.execute(
            select(StaffScope.scope).where(
                StaffScope.staff_account_id == staff.id, StaffScope.revoked_at.is_(None)
            )
        ).scalars()
    )


def is_organiser(db: Session, staff: StaffAccount) -> bool:
    return staff.is_active and (
        staff.role == StaffRole.ADMIN or has_scope(db, staff, "GAME_MASTER", None)
    )


def workspace(db: Session, staff: StaffAccount) -> dict[str, Any]:
    """What this account may do in the workspace. Refused for a non-organiser."""
    if not is_organiser(db, staff):
        raise StaffNotAuthorizedError("The organiser panel is for FACEMARK organisers only.")
    admin = staff.role == StaffRole.ADMIN
    scopes = _live_scopes(db, staff)
    game_master = "GAME_MASTER" in scopes
    return {
        "title": TITLE,
        "full_name": staff.full_name,
        "role": staff.role.value,
        "game_master": game_master,
        # Each flag mirrors the check of the endpoints that section uses.
        "sections": {
            "overview": True,
            # The scanning desk itself lists the jobs this account's scopes allow
            # (a door, a mentor group); an organiser without one sees that.
            "scanning": True,
            "qmeter": game_master,  # POST /api/staff/qmeter/*: GAME_MASTER
            "corrections": admin,  # POST /api/admin/points-log/{id}/reverse: ADMIN
            "final": admin or game_master,  # final.require_reviewer
            "screens": True,  # public screens; panel codes need ADMIN (below)
        },
        "scanning_jobs": sorted(scopes & {"MASTERCLASS_DOOR", "MENTOR_HOST"}),
        "panel_codes": admin,  # GET /api/admin/panels/{id}/screen: ADMIN
    }
