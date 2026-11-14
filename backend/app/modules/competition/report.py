"""After the event: each attendee's own report, and a sponsor booth's own leads.

Attendee (own session only): points per category and total, the published final
place (or that the result is still under review), Founder / Lucky Draw, what they
verifiably completed (`attended`: a masterclass with entry AND exit scanned, a
completed Playground session, a mentor group the host confirmed, a panel whose
entry was scanned), apart from what they only started (`not_completed`: a masterclass
whose exit was never scanned, a Playground session they were admitted to but
whose end was never scanned - neither is a completion, neither earns points),
their confirmed connections with the contact the pair opened, and their accepted
pair photos.

Sponsor booth (BOOTH_STAFF of THAT booth only - never another booth, never an
unrestricted export): aggregate statistics of the booth, and the contact details
of attendees who interacted with the booth (a Playground session they were
admitted to, or a legacy booth activity) AND whose sponsor consent is given now.
A withdrawn consent removes the person at the next read. Sponsor consent is not
pair-photo or connection consent, and none of those is read here. Every lead
read is audited.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import StaffNotAuthorizedError
from app.modules.admin.models import AdminAudit
from app.modules.attendees.models import User, UserStatus
from app.modules.competition.models import Panel, PanelScan
from app.modules.competition.rules import CATEGORIES, GRAND_FINAL_CODE
from app.modules.playground.models import PlaygroundBooking, PlaygroundSlot
from app.modules.points.models import PointsLog
from app.modules.staff.models import StaffAccount, StaffRole

AUDIT_LEADS = "booth.leads"
# Programme statuses (competition/programme.py) of what was done in full, and of
# what was only begun. The point rules are separate (an award needs the exit /
# end scan); these sets only decide where the report lists an item.
_COMPLETED = {"attended", "completed"}
_NOT_COMPLETED = {"entered", "checked_in"}


def attendee_report(db: Session, user: User) -> dict[str, Any]:
    from app.modules.competition import final
    from app.modules.competition.programme import programme
    from app.modules.people import pair_photos
    from app.modules.people.connections import my_connections

    totals = dict(
        db.execute(
            select(PointsLog.source, func.coalesce(func.sum(PointsLog.points), 0))
            .where(PointsLog.user_id == user.id, PointsLog.source.in_(tuple(CATEGORIES)))
            .group_by(PointsLog.source)
        ).all()
    )
    categories = [
        {"category": c.source.value, "points": int(totals.get(c.source, 0)), "maximum": c.maximum}
        for c in CATEGORIES.values()
    ]
    full_panels = [
        {"title": title, "starts_at": start.isoformat(), "ends_at": end.isoformat()}
        for title, start, end in db.execute(
            select(Panel.title, Panel.starts_at, Panel.ends_at)
            .join(PanelScan, PanelScan.panel_id == Panel.id)
            .where(PanelScan.user_id == user.id)
            .where(PanelScan.phase == "ENTRY", Panel.code != GRAND_FINAL_CODE)
            .group_by(Panel.id)
            .order_by(Panel.starts_at)
        )
    ]
    day = programme(db, user.id)
    return {
        "eligible": user.competition_excluded_reason is None,
        "total": sum(c["points"] for c in categories),
        "categories": categories,
        **final.founder_status(db, user.id),
        "final": final.my_final(db, user.id),
        "final_status": final.public(db, datetime.now(UTC))["status"],
        "timezone": day["timezone"],
        "attended": [i for i in day["items"] if i["status"] in _COMPLETED],
        "not_completed": [i for i in day["items"] if i["status"] in _NOT_COMPLETED],
        "panels": full_panels,
        "connections": my_connections(db, user),
        "photos": [p for p in pair_photos.mine(db, user) if p["status"] == "accepted"],
    }


def booth_report(db: Session, staff: StaffAccount) -> dict[str, Any]:
    if staff.role != StaffRole.BOOTH_STAFF or staff.booth_id is None:
        raise StaffNotAuthorizedError("Only a booth's own staff can see its report.")
    booth_id = staff.booth_id
    admitted = (
        select(PlaygroundBooking.user_id)
        .join(PlaygroundSlot, PlaygroundSlot.id == PlaygroundBooking.slot_id)
        .where(
            PlaygroundSlot.booth_id == booth_id,
            PlaygroundBooking.status.in_(("CHECKED_IN", "COMPLETED")),
        )
    )
    legacy = select(PointsLog.user_id).where(PointsLog.booth_id == booth_id)
    stats = {
        "playground_sessions_completed": int(
            db.execute(
                select(func.count())
                .select_from(PlaygroundBooking)
                .join(PlaygroundSlot, PlaygroundSlot.id == PlaygroundBooking.slot_id)
                .where(PlaygroundSlot.booth_id == booth_id, PlaygroundBooking.status == "COMPLETED")
            ).scalar_one()
        ),
        "distinct_visitors": int(
            db.execute(
                select(func.count(func.distinct(User.id))).where(
                    or_(User.id.in_(admitted), User.id.in_(legacy))
                )
            ).scalar_one()
        ),
    }
    leads = [
        {
            "first_name": u.first_name,
            "last_name": u.last_name,
            "profession": u.profession,
            "email": str(u.email),
            "phone": u.phone,
            "linkedin_url": u.linkedin_url,
        }
        for u in db.execute(
            select(User)
            .where(
                User.status == UserStatus.ACTIVE,
                User.sponsor_consent.is_(True),
                or_(User.id.in_(admitted), User.id.in_(legacy)),
            )
            .order_by(User.last_name, User.first_name, User.id)
        ).scalars()
    ]
    stats["consenting_leads"] = len(leads)
    db.add(
        AdminAudit(
            staff_account_id=staff.id,
            action=AUDIT_LEADS,
            target_type="booth",
            target_id=str(booth_id),
            before_value=None,
            after_value={"id": booth_id, "leads": len(leads)},
        )
    )
    db.flush()
    return {"booth_id": booth_id, "stats": stats, "leads": leads}
