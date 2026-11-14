"""Connections that never paid CONNECTION points: an administrator's review.

A mutual scan always creates the connection (it opens the contact to the pair),
also when nothing could be paid at that moment - for instance before the
competition close was configured. A later scan never pays such a connection
(people/connections.py). This module lets an administrator see them and, only
where the recorded facts allow it, pay them through the ledger.

* `review` is READ-ONLY. For every side of a connection that holds no
  CONNECTION award it says why, from what is recorded:
  - `same_company`, `ineligible` (the person is not competing now; an expired
    eligibility is never reopened), `after_close` (connected at or after the
    close): legitimate zero points - nothing to correct;
  - `not_configured`: there is still no close; nothing can be decided yet;
  - `needs_decision`: connected before the close but not on the event day
    (e.g. during setup), or the person's competition eligibility was changed by
    an administrator after the pair connected (today's eligibility is then no
    evidence of the eligibility at that moment) - the organiser's decision;
  - `eligible`: connected on the event day, before the close, by a competing
    person, worth points by its FROZEN kind (fixed when the pair connected - the
    recommendation is never re-read from today's suggestions).
  A side that was paid and later replaced (best four) or reversed by an
  organiser is not "unpaid" and is not listed.
* `apply` pays ONLY the selected sides that are still `eligible` when it runs,
  inside the caller's fenced transaction, through the normal connection payment
  (`connections._pay` dated at the moment the pair connected): the ledger's
  unique key (once per pair and person), eligibility, the close, the category
  maximum and the best-four replacement all apply. Every applied side gets its
  own audit row; a repeat pays nothing (`already_paid`). Anything else is
  returned as skipped with its category, unchanged.
* A final snapshot (draft or published) is never edited by a payment. When
  something is paid while one exists, the answer says `final_review_needed`: the
  existing safeguards refuse to publish a stale draft, and a published result
  changes only through a NEW version a Game Master reviews and publishes.
"""

from __future__ import annotations

from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.names import short_name
from app.modules.admin.models import AdminAudit
from app.modules.attendees.models import User
from app.modules.competition.final_models import FinalResult
from app.modules.competition.ledger import closes_at, eligible
from app.modules.masterclasses.models import MasterclassEvent
from app.modules.people import connections
from app.modules.people.connections import award_entry, value
from app.modules.people.models import Connection
from app.modules.staff.models import StaffAccount

AUDIT_RECONCILE: Final = "connection.reconcile"
APPLICABLE: Final = "eligible"
LIMIT: Final = 500


AUDIT_ELIGIBILITY: Final = "competition.eligibility"  # competition/routers.py


def _eligibility_changed_since(db: Session, user_id: int, since: Any) -> bool:
    return (
        db.execute(
            select(AdminAudit.id)
            .where(
                AdminAudit.action == AUDIT_ELIGIBILITY,
                AdminAudit.target_type == "user",
                AdminAudit.target_id == str(user_id),
                AdminAudit.created_at > since,
            )
            .limit(1)
        ).first()
        is not None
    )


def _category(db: Session, row: Connection, user: User | None) -> str:
    if value(row) == 0:
        return "same_company"
    if not eligible(user):
        return "ineligible"
    if user is not None and _eligibility_changed_since(db, user.id, row.established_at):
        return "needs_decision"
    close = closes_at(db)
    if close is None:
        return "not_configured"
    if row.established_at >= close:
        return "after_close"
    event = db.execute(select(MasterclassEvent).order_by(MasterclassEvent.id).limit(1)).scalar()
    if event is None:
        return "not_configured"
    local_day = row.established_at.astimezone(ZoneInfo(event.timezone)).date()
    if local_day != event.event_date:
        return "needs_decision"
    return APPLICABLE


def review(db: Session) -> dict[str, Any]:
    """Every unpaid side of a connection, newest first, with its category."""
    rows = list(
        db.execute(
            select(Connection).order_by(Connection.established_at.desc(), Connection.id.desc())
        ).scalars()
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        if len(items) >= LIMIT:
            break
        for uid, other_id in (
            (row.user_low_id, row.user_high_id),
            (row.user_high_id, row.user_low_id),
        ):
            if award_entry(db, uid, row) is not None:
                continue  # paid (and maybe later replaced or reversed): not unpaid
            user, other = db.get(User, uid), db.get(User, other_id)
            items.append(
                {
                    "connection_id": row.id,
                    "user_id": uid,
                    "name": short_name(user.first_name, user.last_name) if user else "",
                    "with": short_name(other.first_name, other.last_name) if other else "",
                    "kind": row.kind.lower(),
                    "points": value(row),  # what it is worth by its frozen kind
                    "established_at": row.established_at.isoformat(),
                    "category": _category(db, row, user),
                }
            )
            if len(items) >= LIMIT:
                break
    counts: dict[str, int] = {}
    for item in items:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return {"items": items, "counts": counts, "applicable": APPLICABLE}


def apply(
    db: Session, staff: StaffAccount, selected: list[tuple[int, int]], reason: str
) -> dict[str, Any]:
    """Pay the selected (connection_id, user_id) sides that are still eligible.
    The caller holds the reset fence and commits once: all of it or nothing."""
    results: list[dict[str, Any]] = []
    for connection_id, user_id in selected:
        row = db.execute(
            select(Connection).where(Connection.id == connection_id).with_for_update()
        ).scalar_one_or_none()
        if row is None or user_id not in (row.user_low_id, row.user_high_id):
            results.append(
                {"connection_id": connection_id, "user_id": user_id, "result": "not_found"}
            )
            continue
        if award_entry(db, user_id, row) is not None:
            results.append(
                {"connection_id": connection_id, "user_id": user_id, "result": "already_paid"}
            )
            continue
        category = _category(db, row, db.get(User, user_id))
        if category != APPLICABLE:
            results.append({"connection_id": connection_id, "user_id": user_id, "result": category})
            continue
        outcome = connections._pay(db, user_id, row, row.established_at)
        entry = award_entry(db, user_id, row)
        points = entry.points if outcome == "awarded" and entry is not None else 0
        db.add(
            AdminAudit(
                staff_account_id=staff.id,
                action=AUDIT_RECONCILE,
                target_type="connection",
                target_id=str(connection_id),
                before_value={"user_id": user_id, "category": category},
                after_value={
                    "user_id": user_id,
                    "outcome": outcome,
                    "points": points,
                    "reason": reason,
                },
            )
        )
        db.flush()
        results.append(
            {
                "connection_id": connection_id,
                "user_id": user_id,
                "result": outcome,
                "points": points,
            }
        )
    paid = any(r["result"] == "awarded" for r in results)
    snapshot = (
        db.execute(
            select(FinalResult.id).where(FinalResult.status.in_(("DRAFT", "PUBLISHED"))).limit(1)
        ).first()
        is not None
    )
    return {"results": results, "final_review_needed": paid and snapshot}
